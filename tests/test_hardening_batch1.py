#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 1 批加固的验收测试 —— 对应审查报告 M1 / M6 / M7 / M11 / L2 / L3。

原则: 断言钉在**行为**上, 不钉字面量。每项都同时钉住"改对了"和"没改坏":

  M1  源 404 必须降级为"无法判定"(None/skip), 不得误判成"被篡改"(fail);
      同时守住反向约束 —— 真的校验和不符仍然必须是 fail。
  M6  未配置指纹时仍走 TOFU 并放行(**不得**改成 fail, 那会破坏降级契约),
      但"未锚定"要能在 /api/health 看见。
  M7  未预期异常的原文不进响应体; 但**校验类** ValueError 必须原样返回
      (那是写给用户看的中文提示, 收敛它就是 UX 回归)。
  M11 /api/protected 的 path 必须是非空字符串, strip 后入库。
  L2  webdav.yml 用占位口令要告警(手工复制示例这条路径绕过了面板校验)。
  L3  鉴权全关要在启动日志里喊出来。
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

# 本地环境可能未装 tqdm(容器内才有), 注入桩模块以便导入被测模块
if "tqdm" not in sys.modules:
    try:
        import tqdm  # noqa: F401
    except ImportError:
        _stub = types.ModuleType("tqdm")
        _stub.tqdm = lambda *a, **k: MagicMock()
        sys.modules["tqdm"] = _stub

import download_linux as dl  # noqa: E402
import gpg_ledger  # noqa: E402
import torrent_client  # noqa: E402
import app  # noqa: E402


def _downloader():
    """造一个不跑 __init__ 的下载器实例(与既有测试同法)。"""
    d = dl.LinuxDistributionDownloader.__new__(dl.LinuxDistributionDownloader)
    d.download_dir = Path(tempfile.mkdtemp())
    return d


# --------------------------------------------------------------------------- M1

class _HttpErrorResponse:
    """模拟 404: 正文是 HTML 错误页(非空!), raise_for_status 抛 HTTPError。"""

    def __init__(self, body=b"<html>404 Not Found</html>"):
        self.content = body
        self.text = body.decode("utf-8", "replace")

    def raise_for_status(self):
        raise dl.requests.HTTPError("404 Client Error: Not Found")


