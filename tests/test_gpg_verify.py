#!/usr/bin/env python3
"""GPG 签名验证功能的单元测试。

覆盖三个状态分支(pass/fail/skip)与向后兼容性回归:
- 无 gpg 环境 / 公钥获取失败 -> 降级 skip, 不阻塞下载
- 签名校验失败 -> 拒绝(fail)
- 无签名发行版(CentOS/Deepin/Proxmox)行为完全不变
"""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

# 本地环境可能未装 tqdm(容器内才有), 注入桩模块以便导入被测模块
if "tqdm" not in sys.modules:
    try:
        import tqdm  # noqa: F401
    except ImportError:
        stub = types.ModuleType("tqdm")
        stub.tqdm = lambda *a, **k: MagicMock()
        sys.modules["tqdm"] = stub

import download_linux as dl  # noqa: E402


class TestSignatureUrlDerivation(unittest.TestCase):
    """_default_sig_url 按上游惯例推导签名 URL。"""

    def setUp(self):
        self.d = dl.LinuxDistributionDownloader.__new__(dl.LinuxDistributionDownloader)

    def test_ubuntu_appends_gpg(self):
        url = "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/26.04/SHA256SUMS"
        self.assertTrue(self.d._default_sig_url(url).endswith("SHA256SUMS.gpg"))

    def test_arch_sig_via_config(self):
        """Arch 的 .sig 由 distributions.json 显式配置(推导默认 .gpg)。"""
        data = json.loads((REPO_ROOT / "iso_download" / "distributions.json").read_text(encoding="utf-8"))
        arch = [d for d in data["distributions"] if d["distribution"] == "Arch"]
        self.assertTrue(arch, "应有 Arch 条目")
        self.assertTrue(arch[0].get("signature_url", "").endswith(".sig"))

    def test_already_signature_not_double_appended(self):
        """已是 .gpg 结尾的 URL 不应重复追加。"""
        url = "https://example.com/SHA256SUMS.gpg"
        self.assertEqual(self.d._default_sig_url(url), url)


class TestGpgSkipDegradation(unittest.TestCase):
    """无 gpg 环境或公钥获取失败时, 必须降级 skip(不阻塞下载)。"""

    def setUp(self):
        self.d = dl.LinuxDistributionDownloader.__new__(dl.LinuxDistributionDownloader)
        self.tmp = Path(__import__("tempfile").mkdtemp())

    @patch.object(dl, "subprocess", None)
    def test_no_gpg_binary_returns_skip(self):
        """gpg/gpgv 都不存在时降级。"""
        with patch("shutil.which", return_value=None):
            status = self.d.verify_signature("abc", "http://sig", "http://key", self.tmp)
            self.assertEqual(status, "skip")

    def test_missing_gpg_key_url_returns_skip(self):
        """未提供公钥获取地址(官方无签名)时跳过。"""
        status = self.d.verify_signature("abc", "http://sig", "", self.tmp)
        self.assertEqual(status, "skip")

    @patch.object(dl.requests, "get")
    def test_key_fetch_failure_returns_skip(self, mock_get):
        """公钥/签名获取异常时降级, 不抛异常。"""
        mock_get.side_effect = Exception("网络不可达")
        status = self.d.verify_signature("abc", "http://sig", "http://key", self.tmp)
        self.assertEqual(status, "skip")


