#!/usr/bin/env python3
"""GPG 签名验证功能的单元测试。

覆盖三个状态分支(pass/fail/skip)与向后兼容性回归:
- 无 gpg 环境 / 公钥获取失败 -> 降级 skip, 不阻塞下载
- 签名校验失败 -> 拒绝(fail)
- 无签名发行版(CentOS/Deepin/Proxmox)行为完全不变

以及公钥指纹锚定(消除 TOFU 风险):
- 指纹匹配 -> pass 且只缓存命中指纹的密钥
- 指纹不匹配 -> fail, 且不写入缓存
- 已存在但被污染的旧 keyring -> 被检测并重建(迁移路径)
- 未配置指纹 -> 不阻塞, 打印 TOFU 告警, 行为与改动前一致
"""

import contextlib
import io
import json
import subprocess as _subprocess
import sys
import tempfile
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

    def test_arch_has_no_gpg_verify(self):
        """Arch 官方只签 ISO 本体(.iso.sig), 从不签 checksum 文件, 现行代码
        不支持验 ISO 签名, 故 Arch 明确不配 gpg_verify, 让其走 SHA256 不误伤。"""
        data = json.loads((REPO_ROOT / "iso_download" / "distributions.json").read_text(encoding="utf-8"))
        arch = [d for d in data["distributions"] if d["distribution"] == "Arch"]
        self.assertTrue(arch, "应有 Arch 条目")
        for d in arch:
            self.assertNotIn("gpg_verify", d, "Arch 不签 checksum 文件, 不应配 gpg_verify")

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
        for name in ("Ubuntu", "Fedora"):
            entries = [d for d in self.data["distributions"] if d["distribution"] == name]
            self.assertTrue(entries, f"应有 {name} 条目")
            self.assertTrue(all(d.get("gpg_verify") for d in entries),
                            f"{name} 应配置 gpg_verify")
            self.assertTrue(all(d.get("gpg_key_url") for d in entries),
                            f"{name} 应配置 gpg_key_url")

    def test_ubuntu_fingerprint_is_pinned_single(self):
        """Ubuntu 单指纹锚定(CD Image Automatic Signing Key)。"""
        for d in [x for x in self.data["distributions"] if x["distribution"] == "Ubuntu"]:
            self.assertEqual(d.get("gpg_key_fingerprint"),
                             "843938DF228D22F7B3742BC0D94AA3F0EFE21092")
            self.assertEqual(d.get("gpg_key_url"),
                             "https://archive.ubuntu.com/ubuntu/project/ubuntu-archive-keyring.gpg")

    def test_fedora_fingerprints_are_multi_pinned(self):
        """Fedora 一份公钥含多把轮换密钥, 指纹按版本不同, 必须配成多指纹数组。"""
        EXPECTED = [
            "C6E7F081CF80E13146676E88829B606631645531",  # F43
            "36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6",  # F44
            "4F50A6114CD5C6976A7F1179655A4B02F577861E",  # F45
            "D924B10D3E810DABDD8B56B596E7E91491211FCE",  # F46
        ]
        for d in [x for x in self.data["distributions"] if x["distribution"] == "Fedora"]:
            self.assertEqual(d.get("gpg_key_url"), "https://fedoraproject.org/fedora.gpg")
            self.assertIsInstance(d.get("gpg_key_fingerprint"), list,
                                  "Fedora 指纹必须为多指纹数组")
            self.assertEqual(d.get("gpg_key_fingerprint"), EXPECTED)

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

    def test_every_signed_entry_has_fingerprint(self):
        """配置了 gpg_verify 就必须配置指纹锚定, 否则仍是 TOFU。"""
        for d in self.data["distributions"]:
            if d.get("gpg_verify"):
                self.assertTrue(
                    d.get("gpg_key_fingerprint"),
                    f"{d['distribution']} {d.get('download_url')} 配置了 gpg_verify "
                    "却缺少 gpg_key_fingerprint")

    def test_fingerprints_are_40_hex(self):
        """distributions.json 里每条指纹都必须是 40 位十六进制。"""
        helper = dl.LinuxDistributionDownloader.__new__(dl.LinuxDistributionDownloader)
        for d in self.data["distributions"]:
            raw = d.get("gpg_key_fingerprint")
            if not raw:
                continue
            fps = helper._normalize_fingerprints(raw)
            self.assertTrue(fps, f"{d['distribution']} 指纹非法: {raw!r}")
            for fp in fps:
                self.assertEqual(len(fp), 40, f"{d['distribution']} 指纹长度错误: {fp}")
                self.assertTrue(all(c in "0123456789ABCDEF" for c in fp),
                                f"{d['distribution']} 指纹含非十六进制字符: {fp}")

    def test_unsigned_distros_have_no_fingerprint(self):
        """无官方签名的发行版不应有指纹字段。"""
        for name in ("CentOS", "Deepin", "Proxmox"):
            for d in [x for x in self.data["distributions"] if x["distribution"] == name]:
                self.assertNotIn("gpg_key_fingerprint", d)

    def test_all_gpg_key_urls_are_https(self):
        """公钥必须经 HTTPS 传输(明文 HTTP 可被中间人替换为任意密钥)。"""
        for d in self.data["distributions"]:
            url = d.get("gpg_key_url")
            if url:
                self.assertTrue(url.startswith("https://"),
                                f"{d['distribution']} 公钥地址非 HTTPS: {url}")


