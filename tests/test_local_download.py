#!/usr/bin/env python3
"""「下载到本机」功能测试（POST /api/files/ticket + GET /api/files/get）。

需求: 服务器上已下载的 ISO 要能直接拉到"打开面板的那台电脑", 不依赖 SMB/WebDAV
sidecar。实现要点与对应断言:

  * 会话 token 走 X-Auth-Token **请求头**, 而浏览器顶层导航(点链接下载)带不上自定义头
    → 不能直接把链接指向文件。改为先换**短时票据**, 再由票据 URL 下载。
    断言: /api/files/get 在强制登录下**不需要会话头**即可访问(靠票据自证身份),
    但票据无效/过期/缺失一律 403。
  * 大文件必须支持 Range(暂停/续传)。断言: Range 请求返回 206 + 正确 Content-Range,
    且该路径**不被**「所有 API 禁缓存」规则覆盖(no-store 会让续传从头开始)。
  * 路径穿越: 复用 _safe_join + resolve 包含性二次校验。断言: 各种穿越输入一律不签发。
  * 只下发完整文件: 半成品(.part 等)是未完成字节。断言: 请求 .part 名不签发。
  * 下载不要求文件在清单内(种子/手动放入的 ISO 也应能拉回本机) —— 与删除接口语义不同。

覆盖:
  后端  POST /api/files/ticket  签发/参数校验/穿越拒绝/半成品拒绝/上限
  后端  GET  /api/files/get     下载/HEAD/Range/伪造票据/过期票据/票据可重复使用
  后端  require_auth            白名单只放行 /api/files/get, 其余 API 仍 401
  后端  no_cache_api            /api/files/get 不被 no-store 覆盖
  后端  build_distros           downloadable / rel 字段由后端下发
  前端  HTML 契约               ⬇ 按钮存在且依据 downloadable; 三个函数存在; i18n 键齐全
"""

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

BODY = b"ISO-PAYLOAD-" * 800          # 9600 字节, 便于做 Range 断言
CJK_NAME = "中文发行版 26.04.iso"