class TestVerifyChecksumSmartBackwardCompat(unittest.TestCase):
    """向后兼容: 未传 dist 或 dist 无 gpg_verify 时行为不变。"""

    def setUp(self):
        self.d = dl.LinuxDistributionDownloader.__new__(dl.LinuxDistributionDownloader)
        self.d.download_dir = Path(__import__("tempfile").mkdtemp())

    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    @patch.object(dl.LinuxDistributionDownloader, "verify_checksum")
    def test_no_dist_still_works(self, mock_vc, mock_gc):
        """不传 dist 时(旧调用方式)校验链不变。"""
        mock_gc.return_value = "a" * 64
        mock_vc.return_value = True
        ok, msg = self.d.verify_checksum_smart(Path("/tmp/x.iso"), "http://c", "")
        self.assertTrue(ok)
        self.assertIn("URL校验和验证通过", msg)

    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    @patch.object(dl.LinuxDistributionDownloader, "verify_checksum")
    def test_dist_without_gpg_verify_unchanged(self, mock_vc, mock_gc):
        """dist 存在但无 gpg_verify 字段时, 不触发 GPG, 行为不变。"""
        mock_gc.return_value = "b" * 64
        mock_vc.return_value = True
        ok, msg = self.d.verify_checksum_smart(Path("/tmp/x.iso"), "http://c", "",
                                               dist={"distribution": "CentOS"})
        self.assertTrue(ok)
        self.assertIn("URL校验和验证通过", msg)

    @patch.object(dl.LinuxDistributionDownloader, "verify_signature")
    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    @patch.object(dl.LinuxDistributionDownloader, "verify_checksum")
    def test_gpg_fail_rejects_download(self, mock_vc, mock_gc, mock_sig):
        """GPG 签名校验失败(篡改)时拒绝下载, 不再比对 SHA256。"""
        mock_sig.return_value = "fail"
        mock_gc.return_value = "c" * 64
        ok, msg = self.d.verify_checksum_smart(
            Path("/tmp/x.iso"), "http://c", "",
            dist={"distribution": "Ubuntu", "gpg_verify": "checksum",
                  "gpg_key_url": "http://key", "signature_url": "http://sig"})
        self.assertFalse(ok)
        self.assertIn("GPG", msg)
        mock_vc.assert_not_called()  # 签名失败不应再信任其 SHA256

    @patch.object(dl.LinuxDistributionDownloader, "verify_signature")
    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    @patch.object(dl.LinuxDistributionDownloader, "verify_checksum")
    def test_gpg_skip_falls_back_to_sha256(self, mock_vc, mock_gc, mock_sig):
        """GPG 跳过(无公钥)时降级到 SHA256, 不阻塞。"""
        mock_sig.return_value = "skip"
        mock_gc.return_value = "d" * 64
        mock_vc.return_value = True
        ok, msg = self.d.verify_checksum_smart(
            Path("/tmp/x.iso"), "http://c", "",
            dist={"distribution": "Ubuntu", "gpg_verify": "checksum",
                  "gpg_key_url": "http://key", "signature_url": "http://sig"})
        self.assertTrue(ok)
        self.assertIn("URL校验和验证通过", msg)

    @patch.object(dl.LinuxDistributionDownloader, "verify_signature")
    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    @patch.object(dl.LinuxDistributionDownloader, "verify_checksum")
    def test_gpg_pass_proceeds(self, mock_vc, mock_gc, mock_sig):
        """GPG 通过时正常继续 SHA256 比对。"""
        mock_sig.return_value = "pass"
        mock_gc.return_value = "e" * 64
        mock_vc.return_value = True
        ok, msg = self.d.verify_checksum_smart(
            Path("/tmp/x.iso"), "http://c", "",
            dist={"distribution": "Ubuntu", "gpg_verify": "checksum",
                  "gpg_key_url": "http://key", "signature_url": "http://sig"})
        self.assertTrue(ok)


class TestDistributionsJsonGpgFields(unittest.TestCase):
    """数据模型: 有签名的发行版带 gpg 字段, 无签名的不带。"""

    def setUp(self):
        self.data = json.loads(
            (REPO_ROOT / "iso_download" / "distributions.json").read_text(encoding="utf-8"))

    def test_signed_distros_have_gpg_fields(self):
        for name in ("Ubuntu", "Arch", "Fedora"):
            entries = [d for d in self.data["distributions"] if d["distribution"] == name]
            self.assertTrue(entries, f"应有 {name} 条目")
            self.assertTrue(all(d.get("gpg_verify") for d in entries),
                            f"{name} 应配置 gpg_verify")
            self.assertTrue(all(d.get("gpg_key_url") for d in entries),
                            f"{name} 应配置 gpg_key_url")

    def test_unsigned_distros_have_no_gpg_verify(self):
        """无官方签名的发行版不应强制 GPG(否则会误拒下载)。"""
        for name in ("CentOS", "Deepin", "Proxmox"):
            entries = [d for d in self.data["distributions"] if d["distribution"] == name]
            for d in entries:
                self.assertNotIn("gpg_verify", d, f"{name} 无官方签名, 不应配置 gpg_verify")

    def test_json_still_valid_schema(self):
        self.assertIn("distributions", self.data)
        for d in self.data["distributions"]:
            for key in ("distribution", "type", "download_url"):
                self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main(verbosity=2)