# ---------------------------------------------------------------------------
# 公钥指纹锚定(TOFU 防护)相关测试
# ---------------------------------------------------------------------------

# 真实发行版指纹(Arch 发布密钥), 仅作测试数据使用
GOOD_FP = "3E80CA1A8B89F69CBA57D98A76A5EF9054449A5C"
ROTATED_FP = "C6E7F081CF80E13146676E88829B606631645531"
UNRELATED_FP = "1111111111111111111111111111111111111111"
BAD_FP = "0000000000000000000000000000000000000000"

KEY_URL = "https://example.org/official-keyring.gpg"
SIG_URL = "https://example.org/SHA256SUMS.gpg"

_KEYRING_MAGIC = b"ISO-HUB-FAKE-KEYRING:"


def _colons(fingerprints):
    """构造 gpg --with-colons 输出(pub 行 + 紧随其后的 fpr 行)。"""
    lines = []
    for fp in fingerprints:
        lines.append("pub:u:4096:1:" + fp[-16:] + ":::::::::::::::::")
        lines.append("fpr:::::::::" + fp + ":")
    return ("\n".join(lines) + "\n").encode()


def _encode_keyring(fingerprints):
    """把"已导出密钥"编码成可反解的字节, 模拟 gpg --export 的产物。"""
    return _KEYRING_MAGIC + ",".join(fingerprints).encode("utf-8")


def _decode_keyring(data: bytes):
    """反解 _encode_keyring, 供假的 --show-keys 读取缓存 keyring。"""
    if not data.startswith(_KEYRING_MAGIC):
        return []
    return [fp for fp in data[len(_KEYRING_MAGIC):].decode("utf-8").split(",") if fp]


def _completed(returncode=0, stdout=b"", stderr=b""):
    """构造 subprocess.CompletedProcess。"""
    return _subprocess.CompletedProcess(args=[], returncode=returncode,
                                        stdout=stdout, stderr=stderr)


def _fake_gpg(imported):
    """返回一个假的 subprocess.run, 模拟 gpg 的 import/list/export/show-keys 与 gpgv。"""
    def _run(args, **kwargs):
        cmd = list(args)
        # 真实 gpg 在 text=True 时返回 str, 否则返回 bytes; 这里保持一致
        def _out(payload: bytes):
            return payload.decode("utf-8") if kwargs.get("text") else payload

        if cmd[0] == "gpgv":
            return _completed(0, b"", b"Good signature")
        if cmd[0] != "gpg":
            return _completed(1, b"", b"unexpected command")
        if "--show-keys" in cmd:
            path = Path(cmd[cmd.index("--show-keys") + 1])
            data = path.read_bytes() if path.exists() else b""
            return _completed(0, _out(_colons(_decode_keyring(data))), b"")
        if "--import" in cmd:
            return _completed(0, b"", b"imported")
        if "--list-keys" in cmd:
            return _completed(0, _out(_colons(imported)), b"")
        if "--export" in cmd:
            selected = [a for a in cmd[cmd.index("--export") + 1:] if not a.startswith("-")]
            return _completed(0, _encode_keyring(selected or imported), b"")
        if "--verify" in cmd:
            return _completed(0, b"", b"Good signature")
        return _completed(1, b"", b"unexpected gpg subcommand")
    return _run