class LocalDownloadBase(unittest.TestCase):
    """搭好临时 DATA_DIR + 清单, 并关掉登录门禁(鉴权单独在 AuthGateForDownload 覆盖)。"""

    # 子类置 True 即可在"强制登录"下跑同一套夹具。用类属性而不是在 setUp 之后再
    # patch 一层 —— 叠加 patch 的还原顺序会让前面的值泄漏到后续测试。
    REQUIRE_LOGIN_FOR_TEST = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.distro_dir = self.data / "linux" / "Ubuntu"
        self.distro_dir.mkdir(parents=True)
        (self.distro_dir / "ubuntu-26.04.iso").write_bytes(BODY)
        # 半成品: 代表"下载停止"时磁盘上真实存在的东西, 但不可作为下载源
        (self.distro_dir / "ubuntu-26.04.iso.part").write_bytes(b"x" * 32)
        # 不在清单里(种子下载/手动放入的典型形态)
        (self.distro_dir / "arch-stray.iso").write_bytes(b"stray-bytes")
        (self.distro_dir / CJK_NAME).write_bytes(b"cjk")

        self.manifest = {
            "updated_at": 0,
            "distributions": [
                {"distribution": "Ubuntu", "type": "linux",
                 "download_url": "https://example.com/ubuntu-26.04.iso"},
            ],
        }
        (self.data / "distributions.json").write_text(
            json.dumps(self.manifest), encoding="utf-8")
        (self.data / "settings.json").write_text("{}", encoding="utf-8")

        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.data / "distributions.json"),
            patch.object(app, "SETTINGS_JSON", self.data / "settings.json"),
            patch.object(app, "running_task", return_value=None),
            patch.object(app, "REQUIRE_LOGIN", self.REQUIRE_LOGIN_FOR_TEST),
            patch.object(app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()
            # addCleanup 在 setUp 半途失败时也会执行, 比 tearDown 更可靠;
            # 补丁统一在这里停止(含子类扩展的), tearDown 不再重复 stop
            self.addCleanup(p.stop)
        app._dl_tickets.clear()
        self.client = app.app.test_client()

    def tearDown(self):
        app._dl_tickets.clear()
        self._tmp.cleanup()

    def ticket(self, items):
        return self.client.post("/api/files/ticket", json={"items": items})

    def item(self, filename, typ="linux", distribution="Ubuntu"):
        return {"type": typ, "distribution": distribution, "filename": filename}


class TestTicketIssuance(LocalDownloadBase):
    """POST /api/files/ticket: 签发与参数校验。"""

    def test_issues_ticket_for_complete_file(self):
        r = self.ticket([self.item("ubuntu-26.04.iso")])
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["skipped"], [])
        self.assertEqual(len(j["tickets"]), 1)
        tk = j["tickets"][0]
        self.assertEqual(tk["filename"], "ubuntu-26.04.iso")
        self.assertEqual(tk["size"], len(BODY))
        self.assertTrue(tk["url"].startswith("/api/files/get?t="),
                        "票据 URL 必须走下载路由")

    def test_ticket_url_carries_ticket_not_session_token(self):
        """票据 URL 里绝不能出现会话 token —— 否则 7 天有效的凭据会留在浏览器历史里。"""
        marker = "SESSION-TOKEN-MUST-NOT-LEAK"
        with patch.object(app, "AUTH_TOKEN", marker):
            r = self.client.post("/api/files/ticket",
                                 json={"items": [self.item("ubuntu-26.04.iso")]},
                                 headers={"X-Auth-Token": marker})
        self.assertEqual(r.status_code, 200)
        url = r.get_json()["tickets"][0]["url"]
        self.assertNotIn(marker, url, "会话 token 不能出现在下载 URL 里")
        self.assertNotIn("X-Auth-Token", url)
        tok = url.split("t=", 1)[1]
        self.assertEqual(len(tok), 32, "token_urlsafe(24) -> 32 个 URL 安全字符")

    def test_empty_items_is_400(self):
        self.assertEqual(self.ticket([]).status_code, 400)

    def test_missing_items_key_is_400(self):
        r = self.client.post("/api/files/ticket", json={})
        self.assertEqual(r.status_code, 400)

    def test_too_many_items_is_400(self):
        items = [self.item("ubuntu-26.04.iso")] * (app.DL_TICKET_MAX + 1)
        r = self.ticket(items)
        self.assertEqual(r.status_code, 400)
        self.assertIn(str(app.DL_TICKET_MAX), r.get_json()["error"])

    def test_exactly_max_items_is_allowed(self):
        items = [self.item("ubuntu-26.04.iso")] * app.DL_TICKET_MAX
        self.assertEqual(self.ticket(items).status_code, 200)

    def test_nonexistent_file_is_skipped_not_issued(self):
        r = self.ticket([self.item("nope.iso")])
        j = r.get_json()
        self.assertFalse(j["ok"])
        self.assertEqual(j["tickets"], [])
        self.assertEqual(len(j["skipped"]), 1)
        self.assertIn("nope.iso", j["skipped"][0])

    def test_partial_file_name_is_refused(self):
        """半成品名(.part)代表未完成的字节, 不能作为下载源。"""
        r = self.ticket([self.item("ubuntu-26.04.iso.part")])
        self.assertEqual(r.get_json()["tickets"], [])

    def test_other_partial_suffixes_refused(self):
        for suf in (".aria2", ".!qB", ".tmp"):
            name = "ubuntu-26.04.iso" + suf
            (self.distro_dir / name).write_bytes(b"zz")
            with self.subTest(suffix=suf):
                self.assertEqual(self.ticket([self.item(name)]).get_json()["tickets"], [])

    def test_final_name_still_works_when_part_exists(self):
        """完整文件与 .part 并存时, 完整文件必须仍可下载。"""
        r = self.ticket([self.item("ubuntu-26.04.iso")])
        self.assertEqual(len(r.get_json()["tickets"]), 1)

    def test_non_manifest_file_is_downloadable(self):
        """种子下载/手动放入的 ISO 不在清单里, 但也应该能拉回本机。

        注意这与删除接口的语义**不同**: 删除要求文件属于当前清单(保护用户自有文件),
        下载只要求文件真实存在且路径合法。
        """
        r = self.ticket([self.item("arch-stray.iso")])
        self.assertEqual(len(r.get_json()["tickets"]), 1)

    def test_cjk_filename_issued(self):
        r = self.ticket([self.item(CJK_NAME)])
        self.assertEqual(len(r.get_json()["tickets"]), 1)

    def test_mixed_items_partial_success(self):
        r = self.ticket([self.item("ubuntu-26.04.iso"), self.item("nope.iso")])
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(len(j["tickets"]), 1)
        self.assertEqual(len(j["skipped"]), 1)


