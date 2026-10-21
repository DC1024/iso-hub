#!/usr/bin/env python3
"""v1.4.0 「未验签文件重新校验」回归测试。

覆盖两层:
  * app 侧 POST /api/verify —— 参数/条目/文件/校验源四道前置判断, 以及任务命令拼接
  * runner 侧 --verify-only  —— 三态落库(通过/不符/无法判定)与退出码

三条必须守住的语义:
  1. **绝不把"没配校验源"当"校验通过"**: verify_checksum_smart 在两个校验字段
     都为空时返回 (True, "跳过校验和验证（无可用信息）"), 那是下载路径的历史语义,
     重校验必须把它降级成"无法判定"。
  2. **绝不自动删文件**: 返回 False 只记 state=mismatch, 是否重下交给用户。
  3. **旧记录没有 state 字段时必须按 unverified 处理**(向后兼容)。
"""

import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402
import iso_runner  # noqa: E402


# --------------------------------------------------------------------------- runner 侧
class _FakeDownloader:
    """只实现 _run_verify_only 用到的那一小块接口。"""

    def __init__(self, download_dir: Path, entries: list, result):
        self.download_dir = str(download_dir)
        self.distributions = {"distributions": entries}
        self._result = result
        self.calls = 0

    def verify_checksum_smart(self, filepath, checksum_url, stored_checksum, dist=None):
        self.calls += 1
        if callable(self._result):
            return self._result(filepath)
        return self._result


def _args(rel: str, storage_mode: str = "classified", download_dir: str = ""):
    return types.SimpleNamespace(rel=rel, storage_mode=storage_mode,
                                 download_dir=download_dir, json_file="")


class ParseRelTest(unittest.TestCase):
    def test_accepts_three_segments(self):
        self.assertEqual(iso_runner._parse_rel("linux/Ubuntu/u.iso"),
                         ("linux", "Ubuntu", "u.iso"))

    def test_rejects_malformed(self):
        for bad in ("", "linux/Ubuntu", "a/b/c/d", "linux//u.iso",
                    "linux/../u.iso", "/etc/passwd/x", "linux/Ubuntu/../x.iso",
                    "linux/Ubuntu/u.iso.part", "linux/Ubuntu/ u.iso",
                    "linux/Ubuntu/u\\iso"):
            self.assertIsNone(iso_runner._parse_rel(bad), bad)