class TestM1UnreachableDegrades(unittest.TestCase):
    """M1: 源不可达(404)必须降级, 不能变成"疑似被篡改"。"""

    def test_sig_404_returns_skip_and_never_invokes_gpg(self):
        """签名文件 404 -> skip, 且**没有**拿 HTML 正文去跑 gpg。

        这是 M1 的根因: 旧实现只看 `if not sig`, 404 的 HTML 正文非空,
        于是继续往下走, gpg 对 HTML 验签必然失败 -> fail -> 上层当作被篡改。
        """
        d = _downloader()
        keyring = d.download_dir / "iso-hub.gpg"
        keyring.write_bytes(b"FAKE-KEYRING")  # 预置缓存, 跳过公钥下载

        fake_run = MagicMock()
        with patch("shutil.which", return_value="/usr/bin/gpg"), \
             patch.object(dl.subprocess, "run", fake_run), \
             patch.object(dl.requests, "get", return_value=_HttpErrorResponse()):
            status = d.verify_signature("checksum text", "http://mirror/SHA256SUMS.gpg",
                                        "http://mirror/key.asc", d.download_dir, None)

        self.assertEqual(status, "skip", "源不可达属无法判定, 只能是 skip")
        fake_run.assert_not_called()

    def test_empty_checksum_text_returns_skip(self):
        """拿不到 checksum 文本时无对象可验 -> skip(不是 fail)。"""
        d = _downloader()
        with patch("shutil.which", return_value="/usr/bin/gpg"):
            status = d.verify_signature("", "http://mirror/x.gpg",
                                        "http://mirror/key.asc", d.download_dir, None)
        self.assertEqual(status, "skip")

    @patch.object(dl.LinuxDistributionDownloader, "sha256_of")
    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    def test_checksum_404_yields_undecidable(self, mock_gc, mock_sha):
        """checksum 源 404 -> verify_checksum_smart 返回 None(无法判定)。

        旧行为: 404 的 HTML 被当 checksum 传给 GPG 预检 -> fail -> 上层重下。
        新行为: 一路降级, 最终是 None, 由调用方按"未验签落定"处理。
        """
        d = _downloader()
        d.headers = {}
        mock_gc.return_value = None
        mock_sha.return_value = "c" * 64
        with patch("shutil.which", return_value="/usr/bin/gpg"), \
             patch.object(dl.requests, "get", return_value=_HttpErrorResponse()):
            ok, msg = d.verify_checksum_smart(
                Path("/tmp/x.iso"), "http://mirror/SHA256SUMS", "",
                dist={"distribution": "Ubuntu", "gpg_verify": "checksum",
                      "gpg_key_url": "http://mirror/key.asc",
                      "signature_url": "http://mirror/SHA256SUMS.gpg"})
        self.assertIsNone(ok, "不可达必须返回 None, 不能是 False")
        self.assertIn("无法判定", msg)

    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    @patch.object(dl.LinuxDistributionDownloader, "verify_checksum")
    def test_real_mismatch_still_fails(self, mock_vc, mock_gc):
        """反向约束: 拿到摘要且对不上 -> 仍然必须 False(mismatch 语义不许被削弱)。"""
        d = _downloader()
        mock_gc.return_value = "a" * 64
        mock_vc.return_value = False
        with patch.object(dl.LinuxDistributionDownloader, "sha256_of",
                          return_value="b" * 64):
            ok, msg = d.verify_checksum_smart(Path("/tmp/x.iso"), "http://c", "")
        self.assertIs(ok, False)
        self.assertIn("不一致", msg)


# --------------------------------------------------------------------------- M6

class TestM6TofuVisible(unittest.TestCase):
    """M6: 未锚定要可见, 但绝不能因此拒绝下载。"""

    def test_has_fingerprint_shapes(self):
        self.assertTrue(gpg_ledger.has_fingerprint({"gpg_key_fingerprint": "AB" * 20}))
        self.assertTrue(gpg_ledger.has_fingerprint({"gpg_key_fingerprint": ["AB" * 20]}))
        self.assertFalse(gpg_ledger.has_fingerprint({"gpg_key_fingerprint": ""}))
        self.assertFalse(gpg_ledger.has_fingerprint({"gpg_key_fingerprint": []}))
        self.assertFalse(gpg_ledger.has_fingerprint({"gpg_key_fingerprint": ["  "]}))
        self.assertFalse(gpg_ledger.has_fingerprint({}))

    def test_no_fingerprint_still_passes_keyring_prepare(self):
        """无指纹 + 已有缓存 keyring -> "ok"(TOFU 放行), **不是** fail。

        这条是 M6 的红线: 任何"显性化"都不能把 TOFU 变成拒绝下载。
        """
        d = _downloader()
        keyring = d.download_dir / "iso-hub.gpg"
        keyring.write_bytes(b"FAKE-KEYRING")
        self.assertEqual(d._prepare_keyring(keyring, "", []), "ok")

    def test_unpinned_listed_in_summary_and_health(self):
        """health 的 gpg_ledger 摘要里要点名未锚定条目。"""
        tmp = Path(tempfile.mkdtemp())
        manifest = {"distributions": [
            {"distribution": "Pinned", "type": "linux", "gpg_verify": "checksum",
             "gpg_key_url": "https://k", "gpg_key_fingerprint": "AB" * 20,
             "download_url": "https://e/pinned.iso"},
            {"distribution": "TofuOnly", "type": "linux", "gpg_verify": "checksum",
             "gpg_key_url": "https://k", "download_url": "https://e/tofu.iso"},
        ]}
        jf = tmp / "distributions.json"
        jf.write_text(json.dumps(manifest), encoding="utf-8")

        summary = gpg_ledger.build_summary(jf, path=tmp / "nope.json", download_dir=tmp)
        self.assertEqual(summary["configured"], 2)
        self.assertEqual(summary["unpinned_count"], 1)
        self.assertIn("TofuOnly", summary["unpinned"][0])
        self.assertNotIn("Pinned", " ".join(summary["unpinned"]))

        # 经由 /api/health 也要能看到
        with patch.object(app, "JSON_FILE", jf), patch.object(app, "DATA_DIR", tmp):
            body = app.app.test_client().get("/api/health").get_json()
        self.assertEqual(body["gpg_ledger"]["unpinned_count"], 1)