class TestPathTraversalRefused(LocalDownloadBase):
    """路径穿越与非法参数: 一律不签发票据。"""

    def assert_refused(self, item, label=""):
        r = self.ticket([item])
        self.assertEqual(r.status_code, 200, label)
        j = r.get_json()
        self.assertEqual(j["tickets"], [], f"{label} 不应签发票据")
        self.assertEqual(len(j["skipped"]), 1, label)

    def test_type_not_in_whitelist(self):
        for typ in ("evil", "", "etc", "linux/..", "../linux", "LINUX"):
            with self.subTest(type=typ):
                self.assert_refused(self.item("ubuntu-26.04.iso", typ=typ), typ)

    def test_distribution_with_separator(self):
        for name in ("../..", "..", ".", "a/b", "a\\b", "../../etc"):
            with self.subTest(distribution=name):
                self.assert_refused(self.item("ubuntu-26.04.iso", distribution=name), name)

    def test_filename_with_separator_or_dots(self):
        for fn in ("../settings.json", "..\\settings.json", "a/b.iso", ".", "..",
                   "/etc/passwd", "linux/Ubuntu/x.iso", ""):
            with self.subTest(filename=fn):
                self.assert_refused(self.item(fn), fn)

    def test_absolute_path_in_filename_refused(self):
        self.assert_refused(self.item("C:\\Windows\\system.ini"), "win-abs")

    def test_escape_attempt_does_not_leak_settings(self):
        """穿越目标确实存在(settings.json)也能被挡住 —— 证明是校验而非"文件不存在"。"""
        self.assertTrue((self.data / "settings.json").exists())
        self.assert_refused(self.item("../settings.json"), "settings")

    def test_dotdot_distribution_cannot_reach_parent_dir(self):
        """把文件放在 DATA_DIR 根部, 用 distribution=".." 试图穿越到它。

        为什么必须加这条: 前面几条穿越用例的目标路径**根本不存在**, 所以"被拒绝"
        既可以来自校验生效、也可以来自文件不存在 —— 证明力不足。这里目标文件真实
        存在, 若 _safe_join 的白名单/穿越校验失效, linux/../secret.iso 会解析到
        DATA_DIR/secret.iso 并被成功下发, 因此这条断言才真正锚定住校验本身
        (它也是 dl-safe-join-removed 变异体的杀手)。
        """
        (self.data / "secret.iso").write_bytes(b"TOP-SECRET")
        self.assert_refused(self.item("secret.iso", distribution=".."), "..distro")
        # 反向确认: 该文件确实存在于磁盘(排除"文件本来就没有"的解释)
        self.assertTrue((self.data / "secret.iso").is_file())

    def test_symlink_pointing_outside_is_refused(self):
        """symlink 指向目标目录外部时必须拒绝(resolve 包含性校验)。"""
        link = self.distro_dir / "escape.iso"
        try:
            link.symlink_to(self.data / "settings.json")
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"本环境无法创建 symlink: {e}")
        self.assert_refused(self.item("escape.iso"), "symlink")


