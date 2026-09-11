#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/cve_check.py 的护栏测试。

为什么必须测: 这个脚本是「CVE 基线增量告警」的唯一守门人。它一旦静默失效
(比如格式解析不到、等级归一化错了), 101 条噪音之上再叠新漏洞就再也看不出来了
—— 与 gpg_ledger「降级伪装成成功」是同一类静默失效。

覆盖点:
  * 退出码分级 (0/1/2/3)
  * Trivy 原生格式与简化格式都能解析
  * 条目消失不算新增(不误报)
  * 非 CVE 条目(GHSA-*)不参与比对
  * 同一 CVE 多包命中时取最高等级
  * 空扫描结果 = 配置异常(不是"无漏洞")
  * --update-baseline 的合并语义
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cve_check.py"

_spec = importlib.util.spec_from_file_location("cve_check", str(SCRIPT))
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def _baseline(extra=None):
    doc = {
        "schema": 1,
        "generated_at": "2026-09-12",
        "app_version": "1.3.12",
        "entries": {
            "CVE-2019-1010022": {"pkg": "glibc", "score": 9.8, "severity": "critical",
                                 "reach": "partial", "debian_status": "open",
                                 "urgency": "unimportant", "fixed": ""},
            "CVE-2026-89092": {"pkg": "glibc", "score": 4.2, "severity": "medium",
                               "reach": "unreachable", "debian_status": "open",
                               "urgency": "not yet assigned", "fixed": ""},
        },
    }
    if extra:
        doc["entries"].update(extra)
    return doc


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _write(self, name, obj):
        p = self.tmp / name
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def _run(self, scan, baseline=None, extra_args=()):
        base = self._write("baseline.json", baseline or _baseline())
        inp = self._write("scan.json", scan)
        return cc.main(["--input", inp, "--baseline", base, *extra_args])


class TestExitCodes(_TmpCase):
    def test_no_new_cve_returns_zero(self):
        self.assertEqual(
            0, self._run({"cves": ["CVE-2019-1010022", "CVE-2026-89092"]}))

    def test_new_high_returns_two(self):
        self.assertEqual(
            2, self._run({"cves": {"CVE-2019-1010022": "LOW", "CVE-2099-00001": "CRITICAL"}}))

    def test_new_medium_only_returns_one(self):
        self.assertEqual(
            1, self._run({"cves": {"CVE-2019-1010022": "LOW", "CVE-2099-00002": "MEDIUM"}}))

    def test_new_unknown_severity_returns_one_not_zero(self):
        """未定级不能静默放过 —— 宁可提醒。"""
        self.assertEqual(
            1, self._run({"cves": {"CVE-2019-1010022": "LOW", "CVE-2099-00003": ""}}))

    def test_removed_entries_do_not_fail(self):
        """条目消失是好事(上游修了), 不应触发告警。"""
        self.assertEqual(0, self._run({"cves": {"CVE-2019-1010022": "LOW"}}))

    def test_empty_scan_is_config_error_not_clean(self):
        self.assertEqual(3, self._run({"cves": []}))

    def test_missing_baseline_is_config_error(self):
        inp = self._write("scan.json", {"cves": {"CVE-2019-1010022": "LOW"}})
        self.assertEqual(3, cc.main(["--input", inp,
                                     "--baseline", str(self.tmp / "nope.json")]))


class TestFormats(_TmpCase):
    def test_trivy_native_format(self):
        trivy = {"Results": [{"Target": "iso-hub", "Vulnerabilities": [
            {"VulnerabilityID": "CVE-2019-1010022", "Severity": "CRITICAL"},
            {"VulnerabilityID": "CVE-2099-00009", "Severity": "HIGH"}]}]}
        self.assertEqual(2, self._run(trivy))

    def test_plain_list_of_strings(self):
        self.assertEqual(0, self._run(["CVE-2019-1010022", "CVE-2026-89092"]))

    def test_vulnerabilities_key(self):
        self.assertEqual(0, self._run({"vulnerabilities": [
            {"id": "CVE-2019-1010022", "severity": "critical"},
            {"id": "CVE-2026-89092", "severity": "medium"}], }))

    def test_non_cve_ids_excluded_from_diff(self):
        """GHSA-* 之类不参与比对, 但不能因此报错。"""
        self.assertEqual(0, self._run({"Results": [{"Vulnerabilities": [
            {"VulnerabilityID": "CVE-2019-1010022", "Severity": "LOW"},
            {"VulnerabilityID": "GHSA-xxxx-yyyy-zzzz", "Severity": "CRITICAL"}]}]}))

    def test_same_cve_multiple_pkgs_takes_highest(self):
        """同一 CVE 命中多个包时, 必须按最高等级判定, 不能被低等级覆盖掉。"""
        self.assertEqual(2, self._run({"Results": [
            {"Vulnerabilities": [{"VulnerabilityID": "CVE-2099-00005",
                                  "Severity": "LOW"}]},
            {"Vulnerabilities": [{"VulnerabilityID": "CVE-2099-00005",
                                  "Severity": "CRITICAL"}]}]}))


class TestUpdateBaseline(_TmpCase):
    def test_update_merges_new_and_marks_gone(self):
        base = self._write("baseline.json", _baseline())
        inp = self._write("scan.json", {"cves": {"CVE-2019-1010022": "LOW",
                                                 "CVE-2099-00007": "HIGH"}})
        self.assertEqual(0, cc.main(["--input", inp, "--baseline", base,
                                     "--update-baseline"]))
        doc = json.loads(Path(base).read_text(encoding="utf-8"))
        self.assertIn("CVE-2099-00007", doc["entries"])
        self.assertEqual("gone", doc["entries"]["CVE-2026-89092"]["reach"])
        # 既有判定不能被覆盖
        self.assertEqual("partial", doc["entries"]["CVE-2019-1010022"]["reach"])

    def test_new_entry_defaults_to_unknown_reach(self):
        base = self._write("baseline.json", _baseline())
        inp = self._write("scan.json", {"cves": {"CVE-2099-00008": "MEDIUM"}})
        cc.main(["--input", inp, "--baseline", base, "--update-baseline"])
        doc = json.loads(Path(base).read_text(encoding="utf-8"))
        self.assertEqual("unknown", doc["entries"]["CVE-2099-00008"]["reach"])


class TestRealBaseline(unittest.TestCase):
    """对仓库里那份真实基线做冒烟: 它必须存在且可被解析。"""

    def test_repo_baseline_loads(self):
        p = REPO_ROOT / "scripts" / "cve_baseline.json"
        self.assertTrue(p.exists(), "基线文件缺失: %s" % p)
        doc = cc.load_baseline(p)
        self.assertIsInstance(doc["entries"], dict)
        self.assertGreater(len(doc["entries"]), 0)

    def test_repo_baseline_has_no_fixed_version(self):
        """所有条目都无 Debian 修复版本 —— 这是「不能靠 apt 修、必须走基线」的依据。"""
        doc = cc.load_baseline(REPO_ROOT / "scripts" / "cve_baseline.json")
        with_fix = [c for c, e in doc["entries"].items() if e.get("fixed")]
        self.assertEqual([], with_fix,
                         "出现了有修复版本的条目, 应优先升级而不是进基线: %s" % with_fix)


if __name__ == "__main__":
    unittest.main()
