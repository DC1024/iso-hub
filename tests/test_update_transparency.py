#!/usr/bin/env python3
"""update_distributions.py 的 entry_defaults 通用透传测试。

回归目标: 各 build_* 函数生成的 entry 只含 distribution/type/download_url/
checksum_url/checksum 等基础字段, GPG 验证所需的 gpg_verify/gpg_key_url/
gpg_key_fingerprint 曾手工写死在 distributions.json 里, 刷新清单时被整体冲掉
(指纹锚定失效)。本测试确保 source 里的 entry_defaults 能透传到每条 entry。
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import update_distributions as ud  # noqa: E402


class TestApplyEntryDefaults(unittest.TestCase):
    """_apply_entry_defaults 的通用透传语义。"""

    def test_no_defaults_returns_unchanged(self):
        """无 entry_defaults 的旧 source 完全向后兼容。"""
        entries = [{"distribution": "X", "x": 1}]
        out = ud._apply_entry_defaults({"distribution": "X"}, entries)
        self.assertEqual(out, entries)

    def test_empty_non_dict_defaults_ignored(self):
        """entry_defaults 缺省/非 dict 时不改变任何条目。"""
        for val in (None, [], "gpg_verify"):
            entries = [{"distribution": "X"}]
            self.assertEqual(ud._apply_entry_defaults({"entry_defaults": val}, entries),
                             entries)

    def test_defaults_merged_into_every_entry(self):
        """defaults 里的键被逐个 merge 到每条 entry, 通用透传(不硬编码字段名)。"""
        defaults = {"gpg_verify": "checksum", "custom_field": "bar"}
        entries = [
            {"distribution": "X", "download_url": "u1"},
            {"distribution": "X", "download_url": "u2"},
        ]
        out = ud._apply_entry_defaults({"entry_defaults": defaults}, entries)
        for e in out:
            self.assertEqual(e["gpg_verify"], "checksum")
            self.assertEqual(e["custom_field"], "bar")

    def test_existing_key_not_overwritten(self):
        """只补 entry 缺失的键, 绝不覆盖 builder 已生成的值(如 distribution)。"""
        source = {"distribution": "Real", "entry_defaults": {"distribution": "Hijack",
                                                             "gpg_verify": "checksum"}}
        entries = [{"distribution": "Real"}]
        out = ud._apply_entry_defaults(source, entries)
        self.assertEqual(out[0]["distribution"], "Real")
        self.assertEqual(out[0]["gpg_verify"], "checksum")

    def test_fingerprint_array_preserved(self):
        """数组型指纹(gpg_key_fingerprint 多指纹)原样透传, 不被拍平/拆分。"""
        fp_list = ["AAAA" * 10, "BBBB" * 10]
        entries = [{"distribution": "Fedora"}]
        out = ud._apply_entry_defaults(
            {"entry_defaults": {"gpg_key_fingerprint": fp_list}}, entries)
        self.assertEqual(out[0]["gpg_key_fingerprint"], fp_list)


class TestBuildEntriesTransparency(unittest.TestCase):
    """走完整 build_entries 流程验证 entry_defaults 透传(static 策略无网络)。"""

    SOURCE = {
        "distribution": "TestDistro",
        "type": "linux",
        "strategy": "static",
        "versions": ["9.0", "8.0"],
        "download_template": "https://example.com/{version}.iso",
        "checksum_template": "https://example.com/{version}.sha256",
        "entry_defaults": {
            "gpg_verify": "checksum",
            "gpg_key_url": "https://example.org/keyring.gpg",
            "gpg_key_fingerprint": "843938DF228D22F7B3742BC0D94AA3F0EFE21092",
        },
    }

    def test_gpg_fields_transparent_through_build_entries(self):
        """build_entries 生成的每条 entry 必须带上 entry_defaults 里的 gpg 字段。"""
        entries = ud.build_entries(self.SOURCE)
        self.assertEqual(len(entries), 2)
        for e in entries:
            self.assertEqual(e["gpg_verify"], "checksum")
            self.assertEqual(e["gpg_key_url"], "https://example.org/keyring.gpg")
            self.assertEqual(e["gpg_key_fingerprint"],
                             "843938DF228D22F7B3742BC0D94AA3F0EFE21092")
            # 基础字段仍齐全
            self.assertEqual(e["distribution"], "TestDistro")
            self.assertTrue(e["download_url"].startswith("https://example.com/"))

    def test_no_entry_defaults_keeps_base_fields_only(self):
        """不配 entry_defaults 时, 产出仍只有基础字段(无 gpg 键)。"""
        source = dict(self.SOURCE)
        source.pop("entry_defaults")
        for e in ud.build_entries(source):
            self.assertNotIn("gpg_verify", e)
            self.assertNotIn("gpg_key_url", e)
            self.assertNotIn("gpg_key_fingerprint", e)


if __name__ == "__main__":
    unittest.main(verbosity=2)