class TestDownloadByTicket(LocalDownloadBase):
    """GET /api/files/get: 实际下发行为。"""

    def url_for(self, filename, **kw):
        r = self.ticket([self.item(filename, **kw)])
        j = r.get_json()
        self.assertEqual(len(j["tickets"]), 1, f"应能签发 {filename}")
        return j["tickets"][0]["url"]

    def test_full_download_bytes_and_headers(self):
        url = self.url_for("ubuntu-26.04.iso")
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, BODY, "字节必须与磁盘完全一致")
        cd = r.headers["Content-Disposition"]
        self.assertTrue(cd.startswith("attachment"), cd)
        self.assertIn("ubuntu-26.04.iso", cd)
        self.assertEqual(r.headers.get("Accept-Ranges"), "bytes")

    def test_head_reports_length(self):
        url = self.url_for("ubuntu-26.04.iso")
        r = self.client.head(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(int(r.headers["Content-Length"]), len(BODY))

    def test_range_request_returns_206(self):
        """断点续传的关键: Range 必须返回 206 与正确分片。"""
        url = self.url_for("ubuntu-26.04.iso")
        r = self.client.get(url, headers={"Range": "bytes=100-199"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.data, BODY[100:200])
        self.assertEqual(r.headers.get("Content-Range"), f"bytes 100-199/{len(BODY)}")

    def test_range_from_offset_to_end(self):
        url = self.url_for("ubuntu-26.04.iso")
        r = self.client.get(url, headers={"Range": "bytes=%d-" % (len(BODY) - 10)})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.data, BODY[-10:])

    def test_ticket_is_reusable_for_resume(self):
        """票据在有效期内必须可重复使用 —— 一次性票据会让续传的第一个分片之后 403。"""
        url = self.url_for("ubuntu-26.04.iso")
        first = self.client.get(url, headers={"Range": "bytes=0-99"})
        second = self.client.get(url, headers={"Range": "bytes=100-199"})
        self.assertEqual(first.status_code, 206)
        self.assertEqual(second.status_code, 206, "同一票据的第二个 Range 请求不应被拒")
        self.assertEqual(first.data + second.data, BODY[:200])

    def test_cache_control_not_no_store(self):
        """no-store 会让浏览器丢弃已下分片 -> 续传从头开始, 必须避免。"""
        url = self.url_for("ubuntu-26.04.iso")
        cc = self.client.get(url).headers.get("Cache-Control", "")
        self.assertNotIn("no-store", cc)
        self.assertIn("private", cc)

    def test_other_api_still_no_store(self):
        """豁免只针对下载路由, 其余 API 的禁缓存规则必须保持。"""
        cc = self.client.get("/api/distros").headers.get("Cache-Control", "")
        self.assertIn("no-store", cc)

    def test_forged_ticket_403(self):
        r = self.client.get("/api/files/get?t=forged")
        self.assertEqual(r.status_code, 403)

    def test_missing_ticket_403(self):
        self.assertEqual(self.client.get("/api/files/get").status_code, 403)

    def test_empty_ticket_403(self):
        self.assertEqual(self.client.get("/api/files/get?t=").status_code, 403)

    def test_expired_ticket_403(self):
        url = self.url_for("ubuntu-26.04.iso")
        tok = url.split("t=", 1)[1]
        typ, name, fname, _ = app._dl_tickets[tok]
        app._dl_tickets[tok] = (typ, name, fname, time.time() - 1)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 403)
        self.assertIn("过期", r.get_json()["error"])

    def test_expired_ticket_is_purged(self):
        url = self.url_for("ubuntu-26.04.iso")
        tok = url.split("t=", 1)[1]
        typ, name, fname, _ = app._dl_tickets[tok]
        app._dl_tickets[tok] = (typ, name, fname, time.time() - 1)
        app._dl_tickets_purge()
        self.assertNotIn(tok, app._dl_tickets)

    def test_file_vanishing_after_ticket_gives_404(self):
        """票据签发后文件被删: 必须 404, 不能因票据里有旧路径就越界读取。"""
        url = self.url_for("ubuntu-26.04.iso")
        (self.distro_dir / "ubuntu-26.04.iso").unlink()
        r = self.client.get(url)
        self.assertEqual(r.status_code, 404)

    def test_cjk_filename_downloads(self):
        url = self.url_for(CJK_NAME)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"cjk")

    def test_ttl_default_is_positive(self):
        self.assertGreater(app.DL_TICKET_TTL, 0)


class TestAuthGateForDownload(LocalDownloadBase):
    """强制登录下的鉴权语义: 下载路由靠票据自证, 其余 API 仍须会话。"""

    REQUIRE_LOGIN_FOR_TEST = True

    def test_ticket_issuance_requires_auth(self):
        """换票必须先过会话校验 —— 这是票据可信的前提。"""
        r = self.ticket([self.item("ubuntu-26.04.iso")])
        self.assertEqual(r.status_code, 401)

    def test_download_route_bypasses_session_but_needs_ticket(self):
        """带票下载不需要会话头(浏览器导航带不上), 无票则 403 而非 401。"""
        # 先在放行状态下拿一张票
        with patch.object(app, "REQUIRE_LOGIN", False):
            url = self.ticket([self.item("ubuntu-26.04.iso")]).get_json()["tickets"][0]["url"]
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, "带有效票据应可下载, 无需会话头")
        self.assertEqual(self.client.get("/api/files/get?t=bogus").status_code, 403)
        self.assertEqual(self.client.get("/api/files/get").status_code, 403)

    def test_unrelated_api_still_401(self):
        """白名单只能放行下载路由, 不能顺手把别的 API 也放开。"""
        for path in ("/api/distros", "/api/protected", "/api/shares", "/api/torrent/info"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401, path)


