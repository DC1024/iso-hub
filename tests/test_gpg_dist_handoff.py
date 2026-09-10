#!/usr/bin/env python3
"""GPG 验签「dist 交接」回归测试。

背景(真实 bug, v1.3.7):
    verify_checksum_smart(filepath, checksum_url, stored_checksum, dist=None) 的 GPG 预检
    只在 `dist and dist.get("gpg_verify")` 成立时才触发。但生产下载的真正入口
    web/iso_runner.py 与 web/sync_subscriptions.py 调用它时只传了 3 个位置参数,
    漏掉了 dist → dist 恒为 None → GPG 验签被 100% 静默跳过, 直接降级 SHA256。
    于是日志面板永远看不到「✓ GPG 签名验证通过」。

    而 download_linux.py 自己的主流程(download_distribution)是正确传了 dist=target_dist 的,
    只是 Web UI 走的是 iso_runner, 不经过那条路径 —— 所以功能代码一直存在, 但生产从未生效。

本文件锁死三处调用点, 确保每处都把 dist 传给 verify_checksum_smart:
  1. iso_runner._download_file_with_failover  -> dist=target_dist
  2. iso_runner.main() 的「文件已存在」分支     -> dist=entry
  3. sync_subscriptions._last_run_verified     -> dist=entry

配合变异测试: 把任一处 dist=xxx 删掉(改回旧逻辑), 对应测试必须变红。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import iso_runner  # noqa: E402
import sync_subscriptions  # noqa: E402


class _FakeResp:
    """模拟 requests 的流式响应(body 完整)。"""

    def __init__(self, data: bytes):
        self._data = data
        self.headers = {"content-length": str(len(data))}
        self.status_code = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        yield self._data


class _RecordingDownloader:
    """记录 verify_checksum_smart 收到的 dist 参数, 供 _download_file_with_failover 使用。"""

    headers = {}

    def __init__(self):
        self.dist_calls = []

    def verify_checksum_smart(self, filepath, checksum_url, stored, dist=None):
        self.dist_calls.append(dist)
        return True, "校验通过(测试桩)"


class TestIsoRunnerFailoverPassesDist(unittest.TestCase):
    """_download_file_with_failover 必须把 target_dist 作为 dist 传下去。"""

    def test_passes_dist_to_verify(self):
        target = {"distribution": "Ubuntu", "checksum": "abc", "gpg_verify": "checksum"}
        rec = _RecordingDownloader()
        with tempfile.TemporaryDirectory() as d:
            dist_dir = Path(d)
            dist_dir.mkdir(parents=True, exist_ok=True)
            filepath = dist_dir / "ubuntu.iso"
            resp = _FakeResp(b"hello")
            with patch.object(iso_runner.requests, "get", return_value=resp):
                ok, _url = iso_runner._download_file_with_failover(
                    rec, target,
                    candidates=[("https://mirror.test/ubuntu.iso", None)],
                    filename="ubuntu.iso", dist_dir=dist_dir, filepath=filepath,
                    head_total=5,
                )
        self.assertTrue(ok)
        self.assertEqual(rec.dist_calls, [target],
                         "必须把 target_dist 原样传给 verify_checksum_smart(dist=...)")


class TestIsoRunnerMainExistingPassesDist(unittest.TestCase):
    """main() 的「文件已存在」分支必须把 entry 作为 dist 传下去。"""

    def test_existing_file_passes_dist(self):
        name = "Ubuntu"
        url = "https://mirror.test/ubuntu-releases/26.04/ubuntu-26.04-desktop.iso"
        entry = {
            "distribution": name,
            "download_url": url,
            "checksum_url": "https://mirror.test/ubuntu-releases/26.04/SHA256SUMS",
            "checksum": "abc",
            "gpg_verify": "checksum",
        }
        received = {}

        def vcs(fp, cu, sc, dist=None):
            received["dist"] = dist
            return True, "ok"

        fake_dl = MagicMock()
        fake_dl.distributions = {"distributions": [entry]}
        fake_dl.headers = {}
        fake_dl.verify_checksum_smart.side_effect = vcs

        with tempfile.TemporaryDirectory() as d:
            dl_dir = Path(d)
            fake_dl.download_dir = str(dl_dir)  # 必须真实路径, 否则 _safe_dist_dir 落到 MagicMock 路径上
            fname = url.rstrip("/").rsplit("/", 1)[-1]
            dist_dir = dl_dir / "linux" / name
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / fname).write_text("pre-existing", encoding="utf-8")

            select_json = json.dumps([{"distribution": name, "download_url": url}])
            argv = ["iso_runner.py", "--json-file", "unused.json",
                    "--download-dir", str(dl_dir),
                    "--select", select_json, "--strategy", "A"]
            with patch.object(sys, "argv", argv), \
                 patch.object(iso_runner, "_head_target_size", return_value=0), \
                 patch("download_linux.LinuxDistributionDownloader", return_value=fake_dl):
                iso_runner.main()

        self.assertTrue(received.get("dist"), "必须把 dist 传给 verify_checksum_smart")
        self.assertEqual(received["dist"]["distribution"], entry["distribution"])
        self.assertEqual(received["dist"]["download_url"], entry["download_url"])
        self.assertEqual(received["dist"]["gpg_verify"], entry["gpg_verify"],
                         "dist 必须携带原始 entry 的 gpg_verify 字段(否则 GPG 仍被跳过)")


class TestSyncSubscriptionsPassesDist(unittest.TestCase):
    """_last_run_verified 必须把 entry 作为 dist 传下去。"""

    def test_last_run_verified_passes_dist(self):
        entry = {"checksum_url": "http://c/SHA256SUMS", "checksum": "abc",
                 "gpg_verify": "checksum"}
        fake_dl = MagicMock()
        fake_dl.verify_checksum_smart.return_value = (True, "ok")

        with tempfile.TemporaryDirectory() as d:
            fp = Path(d) / "ubuntu.iso"
            fp.write_text("x", encoding="utf-8")
            ok = sync_subscriptions._last_run_verified(fake_dl, entry, fp)

        self.assertTrue(ok)
        fake_dl.verify_checksum_smart.assert_called_once()
        self.assertEqual(fake_dl.verify_checksum_smart.call_args.kwargs.get("dist"), entry,
                         "_last_run_verified 必须把 entry 传给 verify_checksum_smart(dist=...)")


if __name__ == "__main__":
    unittest.main()