# --------------------------------------------------------------------------- M7

class TestM7NoRawException(unittest.TestCase):
    """M7: 未预期异常只留 ID, 校验提示原样返回。"""

    def test_server_error_hides_detail_but_logs_it(self):
        from api import torrent as torrent_api
        secret = "OSError: /downloads/secret.iso 权限不足 host=qb-internal"
        # 必须在 app context 里调用: api._ctx.core 是活代理, 有上下文时按
        # current_app.extensions 解析内核, 无上下文时才走 sys.modules 兜底 ——
        # 后者在全量测试里可能解析到另一个 app 模块对象, 桩就打不上。
        with app.app.app_context(), patch.object(app, "log") as fake_log:
            msg = torrent_api._server_error("添加失败", OSError(secret))
        self.assertIn("错误编号", msg)
        self.assertNotIn("/downloads/", msg)
        self.assertNotIn("qb-internal", msg)
        # 原文必须进服务端日志(用同一 ID 关联)
        eid = msg.split("错误编号 ")[1].split(")")[0]
        logged = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn(eid, logged)
        self.assertIn("qb-internal", logged)

    def test_qb_reject_hides_response_body(self):
        detail = {"error": "savepath /downloads/inner is not writable"}
        with patch.object(torrent_client, "_log") as fake_log:
            out = torrent_client._qb_reject(detail)
        self.assertFalse(out["ok"])
        self.assertNotIn("/downloads/", out["error"])
        self.assertTrue(fake_log.warning.called)

    def test_download_ticket_404_does_not_echo_container_path(self):
        """files.py 不再回显 fname(可能是 /downloads/xxx.iso 这样的绝对路径)。"""
        tmp = Path(tempfile.mkdtemp())
        (tmp / "settings.json").write_text("{}", encoding="utf-8")
        with patch.object(app, "DATA_DIR", tmp), \
             patch.object(app, "SETTINGS_JSON", tmp / "settings.json"), \
             patch.object(app, "REQUIRE_LOGIN", False), \
             patch.object(app, "AUTH_TOKEN", ""), \
             patch.object(app, "log") as fake_log:
            app._dl_tickets.clear()
            app._dl_tickets["tok"] = ("linux", "Ghost", "/downloads/ghost.iso",
                                      __import__("time").time() + 600)
            resp = app.app.test_client().get("/api/files/get?t=tok")
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertNotIn("/downloads/", body["error"])
        self.assertIn("/downloads/ghost.iso",
                      " ".join(str(c.args[0]) for c in fake_log.call_args_list))


# --------------------------------------------------------------------------- M11