class TestBuildDistrosFields(LocalDownloadBase):
    """build_distros 下发的 downloadable / rel 字段。"""

    def entry(self, filename):
        for g in app.build_distros()["groups"]:
            for e in g["entries"]:
                if e["filename"] == filename:
                    return e
        return None

    def test_downloadable_true_for_complete_file(self):
        e = self.entry("ubuntu-26.04.iso")
        self.assertTrue(e["downloadable"])

    def test_rel_is_backend_issued(self):
        e = self.entry("ubuntu-26.04.iso")
        self.assertEqual(e["rel"], "linux/Ubuntu/ubuntu-26.04.iso")

    def test_downloadable_true_even_when_status_says_stopped(self):
        """完整文件与更新的 .part 并存时 status=partial, 但完整文件仍可下载。

        这正是"必须由后端算 downloadable"的原因: 前端从 status 或 local_size
        都推不出这个结论(两者都会指向较新的半成品)。
        """
        part = self.distro_dir / "ubuntu-26.04.iso.part"
        part.write_bytes(b"y" * 4096)
        st = os.stat(self.distro_dir / "ubuntu-26.04.iso")
        os.utime(part, (st.st_atime + 100, st.st_mtime + 100))  # 让 .part 更新
        e = self.entry("ubuntu-26.04.iso")
        self.assertEqual(e["status"], "partial")
        self.assertTrue(e["downloadable"], "完整文件仍在, 应标记为可下载")

    def test_downloadable_false_when_only_partial(self):
        (self.distro_dir / "ubuntu-26.04.iso").unlink()
        e = self.entry("ubuntu-26.04.iso")
        self.assertFalse(e["downloadable"])


class TestFrontendContract(unittest.TestCase):
    """前端静态契约: 下载按钮与函数存在, 且不靠猜状态。"""

    def test_toolbar_button_present(self):
        self.assertIn('id="btnDlLocal"', HTML)
        self.assertIn("downloadLocalSel()", HTML)
        self.assertRegex(HTML, r'data-i18n="dlLocalSel"')

    def test_row_and_stray_buttons_guarded_by_downloadable(self):
        """行内 ⬇ 必须由后端下发的 downloadable 决定, 不能从 status/local_size 猜。"""
        self.assertIn("e.downloadable?", HTML)
        # 交付约定: 行内下载按钮的显示条件里不出现 status 派生变量
        m = re.search(r"\$\{e\.downloadable\?`<button[^`]*`:", HTML)
        self.assertIsNotNone(m, "找不到行内下载按钮的渲染表达式")
        self.assertNotRegex(m.group(0), r"isOk|isRun|isFail|isStop|local_size")

    def test_functions_defined(self):
        for fn in ("async function dlFile(", "function triggerDownload(",
                   "async function downloadLocalSel("):
            self.assertIn(fn, HTML, fn)

    def test_download_uses_ticket_endpoint(self):
        self.assertIn("'/api/files/ticket'", HTML)

    def test_does_not_navigate_away(self):
        """必须用临时 <a> 触发下载; location.href 会在响应非 attachment 时把面板导航走。"""
        m = re.search(r"function triggerDownload\(url\)\{(.*?)\}", HTML, re.S)
        self.assertIsNotNone(m, "找不到 triggerDownload 实现")
        body = m.group(1)
        self.assertIn("createElement('a')", body)
        self.assertIn(".click()", body)
        self.assertNotIn("location.href", body)

    def test_data_rel_falls_back_and_uses_backend_field(self):
        self.assertIn("e.rel||(g.type+'/'+g.name+'/'+e.filename)", HTML)

    def test_no_session_token_in_download_url(self):
        """前端拼下载 URL 时不得把 token 塞进 query。"""
        m = re.search(r"function triggerDownload\(url\)\{(.*?)\}", HTML, re.S)
        self.assertNotIn("TOKEN", m.group(1))


class TestFrontendI18n(unittest.TestCase):
    """新增文案必须进 I18N 字典, 否则界面显示原始 key。"""

    KEYS = ["dlLocalSel", "dlToLocal", "dlStarted", "dlTicketFail",
            "dlNothingLocal", "dlMultiConfirm"]

    def test_keys_defined(self):
        for k in self.KEYS:
            with self.subTest(key=k):
                self.assertRegex(HTML, r"'%s':\{zh:'[^']+',en:'[^']+'\}" % k)

    def test_keys_are_used(self):
        """两种合法用法: JS 里 t('key'), 或 HTML 上用 data-i18n="key"(由 applyLang 覆盖)。"""
        for k in self.KEYS:
            with self.subTest(key=k):
                used = (re.findall(r"t\('%s'\)" % k, HTML)
                        or re.findall(r'data-i18n="%s"' % k, HTML))
                self.assertTrue(used, f"{k} 定义了但没有任何地方使用")

    def test_download_tooltip_has_bilingual(self):
        self.assertIn("download to this computer", HTML.lower())


