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

    def verify_checksum_smart(self, filepath, checksum_url, stored):
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

    def test_checksum_failure_keeps_part_not_final(self):
        """校验失败: 保留 .part, 绝不产生最终文件。"""
        resp = _FakeResp([b"a" * 100], {"content-length": "100"})
        (ok, _), _ = self._run(resp, downloader=_FakeDownloader(checksum_ok=False))
        self.assertFalse(ok)
        self.assertTrue(self.part.exists(), "失败时应保留 .part 供续传")
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


class TestRunnerSourceContract(unittest.TestCase):
    """源码静态断言: 防止回归到"直接写最终名"的旧实现。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO_ROOT / "web" / "iso_runner.py").read_text(encoding="utf-8")
        cls.dl_src = (REPO_ROOT / "iso_download" / "download_linux.py").read_text(encoding="utf-8")

    def test_runner_uses_part_suffix_constant(self):
        self.assertIn('PART_SUFFIX = ".part"', self.src)

    def test_runner_opens_part_not_final(self):
        """下载循环必须打开 .part 文件句柄。"""
        self.assertIn("with open(part, \"wb\") as f:", self.src)

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


if __name__ == "__main__":
    unittest.main()
