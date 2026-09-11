#!/usr/bin/env python3
"""GPG「接线」契约测试 —— 第二层守卫: verify_checksum_smart 内部必须真的把 dist 用起来。

背景(真实 bug, v1.3.7):
    tests/test_gpg_dist_handoff.py 已锁死「调用方必须传 dist=...」这一层;
    本文件锁死更内层的一根线: **verify_checksum_smart 收到配置了 gpg_verify 的 dist 后,
    必须真的调用 verify_signature, 且三种结果(pass/fail/skip)行为可区分**。

    若有人重构时把 GPG 预检分支整体摘掉(或改成恒 False), 本文件的
    test_gpg_enabled_calls_verify_signature 立刻变红 —— 「漏接线」品类在 CI 被拦截。

配合变异测试(scripts/mutations.json):
    * 摘掉 GPG 预检 if 条件     -> test_gpg_enabled_calls_verify_signature 变红
    * 把 fail 的阻断返回改成放行 -> test_gpg_fail_blocks 变红
    * 删掉 pass/skip 日志行      -> test_degradation_visible.py 变红
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import download_linux  # noqa: E402
from download_linux import LinuxDistributionDownloader  # noqa: E402

CHECKSUM_TEXT = "abc123  ubuntu-24.04-desktop-amd64.iso\n"
SHA256 = "a" * 64


def _make_downloader(tmp: str) -> LinuxDistributionDownloader:
    """构造真实下载器实例(不发起网络请求; json 指向临时空清单)。"""
    jp = Path(tmp) / "distributions.json"
    jp.write_text('{"distributions": []}', encoding="utf-8")
    return LinuxDistributionDownloader(json_file=str(jp), download_dir=str(tmp))


def _mk_iso(tmp: str, name: str = "ubuntu.iso") -> Path:
    fp = Path(tmp) / name
    fp.write_bytes(b"iso-bytes")
    return fp


class TestVerifySmartInvokesGpgSignature(unittest.TestCase):
    """接线守卫: 配置了 gpg_verify 的 dist 必须触发 verify_signature 调用。"""

    def test_gpg_enabled_calls_verify_signature(self):
        """dist 配置 gpg_verify=True 时, verify_signature 必须被调用(防 v1.3.7 复发)。"""
        with tempfile.TemporaryDirectory() as d:
            dl = _make_downloader(d)
            fp = _mk_iso(d)
            with patch.object(LinuxDistributionDownloader, "verify_signature",
                              return_value="pass") as spy, \
                 patch.object(download_linux.requests, "get",
                              return_value=MagicMock(text=CHECKSUM_TEXT)), \
                 patch.object(LinuxDistributionDownloader, "get_checksum_from_url",
                              return_value=SHA256), \
                 patch.object(LinuxDistributionDownloader, "verify_checksum",
                              return_value=True):
                ok, _msg = dl.verify_checksum_smart(
                    fp, "https://mirror.test/SHA256SUMS", None,
                    dist={"gpg_verify": "checksum", "gpg_key_fingerprint": "ABCD"},
                )
        self.assertTrue(ok)
        self.assertTrue(
            spy.called,
            "dist 配置了 gpg_verify 但 verify_signature 未被调用 —— GPG 预检接线断了(v1.3.7 回归)",
        )

    def test_gpg_fail_blocks(self):
        """签名校验 fail 必须阻断(返回 False), 不能降级放行。"""
        with tempfile.TemporaryDirectory() as d:
            dl = _make_downloader(d)
            fp = _mk_iso(d)
            with patch.object(LinuxDistributionDownloader, "verify_signature",
                              return_value="fail"), \
                 patch.object(download_linux.requests, "get",
                              return_value=MagicMock(text=CHECKSUM_TEXT)):
                ok, msg = dl.verify_checksum_smart(
                    fp, "https://mirror.test/SHA256SUMS", None,
                    dist={"gpg_verify": "checksum"},
                )
        self.assertFalse(ok, "签名校验失败却放行 —— 篡改场景失守")
        self.assertIn("GPG", msg, "阻断消息必须提及 GPG, 让用户能区分失败原因")

    def test_gpg_pass_verifies_sha256_afterwards(self):
        """签名 pass 后仍须做 SHA256 比对 —— 两道保险缺一不可。"""
        with tempfile.TemporaryDirectory() as d:
            dl = _make_downloader(d)
            fp = _mk_iso(d)
            with patch.object(LinuxDistributionDownloader, "verify_signature",
                              return_value="pass"), \
                 patch.object(download_linux.requests, "get",
                              return_value=MagicMock(text=CHECKSUM_TEXT)), \
                 patch.object(LinuxDistributionDownloader, "get_checksum_from_url",
                              return_value=SHA256) as spy_sum, \
                 patch.object(LinuxDistributionDownloader, "verify_checksum",
                              return_value=True):
                ok, _msg = dl.verify_checksum_smart(
                    fp, "https://mirror.test/SHA256SUMS", None,
                    dist={"gpg_verify": "checksum"},
                )
        self.assertTrue(ok)
        self.assertTrue(spy_sum.called, "GPG 通过后跳过了 SHA256 比对 —— 双保险被拆掉一道")

    def test_gpg_skip_degrades_to_sha256(self):
        """签名 skip(官方无公钥)应降级到 SHA256 继续校验, 不阻塞可用性。"""
        with tempfile.TemporaryDirectory() as d:
            dl = _make_downloader(d)
            fp = _mk_iso(d)
            with patch.object(LinuxDistributionDownloader, "verify_signature",
                              return_value="skip"), \
                 patch.object(download_linux.requests, "get",
                              return_value=MagicMock(text=CHECKSUM_TEXT)), \
                 patch.object(LinuxDistributionDownloader, "get_checksum_from_url",
                              return_value=SHA256), \
                 patch.object(LinuxDistributionDownloader, "verify_checksum",
                              return_value=True):
                ok, _msg = dl.verify_checksum_smart(
                    fp, "https://mirror.test/SHA256SUMS", None,
                    dist={"gpg_verify": "checksum"},
                )
        self.assertTrue(ok, "skip 属于降级, 应放行继续 SHA256, 不应阻塞")

    def test_dist_none_never_calls_gpg(self):
        """dist=None(旧调用方式)不应触发 GPG 分支, 也不应崩溃。"""
        with tempfile.TemporaryDirectory() as d:
            dl = _make_downloader(d)
            fp = _mk_iso(d)
            with patch.object(LinuxDistributionDownloader, "verify_signature") as spy, \
                 patch.object(download_linux.requests, "get",
                              return_value=MagicMock(text=CHECKSUM_TEXT)), \
                 patch.object(LinuxDistributionDownloader, "get_checksum_from_url",
                              return_value=SHA256), \
                 patch.object(LinuxDistributionDownloader, "verify_checksum",
                              return_value=True):
                ok, _msg = dl.verify_checksum_smart(
                    fp, "https://mirror.test/SHA256SUMS", None, dist=None,
                )
        self.assertTrue(ok)
        self.assertFalse(spy.called, "dist=None 不应调用 verify_signature")

    def test_gpg_disabled_no_call(self):
        """dist 未配置 gpg_verify(无该字段/值为空)不应触发 GPG 分支。"""
        for dist in ({}, {"gpg_verify": None}, {"gpg_verify": ""}, {"gpg_verify": False}):
            with self.subTest(dist=dist):
                with tempfile.TemporaryDirectory() as d:
                    dl = _make_downloader(d)
                    fp = _mk_iso(d)
                    with patch.object(LinuxDistributionDownloader, "verify_signature") as spy, \
                         patch.object(download_linux.requests, "get",
                                      return_value=MagicMock(text=CHECKSUM_TEXT)), \
                         patch.object(LinuxDistributionDownloader, "get_checksum_from_url",
                                      return_value=SHA256), \
                         patch.object(LinuxDistributionDownloader, "verify_checksum",
                                      return_value=True):
                        ok, _msg = dl.verify_checksum_smart(
                            fp, "https://mirror.test/SHA256SUMS", None, dist=dist,
                        )
                self.assertTrue(ok)
                self.assertFalse(spy.called)


if __name__ == "__main__":
    unittest.main()