class TestRelPathDownload(LocalDownloadBase):
    """POST /api/files/ticket 的 {rel} 形态 + GET /api/files/get 的 _rel 解析。

    种子/手动放入的文件常落在 catalog 结构之外(如 /data/_torrents/),
    只能靠相对路径定位。安全边界与 catalog 形态一致: 必须仍在 DATA_DIR 内、
    不得是半成品、不得用 .. 越界。
    """

    def setUp(self):
        super().setUp()
        self.rel_dir = self.data / "_torrents"
        self.rel_dir.mkdir(parents=True)
        (self.rel_dir / "ubuntu.iso").write_bytes(BODY)

    def ticket_rel(self, rel):
        return self.client.post("/api/files/ticket", json={"items": [{"rel": rel}]})

    def test_rel_path_issues_ticket_and_downloads(self):
        r = self.ticket_rel("_torrents/ubuntu.iso")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["skipped"], [])
        self.assertEqual(len(j["tickets"]), 1)
        tk = j["tickets"][0]
        self.assertEqual(tk["filename"], "ubuntu.iso")
        self.assertEqual(tk["rel"], "_torrents/ubuntu.iso")
        tok = tk["url"].split("t=", 1)[1]
        g = self.client.get("/api/files/get?t=" + tok)
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.data, BODY)

    def test_rel_path_traversal_refused(self):
        r = self.ticket_rel("../secret.txt")
        j = r.get_json()
        self.assertEqual(j["tickets"], [])
        self.assertTrue(j["skipped"])

    def test_rel_path_absolute_refused(self):
        r = self.ticket_rel("/etc/passwd")
        self.assertEqual(r.get_json()["tickets"], [])

    def test_rel_path_partial_refused(self):
        (self.rel_dir / "x.iso.part").write_bytes(b"z")
        r = self.ticket_rel("_torrents/x.iso.part")
        self.assertEqual(r.get_json()["tickets"], [])

    def test_rel_path_nonexistent_skipped(self):
        r = self.ticket_rel("_torrents/nope.iso")
        j = r.get_json()
        self.assertEqual(j["tickets"], [])
        self.assertTrue(j["skipped"])


class FakeQB:
    """极简 qBittorrent 客户端桩, 只实现 list_torrents / torrent_files。"""

    def __init__(self, save_path, files):
        self.save_path = save_path
        self.files = files

    def list_torrents(self, tag=""):
        return [{"hash": "abc123", "name": "ubuntu-26.04",
                 "save_path": str(self.save_path), "progress": 1.0,
                 "state": "uploading"}]

    def torrent_files(self, h):
        return self.files


class TestTorrentHashDownload(LocalDownloadBase):
    """POST /api/files/ticket 的 {hash} 形态: 走 qBittorrent 枚举已完成文件。"""

    def setUp(self):
        super().setUp()
        self.rel_dir = self.data / "_torrents"
        self.rel_dir.mkdir(parents=True)
        (self.rel_dir / "ubuntu.iso").write_bytes(BODY)
        self._fake = FakeQB(self.rel_dir,
                            [{"name": "ubuntu.iso", "progress": 1.0,
                              "is_seed": True, "size": len(BODY)}])
        # 注意: 必须 += 扩展而不是重新赋值 —— 重新赋值会把基类已 start 的补丁
        # (running_task/REQUIRE_LOGIN/AUTH_TOKEN...)的引用弄丢, 永远无人 stop,
        # 桩泄漏到本模块之后的所有测试(正是 v1.3.17 CI Tests 红掉的根因)。
        self._patches += [
            patch.object(app, "TORRENT_AVAILABLE", True),
            patch.object(app, "_ensure_qb_enabled", lambda: (True, None)),
            patch.object(app, "_qb", lambda: self._fake),
        ]
        for p in self._patches[-3:]:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        super().tearDown()

    def test_hash_issues_ticket_for_completed_file(self):
        r = self.client.post("/api/files/ticket",
                              json={"items": [{"hash": "abc123"}]})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["skipped"], [])
        self.assertEqual(len(j["tickets"]), 1)
        tk = j["tickets"][0]
        self.assertEqual(tk["rel"], "_torrents/ubuntu.iso")
        tok = tk["url"].split("t=", 1)[1]
        g = self.client.get("/api/files/get?t=" + tok)
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.data, BODY)

    def test_hash_skips_incomplete_file(self):
        self._fake.files = [{"name": "ubuntu.iso", "progress": 0.5,
                             "is_seed": False, "size": 10}]
        r = self.client.post("/api/files/ticket",
                              json={"items": [{"hash": "abc123"}]})
        j = r.get_json()
        self.assertEqual(j["tickets"], [])
        self.assertTrue(any("没有已完成" in s for s in j["skipped"]), j["skipped"])

    def test_hash_unknown_is_skipped(self):
        r = self.client.post("/api/files/ticket",
                              json={"items": [{"hash": "deadbeef"}]})
        j = r.get_json()
        self.assertEqual(j["tickets"], [])
        self.assertTrue(j["skipped"])


