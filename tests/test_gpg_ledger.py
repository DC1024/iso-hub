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

    def test_key_disambiguates_by_url_fragment(self):
        """无 version 的镜像组预设靠 download_url 文件名片段区分(真实数据)。"""
        e1 = {"type": "linux", "distribution": "Fedora",
              "download_url": "https://m/fedora/.../Fedora-Everything-netinst-x86_64-43-1.6.iso"}
        e2 = {"type": "linux", "distribution": "Fedora",
              "download_url": "https://m/fedora/.../Fedora-Everything-netinst-x86_64-42-1.5.iso"}
        k1, k2 = gpg_ledger.key_for(e1), gpg_ledger.key_for(e2)
        self.assertNotEqual(k1, k2, "同 type/name 不同版本必须用 URL 片段区分")
        self.assertIn("Fedora-Everything-netinst-x86_64-43-1.6.iso", k1)
        self.assertIn("Fedora-Everything-netinst-x86_64-42-1.5.iso", k2)

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


class TestNeverInvokedDiskAware(unittest.TestCase):
    """v1.5.1: never_invoked 只点名「盘上有文件却没验过」的条目。

    生产实测: 清单里 5 条 gpg_verify, 硬盘上只有 1 个 ISO, 另外 4 条曾被
    永久报成 never_invoked —— 那不是缺陷, 是还没下载。
    """

    ISO_NAME = "Fedora-Workstation-Live-43-1.6.x86_64.iso"
    W_ENTRY = {"type": "linux", "distribution": "Fedora", "gpg_verify": "checksum",
               "download_url": "https://mirrors.tuna.tsinghua.edu.cn/fedora/releases/43/"
                               "Workstation/x86_64/iso/" + ISO_NAME}
    KEY = "linux/Fedora#" + ISO_NAME

    def _config(self, d, entries=None) -> Path:
        jp = Path(d) / "distributions.json"
        jp.write_text(json.dumps({"distributions": entries or [self.W_ENTRY]},
                                 ensure_ascii=False), encoding="utf-8")
        return jp

    def _touch(self, p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ISO")

    def test_filename_from_url_strips_query(self):
        e = dict(self.W_ENTRY, download_url=self.W_ENTRY["download_url"] + "?mirror=a")
        self.assertEqual(gpg_ledger.entry_filename(e), self.ISO_NAME,
                         "query 不得混进文件名, 否则磁盘永远匹配不上")

    def test_flat_file_present_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            self._touch(Path(d) / "iso" / self.ISO_NAME)
            s = gpg_ledger.build_summary(json_path=self._config(d), download_dir=d)
            self.assertEqual(s["on_disk"], 1)
            self.assertEqual(s["never_invoked"], [self.KEY])
            self.assertEqual(s["not_downloaded"], [])
            self.assertTrue(s["disk_available"])

    def test_classified_file_present_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            self._touch(Path(d) / "linux" / "Fedora" / self.ISO_NAME)
            s = gpg_ledger.build_summary(json_path=self._config(d), download_dir=d)
            self.assertEqual(s["on_disk"], 1, "classified 模式也必须识别")
            self.assertEqual(s["never_invoked"], [self.KEY])

    def test_missing_file_goes_to_not_downloaded(self):
        """核心回归: 清单里有、盘上没有 -> 不算 never_invoked。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "iso").mkdir()
            s = gpg_ledger.build_summary(json_path=self._config(d), download_dir=d)
            self.assertEqual(s["never_invoked"], [], "没下载的条目不得再冒充'验签失败'")
            self.assertEqual(s["not_downloaded"], [self.KEY])
            self.assertEqual(s["on_disk"], 0)

    def test_disk_unavailable_is_conservative(self):
        """下载根目录不存在 -> 保守报警, 不允许把"没扫到盘"当成"一切正常"。"""
        with tempfile.TemporaryDirectory() as d:
            s = gpg_ledger.build_summary(json_path=self._config(d),
                                         download_dir=Path(d) / "not-mounted")
            self.assertFalse(s["disk_available"])
            self.assertEqual(s["never_invoked"], [self.KEY])
            self.assertEqual(s["not_downloaded"], [])

    def test_entry_without_url_is_undecidable(self):
        """缺 download_url/filename 的条目无法判定 -> 保守计入 never_invoked。"""
        with tempfile.TemporaryDirectory() as d:
            jp = Path(d) / "distributions.json"
            jp.write_text(json.dumps({"distributions": [ENTRY]}), encoding="utf-8")
            s = gpg_ledger.build_summary(json_path=jp, download_dir=d)
            self.assertEqual(s["on_disk"], 0)
            self.assertEqual(s["never_invoked"], [ENTRY_KEY])

    def test_downloaded_and_recorded_is_clean(self):
        """盘上有、账本也有 -> 什么都不该报。"""
        with tempfile.TemporaryDirectory() as d:
            self._touch(Path(d) / "iso" / self.ISO_NAME)
            lp = Path(d) / "gpg_ledger.json"
            gpg_ledger.record_from_msg(self.W_ENTRY, True, PASS_MSG, path=lp)
            s = gpg_ledger.build_summary(json_path=self._config(d), path=lp, download_dir=d)
            self.assertEqual(s["never_invoked"], [])
            self.assertEqual(s["not_downloaded"], [])
            self.assertEqual(s["states"]["pass"], 1)
            self.assertEqual(s["on_disk"], 1)

    def test_states_only_count_configured(self):
        """账本里残留的已下线配置: 不进 states, 但 ledger_total 要看得见。"""
        with tempfile.TemporaryDirectory() as d:
            lp = Path(d) / "gpg_ledger.json"
            lp.write_text(json.dumps({"linux/old@1": {"state": "pass", "ts": 1}}),
                          encoding="utf-8")
            s = gpg_ledger.build_summary(json_path=self._config(d), path=lp, download_dir=d)
            self.assertEqual(s["states"]["pass"], 0, "states 必须与 configured 同口径")
            self.assertEqual(s["ledger_total"], 1)

    def test_duplicate_keys_collapse(self):
        """同 key 的多条 entry(镜像组预设)不得让 configured/never_invoked 重复。"""
        with tempfile.TemporaryDirectory() as d:
            s = gpg_ledger.build_summary(
                json_path=self._config(d, [self.W_ENTRY, dict(self.W_ENTRY)]), download_dir=d)
            self.assertEqual(s["configured"], 1)
            self.assertEqual(s["not_downloaded"], [self.KEY])

    def test_constants_match_app_and_downloader(self):
        """路径常量复制了一份, 必须与真正落盘的模块保持一致。"""
        import download_linux
        import web.app as web_app
        self.assertEqual(gpg_ledger.FLAT_DIRNAME, web_app.FLAT_ISO_DIRNAME)
        self.assertEqual(gpg_ledger.FLAT_DIRNAME, download_linux.FLAT_DIRNAME)
        self.assertEqual(gpg_ledger.ALLOWED_TYPES, web_app.ALLOWED_TYPES)
        self.assertEqual(gpg_ledger.ALLOWED_TYPES, download_linux.ALLOWED_TYPES)


class TestStaleRecords(unittest.TestCase):
    """v1.5.4: 账本说 pass、盘上却没这个文件 -> stale, 不该再算进"当前通过"。

    生产实测 states.pass=6 而盘上只有 4 个文件通过校验: 两条 26.04.1 的 pass
    记录指向的文件早被删/替换了。删文件不会回头改账本, 于是 pass 计数永久虚高,
    而且越积越多 —— 看到的"通过 6 个"里有两个根本不存在。
    """

    ISO_NAME = "Fedora-Workstation-Live-43-1.6.x86_64.iso"
    W_ENTRY = {"type": "linux", "distribution": "Fedora", "gpg_verify": "checksum",
               "download_url": "https://mirrors.tuna.tsinghua.edu.cn/fedora/releases/43/"
                               "Workstation/x86_64/iso/" + ISO_NAME}
    KEY = "linux/Fedora#" + ISO_NAME

    def _config(self, d) -> Path:
        jp = Path(d) / "distributions.json"
        jp.write_text(json.dumps({"distributions": [self.W_ENTRY]}, ensure_ascii=False),
                      encoding="utf-8")
        return jp

    def _summary(self, d, ok=True, msg=PASS_MSG, entry=None):
        lp = Path(d) / "gpg_ledger.json"
        gpg_ledger.record_from_msg(entry or self.W_ENTRY, ok, msg, path=lp)
        return gpg_ledger.build_summary(json_path=self._config(d), path=lp, download_dir=d)

    def test_deleted_file_makes_record_stale(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "iso").mkdir()          # 目录在, 文件已被删
            s = self._summary(d)
            self.assertEqual(s["states"]["pass"], 1, "账本口径不变: 记录确实存在过")
            self.assertEqual(s["stale"], [self.KEY], "文件没了必须点名, 否则 pass 虚高")
            self.assertEqual(s["stale_by_state"]["pass"], 1)
            self.assertEqual(s["on_disk"], 0)
            self.assertEqual(
                s["states"]["pass"] - s["stale_by_state"]["pass"], 0,
                "监控口径: 当前真正在盘上的通过数应为 0")

    def test_present_file_is_not_stale(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "iso" / self.ISO_NAME
            p.parent.mkdir(parents=True)
            p.write_bytes(b"ISO")
            s = self._summary(d)
            self.assertEqual(s["stale"], [], "文件还在就不该标 stale")
            self.assertEqual(s["on_disk"], 1)

    def test_fail_record_can_also_be_stale(self):
        """fail 一样会陈旧: 验签拒绝过的文件被删, 记录同样不该继续计数。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "iso").mkdir()
            s = self._summary(d, ok=False, msg=FAIL_MSG)
            self.assertEqual(s["stale"], [self.KEY])
            self.assertEqual(s["stale_by_state"]["fail"], 1)
            self.assertEqual(s["stale_by_state"]["pass"], 0)

    def test_disk_unavailable_is_not_stale(self):
        """盘都没扫到 -> 不能断言"文件丢了"。"""
        with tempfile.TemporaryDirectory() as d:
            lp = Path(d) / "gpg_ledger.json"
            gpg_ledger.record_from_msg(self.W_ENTRY, True, PASS_MSG, path=lp)
            s = gpg_ledger.build_summary(json_path=self._config(d), path=lp,
                                         download_dir=Path(d) / "not-mounted")
            self.assertFalse(s["disk_available"])
            self.assertEqual(s["stale"], [], "没扫到盘不等于文件没了")

    def test_undecidable_entry_is_not_stale(self):
        """缺 filename/download_url -> 拼不出候选路径, 无法判定, 不进 stale。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "iso").mkdir()
            s = self._summary(d, entry=ENTRY)   # ENTRY 无 download_url
            self.assertEqual(s["stale"], [])
            self.assertEqual(s["stale_by_state"]["pass"], 0)

    def test_no_record_no_stale(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "iso").mkdir()
            s = gpg_ledger.build_summary(json_path=self._config(d), download_dir=d)
            self.assertEqual(s["stale"], [])
            self.assertEqual(s["not_downloaded"], [self.KEY],
                             "没记录又没文件 -> 仍然只是 not_downloaded")

    def test_stale_and_never_invoked_are_exclusive(self):
        """两者互斥: 账本里有记录就不可能 never_invoked。"""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "iso").mkdir()
            s = self._summary(d)
            self.assertEqual(s["never_invoked"], [])
            self.assertEqual(s["not_downloaded"], [])
            self.assertEqual(s["stale"], [self.KEY])


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
