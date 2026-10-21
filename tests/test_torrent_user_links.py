#!/usr/bin/env python3
"""用户自加种子链接(user_links) —— 持久化语义 + 前后端接线。

v1.5.4 之前 /api/torrent/link/add 是个孤儿接口: 后端 43 行完整实现, 前端
renderTorrLinks 只画了 ✖ 删除按钮, 从来不调 add。结果是列表能减不能增 ——
想加只能改 settings.json。本文件把这条链路钉住, 重点:

  1. add_user_link 的协议白名单(magnet 放行是这里独有的, RSS 那侧只认 http)
  2. 写入共享 settings.json 时不得冲掉其它顶层键
  3. **前端必须有调用者** —— 这是"接口不再变孤儿"的回归守卫
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import app as web_app  # noqa: E402
import distro_torrents as dtorrents  # noqa: E402

INDEX_HTML = REPO_ROOT / "web" / "static" / "index.html"


class _TmpSettings(unittest.TestCase):
    """每个用例一份 settings.json, 并把两个模块的路径都指向它。

    必须同时 patch distro_torrents.SETTINGS_JSON: app.py 里的 dtorrents 就是
    这个模块本身, 只改 app.SETTINGS_JSON 的话写入会落到别处, 断言不到。
    """

    def _settings(self, d, initial=None) -> Path:
        p = Path(d) / "settings.json"
        p.write_text(json.dumps(initial or {}, ensure_ascii=False), encoding="utf-8")
        return p

    def _use(self, p: Path):
        for mod in (dtorrents,):
            patcher = patch.object(mod, "SETTINGS_JSON", p)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(web_app, "SETTINGS_JSON", p)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestUserLinkPersistence(_TmpSettings):

    def test_add_stores_url_distro_and_ts(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._settings(d)
            self._use(p)
            dtorrents.add_user_link("magnet:?xt=urn:btih:abc", "Fedora")
            links = dtorrents.get_user_links()
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0]["url"], "magnet:?xt=urn:btih:abc")
            self.assertEqual(links[0]["distro"], "Fedora")
            self.assertIsInstance(links[0]["ts"], int)

    def test_magnet_scheme_allowed(self):
        """magnet 是这里独有的白名单项(RSS 那侧只认 http/https)。"""
        with tempfile.TemporaryDirectory() as d:
            self._use(self._settings(d))
            dtorrents.add_user_link("magnet:?xt=urn:btih:deadbeef")
            self.assertEqual(len(dtorrents.get_user_links()), 1)

    def test_http_and_https_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            self._use(self._settings(d))
            dtorrents.add_user_link("https://m.example.com/x.torrent")
            dtorrents.add_user_link("http://m.example.com/y.torrent")
            self.assertEqual(len(dtorrents.get_user_links()), 2)

    def test_other_schemes_rejected(self):
        """file:// / ftp:// 必须拒 —— 链接会被周期性地真的取回来解析。"""
        with tempfile.TemporaryDirectory() as d:
            self._use(self._settings(d))
            for bad in ("file:///etc/passwd", "ftp://h/x.torrent", "smb://h/x", "javascript:alert(1)"):
                with self.assertRaises(ValueError, msg=f"{bad!r} 不该被接受"):
                    dtorrents.add_user_link(bad)
            self.assertEqual(dtorrents.get_user_links(), [])

    def test_empty_url_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            self._use(self._settings(d))
            with self.assertRaises(ValueError):
                dtorrents.add_user_link("   ")
            self.assertEqual(dtorrents.get_user_links(), [])

    def test_remove_by_url(self):
        with tempfile.TemporaryDirectory() as d:
            self._use(self._settings(d))
            dtorrents.add_user_link("https://m/a.torrent")
            dtorrents.add_user_link("https://m/b.torrent")
            lst = dtorrents.remove_user_link("https://m/a.torrent")
            self.assertEqual([x["url"] for x in lst], ["https://m/b.torrent"])
            self.assertEqual([x["url"] for x in dtorrents.get_user_links()],
                             ["https://m/b.torrent"], "删除必须落盘")

    def test_malformed_stored_entries_filtered(self):
        """settings.json 被手改过 / 旧版本残留 -> 脏条目不得污染列表。"""
        with tempfile.TemporaryDirectory() as d:
            p = self._settings(d, {"torrent_links": [
                {"url": "https://m/ok.torrent", "distro": "X"},
                "not-a-dict",
                {"distro": "no-url"},
                None,
            ]})
            self._use(p)
            self.assertEqual([x["url"] for x in dtorrents.get_user_links()],
                             ["https://m/ok.torrent"])

    def test_other_top_level_keys_preserved(self):
        """写 settings.json 走的是 update(合并) 而非覆盖。

        settings.json 里还躺着 users / sessions / notify —— 被冲掉等于全员掉线。
        """
        with tempfile.TemporaryDirectory() as d:
            p = self._settings(d, {"users": {"admin": {}}, "sessions": {"t": 1},
                                   "notify": {"smtp": {"host": "h"}}})
            self._use(p)
            dtorrents.add_user_link("https://m/a.torrent")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn("users", data)
            self.assertIn("sessions", data)
            self.assertEqual(data["notify"]["smtp"]["host"], "h")
            self.assertIn("torrent_links", data)