class TestExternalQBAbsDownload(LocalDownloadBase):
    """外部 qB 形态: 种子文件在 DATA_DIR 外(如 /downloads), 按原路径挂进容器。

    这是 Z4Pro 部署的真实形态 —— iso-hub 只挂 /data, qB 写独立目录。修复前
    relative_to 失败会静默跳过, 用户只能看到笼统的"没有已完成的可下载文件"。
    修复后: 容器内真实可见 → 签发 _abs 票据; 不可见 → 给出可行动提示。
    """

    def setUp(self):
        super().setUp()
        # DATA_DIR 之外的"NAS 媒体目录"(容器内同路径可见)
        self._ext = tempfile.TemporaryDirectory()
        self.addCleanup(self._ext.cleanup)
        self.ext_dir = Path(self._ext.name).resolve()
        # Windows 的 tempdir 是 8.3 短路径(RUNNER~1), 而被测代码对路径做 resolve()
        # 会得到长路径 → 逐字符串比对必然不等。统一换成短路径的长路径形态,
        # 让"qB 报告的字符串"与"resolve 后的字符串"一致(真机上 qB 报告即真实路径)。
        self.ext_dir = Path(os.path.realpath(str(self.ext_dir)))
        (self.ext_dir / "动漫").mkdir()
        self.ext_file = self.ext_dir / "动漫" / "Player S3.mp4"
        self.ext_file.write_bytes(BODY)
        self._fake = FakeQB(self.ext_dir,
                            [{"name": "动漫/Player S3.mp4", "progress": 1.0,
                              "is_seed": True, "size": len(BODY)}])
        # += 扩展, 绝不重新赋值(见 TestTorrentHashDownload 注释的泄漏教训)
        self._patches += [
            patch.object(app, "TORRENT_AVAILABLE", True),
            patch.object(app, "_ensure_qb_enabled", lambda: (True, None)),
            patch.object(app, "_qb", lambda: self._fake),
        ]
        for p in self._patches[-3:]:
            p.start()
            self.addCleanup(p.stop)

    def post_hash(self, h="abc123"):
        return self.client.post("/api/files/ticket",
                                json={"items": [{"hash": h}]}).get_json()

    def test_abs_file_outside_data_dir_is_issued_and_downloadable(self):
        j = self.post_hash()
        self.assertTrue(j["ok"], j)
        self.assertEqual(j["skipped"], [])
        self.assertEqual(len(j["tickets"]), 1)
        tk = j["tickets"][0]
        self.assertEqual(tk["filename"], "Player S3.mp4")
        self.assertEqual(tk["type"], "_abs")
        self.assertIsNone(tk["rel"], "_abs 票据不应有 DATA_DIR 相对路径")
        g = self.client.get(tk["url"])
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.data, BODY)

    def test_abs_partial_file_not_issued(self):
        (self.ext_dir / "动漫" / "Player S3.mp4.part").write_bytes(b"x" * 16)
        self._fake.files = [{"name": "动漫/Player S3.mp4.part", "progress": 1.0,
                             "is_seed": False, "size": 16}]
        j = self.post_hash()
        self.assertEqual(j["tickets"], [])

    def test_abs_invisible_file_gives_actionable_hint(self):
        """文件在 qB 报告里但容器内看不到(没挂卷): 提示要挂卷, 而不是笼统报错。"""
        ghost = self.ext_dir / "动漫" / "gone.mp4"
        self._fake.files = [{"name": "动漫/gone.mp4", "progress": 1.0,
                             "is_seed": True, "size": 1}]
        j = self.post_hash()
        self.assertEqual(j["tickets"], [])
        self.assertTrue(any("挂载进容器" in s for s in j["skipped"]), j["skipped"])
        ghost.unlink(missing_ok=True)

    def test_forged_abs_path_refused(self):
        """绕过签票直接伪造 _abs 票据: 下载时 qB 复核不过 → 403/404, 不得读文件。"""
        victim = self.ext_dir / "动漫" / "Player S3.mp4"
        tok = app._issue_dl_ticket("_abs", "abc123", str(victim))
        # 先确认正常情况下这张票可用(证明拒绝来自复核, 而不是"文件本来就不在")
        self.assertEqual(self.client.get("/api/files/get?t=" + tok).status_code, 200)
        # qB 不再报告该文件(种子被删/改名) → 同一张票必须失效
        self._fake.files = []
        g = self.client.get("/api/files/get?t=" + tok)
        self.assertIn(g.status_code, (403, 404))

    def test_forged_abs_hash_refused(self):
        """hash 换成 qB 里不存在的种子: 复核直接失败, 即使路径真实存在。"""
        victim = self.ext_dir / "动漫" / "Player S3.mp4"
        tok = app._issue_dl_ticket("_abs", "deadbeef", str(victim))
        g = self.client.get("/api/files/get?t=" + tok)
        self.assertIn(g.status_code, (403, 404))

    def test_resolve_abs_qb_rejects_partial_and_missing(self):
        good = app._resolve_abs_qb("abc123", str(self.ext_file))
        self.assertIsNotNone(good)
        self.assertEqual(good.name, "Player S3.mp4")
        part = self.ext_dir / "动漫" / "p.mkv.part"
        part.write_bytes(b"z")
        self.assertIsNone(app._resolve_abs_qb("abc123", str(part)))
        self.assertIsNone(app._resolve_abs_qb("abc123", str(self.ext_dir / "nope.mkv")))
        self.assertIsNone(app._resolve_abs_qb("", str(self.ext_file)))
        self.assertIsNone(app._resolve_abs_qb("abc123", ""))