class _FakeResponse:
    """最小 requests.Response 替身: 只需 content / text / raise_for_status。"""

    def __init__(self, content: bytes):
        self.content = content
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        return None


def _fake_requests_get(url, *args, **kwargs):
    if url == KEY_URL:
        return _FakeResponse(b"FAKE-OFFICIAL-PUBKEY")
    if url == SIG_URL:
        return _FakeResponse(b"FAKE-DETACHED-SIGNATURE")
    raise AssertionError(f"未预期的请求: {url}")


class TestFingerprintPinning(unittest.TestCase):
    """公钥指纹锚定: 指纹不符即拒绝, 且绝不写入持久缓存。"""

    def setUp(self):
        self.d = dl.LinuxDistributionDownloader.__new__(dl.LinuxDistributionDownloader)
        self.tmp = Path(tempfile.mkdtemp())
        self.keyring = self.tmp / "iso-hub.gpg"

    def _call(self, expected, imported, stdout_buffer=None):
        """在假 gpg / 假网络环境下调用 verify_signature。"""
        ctx = contextlib.redirect_stdout(stdout_buffer) if stdout_buffer is not None \
            else contextlib.nullcontext()
        with patch("shutil.which", return_value="/usr/bin/gpg"), \
             patch.object(dl.subprocess, "run", side_effect=_fake_gpg(imported)), \
             patch.object(dl.requests, "get", side_effect=_fake_requests_get), ctx:
            return self.d.verify_signature("checksum text", SIG_URL, KEY_URL,
                                           self.tmp, expected)

    def test_fingerprint_match_passes_and_caches_only_pinned_key(self):
        """指纹匹配 -> pass; keyring 只留命中指纹的密钥, 不留"旁密钥"。"""
        status = self._call(GOOD_FP, [GOOD_FP, UNRELATED_FP])
        self.assertEqual(status, "pass")
        self.assertTrue(self.keyring.exists())
        self.assertEqual(_decode_keyring(self.keyring.read_bytes()), [GOOD_FP])

    def test_keyring_parent_dir_auto_created(self):
        """回归: 持久 keyring 目录不存在时 _prepare_keyring 需自动创建。

        旧实现把 mkdir 放在 verify_signature, 重构到 _prepare_keyring 后丢失,
        导致 write_bytes 抛 FileNotFoundError, 被上层 except 兜底成 skip ——
        GPG 静默降级、防护形同虚设。本测试确保父目录被自动创建。
        """
        deep = self.tmp / "deep" / "nested" / "gpg-keyring" / "iso-hub.gpg"
        self.assertFalse(deep.parent.exists(), "前置: 父目录必须不存在")
        with patch("shutil.which", return_value="/usr/bin/gpg"), \
             patch.object(dl.subprocess, "run", side_effect=_fake_gpg([GOOD_FP])), \
             patch.object(dl.requests, "get", side_effect=_fake_requests_get):
            status = self.d.verify_signature("checksum text", SIG_URL, KEY_URL,
                                             deep.parent, GOOD_FP)
        self.assertEqual(status, "pass")
        self.assertTrue(deep.exists(), "应自动创建父目录并写入 keyring")

    def test_fingerprint_mismatch_fails_and_never_writes_cache(self):
        """指纹不匹配 -> fail(拒绝下载), 且不写入缓存(否则毒 key 被永久固化)。"""
        status = self._call(GOOD_FP, [BAD_FP])
        self.assertEqual(status, "fail")
        self.assertFalse(self.keyring.exists(), "指纹不匹配时不应创建/保留缓存")

    def test_mismatch_logs_expected_and_actual_fingerprints(self):
        """失败日志必须同时打印预期与实际指纹, 便于定位是投毒还是配置过时。"""
        buf = io.StringIO()
        status = self._call(GOOD_FP, [BAD_FP], stdout_buffer=buf)
        self.assertEqual(status, "fail")
        log = buf.getvalue()
        self.assertIn(GOOD_FP, log)
        self.assertIn(BAD_FP, log)

    def test_poisoned_cached_keyring_is_detected_and_rebuilt(self):
        """迁移路径: 无指纹校验时代写入的旧缓存会被校验并重建。"""
        self.keyring.write_bytes(_encode_keyring([BAD_FP]))
        status = self._call(GOOD_FP, [GOOD_FP])
        self.assertEqual(status, "pass")
        self.assertEqual(_decode_keyring(self.keyring.read_bytes()), [GOOD_FP])

    def test_valid_cached_keyring_is_reused_without_refetch(self):
        """缓存指纹命中 -> 直接复用, 不再拉取公钥。"""
        self.keyring.write_bytes(_encode_keyring([GOOD_FP]))
        seen = []

        def _spy(url, *args, **kwargs):
            seen.append(url)
            return _fake_requests_get(url, *args, **kwargs)

        with patch("shutil.which", return_value="/usr/bin/gpg"), \
             patch.object(dl.subprocess, "run", side_effect=_fake_gpg([GOOD_FP])), \
             patch.object(dl.requests, "get", side_effect=_spy):
            status = self.d.verify_signature("t", SIG_URL, KEY_URL, self.tmp, GOOD_FP)
        self.assertEqual(status, "pass")
        self.assertNotIn(KEY_URL, seen)
        self.assertIn(SIG_URL, seen)

    def test_missing_fingerprint_warns_and_keeps_legacy_behavior(self):
        """未配置指纹 -> 不阻塞, 打印 TOFU 告警, 行为与改动前一致。"""
        buf = io.StringIO()
        status = self._call(None, [GOOD_FP, UNRELATED_FP], stdout_buffer=buf)
        self.assertEqual(status, "pass")
        self.assertIn("TOFU", buf.getvalue())
        # 未锚定时全量导出(与改动前一致)
        self.assertEqual(_decode_keyring(self.keyring.read_bytes()),
                         [GOOD_FP, UNRELATED_FP])

    def test_invalid_fingerprint_config_is_ignored_not_blocking(self):
        """误配的非法指纹只被忽略并告警, 不应导致拒绝下载。"""
        buf = io.StringIO()
        status = self._call("NOT-A-FINGERPRINT", [GOOD_FP], stdout_buffer=buf)
        self.assertEqual(status, "pass")
        self.assertIn("非法指纹", buf.getvalue())

    def test_multiple_fingerprints_any_match_is_enough(self):
        """支持多个合法指纹(密钥轮换/多位签名者), 命中任一即可。"""
        status = self._call([BAD_FP, GOOD_FP], [GOOD_FP, UNRELATED_FP])
        self.assertEqual(status, "pass")
        self.assertEqual(_decode_keyring(self.keyring.read_bytes()), [GOOD_FP])

    def test_rotated_key_accepted_by_second_fingerprint(self):
        """发行版轮换密钥后, 新指纹应在同一条配置里通过。"""
        status = self._call([BAD_FP, ROTATED_FP], [ROTATED_FP])
        self.assertEqual(status, "pass")
        self.assertEqual(_decode_keyring(self.keyring.read_bytes()), [ROTATED_FP])

    def test_empty_cached_keyring_is_rebuilt(self):
        """0 字节的坏缓存(旧代码可能写出)必须被重建, 而不是永久失效。"""
        self.keyring.write_bytes(b"")
        status = self._call(GOOD_FP, [GOOD_FP])
        self.assertEqual(status, "pass")
        self.assertEqual(_decode_keyring(self.keyring.read_bytes()), [GOOD_FP])


