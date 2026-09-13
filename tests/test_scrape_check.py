#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/scrape_check.py 的护栏测试。

为什么必须测: 这个脚本是「镜像源解析层失效」的季度守门人。它一旦静默失效
(比如把 BROKEN 误判成 SUSPECT、基线损坏不报错), 页面改版就会再次变成长期
静默降级 —— 与 endpoint_smoke 查不出的那类失效完全重叠, 巡检等于白做。

覆盖点:
  * 退出码分级 (0/1/2/3)
  * 版本正则匹配 0 个 = BROKEN(页面改版的核心信号)
  * build_entries 异常 = BROKEN
  * 列表页拉取异常 = SUSPECT(不判死, 避免网络抖动误报)
  * static 策略跳过, 不计入故障
  * 首跑无基线 = SUSPECT(warning, 提示 --update-baseline)
  * --update-baseline 落基线后, 同样的结果变为绿
  * 基线 JSON 损坏 / 配置为空 = 配置异常(退出码 3)
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "scrape_check.py"

_spec = importlib.util.spec_from_file_location("scrape_check", str(SCRIPT))
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

# 复用被测脚本已 import 的 update_distributions, 测试里 patch 它的 fetch_text
ud = sc.ud

_CFG = {
    "sources": [
        {"distribution": "Arch", "strategy": "dated_directory",
         "listing_url": "https://example.test/iso/",
         "version_regex": "href=\"(?P<value>\\d{4}\\.\\d{2}\\.\\d{2})/\"",
         "download_template": "{listing_url}{version}/a.iso",
         "checksum_template": "{listing_url}{version}/sha256sums.txt"},
        {"distribution": "CentOS", "strategy": "static", "versions": ["7.9.2009"]},
        {"distribution": "Fedora", "strategy": "versioned_flat_listing",
         "listing_url": "https://example.test/releases/",
         "version_regex": "href=\"(?P<value>\\d+)/\"",
         "sub_listing_template": "{listing_url}{version}/iso/",
         "artifact_regex": "href=\"(?P<value>Fedora-netinst-x86_64.iso)\""},
    ],
}

_LISTING_PAGES = {
    "https://example.test/iso/": "href=\"2026.09.01/\" href=\"2026.08.01/\"",
    "https://example.test/releases/": "href=\"43/\" href=\"42/\"",
}


def _fake_fetch(url, timeout=None):
    page = _LISTING_PAGES.get(url)
    if page is None:
        raise TimeoutError("connect timeout")
    return page


def _fake_build(entries_by_dist):
    def _build(source):
        got = entries_by_dist.get(source["distribution"], [])
        if got is None:
            raise ud.SourceBuilderError("boom")
        return [dict(distribution=source["distribution"], download_url=u)
                for u in got]
    return _build


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self.cfg = self.tmp / "sources_config.json"
        self.cfg.write_text(json.dumps(_CFG), encoding="utf-8")
        self.baseline = self.tmp / "baseline.json"
        self.out = self.tmp / "artifacts" / "out.json"

    def _run(self, extra=()):
        argv = ["--config", str(self.cfg), "--baseline", str(self.baseline),
                "--out", str(self.out), *extra]
        return sc.main(argv)


class TestExitCodes(_TmpCase):
    def setUp(self):
        super().setUp()
        self._orig_fetch = ud.fetch_text
        self._orig_build = ud.build_entries
        self.addCleanup(setattr, ud, "fetch_text", self._orig_fetch)
        self.addCleanup(setattr, ud, "build_entries", self._orig_build)
        ud.fetch_text = _fake_fetch
        ud.build_entries = _fake_build({
            "Arch": ["https://example.test/iso/2026.09.01/a.iso"],
            "Fedora": ["https://example.test/releases/43/x.iso"],
        })

    def test_all_ok_returns_zero_with_baseline(self):
        baseline = {"schema": 1, "sources": {
            "Arch": {"versions_total": 2, "latest": "2026.09.01", "entries": 1},
            "Fedora": {"versions_total": 1, "latest": "43", "entries": 1},
        }}
        self.baseline.write_text(json.dumps(baseline), encoding="utf-8")
        self.assertEqual(0, self._run())

    def test_regex_zero_matches_is_broken(self):
        ud.fetch_text = lambda url, timeout=None: "<html>404 nothing here</html>"
        self.assertEqual(2, self._run())

    def test_build_entries_exception_is_broken(self):
        ud.build_entries = _fake_build({"Arch": None, "Fedora": ["x"]})
        self.assertEqual(2, self._run())

    def test_fetch_failure_is_suspect(self):
        def _boom(url, timeout=None):
            raise TimeoutError("connect timeout")
        ud.fetch_text = _boom
        self.assertEqual(1, self._run())

    def test_no_baseline_is_warning(self):
        self.assertEqual(1, self._run())

    def test_corrupted_baseline_is_config_error(self):
        self.baseline.write_text("{not json", encoding="utf-8")
        self.assertEqual(3, self._run())

    def test_empty_config_is_config_error(self):
        self.cfg.write_text(json.dumps({"sources": []}), encoding="utf-8")
        self.assertEqual(3, self._run())


class TestUpdateBaseline(_TmpCase):
    def setUp(self):
        super().setUp()
        self._orig_fetch = ud.fetch_text
        self._orig_build = ud.build_entries
        self.addCleanup(setattr, ud, "fetch_text", self._orig_fetch)
        self.addCleanup(setattr, ud, "build_entries", self._orig_build)
        ud.fetch_text = _fake_fetch
        ud.build_entries = _fake_build({
            "Arch": ["https://example.test/iso/2026.09.01/a.iso"],
            "Fedora": ["https://example.test/releases/43/x.iso"],
        })

    def test_update_baseline_then_green(self):
        # 首跑无基线 -> warning; 落基线 -> 同样结果变绿
        self.assertEqual(1, self._run())
        self.assertEqual(0, self._run(["--update-baseline"]))
        saved = json.loads(self.baseline.read_text(encoding="utf-8"))
        # key 是 listing_url(唯一), static(CentOS) 不进基线
        self.assertIn("https://example.test/iso/", saved["sources"])
        self.assertEqual(0, self._run())

    def test_static_skipped_not_broken(self):
        rc = self._run(["--update-baseline"])
        self.assertEqual(0, rc)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        centos = [r for r in data["results"] if r["distribution"] == "CentOS"][0]
        self.assertEqual("skipped", centos["status"])


class TestDiffInfo(_TmpCase):
    def setUp(self):
        super().setUp()
        self._orig_fetch = ud.fetch_text
        self._orig_build = ud.build_entries
        self.addCleanup(setattr, ud, "fetch_text", self._orig_fetch)
        self.addCleanup(setattr, ud, "build_entries", self._orig_build)
        ud.fetch_text = _fake_fetch
        ud.build_entries = _fake_build({
            "Arch": ["https://example.test/iso/2026.09.01/a.iso"],
            "Fedora": ["https://example.test/releases/43/x.iso"],
        })

    def test_version_advance_is_info_not_failure(self):
        baseline = {"schema": 1, "sources": {
            "https://example.test/iso/": {
                "distribution": "Arch", "versions_total": 1,
                "latest": "2026.01.01", "entries": 2},
            "https://example.test/releases/": {
                "distribution": "Fedora", "versions_total": 2,
                "latest": "43", "entries": 1},
        }}
        self.baseline.write_text(json.dumps(baseline), encoding="utf-8")
        self.assertEqual(0, self._run())
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertTrue(any("Arch" in d for d in data["diffs"]))


if __name__ == "__main__":
    unittest.main()
