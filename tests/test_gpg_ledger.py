#!/usr/bin/env python3
"""GPG 验证状态账本测试(P1-⑤b) —— 应用必须自证「每条发行版验没验过」。

外部守卫(test_wiring_gpg 的 spy 断言)证明接线存在; 本文件证明账本本身可信:
  1. record_from_msg 三态分类与 download_linux 日志语义一致
  2. 未配置 gpg_verify 的条目不记账(never_invoked 只覆盖该管的)
  3. fail 也算"有过机会", 只有从未落账的才进 never_invoked
  4. 账本跨实例持久(子进程写、健康接口读, 共享同一个 JSON 文件)
  5. /api/health 暴露 gpg_ledger 摘要
  6. app.py 的 import 不得因数据目录不可写而炸(C 端 C:\data 教训的回归守卫)
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import gpg_ledger  # noqa: E402

ENTRY = {"type": "linux", "distribution": "debian", "version": "13",
         "gpg_verify": "checksum"}
ENTRY_KEY = "linux/debian@13"
PLAIN = {"type": "linux", "distribution": "custom-iso", "version": ""}

PASS_MSG = "✓ GPG 签名验证通过"
SKIP_MSG = "  - GPG 签名验证跳过(官方无公钥)"
FAIL_MSG = "⚠ GPG 校验未通过, 拒绝下载"


class TestLedgerRecording(unittest.TestCase):
    """三态分类必须与验签语义一致。"""

    def test_pass_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "gpg_ledger.json"
            state = gpg_ledger.record_from_msg(ENTRY, True, PASS_MSG, path=p)
            self.assertEqual(state, "pass")
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))[ENTRY_KEY]["state"], "pass")

    def test_skip_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "gpg_ledger.json"
            state = gpg_ledger.record_from_msg(ENTRY, True, SKIP_MSG, path=p)
            self.assertEqual(state, "skip", "ok=True 且日志含'跳过'必须记为 skip")

    def test_fail_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "gpg_ledger.json"
            state = gpg_ledger.record_from_msg(ENTRY, False, FAIL_MSG, path=p)
            self.assertEqual(state, "fail", "ok=False 必须记为 fail(阻断也是'验过')")

    def test_unconfigured_entry_not_recorded(self):
        """没配 gpg_verify 的条目不记账 —— 账本只覆盖该验签的条目。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "gpg_ledger.json"
            self.assertIsNone(gpg_ledger.record_from_msg(PLAIN, True, PASS_MSG, path=p))
            self.assertFalse(p.exists(), "未配置条目不应产生任何账本文件")
            self.assertIsNone(gpg_ledger.record_from_msg(None, True, PASS_MSG, path=p))

    def test_record_never_raises(self):
        """账本是观测设施: 任何底层异常都不允许波及下载主流程。"""
        with tempfile.TemporaryDirectory() as d:
            # path 指向一个已存在的"文件" -> _save 必炸 -> 但接口必须吞掉
            blocker = Path(d) / "blocker"
            blocker.write_text("not a dir", encoding="utf-8")
            bad = blocker / "gpg_ledger.json"
            self.assertIsNone(
                gpg_ledger.record_from_msg(ENTRY, True, PASS_MSG, path=bad))