class TestTorrentPanelDownloadUI(unittest.TestCase):
    """种子面板「下载完成」筛选 + 勾选下载 的 HTML 契约(纯字符串断言)。

    v1.3.18 起「下载到本机」只剩工具条一个按钮: 面板内的 torrDlLocalBtn 已删除,
    由 downloadLocalSel() 按当前面板分发(种子面板走 TORR_CHECKED 的 {hash} 形态)。
    """

    def test_filter_chips_present(self):
        self.assertIn('id="torr-state-filter"', HTML)
        for f in ("all", "dl", "done", "seed"):
            self.assertIn('data-f="%s"' % f, HTML)

    def test_single_download_button(self):
        """全局只允许一个「下载到本机」按钮(工具条), 面板内不得再有第二个。"""
        self.assertIn('id="btnDlLocal"', HTML)
        self.assertNotIn('id="torrDlLocalBtn"', HTML)
        self.assertNotIn("torrDlLocalBtn", HTML)
        self.assertEqual(HTML.count("onclick=\"downloadLocalSel()\""), 1)

    def test_toolbar_button_dispatches_by_panel(self):
        """工具条按钮在种子面板必须分发到 torrDlLocalSel(), 其余面板走 .ck 勾选。"""
        m = re.search(r"async function downloadLocalSel\(\)\{(.*?)const cks=", HTML, re.S)
        self.assertIsNotNone(m, "找不到 downloadLocalSel 的分发段")
        head = m.group(1)
        self.assertIn("dataset.tab==='torrent'", head)
        self.assertIn("return torrDlLocalSel()", head)

    def test_render_functions_defined(self):
        for fn in ("function torrStateClass(", "function renderTorrList(",
                   "function setTorrFilter(", "function torrCkChange(",
                   "async function torrDlLocalSel("):
            self.assertIn(fn, HTML, fn)

    def test_hash_items_sent_to_ticket(self):
        self.assertIn("{hash:h}", HTML)

    def test_subtab_renamed_to_download_list(self):
        """子标签「下载中」已改名「下载列表」, 且中英文都进了 i18n。"""
        self.assertRegex(HTML, r"'torrRunning':\{zh:'⏳ 下载列表',en:'⏳ Downloads'\}")
        self.assertIn('data-i18n="torrRunning"', HTML)

    def test_new_i18n_keys_present_and_used(self):
        for k in ("torrFilterAll", "torrFilterDl",
                  "torrFilterDone", "torrFilterSeed", "torrNoTorrents",
                  "torrFilteredEmpty"):
            with self.subTest(key=k):
                self.assertRegex(HTML, r"'%s':\{zh:'[^']+',en:'[^']+'\}" % k)
                used = (re.findall(r"t\('%s'\)" % k, HTML)
                        or re.findall(r'data-i18n="%s"' % k, HTML))
                self.assertTrue(used, "%s 定义了但没使用" % k)

    def test_removed_i18n_key_gone(self):
        """面板按钮删除后, 其专属 i18n 键不得残留(残留=死代码)。"""
        self.assertNotIn("'torrDlLocal':", HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