class TestEmbeddedClearsignedSignature(unittest.TestCase):
    """Fedora CHECKSUM 是内嵌 clearsigned 签名(direct signature), 无独立 .gpg/.asc
    文件可下载, 走 gpg --verify 单文件模式即可, 不需要再拉 detached 签名文件。"""

    CLEARSIGNED = (
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA256\n\n"
        "abc  *.iso\n"
        "-----BEGIN PGP SIGNATURE-----\n"
        "FAKEBLOCK=fake\n"
        "-----END PGP SIGNATURE-----\n"
    )

    def setUp(self):
        self.d = dl.LinuxDistributionDownloader.__new__(dl.LinuxDistributionDownloader)
        self.tmp = Path(tempfile.mkdtemp())

    def test_has_embedded_signature_true(self):
        """同时含 PGP signed message 头与 PGP signature 块即判为内嵌签名。"""
        self.assertTrue(self.d._has_embedded_signature(self.CLEARSIGNED))

    def test_has_embedded_signature_false_on_plain(self):
        """普通 checksum 文本(无签名块)不判为内嵌签名。"""
        self.assertFalse(self.d._has_embedded_signature("abc  file.iso\n"))
        self.assertFalse(self.d._has_embedded_signature(""))
        self.assertFalse(self.d._has_embedded_signature(None))

    def _make_spy_gpg(self, imported, verify_rc=0):
        """复用 _fake_gpg 的 keyring/import/export 行为, 仅包一层以记录调用并
        自定义 --verify 返回码。"""
        base = _fake_gpg(imported)
        calls = []

        def _run(args, **kwargs):
            calls.append(list(args))
            if "--verify" in args:
                out = b"Good signature" if verify_rc == 0 else b"BAD signature"
                return _completed(verify_rc, b"", out)
            return base(args, **kwargs)

        return _run, calls

    def _seed_valid_keyring(self):
        keyring = self.tmp / "iso-hub.gpg"
        keyring.write_bytes(_encode_keyring([GOOD_FP]))
        return keyring

    def test_embedded_signature_uses_gpg_single_file_mode(self):
        """内嵌签名走 gpg --verify 单文件模式, 不下载 detached 签名文件。"""
        self._seed_valid_keyring()
        run, calls = self._make_spy_gpg([GOOD_FP], verify_rc=0)
        with patch("shutil.which", return_value="/usr/bin/gpg"), \
             patch.object(dl.subprocess, "run", side_effect=run):
            status = self.d.verify_signature(self.CLEARSIGNED, "", KEY_URL,
                                             self.tmp, GOOD_FP)
        self.assertEqual(status, "pass")
        # 过滤出 --verify 那次调用: 单文件模式, 参数末尾是 checksum.txt 且无独立 .sig
        verify_calls = [c for c in calls if "--verify" in c]
        self.assertEqual(len(verify_calls), 1)
        verify_cmd = verify_calls[0]
        self.assertEqual(verify_cmd[0], "gpg")
        self.assertTrue(verify_cmd[-1].endswith("checksum.txt"))
        self.assertFalse(any(a.endswith(".sig") for a in verify_cmd))

    def test_embedded_signature_failure_returns_fail(self):
        """内嵌签名校验失败(returncode != 0)必须 fail, 不静默降级。"""
        self._seed_valid_keyring()
        run, _ = self._make_spy_gpg([GOOD_FP], verify_rc=1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             patch("shutil.which", return_value="/usr/bin/gpg"), \
             patch.object(dl.subprocess, "run", side_effect=run):
            status = self.d.verify_signature(self.CLEARSIGNED, "", KEY_URL,
                                             self.tmp, GOOD_FP)
        self.assertEqual(status, "fail")


class TestChecksumSmartFingerprintWiring(unittest.TestCase):
    """verify_checksum_smart 必须把指纹透传给 verify_signature。"""

    def setUp(self):
        self.d = dl.LinuxDistributionDownloader.__new__(dl.LinuxDistributionDownloader)
        self.d.download_dir = Path(tempfile.mkdtemp())

    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    @patch.object(dl.LinuxDistributionDownloader, "verify_checksum")
    def test_fingerprint_is_forwarded(self, mock_vc, mock_gc):
        mock_gc.return_value = "a" * 64
        mock_vc.return_value = True
        dist = {"distribution": "Ubuntu", "gpg_verify": "checksum",
                "gpg_key_url": KEY_URL, "signature_url": SIG_URL,
                "gpg_key_fingerprint": GOOD_FP}
        with patch.object(dl.LinuxDistributionDownloader, "verify_signature",
                          return_value="pass") as mock_sig:
            ok, _ = self.d.verify_checksum_smart(Path("/tmp/x.iso"),
                                                 "http://c", "", dist=dist)
        self.assertTrue(ok)
        self.assertEqual(mock_sig.call_args[0][4], GOOD_FP)

    @patch.object(dl.LinuxDistributionDownloader, "get_checksum_from_url")
    @patch.object(dl.LinuxDistributionDownloader, "verify_checksum")
    def test_absent_fingerprint_forwards_none(self, mock_vc, mock_gc):
        """旧条目没有指纹字段时透传 None, 保证向后兼容。"""
        mock_gc.return_value = "b" * 64
        mock_vc.return_value = True
        dist = {"distribution": "Ubuntu", "gpg_verify": "checksum",
                "gpg_key_url": KEY_URL, "signature_url": SIG_URL}
        with patch.object(dl.LinuxDistributionDownloader, "verify_signature",
                          return_value="skip") as mock_sig:
            ok, _ = self.d.verify_checksum_smart(Path("/tmp/x.iso"),
                                                 "http://c", "", dist=dist)
        self.assertTrue(ok)  # skip 降级到 SHA256
        self.assertIsNone(mock_sig.call_args[0][4])


class MigrationDistributionsTest(unittest.TestCase):
    """web/app.py 的配置遮蔽迁移函数(_migrate_distribution_fields)单元测试。

    覆盖: 旧副本补齐缺失字段 / 用户自定义条目保留 / 已有字段值不覆盖 /
    幂等(二次运行不重写)。用 AST 提取函数源码, 在隔离命名空间执行, 不触发 app 启动。
    """

    @staticmethod
    def _extract_migrate():
        import ast
        src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "_migrate_distribution_fields":
                return ast.get_source_segment(src, n)
        raise AssertionError("web/app.py 未找到 _migrate_distribution_fields")

    def _run_migrate(self, data_dir: Path, builtin: dict):
        fn_src = self._extract_migrate()
        ns = {
            "json": json,
            "DEFAULT_JSON": data_dir / "_builtin.json",
            "JSON_FILE": data_dir / "distributions.json",
            "log": lambda m: None,
        }
        exec(compile(fn_src, "<migrate>", "exec"), ns)  # noqa: S102
        ns["_migrate_distribution_fields"]()
        return json.loads((data_dir / "distributions.json").read_text(encoding="utf-8"))

    def test_migration_backfills_missing_fields_only(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            builtin = json.loads((REPO_ROOT / "iso_download" / "distributions.json")
                                 .read_text(encoding="utf-8"))
            (d / "_builtin.json").write_text(json.dumps(builtin), encoding="utf-8")
            ubuntu_url = "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/26.04/ubuntu-26.04-desktop-amd64.iso"
            old = {"distributions": [
                {"distribution": "Ubuntu", "type": "linux", "download_url": ubuntu_url,
                 "checksum_url": "cu", "checksum": ""},
                {"distribution": "MyDistro", "type": "linux", "download_url": "http://c/x.iso",
                 "checksum_url": "cc", "checksum": "", "myfield": "keep"},
            ]}
            (d / "distributions.json").write_text(json.dumps(old), encoding="utf-8")
            res = self._run_migrate(d, builtin)
            ub = next(e for e in res["distributions"]
                      if e["download_url"] == ubuntu_url)
            cust = next(e for e in res["distributions"]
                        if e["distribution"] == "MyDistro")
            # 补齐 gpg 字段
            self.assertEqual(ub.get("gpg_verify"), "checksum")
            self.assertEqual(ub.get("gpg_key_fingerprint"),
                             "843938DF228D22F7B3742BC0D94AA3F0EFE21092")
            # 已有字段不被覆盖
            self.assertEqual(ub.get("checksum"), "")
            # 用户自定义条目完整保留, 不误补
            self.assertEqual(cust.get("myfield"), "keep")
            self.assertIsNone(cust.get("gpg_verify"))
            # 幂等: 二次运行不重写
            m1 = (d / "distributions.json").stat().st_mtime
            self._run_migrate(d, builtin)
            m2 = (d / "distributions.json").stat().st_mtime
            self.assertEqual(m1, m2)


if __name__ == "__main__":
    unittest.main(verbosity=2)