class TestLedgerSummary(unittest.TestCase):
    """never_invoked = 配置了验签 - 账本里有记录(fail 也算验过)。"""

    def _write_config(self, d: str) -> Path:
        jp = Path(d) / "distributions.json"
        jp.write_text(json.dumps({"distributions": [ENTRY, PLAIN]}, ensure_ascii=False),
                      encoding="utf-8")
        return jp

    def test_never_invoked_lists_unverified(self):
        with tempfile.TemporaryDirectory() as d:
            jp, lp = self._write_config(d), Path(d) / "gpg_ledger.json"
            s = gpg_ledger.build_summary(json_path=jp, path=lp)
            self.assertEqual(s["configured"], 1, "只有 gpg_verify 条目计入 configured")
            self.assertEqual(s["never_invoked"], [ENTRY_KEY],
                             "从未落账的验签条目必须被点名")
            self.assertEqual(s["states"], {"pass": 0, "fail": 0, "skip": 0})

    def test_recorded_entry_leaves_never_invoked(self):
        with tempfile.TemporaryDirectory() as d:
            jp, lp = self._write_config(d), Path(d) / "gpg_ledger.json"
            gpg_ledger.record_from_msg(ENTRY, False, FAIL_MSG, path=lp)
            s = gpg_ledger.build_summary(json_path=jp, path=lp)
            self.assertEqual(s["never_invoked"], [], "fail 也是'验过', 不得误报")
            self.assertEqual(s["states"]["fail"], 1)
            self.assertEqual(s["states"]["pass"], 0)

    def test_ledger_persists_across_instances(self):
        """账本走共享文件: 子进程写完, 另一个进程(summary)必须读得到。"""
        with tempfile.TemporaryDirectory() as d:
            jp, lp = self._write_config(d), Path(d) / "gpg_ledger.json"
            gpg_ledger.record_from_msg(ENTRY, True, PASS_MSG, path=lp)
            s = gpg_ledger.build_summary(json_path=jp, path=lp)
            self.assertEqual(s["states"]["pass"], 1)
            self.assertEqual(s["never_invoked"], [])

    def test_corrupt_inputs_degrade_to_zero(self):
        """配置/账本损坏时 summary 不得抛异常(health 永远 200)。"""
        with tempfile.TemporaryDirectory() as d:
            jp = Path(d) / "distributions.json"
            jp.write_text("{corrupt!", encoding="utf-8")
            lp = Path(d) / "gpg_ledger.json"
            lp.write_text("not json", encoding="utf-8")
            s = gpg_ledger.build_summary(json_path=jp, path=lp)
            self.assertEqual(s["configured"], 0)
            self.assertEqual(s["never_invoked"], [])


class TestHealthExposesLedger(unittest.TestCase):
    """/api/health 必须携带 gpg_ledger 摘要(运维/巡检的单一入口)。"""

    def test_health_contains_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            jp = Path(d) / "distributions.json"
            jp.write_text(json.dumps({"distributions": [ENTRY]}), encoding="utf-8")
            sys.path.insert(0, str(REPO_ROOT / "web"))
            import web.app as web_app
            env = dict(os.environ, ISO_DATA_DIR=d)
            with patch.object(web_app, "JSON_FILE", jp), \
                 patch.dict(os.environ, env):
                client = web_app.app.test_client()
                resp = client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertTrue(body["ok"])
            ledger = body.get("gpg_ledger")
            self.assertIsInstance(ledger, dict, "health 必须暴露 gpg_ledger 字段")
            self.assertEqual(ledger["configured"], 1)
            self.assertIn("never_invoked", ledger)
            self.assertIn(ENTRY_KEY, ledger["never_invoked"],
                          "空账本时该验签条目必须出现在 never_invoked 里")


class TestImportSideEffectHardened(unittest.TestCase):
    """app.py 的 import 不得因数据目录不可写而炸(C:\data 教训)。

    ISO_DATA_DIR 指向一个已存在的"文件" -> mkdir 必失败 -> import 仍须成功。
    必须用真子进程: 测试进程里 web.app 已被其他用例 import 过, 测不到副作用。
    """

    def test_import_survives_unwritable_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "data"      # 先造成"路径存在但是文件"
            blocker.write_text("not a dir", encoding="utf-8")
            env = dict(os.environ, ISO_DATA_DIR=str(blocker))
            web_dir, iso_dir = REPO_ROOT / "web", REPO_ROOT / "iso_download"
            code = (
                "import sys; "
                f"sys.path.insert(0, r'{web_dir}'); "
                f"sys.path.insert(0, r'{iso_dir}'); "
                "import web.app"
            )
            r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, env=env, timeout=120)
            self.assertEqual(
                r.returncode, 0,
                "数据目录不可写时 import web.app 不得崩溃(炸 import 会让 CI 环境全红):\\n"
                + r.stderr[-800:])


if __name__ == "__main__":
    unittest.main()
