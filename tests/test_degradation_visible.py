#!/usr/bin/env python3
"""降级可观测性测试 —— 「成功 / 跳过 / 未调用」三态必须可区分。

背景:
    iso-hub 的设计哲学是「可选环节失败不阻塞主流程」(可用性好), 但副作用是:
        GPG 验证成功  -> exit 0, 日志 ✓
        GPG 被跳过    -> exit 0, 日志 -
        GPG 根本没调用 -> exit 0, 日志里压根没有这行
    三种状态在外部几乎无法区分 —— 降级是可用性的朋友, 是测试的敌人。

    修复方向不是去掉降级, 而是**降级必须留下可区分的痕迹**。本文件锁死:
      1. 三态日志标记(✓/⚠/-)确实会按预期出现在输出里
      2. count_gpg_outcomes() 聚合函数: 全部 skip 视为验签整体失效
      3. fail 必须阻断(与 test_wiring_gpg 互补, 这里从日志视角断言"拒绝"痕迹)

    CI 的 Tests workflow 会运行本文件; 运维巡检也可复用 count_gpg_outcomes()
    扫描生产容器日志。
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import download_linux  # noqa: E402
from download_linux import LinuxDistributionDownloader  # noqa: E402

CHECKSUM_TEXT = "abc123  ubuntu-24.04-desktop-amd64.iso\n"
SHA256 = "a" * 64

LOG_PATTERNS = {
    "pass": "✓ GPG 签名验证通过",
    "fail": "⚠ GPG 校验未通过",
    "skip": "GPG 签名验证跳过",
}


def count_gpg_outcomes(log_text: str) -> Counter:
    """统计一段日志里 GPG 三态出现次数(供测试与运维巡检共用)。"""
    return Counter({name: log_text.count(marker)
                    for name, marker in LOG_PATTERNS.items()})


def assert_not_all_skipped(log_text: str) -> None:
    """全部 skip 视为验签整体失效(v1.3.7 症状) —— 供巡检脚本复用。

    没有任何 GPG 输出同样视为失败(验签分支根本没执行)。
    """
    counts = count_gpg_outcomes(log_text)
    total = sum(counts.values())
    if total == 0:
        raise AssertionError("日志里没有任何 GPG 输出 —— 验签分支未被执行")
    if counts["skip"] == total:
        raise AssertionError(
            f"全部 {total} 项均为 skip —— GPG 验签整体失效(v1.3.7 症状)")


def _run_verify(tmp: str, gpg_status: str):
    """跑一次配置了 gpg_verify 的校验, 返回 (ok, msg, 捕获的日志文本)。"""
    jp = Path(tmp) / "distributions.json"
    jp.write_text('{"distributions": []}', encoding="utf-8")
    dl = LinuxDistributionDownloader(json_file=str(jp), download_dir=str(tmp))
    fp = Path(tmp) / "ubuntu.iso"
    fp.write_bytes(b"iso-bytes")
    buf = io.StringIO()
    with patch.object(LinuxDistributionDownloader, "verify_signature",
                      return_value=gpg_status), \
         patch.object(download_linux.requests, "get",
                      return_value=MagicMock(text=CHECKSUM_TEXT)), \
         patch.object(LinuxDistributionDownloader, "get_checksum_from_url",
                      return_value=SHA256), \
         patch.object(LinuxDistributionDownloader, "verify_checksum",
                      return_value=True), \
         contextlib.redirect_stdout(buf):
        ok, msg = dl.verify_checksum_smart(
            fp, "https://mirror.test/SHA256SUMS", None,
            dist={"gpg_verify": "checksum"},
        )
    return ok, msg, buf.getvalue()


class TestGpgOutcomeVisibility(unittest.TestCase):
    """三态日志必须可区分 —— 降级必须留痕。"""

    def test_pass_leaves_visible_marker(self):
        with tempfile.TemporaryDirectory() as d:
            ok, _msg, log = _run_verify(d, "pass")
        self.assertTrue(ok)
        self.assertIn(LOG_PATTERNS["pass"], log,
                      "验签通过却没有留下 ✓ 痕迹 —— 成功状态不可观测")

    def test_fail_leaves_visible_marker_and_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg, log = _run_verify(d, "fail")
        self.assertFalse(ok, "签名 fail 必须阻断下载")
        self.assertIn(LOG_PATTERNS["fail"], log,
                      "签名失败却没有留下 ⚠ 痕迹 —— 篡改场景不可观测")
        self.assertIn("拒绝下载", log, "fail 日志必须说明已拒绝下载")

    def test_skip_leaves_visible_marker(self):
        with tempfile.TemporaryDirectory() as d:
            ok, _msg, log = _run_verify(d, "skip")
        self.assertTrue(ok, "skip 应降级放行, 不阻塞")
        self.assertIn(LOG_PATTERNS["skip"], log,
                      "skip 却没有留痕 —— 降级不可观测, 与成功无法区分")

    def test_not_all_skipped_detector(self):
        """聚合判定: 全部 skip / 零输出 必须被判为失效; 混合 pass 正常。"""
        all_skip = "\n".join(["  - GPG 签名验证跳过(官方无公钥)"] * 10)
        with self.assertRaises(AssertionError):
            assert_not_all_skipped(all_skip)
        with self.assertRaises(AssertionError):
            assert_not_all_skipped("下载完成, 无任何 GPG 相关输出")
        mixed = ("  ✓ GPG 签名验证通过\n" + "\n".join(
            ["  - GPG 签名验证跳过(官方无公钥)"] * 9))
        assert_not_all_skipped(mixed)  # 有一个真验签即不触发告警

    def test_outcome_counter(self):
        counts = count_gpg_outcomes(
            "✓ GPG 签名验证通过\n- GPG 签名验证跳过\n- GPG 签名验证跳过")
        self.assertEqual(counts["pass"], 1)
        self.assertEqual(counts["skip"], 2)
        self.assertEqual(counts["fail"], 0)

    def test_fail_message_is_actionable_json(self):
        """阻断消息必须包含 GPG 字样(前端/日志面板可直接辨识根因)。"""
        with tempfile.TemporaryDirectory() as d:
            _ok, msg, _log = _run_verify(d, "fail")
        payload = json.dumps({"msg": msg}, ensure_ascii=False)
        self.assertIn("GPG", payload)


if __name__ == "__main__":
    unittest.main()