class VerifyOnlyStateTest(unittest.TestCase):
    """三态 + 两个守卫。用 tmp 目录当 DATA_DIR, 不走网络。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.dist = self.data / "linux" / "Ubuntu"
        self.dist.mkdir(parents=True)
        self.iso = self.dist / "u.iso"
        self.iso.write_bytes(b"x" * 32)
        self.entry = {
            "distribution": "Ubuntu", "type": "linux",
            "download_url": "https://example.com/u.iso",
            "checksum_url": "https://example.com/SHA256SUMS",
        }
        self.record = self.data / "download_unverified.json"
        self.record.write_text(json.dumps({
            "linux/Ubuntu/u.iso": {"at": 1, "reason": "checksum 404"},
        }), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, result):
        dl = _FakeDownloader(self.data, [self.entry], result)
        with patch.object(iso_runner, "_record_unverified",
                          wraps=iso_runner._record_unverified):
            code = iso_runner._run_verify_only(
                dl, _args("linux/Ubuntu/u.iso", download_dir=str(self.data)))
        return code, dl

    def _rec(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))["linux/Ubuntu/u.iso"]

    def test_pass_clears_record(self):
        code, dl = self._run((True, "URL校验和验证通过: abc"))
        self.assertEqual(code, iso_runner.VERIFY_EXIT_OK)
        self.assertEqual(dl.calls, 1)
        self.assertNotIn("linux/Ubuntu/u.iso",
                         json.loads(self.record.read_text(encoding="utf-8")))

    def test_mismatch_marks_state_and_keeps_file(self):
        code, _ = self._run((False, "内容校验不符: 期望 a, 实际 b"))
        self.assertEqual(code, iso_runner.VERIFY_EXIT_MISMATCH)
        rec = self._rec()
        self.assertEqual(rec["state"], "mismatch")
        self.assertIn("内容校验不符", rec["reason"])
        self.assertTrue(self.iso.exists(), "校验不符绝不能自动删文件")

    def test_undecidable_keeps_unverified(self):
        code, _ = self._run((None, "无法判定: 校验源不可达"))
        self.assertEqual(code, iso_runner.VERIFY_EXIT_UNDECIDABLE)
        rec = self._rec()
        self.assertEqual(rec["state"], "unverified")
        self.assertIn("无法判定", rec["reason"])
        self.assertEqual(rec["at"] > 1, True, "应刷新时间戳而不是沿用旧值")

    def test_skip_message_downgraded_to_undecidable(self):
        """核心守卫: '跳过校验和验证' 不能当通过。"""
        code, _ = self._run((True, "跳过校验和验证（无可用信息）"))
        self.assertEqual(code, iso_runner.VERIFY_EXIT_UNDECIDABLE)
        self.assertEqual(self._rec()["state"], "unverified")

    def test_no_checksum_source_never_calls_verifier(self):
        self.entry.pop("checksum_url")
        code, dl = self._run((True, "不该被调用"))
        self.assertEqual(code, iso_runner.VERIFY_EXIT_UNDECIDABLE)
        self.assertEqual(dl.calls, 0, "没有校验源时不应调用校验器(它会返回 True/跳过)")
        self.assertEqual(self._rec()["state"], "unverified")

    def test_changed_during_verify_discards_result(self):
        def mutate(fp):
            time.sleep(0.01)
            fp.write_bytes(b"y" * 64)   # 模拟并发下载改写
            return True, "URL校验和验证通过"

        code, _ = self._run(mutate)
        self.assertEqual(code, iso_runner.VERIFY_EXIT_UNDECIDABLE)
        # 结果作废时**不写记录**: 既不能洗白成"通过", 也不该用一次无效结果
        # 覆盖原有原因/严重级别(mismatch 不能被降级成 unverified)
        rec = self._rec()
        self.assertEqual(rec["reason"], "checksum 404")
        self.assertEqual(rec["at"], 1)
        self.assertNotIn("state", rec)

    def test_missing_file_badarg(self):
        self.iso.unlink()
        code, dl = self._run((True, "x"))
        self.assertEqual(code, iso_runner.VERIFY_EXIT_BADARG)
        self.assertEqual(dl.calls, 0)

    def test_entry_missing_badarg(self):
        dl = _FakeDownloader(self.data, [], (True, "x"))
        self.assertEqual(iso_runner._run_verify_only(dl, _args("linux/Ubuntu/u.iso")),
                         iso_runner.VERIFY_EXIT_BADARG)

    def test_flat_mode_looks_in_iso_dir(self):
        flat = self.data / "iso"
        flat.mkdir()
        (flat / "u.iso").write_bytes(b"z" * 8)
        dl = _FakeDownloader(self.data, [self.entry], (True, "ok"))
        code = iso_runner._run_verify_only(dl, _args("linux/Ubuntu/u.iso", "flat"))
        self.assertEqual(code, iso_runner.VERIFY_EXIT_OK)
        self.assertEqual(dl.calls, 1)


class RelMappingConsistencyTest(unittest.TestCase):
    """runner 与 app 的 rel 拼法必须一致, 否则按钮会指向不存在的文件。"""

    def test_both_sides_use_type_name_file(self):
        app_src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        self.assertIn('rel = f"{typ}/{name}/{fname}"', app_src)
        self.assertIn('"--rel", rel', app_src)


# --------------------------------------------------------------------------- app 侧
class _ApiBase(unittest.TestCase):
    require_login = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        (self.data / "linux" / "Ubuntu").mkdir(parents=True)
        (self.data / "linux" / "Ubuntu" / "u.iso").write_bytes(b"x" * 32)
        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.data / "distributions.json"),
            patch.object(app, "SETTINGS_JSON", self.data / "settings.json"),
            patch.object(app, "FAILURES_JSON", self.data / "download_failures.json"),
            patch.object(app, "UNVERIFIED_JSON", self.data / "download_unverified.json"),
            patch.object(app, "REQUIRE_LOGIN", self.require_login),
            patch.object(app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        app._sessions.clear()
        self.addCleanup(app._sessions.clear)
        self.client = app.app.test_client()
        self.write_distros([{
            "distribution": "Ubuntu", "type": "linux",
            "download_url": "https://example.com/u.iso",
            "checksum_url": "https://example.com/SHA256SUMS",
        }])

    def tearDown(self):
        self._tmp.cleanup()

    def write_distros(self, entries):
        (self.data / "distributions.json").write_text(
            json.dumps({"distributions": entries}), encoding="utf-8")

    def post(self, rel):
        return self.client.post("/api/verify", json={"rel": rel})


class ApiVerifyGuardTest(_ApiBase):
    def test_rejects_bad_rel(self):
        for bad in ("", "linux/Ubuntu", "a/b/c/d", "linux/../u.iso",
                    "/etc/passwd/x", "linux/Ubuntu/../x.iso"):
            r = self.post(bad)
            self.assertEqual(r.status_code, 400, bad)

    def test_rejects_part_file(self):
        r = self.post("linux/Ubuntu/u.iso.part")
        self.assertEqual(r.status_code, 409)

    def test_unknown_entry_404(self):
        r = self.post("linux/Debian/d.iso")
        self.assertEqual(r.status_code, 404)

    def test_no_checksum_source_400(self):
        self.write_distros([{
            "distribution": "Ubuntu", "type": "linux",
            "download_url": "https://example.com/u.iso",
        }])
        r = self.post("linux/Ubuntu/u.iso")
        self.assertEqual(r.status_code, 400)
        self.assertIn("没有配置校验源", r.get_json()["error"])

    def test_missing_file_409(self):
        (self.data / "linux" / "Ubuntu" / "u.iso").unlink()
        r = self.post("linux/Ubuntu/u.iso")
        self.assertEqual(r.status_code, 409)

    def test_busy_409(self):
        with patch.object(app, "start_task", return_value=False):
            r = self.post("linux/Ubuntu/u.iso")
        self.assertEqual(r.status_code, 409)
        self.assertIn("已有任务", r.get_json()["error"])

    def test_starts_verify_task_with_runner_flags(self):
        with patch.object(app, "start_task", return_value=True) as st:
            r = self.post("linux/Ubuntu/u.iso")
        self.assertEqual(r.status_code, 200)
        kind, title, cmd = st.call_args[0][0], st.call_args[0][1], st.call_args[0][2]
        self.assertEqual(kind, "verify")
        self.assertIn("u.iso", title)
        self.assertIn("--verify-only", cmd)
        self.assertIn("--rel", cmd)
        self.assertEqual(cmd[cmd.index("--rel") + 1], "linux/Ubuntu/u.iso")
        # 必须带 --json-file / --download-dir / --storage-mode, 否则 runner 起不来
        for flag in ("--json-file", "--download-dir", "--storage-mode"):
            self.assertIn(flag, cmd)


class VerifyStateDeliveryTest(_ApiBase):
    def _write_unv(self, payload):
        (self.data / "download_unverified.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def _entry(self):
        d = self.client.get("/api/distros").get_json()
        for g in d["groups"]:
            for e in g["entries"]:
                if e["filename"] == "u.iso":
                    return e
        self.fail("没找到 u.iso 条目")

    def test_mismatch_state_exposed(self):
        self._write_unv({"linux/Ubuntu/u.iso": {
            "at": int(time.time()), "reason": "内容校验不符", "state": "mismatch"}})
        e = self._entry()
        self.assertTrue(e["unverified"])
        self.assertEqual(e["verify_state"], "mismatch")

    def test_legacy_record_without_state_defaults_to_unverified(self):
        self._write_unv({"linux/Ubuntu/u.iso": {"at": 1, "reason": "checksum 404"}})
        e = self._entry()
        self.assertEqual(e["verify_state"], "unverified")

    def test_no_record_means_empty_state(self):
        e = self._entry()
        self.assertFalse(e["unverified"])
        self.assertEqual(e["verify_state"], "")


class FrontendContractTest(unittest.TestCase):
    """前端只做静态断言(与 test_no_inline_js_interpolation 同款做法)。

    页面渲染靠浏览器, 这里锁定几个容易回归的接线点: 按钮存在且走事件委托(不是行内
    onclick)、mismatch 走红色分支、i18n 键齐全、任务条认 verify 类型。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_button_uses_delegation_not_inline_onclick(self):
        self.assertIn('data-act="verify"', self.html)
        self.assertIn("case 'verify':verifyNow(d.rel,b);break;", self.html)
        self.assertNotIn('onclick="verifyNow', self.html)

    def test_button_only_for_unverified_downloaded_rows(self):
        self.assertIn("(isOk&&e.unverified)?`<button", self.html)
        self.assertIn('data-rel="${esc(e.rel', self.html)

    def test_mismatch_renders_red_branch(self):
        self.assertIn("const mis=(e.verify_state==='mismatch');", self.html)
        self.assertIn("t('verifyMismatch')", self.html)

    def test_i18n_keys_present_in_both_languages(self):
        for key in ("verifyNow", "verifyRunning", "verifyMismatch",
                    "verifyMismatchTip", "taskVerify"):
            self.assertIn(f"'{key}':{{zh:", self.html)
            self.assertIn("en:", self.html)

    def test_task_bar_recognises_verify_kind(self):
        self.assertIn("tk.kind==='verify'?t('taskVerify')", self.html)
        self.assertIn("#progfill.indet::before", self.html)
        self.assertIn("prefers-reduced-motion", self.html)
