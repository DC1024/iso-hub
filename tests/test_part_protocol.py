#!/usr/bin/env python3
"""下载落盘协议(.part 原子改名)的回归测试。

背景(真实 bug):
    用户在面板上手动勾选下载, 又手动点「停止任务」结束下载。此时磁盘上留下一个
    59MB 的残缺文件, 但面板显示「已下载」。

根因:
    1. 下载器直接写最终文件名 xxx.iso, 不是 xxx.iso.part → 后端 disk_inventory()
       的半成品后缀检测(.part/.aria2/.!qB/.tmp)永远匹配不到 → 误判为完整;
    2. 「停止任务」向进程组发 SIGTERM 杀进程, main() 里下载循环之后的
       _record_failure() 根本执行不到 → download_failures.json 不生成。

修复:
    下载期间一律写 <最终名>.part, 只有"大小校验 + 校验和"全通过后才 os.replace
    改名为最终文件名。进程被 kill 也会留下 .part 被识别。

本文件直接对 iso_runner._download_file_with_failover 做行为测试(用假 requests),
验证三种场景: 成功改名 / 校验失败留 .part / 传输中途异常留 .part。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import iso_runner  # noqa: E402


class _FakeResp:
    """模拟 requests 的流式响应。"""

    def __init__(self, chunks, headers=None, status=200):
        self._chunks = chunks
        self.headers = headers or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        yield from self._chunks


class _FakeDownloader:
    """只实现 _download_file_with_failover 用到的两个接口。"""

    headers = {}

    def __init__(self, checksum_ok=True):
        self.checksum_ok = checksum_ok
        self.verified_paths = []

    def verify_checksum_smart(self, filepath, checksum_url, stored, dist=None):
        self.verified_paths.append(Path(filepath))
        if self.checksum_ok:
            return True, "校验通过(测试桩)"
        return False, "校验和不匹配(测试桩)"


class TestPartAtomicProtocol(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.final = self.dir / "ubuntu.iso"
        self.part = self.dir / "ubuntu.iso.part"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, resp_or_exc, downloader=None, candidates=None):
        dl = downloader or _FakeDownloader()
        if candidates is None:
            candidates = [("https://mirror.test/ubuntu.iso", None)]

        def _fake_get(url, **kw):
            if isinstance(resp_or_exc, Exception):
                raise resp_or_exc
            return resp_or_exc

        with patch.object(iso_runner.requests, "get", _fake_get):
            return iso_runner._download_file_with_failover(
                dl, {"checksum": ""}, candidates, "ubuntu.iso", self.dir, self.final
            ), dl

    def test_success_writes_part_then_renames(self):
        """成功: 中途写 .part, 最终只剩以正式名存在的完整文件。"""
        resp = _FakeResp([b"a" * 100], {"content-length": "100"})
        (ok, url), dl = self._run(resp)
        self.assertTrue(ok)
        self.assertTrue(self.final.exists(), "成功后应有最终文件")
        self.assertFalse(self.part.exists(), "成功后不应残留 .part")
        self.assertEqual(self.final.stat().st_size, 100)

    def test_part_is_actually_used_during_download(self):
        """下载期间必须写 .part: 校验回调收到的路径应是 .part。"""
        resp = _FakeResp([b"a" * 50], {"content-length": "50"})
        (ok, _), dl = self._run(resp)
        self.assertTrue(ok)
        self.assertTrue(dl.verified_paths, "应调用过校验")
        self.assertEqual(dl.verified_paths[0].name, "ubuntu.iso.part",
                         "校验应针对 .part 进行, 通过后才改名")

    def test_checksum_failure_never_produces_final(self):
        """校验失败: 绝不产生最终文件(且损坏的半成品会被丢弃, 见 Resume 测试)。"""
        resp = _FakeResp([b"a" * 100], {"content-length": "100"})
        (ok, _), _ = self._run(resp, downloader=_FakeDownloader(checksum_ok=False))
        self.assertFalse(ok)
        self.assertFalse(self.final.exists(), "失败时不得出现最终文件")

    def test_size_mismatch_keeps_part_not_final(self):
        """content-length 与实际字节不一致(截断流): 保留 .part。"""
        resp = _FakeResp([b"a" * 30], {"content-length": "100"})
        (ok, _), _ = self._run(resp)
        self.assertFalse(ok)
        self.assertTrue(self.part.exists())
        self.assertFalse(self.final.exists())

    def test_network_error_midway_keeps_part(self):
        """下载中途抛网络异常: 已写入的数据留在 .part, 不产生最终文件。"""
        (ok, _), _ = self._run(RuntimeError("connection reset"))
        self.assertFalse(ok)
        self.assertFalse(self.final.exists(), "传输异常不得产生最终文件")

    def test_kill_scenario_leaves_part_on_disk(self):
        """模拟「停止任务」: 进程在写入 .part 途中被杀, 磁盘只剩 .part。

        这里用 IterGenerator 在产出首个 chunk 后抛 KeyboardInterrupt 之外的
        异常来模拟被中断的流; 关键断言是最终文件名不存在、.part 存在。
        """
        class _InterruptedResp:
            headers = {"content-length": "1000"}
            status_code = 200

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=8192):
                yield b"x" * 61825024  # 59MB 后连接被切断
                raise RuntimeError("任务被停止")

        with patch.object(iso_runner.requests, "get", lambda url, **kw: _InterruptedResp()):
            ok, _ = iso_runner._download_file_with_failover(
                _FakeDownloader(), {"checksum": ""},
                [("https://mirror.test/ubuntu.iso", None)], "ubuntu.iso", self.dir, self.final
            )
        self.assertFalse(ok)
        self.assertFalse(self.final.exists(), "被中断时绝不能留下正式名的残缺文件")
        self.assertTrue(self.part.exists(), "被中断时应留下 .part 供识别")

    def test_existing_final_is_replaced_on_success(self):
        """同名旧文件存在时, 成功后应被新内容覆盖(不产生双份)。"""
        self.final.write_bytes(b"stale")
        resp = _FakeResp([b"b" * 200], {"content-length": "200"})
        (ok, _), _ = self._run(resp)
        self.assertTrue(ok)
        self.assertEqual(self.final.stat().st_size, 200)
        self.assertFalse(self.part.exists())


class TestFailoverAcrossSources(unittest.TestCase):
    """一个源失败要能切到下一个源, 且不会污染最终文件名。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.final = self.dir / "x.iso"

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_source_fails_then_second_succeeds(self):
        calls = {"n": 0}

        def _fake_get(url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("源1不可达")
            return _FakeResp([b"z" * 64], {"content-length": "64"})

        with patch.object(iso_runner.requests, "get", _fake_get):
            ok, url = iso_runner._download_file_with_failover(
                _FakeDownloader(), {"checksum": ""},
                [("https://a.test/x.iso", None), ("https://b.test/x.iso", None)],
                "x.iso", self.dir, self.final
            )
        self.assertTrue(ok)
        self.assertEqual(url, "https://b.test/x.iso")
        self.assertTrue(self.final.exists())
        self.assertFalse((self.dir / "x.iso.part").exists())


class TestResumeSupport(unittest.TestCase):
    """断点续传: 已存在 .part 时应带 Range 头, 服务器返回 206 时追加写入。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.final = self.dir / "big.iso"
        self.part = self.dir / "big.iso.part"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, resp, downloader=None):
        seen = {}

        def _fake_get(url, **kw):
            seen.update(kw)
            return resp

        with patch.object(iso_runner.requests, "get", _fake_get):
            res = iso_runner._download_file_with_failover(
                downloader or _FakeDownloader(), {"checksum": ""},
                [("https://mirror.test/big.iso", None)], "big.iso", self.dir, self.final
            )
        return res, seen

    def test_range_header_sent_when_part_exists(self):
        """已有 1000B 的 .part -> 必须带 Range: bytes=1000- 续传。"""
        self.part.write_bytes(b"x" * 1000)
        resp = _FakeResp([b"y" * 500], {"content-length": "500",
                                        "content-range": "bytes 1000-1499/1500"},
                         status=206)
        (ok, _), sent = self._run(resp)
        self.assertTrue(ok)
        self.assertEqual(sent.get("headers", {}).get("Range"), "bytes=1000-",
                         "续传必须发送 Range 头")
        self.assertEqual(self.final.stat().st_size, 1500, "追加写入后应是 1000+500")

    def test_no_range_header_for_fresh_download(self):
        """没有 .part 时不应发 Range 头。"""
        resp = _FakeResp([b"a" * 100], {"content-length": "100"})
        (ok, _), sent = self._run(resp)
        self.assertTrue(ok)
        self.assertNotIn("Range", sent.get("headers", {}))

    def test_server_without_range_falls_back_to_full_download(self):
        """服务器不支持 Range(返回 200) -> 丢弃旧半成品, 从头下(不出现拼接错误)。"""
        self.part.write_bytes(b"x" * 1000)
        resp = _FakeResp([b"z" * 800], {"content-length": "800"}, status=200)
        (ok, _), _ = self._run(resp)
        self.assertTrue(ok)
        self.assertEqual(self.final.stat().st_size, 800,
                         "应从头重下(800B), 而不是糟糕地拼接成 1800B")

    def test_truncated_resume_keeps_part(self):
        """续传中途被打断: .part 保留, 不产生最终文件。"""
        self.part.write_bytes(b"x" * 1000)
        resp = _FakeResp([b"y" * 50], {"content-length": "500",
                                       "content-range": "bytes 1000-1499/1500"},
                         status=206)
        (ok, _), _ = self._run(resp)
        self.assertFalse(ok)
        self.assertFalse(self.final.exists())
        self.assertTrue(self.part.exists())

    def test_checksum_failure_discards_corrupt_part(self):
        """校验失败时 .part 已损坏, 应删除以免下轮拿坏数据做续传。"""
        resp = _FakeResp([b"a" * 100], {"content-length": "100"})
        (ok, _), _ = self._run(resp, downloader=_FakeDownloader(checksum_ok=False))
        self.assertFalse(ok)
        self.assertFalse(self.part.exists(), "损坏的半成品应被丢弃")


class TestTotalResolution(unittest.TestCase):
    """v1.2.9: 期望长度的三级兜底解析 —— 修复"total=0 时跳过完整性校验"。"""

    class _H:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=None):
            return self._d.get(k, default)

    def _r(self, headers):
        return type("R", (), {"headers": self._H(headers)})()

    def test_content_range_wins(self):
        """Content-Range 的全长最可信, 优先采用。"""
        n = iso_runner._resolve_total(
            self._r({"content-range": "bytes 1000-1499/1500",
                     "content-length": "500"}), have=1000, head_total=99999)
        self.assertEqual(n, 1500)

    def test_content_length_plus_have_for_resume(self):
        """续传时只有 Content-Length(剩余量) -> 加上已下的 have 才是全长。"""
        n = iso_runner._resolve_total(self._r({"content-length": "500"}),
                                      have=1000, head_total=0)
        self.assertEqual(n, 1500)

    def test_head_total_fallback(self):
        """Content-Range/Content-Length 都没有 -> 用 HEAD 预取的大小兜底。"""
        n = iso_runner._resolve_total(self._r({}), have=0, head_total=2048)
        self.assertEqual(n, 2048, "这是修复前 total 会被算成 0 的分支")

    def test_returns_zero_when_truly_unknown(self):
        """三级全无 -> 返回 0(语义: 确实无从判断, 而非静默放行)。"""
        n = iso_runner._resolve_total(self._r({}), have=0, head_total=0)
        self.assertEqual(n, 0)

    def test_bad_content_length_is_tolerated(self):
        """非法 Content-Length 不应抛异常, 应继续走兜底。"""
        n = iso_runner._resolve_total(self._r({"content-length": "abc"}),
                                      have=0, head_total=77)
        self.assertEqual(n, 77)


class TestTruncationVsCorruption(unittest.TestCase):
    """v1.2.9: 截断(保留 .part 续传) 与 损坏(丢弃 .part) 必须区分对待。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.final = self.dir / "big.iso"
        self.part = self.dir / "big.iso.part"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, resp, downloader=None, head_total=0):
        with patch.object(iso_runner.requests, "get", lambda url, **kw: resp):
            return iso_runner._download_file_with_failover(
                downloader or _FakeDownloader(), {"checksum": ""},
                [("https://mirror.test/big.iso", None)], "big.iso", self.dir,
                self.final, head_total=head_total
            )

    def test_truncation_keeps_part_for_resume(self):
        """有预期长度但写入不足 = 截断 -> 保留 .part, 下次续传。"""
        resp = _FakeResp([b"a" * 30], {"content-length": "100"})
        (ok, _) = self._run(resp)
        self.assertFalse(ok)
        self.assertTrue(self.part.exists(), "截断半成品是有效前缀, 必须保留")
        self.assertEqual(self.part.stat().st_size, 30)
        self.assertFalse(self.final.exists())

    def test_size_ok_but_checksum_bad_discards_part(self):
        """尺寸达标却校验不过 = 内容损坏 -> 丢弃, 避免坏数据被续传。"""
        resp = _FakeResp([b"a" * 100], {"content-length": "100"})
        (ok, _) = self._run(resp, downloader=_FakeDownloader(checksum_ok=False))
        self.assertFalse(ok)
        self.assertFalse(self.part.exists(), "内容损坏的半成品应丢弃")

    def test_unknown_length_bad_checksum_conservatively_keeps_part(self):
        """长度未知且校验不过 -> 无法区分截断/损坏 -> 保守保留(宁多占磁盘不误删进度)。"""
        resp = _FakeResp([b"a" * 100], {})  # 无任何长度信息
        (ok, _) = self._run(resp, downloader=_FakeDownloader(checksum_ok=False))
        self.assertFalse(ok)
        self.assertTrue(self.part.exists(), "长度未知时应保守保留半成品")

    def test_head_total_used_to_detect_truncation(self):
        """服务器不给长度, 但调用方 HEAD 预取了大小 -> 仍能判定截断并保留 .part。"""
        resp = _FakeResp([b"a" * 30], {})  # 无长度头
        (ok, _) = self._run(resp, head_total=100)
        self.assertFalse(ok)
        self.assertTrue(self.part.exists())
        self.assertEqual(self.part.stat().st_size, 30)

    def test_oversized_payload_is_treated_as_corrupt(self):
        """写入超过预期长度 = 内容不可信 -> 丢弃。"""
        resp = _FakeResp([b"a" * 150], {"content-length": "100"})
        (ok, _) = self._run(resp)
        self.assertFalse(ok)
        self.assertFalse(self.part.exists(), "超长内容应被丢弃")

    def test_full_and_verified_succeeds(self):
        """对照组: 长度达标 + 校验通过 -> 正常改名成功。"""
        resp = _FakeResp([b"a" * 100], {"content-length": "100"})
        (ok, _) = self._run(resp)
        self.assertTrue(ok)
        self.assertTrue(self.final.exists())
        self.assertFalse(self.part.exists())

    def test_head_total_zero_and_no_headers_still_checksums(self):
        """长度完全未知时不应抛"大小不匹配", 而是交给校验和判定(能过就成功)。"""
        resp = _FakeResp([b"a" * 100], {})
        (ok, _) = self._run(resp)
        self.assertTrue(ok, "长度未知但校验通过应算成功(v1.2.9 不再误判)")
        self.assertTrue(self.final.exists())


