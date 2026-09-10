#!/usr/bin/env python3
"""校验和查表的回归测试 —— 重点覆盖 .part 文件名缺陷 (v1.3.1)。

背景(真实 bug, 用户在生产环境遇到):
    清华源与科大源接连报「✗ 校验和验证失败: 所有校验和验证都失败」。
    两个高可信镜像同时返回坏文件几乎不可能 → 实为客户端 bug。

根因:
    下载期间校验的对象是 ``xxx.iso.part``(v1.2.5 引入的原子落盘中间名), 而
    sha256sums.txt 里登记的是 ``xxx.iso``。旧实现用**子串包含**判断:

        if filename in line:      # "xxx.iso.part" in "...xxx.iso" → False

    查询串比行内容更长, `in` 恒为 False → 返回 None → 与 stored_checksum
    (配置里为空串)两路皆空 → 报"所有校验和验证都失败"。
    **每个源都必然失败**, 与镜像站质量无关。

本文件锁定:
  1. .part 名能查到与最终名**完全相同**的校验和(核心回归)
  2. 按字段精确比对文件名, 不用子串包含(防前缀误命中)
  3. 支持标准格式与 BSD/PGP 格式
  4. 三种"查不到"的情形给出可区分的信息, 不再笼统报"校验和验证失败"
  5. verify_checksum 大小写/空白归一化
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import download_linux as M  # noqa: E402

# 真实 Arch 2026.09.01 清单内容(取自 mirrors.tuna.tsinghua.edu.cn, 已实测核对)
REAL_SUMS = """\
be8458032f8105e60ee2a3067f950b6e3c007ee51b38dac50e8b48e765561c91  archlinux-2026.09.01-x86_64.iso
be8458032f8105e60ee2a3067f950b6e3c007ee51b38dac50e8b48e765561c91  archlinux-x86_64.iso
895661bdf6c64e91b7725874165fd05dd30c438d3ffec661671ab5cfb261ca58  archlinux-bootstrap-2026.09.01-x86_64.tar.zst
895661bdf6c64e91b7725874165fd05dd30c438d3ffec661671ab5cfb261ca58  archlinux-bootstrap-x86_64.tar.zst
"""

ISO = "archlinux-2026.09.01-x86_64.iso"
ISO_HASH = "be8458032f8105e60ee2a3067f950b6e3c007ee51b38dac50e8b48e765561c91"


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _downloader():
    """不跑 __init__(避免建目录/联网), 只要 headers 可用。"""
    d = M.LinuxDistributionDownloader.__new__(M.LinuxDistributionDownloader)
    d.headers = {}
    return d


class TestPartSuffixChecksumLookup(unittest.TestCase):
    """核心回归: 传 .part 名必须能查到校验和。"""

    def test_part_name_resolves_to_same_checksum(self):
        """下载时传 .part 名 → 必须拿到与最终名一致的校验和。

        修复前这里返回 None, 直接导致"所有校验和验证都失败"。
        """
        d = _downloader()
        with patch.object(M.requests, "get", lambda *a, **k: _FakeResp(REAL_SUMS)):
            got = d.get_checksum_from_url("https://x/sha256sums.txt", ISO + ".part")
        self.assertEqual(got, ISO_HASH,
                         ".part 名必须剥离后缀后查到校验和(修复前的 bug 点)")

    def test_final_name_still_works(self):
        """对照: 最终名行为不变。"""
        d = _downloader()
        with patch.object(M.requests, "get", lambda *a, **k: _FakeResp(REAL_SUMS)):
            self.assertEqual(d.get_checksum_from_url("https://x/s.txt", ISO), ISO_HASH)

    def test_part_and_final_agree(self):
        """.part 名与最终名必须得到同一个值 —— 这是本次修复的本质。"""
        d = _downloader()
        with patch.object(M.requests, "get", lambda *a, **k: _FakeResp(REAL_SUMS)):
            a = d.get_checksum_from_url("https://x/s.txt", ISO)
            b = d.get_checksum_from_url("https://x/s.txt", ISO + ".part")
        self.assertIsNotNone(b, ".part 不得解析失败")
        self.assertEqual(a, b)


class TestExactFieldMatching(unittest.TestCase):
    """必须按字段精确比对, 不能子串包含 —— 否则前缀相同的文件会互相误命中。"""

    def test_prefix_name_does_not_false_match(self):
        """查 archlinux-x86_64.iso 时, 不得命中 archlinux-2026.09.01-x86_64.iso 那行。

        清单里两行哈希恰好相同(同一文件的别名), 所以旧代码"看起来正常";
        若两行哈希不同, 子串匹配就会返回错误的期望值 → 校验必然失败。
        """
        content = (
            "1111111111111111111111111111111111111111111111111111111111111111  archlinux-2026.09.01-x86_64.iso\n"
        )
        got = M.LinuxDistributionDownloader._extract_checksum_for(
            content, "archlinux-x86_64.iso")
        self.assertIsNone(got, "前缀相同的不同文件名不得被误命中")

    def test_exact_alias_matches_own_line(self):
        """同名别名行应各自正确匹配。"""
        got = M.LinuxDistributionDownloader._extract_checksum_for(
            REAL_SUMS, "archlinux-x86_64.iso")
        self.assertEqual(got, ISO_HASH)

    def test_unknown_name_returns_none(self):
        self.assertIsNone(M.LinuxDistributionDownloader._extract_checksum_for(
            REAL_SUMS, "debian-13.0.0-amd64-netinst.iso"))

    def test_star_prefixed_format_supported(self):
        """部分清单用 `checksum *filename` (二进制模式星号)。"""
        content = f"{ISO_HASH} *{ISO}\n"
        self.assertEqual(
            M.LinuxDistributionDownloader._extract_checksum_for(content, ISO), ISO_HASH)

    def test_bsd_pgp_format_supported(self):
        """BSD 风格: SHA256 (filename) = checksum。"""
        content = f"SHA256 ({ISO}) = {ISO_HASH}\n"
        self.assertEqual(
            M.LinuxDistributionDownloader._extract_checksum_for(content, ISO), ISO_HASH)

    def test_short_or_invalid_hex_rejected(self):
        """长度/字符不合法不得被当成有效摘要返回。"""
        bad = f"be8458  {ISO}\n"
        self.assertIsNone(M.LinuxDistributionDownloader._extract_checksum_for(bad, ISO))


class TestDiagnostics(unittest.TestCase):
    """查不到校验和时要能区分原因, 不再笼统报"校验和验证失败"。"""

    def test_part_lookup_failure_reports_stripped_name(self):
        """清单里真没有该文件时, 提示应说明"已按最终名查表"。"""
        import io
        import contextlib
        d = _downloader()
        buf = io.StringIO()
        with patch.object(M.requests, "get", lambda *a, **k: _FakeResp("nothing here\n")):
            with contextlib.redirect_stdout(buf):
                got = d.get_checksum_from_url("https://x/s.txt", ISO + ".part")
        self.assertIsNone(got)
        out = buf.getvalue()
        self.assertIn(ISO, out, "应提示查的是最终名")

    def test_verify_smart_distinguishes_not_found_from_mismatch(self):
        """解析不到 vs 内容不符, 返回信息必须不同。"""
        d = _downloader()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / (ISO + ".part")
            f.write_bytes(b"whatever")
            with patch.object(M.requests, "get", lambda *a, **k: _FakeResp("nothing\n")):
                ok, msg = d.verify_checksum_smart(f, "https://x/s.txt", "")
        self.assertFalse(ok)
        self.assertIn("未能在清单中找到该文件名", msg,
                      "应明确是'找不到文件名', 而不是含糊的'校验和验证失败'")
        self.assertIn("实际 SHA256=", msg, "应带上实际摘要便于排查")


class TestVerifyChecksumNormalization(unittest.TestCase):
    """摘要比对必须大小写/空白无关。"""

    def test_uppercase_expected_accepted(self):
        d = _downloader()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.bin"
            f.write_bytes(b"hello")
            actual = d.sha256_of(f)
            self.assertTrue(d.verify_checksum(f, actual.upper()),
                            "大写摘要应视为匹配")
            self.assertTrue(d.verify_checksum(f, "  " + actual + "  "),
                            "首尾空白应被忽略")
            self.assertFalse(d.verify_checksum(f, "0" * 64))


class TestSourceContractV131(unittest.TestCase):
    """静态护栏: 防止回退到旧的子串匹配实现。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO_ROOT / "iso_download" / "download_linux.py").read_text(encoding="utf-8")

    def test_part_suffix_constant_defined(self):
        self.assertIn('PART_SUFFIX = ".part"', self.src)

    def test_lookup_strips_part_suffix(self):
        self.assertIn("query_name.endswith(PART_SUFFIX)", self.src,
                      "查表前必须剥离 .part 后缀(本次 bug 的修复点)")

    def test_no_naive_substring_hit_return(self):
        """不得再出现"只要 filename in line 就返回首个 64hex"的旧逻辑。"""
        self.assertNotIn("if filename in line:", self.src)

    def test_uses_field_exact_extractor(self):
        self.assertIn("_extract_checksum_for", self.src)


if __name__ == "__main__":
    unittest.main()