class TestM11ProtectedPathValidation(unittest.TestCase):
    """M11: path 必须是非空字符串, 且 strip 后入库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.settings = self.data / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")
        for p in (patch.object(app, "DATA_DIR", self.data),
                  patch.object(app, "SETTINGS_JSON", self.settings),
                  patch.object(app, "REQUIRE_LOGIN", False),
                  patch.object(app, "AUTH_TOKEN", "")):
            p.start()
            self.addCleanup(p.stop)
        self.client = app.app.test_client()

    def _protected(self):
        return json.loads(self.settings.read_text(encoding="utf-8")).get("protected", [])

    def test_non_string_rejected(self):
        for bad in ({"a": 1}, ["linux/x.iso"], 123, True):
            resp = self.client.post("/api/protected", json={"path": bad})
            self.assertEqual(resp.status_code, 400, f"{bad!r} 应被拒绝")
        self.assertEqual(self._protected(), [], "非法输入不得落盘")

    def test_blank_rejected(self):
        for bad in ("", "   ", "\t\n"):
            resp = self.client.post("/api/protected", json={"path": bad})
            self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._protected(), [])

    def test_valid_path_is_stripped_and_toggles(self):
        resp = self.client.post("/api/protected",
                                json={"path": "  linux/Ubuntu/x.iso  "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._protected(), ["linux/Ubuntu/x.iso"],
                         "入库前必须 strip, 否则相等匹配永远匹配不上")

        # 再点一次取消锁定
        resp = self.client.post("/api/protected", json={"path": "linux/Ubuntu/x.iso"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["removed"])
        self.assertEqual(self._protected(), [])


# --------------------------------------------------------------------------- L2

class TestL2WebdavPlaceholderWarns(unittest.TestCase):
    """L2: 占位口令要告警; 强口令不该误报。"""

    def _warn(self, content):
        tmp = Path(tempfile.mkdtemp()) / "webdav.yml"
        tmp.write_text(content, encoding="utf-8")
        with patch.object(app, "log") as fake_log:
            app._warn_if_webdav_conf_weak(tmp)
        return " ".join(str(c.args[0]) for c in fake_log.call_args_list)

    def test_placeholder_warns(self):
        for pw in ("change-me-please", "CHANGE-ME-BEFORE-USE", "", "iso123"):
            out = self._warn(f"users:\n  - username: iso\n    password: {pw}\n")
            self.assertIn("占位/空口令", out, f"{pw!r} 应触发告警")

    def test_strong_password_silent(self):
        out = self._warn('users:\n  - username: iso\n    password: "Zx9!long-real-pw"\n')
        self.assertEqual(out, "", "真口令不该误报")

    def test_repo_example_triggers_warning(self):
        """仓库里的示例文件本身就必须被识别为占位口令。

        这条是端到端断言: 示例写成什么样, 启动时就该告警。不去在测试里另写一套
        解析 —— 那样解析器出错反而会被测试掩盖。
        """
        text = (REPO_ROOT / "webdav.yml").read_text(encoding="utf-8")
        self.assertIn("占位/空口令", self._warn(text))

    def test_scalar_parser_handles_inline_comment(self):
        """行尾注释必须先剥掉再判口令, 否则占位值永远识别不出来。"""
        self.assertEqual(app._yaml_scalar_value('password: "abc"   # 注释'), "abc")
        self.assertEqual(app._yaml_scalar_value("password: abc # 注释"), "abc")
        self.assertEqual(app._yaml_scalar_value("password: ''"), "")
        self.assertEqual(app._yaml_scalar_value("password:   "), "")


# --------------------------------------------------------------------------- L3

class TestL3AuthDisabledBanner(unittest.TestCase):
    """L3: 鉴权全关要喊出来; 正常配置下不吵。"""

    def test_banner_when_fully_open(self):
        with patch.object(app, "REQUIRE_LOGIN", False), \
             patch.object(app, "AUTH_TOKEN", ""), \
             patch.object(app, "log") as fake_log:
            fired = app._warn_if_auth_disabled()
        self.assertTrue(fired)
        out = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("鉴权已完全关闭", out)

    def test_silent_when_login_required(self):
        with patch.object(app, "REQUIRE_LOGIN", True), \
             patch.object(app, "AUTH_TOKEN", ""), \
             patch.object(app, "log") as fake_log:
            self.assertFalse(app._warn_if_auth_disabled())
        self.assertFalse(fake_log.called)

    def test_silent_when_token_set(self):
        with patch.object(app, "REQUIRE_LOGIN", False), \
             patch.object(app, "AUTH_TOKEN", "secret"), \
             patch.object(app, "log") as fake_log:
            self.assertFalse(app._warn_if_auth_disabled())
        self.assertFalse(fake_log.called)


if __name__ == "__main__":
    unittest.main()