class TestRunnerSourceContract(unittest.TestCase):
    """源码静态断言: 防止回归到"直接写最终名"的旧实现。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO_ROOT / "web" / "iso_runner.py").read_text(encoding="utf-8")
        cls.dl_src = (REPO_ROOT / "iso_download" / "download_linux.py").read_text(encoding="utf-8")
        cls.app_src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")

    def test_app_download_payload_uses_part_path(self):
        """回归: /api/download 的 downloads.path 必须用 .part 路径。

        否则 running_task() 对最终名 stat() 得 0, 且 targets 的 key 是 .part
        名 → 两边对不上 → 进度条永远卡 0%。
        """
        self.assertIn("PART_SUFFIX", self.app_src)
        self.assertIn("target / (fname + PART_SUFFIX)", self.app_src)

    def test_runner_sends_range_header(self):
        self.assertIn('headers["Range"] = f"bytes={have}-"', self.src)

    def test_runner_handles_206_append(self):
        self.assertIn("resp.status_code == 206", self.src)
        self.assertIn('mode = "ab"', self.src)

    def test_upstream_supports_resume(self):
        self.assertIn("Range", self.dl_src)
        self.assertIn("206", self.dl_src)

    def test_runner_uses_part_suffix_constant(self):
        self.assertIn('PART_SUFFIX = ".part"', self.src)

    def test_runner_opens_part_not_final(self):
        """下载循环必须打开 .part 文件句柄(而非最终名)。

        mode 是变量: 全新下载/不支持 Range 时为 "wb", 续传命中 206 时为 "ab"。
        """
        self.assertIn("with open(part, mode) as f:", self.src)
        self.assertNotIn("with open(filepath, \"wb\") as f:", self.src)

    def test_runner_replaces_atomically(self):
        self.assertIn("os.replace(part, filepath)", self.src)

    def test_runner_reports_part_path_as_target(self):
        """#TARGET 必须上报 .part 路径, 否则前端进度条永远读到 0。"""
        self.assertIn("#TARGET {part_path}", self.src)

    def test_upstream_uses_part_and_replace(self):
        """订阅同步走的 download_linux 也必须遵守同一协议。"""
        self.assertIn(".part", self.dl_src)
        self.assertIn("os.replace(part_path, filepath)", self.dl_src)

    def test_cleanup_preserves_part_files(self):
        """目录清理不得删除半成品(.part), 否则续传数据丢失。"""
        self.assertIn('f.lower().endswith((".part", ".aria2"))', self.dl_src)

    # ---- v1.2.9: 完整性判据回归护栏 ----

    def test_runner_uses_three_tier_total_resolution(self):
        """回归: total 必须经 _resolve_total 三级兜底, 不得退回只看 content-length。"""
        self.assertIn("def _resolve_total(", self.src)
        self.assertIn("_resolve_total(resp", self.src)

    def test_runner_never_skips_size_check_silently(self):
        """回归: 不得再出现 `if total and ...` 形式的静默短路(这正是本次 bug 根因)。"""
        self.assertNotIn("if total and part.stat().st_size != total:", self.src)
        self.assertIn("if total and written < total:", self.src)

    def test_runner_distinguishes_truncation_from_corruption(self):
        """必须有两类异常, 且截断分支保留 .part。"""
        self.assertIn("class TruncatedTransfer(Exception):", self.src)
        self.assertIn("class CorruptPayload(Exception):", self.src)
        self.assertIn("except TruncatedTransfer as e:", self.src)
        self.assertIn("except CorruptPayload as e:", self.src)

    def test_runner_probes_candidate_sources_for_size(self):
        """默认源拿不到大小时应继续探测候选源, 避免 UI 进度条失去基准。"""
        self.assertIn("for _u, _c in candidates[1:]:", self.src)

    def test_head_total_threaded_into_download(self):
        """HEAD 预取的大小必须真正传入下载函数, 否则兜底形同虚设。"""
        self.assertIn("head_total=total", self.src)

    def test_upstream_has_head_fallback(self):
        """订阅同步走的 download_linux 也必须能补长度判据, 且不得出现未定义变量。"""
        self.assertIn("def _head_content_length(self, url: str) -> int:", self.dl_src)
        self.assertIn('total_size = self._head_content_length(target_dist["download_url"])',
                      self.dl_src)
        self.assertNotIn("self._head_content_length(url)", self.dl_src)


if __name__ == "__main__":
    unittest.main()