class TestLinkAddEndpoint(_TmpSettings):
    """/api/torrent/link/add 的协议校验与前置检查。"""

    def _client(self, d, qb_enabled=True):
        p = self._settings(d, {"qb": {"enabled": qb_enabled}})
        self._use(p)
        web_app._sessions.clear()
        web_app._sessions["tok"] = ("admin", int(time.time()) + 3600)
        self.addCleanup(web_app._sessions.clear)
        return web_app.app.test_client(), p

    def _post(self, client, payload):
        return client.post("/api/torrent/link/add", json=payload,
                           headers={"X-Auth-Token": "tok"})

    def test_magnet_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            client, p = self._client(d)
            r = self._post(client, {"url": "magnet:?xt=urn:btih:abc", "distro": "Fedora"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            body = r.get_json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["user_links"][0]["distro"], "Fedora")
            self.assertEqual(
                json.loads(p.read_text(encoding="utf-8"))["torrent_links"][0]["url"],
                "magnet:?xt=urn:btih:abc", "必须真的落盘, 不能只回内存")

    def test_non_http_rejected_with_400(self):
        with tempfile.TemporaryDirectory() as d:
            client, _p = self._client(d)
            r = self._post(client, {"url": "file:///etc/passwd"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("error", r.get_json())

    def test_qb_disabled_returns_403(self):
        with tempfile.TemporaryDirectory() as d:
            client, _p = self._client(d, qb_enabled=False)
            r = self._post(client, {"url": "magnet:?xt=urn:btih:abc"})
            self.assertEqual(r.status_code, 403)


class TestFrontendWiring(unittest.TestCase):
    """接口不能没有调用者 —— 这正是 v1.5.4 要修的东西。"""

    def setUp(self):
        self.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_frontend_calls_link_add(self):
        self.assertIn("/api/torrent/link/add", self.html,
                      "link/add 又变成孤儿接口了: 前端没有任何地方调用它")

    def test_add_button_exists_and_is_wired(self):
        self.assertIn("torrLinkAdd()", self.html)
        self.assertIn('data-i18n="torrLinkSaveBtn"', self.html)

    def test_add_function_reuses_manual_inputs(self):
        """存为常驻源复用 #tm-url / #tm-dist, 成功后清空并刷新列表。"""
        self.assertRegex(self.html, r"function torrLinkAdd\(\)\{[\s\S]{0,400}tm-url",
                         "存为常驻源应当复用手动添加的输入框, 而不是另开一套")
        self.assertIn("loadTorrentSources()", self.html)

    def test_i18n_keys_defined(self):
        """按钮/提示/结果都要有中英词条, 否则切英文时露出 key 名。"""
        for key in ("torrLinkSaveBtn", "torrLinkHint", "torrLinkSaveOk",
                    "torrLinkSaveFail", "torrLinkNeedUrl"):
            self.assertIn(f"'{key}':{{zh:", self.html, f"缺少 i18n 词条 {key}")

    def test_link_rows_show_distro(self):
        """add 时能指定发行版, 列表里就该看得见, 否则用户不知道生效了。"""
        self.assertIn("x.distro?", self.html)


if __name__ == "__main__":
    unittest.main()
