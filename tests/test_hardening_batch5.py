#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 5 批加固的验收测试 —— 对应审查报告 M3 / M4 / M5 / L1 / L4。

本文件按项分节, 每节只测"这一项到底有没有真的生效", 不做重复的端到端。

M3 的关键点: **线程锁 ≠ 跨进程锁**。
`config_files.file_lock()` 是 threading.RLock, 只能让同一进程里的线程排队;
而 download_failures.json / download_unverified.json 的三个写方里有两个是
**独立子进程**(iso_runner、sync_subscriptions)。所以 M3 的验收必须真的起
第二个进程去抢锁 —— 只在同进程里起两个线程, 无论锁写没写对都会通过。
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"
sys.path.insert(0, str(WEB))

import app  # noqa: E402
import config_files  # noqa: E402
import iso_runner  # noqa: E402


class _TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.ledger = self.tmp / "download_failures.json"

    def tearDown(self):
        self._td.cleanup()

    def _hold_lock_in_child(self, path: Path, seconds: float) -> subprocess.Popen:
        """起一个子进程拿住 path 的跨进程锁, 等它打印 LOCKED 再返回。

        拿 stdout 的 LOCKED 做同步点, 而不是 sleep 一个拍脑袋的时长 ——
        否则慢机器上子进程还没拿到锁, 父进程就先试了, 测试会假通过。
        """
        script = self.tmp / "hold_lock.py"
        script.write_text(
            "import sys, time\n"
            f"sys.path.insert(0, {str(WEB)!r})\n"
            "import config_files\n"
            f"with config_files.cross_process_lock({str(path)!r}, timeout=5):\n"
            "    print('LOCKED', flush=True)\n"
            f"    time.sleep({seconds!r})\n",
            encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-u", str(script)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(REPO_ROOT))
        line = proc.stdout.readline().strip()
        self.assertEqual(line, "LOCKED", "子进程没能拿到锁")
        return proc


class CrossProcessLockTest(_TmpDirCase):
    """M3: 跨进程排他锁本身的行为。"""

    def test_lock_file_is_a_sidecar(self):
        """锁文件必须是独立文件。

        不能直接锁数据文件: 数据文件每次写入都被 os.replace 换掉, inode 变了,
        两边锁的就是不同 inode —— 看起来加了锁, 实际谁也没挡住谁。
        """
        self.assertEqual(config_files.lock_file_for(self.ledger),
                         self.tmp / "download_failures.json.lock")

    def test_same_thread_reentrant_does_not_self_deadlock(self):
        """同线程嵌套不能死锁。

        flock 是"按打开的文件描述"加锁的: 同一线程再 open 一次会拿到另一个描述,
        自己去抢自己那把锁 → 永久阻塞。所以重入只能靠线程本地计数短路。
        真死锁的话内层会在 timeout 后降级, 断言 notes 为空就会失败(而不是挂住)。
        """
        notes = []
        t0 = time.monotonic()
        with config_files.cross_process_lock(self.ledger, timeout=0.3,
                                             on_degraded=notes.append):
            with config_files.cross_process_lock(self.ledger, timeout=0.3,
                                                 on_degraded=notes.append):
                pass
        self.assertEqual(notes, [], "内层重入不应降级")
        self.assertLess(time.monotonic() - t0, 0.25)

    def test_excludes_another_process(self):
        """另一个进程持锁时, 本进程必须等不到 —— 这是 M3 的核心断言。"""
        proc = self._hold_lock_in_child(self.ledger, 1.5)
        try:
            notes = []
            t0 = time.monotonic()
            with config_files.cross_process_lock(self.ledger, timeout=0.3,
                                                 on_degraded=notes.append):
                pass
            elapsed = time.monotonic() - t0
            self.assertGreater(elapsed, 0.25, "没等就进去了 = 锁没生效")
            self.assertEqual(len(notes), 1, "拿不到锁必须回调一次 on_degraded")
            self.assertIn("超过", notes[0])
        finally:
            proc.communicate(timeout=20)

    def test_degrades_instead_of_failing(self):
        """拿不到锁时**不能抛异常**, 也不能让调用方失败。

        账本写不进只是少一条 UI 标记, 不该把一次 6GB 的下载判成失败。
        """
        proc = self._hold_lock_in_child(self.ledger, 1.0)
        try:
            notes = []
            with config_files.cross_process_lock(self.ledger, timeout=0.2,
                                                 on_degraded=notes.append):
                self.ledger.write_text("{}", encoding="utf-8")
            self.assertTrue(notes)
        finally:
            proc.communicate(timeout=20)

    def test_degraded_callback_runs_after_lock_released(self):
        """回调必须在**锁外**执行。

        本模块的硬约束: 持锁期间不许再拿别的锁。回调里通常要打日志, 而 app.log()
        要 app._lock —— 若在持锁时回调, 就是"json 锁 → app 锁"的经典环路死锁。
        """
        proc = self._hold_lock_in_child(self.ledger, 1.0)
        seen = []

        def on_degraded(msg):
            seen.append(msg)
            lk = config_files.file_lock(self.ledger)
            got = lk.acquire(blocking=False)
            lk.release()
            self.assertTrue(got, "回调时进程内锁仍被持有 —— 会与 app.log() 形成死锁")

        try:
            with config_files.cross_process_lock(self.ledger, timeout=0.2,
                                                 on_degraded=on_degraded):
                pass
        finally:
            proc.communicate(timeout=20)
        self.assertEqual(len(seen), 1)

    def test_serialises_threads_within_process(self):
        """进程内的线程也必须排队(跨进程锁同时包含线程锁)。"""
        order = []
        t0 = time.monotonic()

        def worker(tag, hold):
            with config_files.cross_process_lock(self.ledger, timeout=5):
                order.append((tag, time.monotonic() - t0))
                time.sleep(hold)

        a = threading.Thread(target=worker, args=("a", 0.3))
        b = threading.Thread(target=worker, args=("b", 0.0))
        a.start()
        time.sleep(0.05)
        b.start()
        a.join(10)
        b.join(10)
        self.assertEqual([t for t, _ in order], ["a", "b"])
        self.assertGreater(order[1][1] - order[0][1], 0.25)

    def test_released_when_body_raises(self):
        """体内抛异常也必须释放, 否则一次异常就把账本永久锁死。"""
        with self.assertRaises(RuntimeError):
            with config_files.cross_process_lock(self.ledger, timeout=0.5):
                raise RuntimeError("boom")
        notes = []
        with config_files.cross_process_lock(self.ledger, timeout=0.0,
                                             on_degraded=notes.append):
            pass
        self.assertEqual(notes, [], "异常后锁没释放")


class LedgerLockWiringTest(_TmpDirCase):
    """M3: 三个写方都必须真的接上了锁(源码契约)。"""

    EXPECTED = {
        "web/app.py": 1,
        "web/iso_runner.py": 4,
        "web/sync_subscriptions.py": 2,
    }

    def test_every_ledger_writer_takes_the_lock(self):
        for rel, least in self.EXPECTED.items():
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertGreaterEqual(
                src.count("cross_process_lock("), least,
                f"{rel} 里跨进程锁的调用点少于 {least} 处 —— 有账本写入漏了锁")

    def test_failure_record_is_not_lost_under_concurrency(self):
        """两个进程各写 25 条, 一条都不能丢。

        这是 M3 的回归测试: 修复前 50 条里常丢几条(读到旧快照再整体覆盖),
        修复后必须 50 条全在。
        """
        script = self.tmp / "hammer.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(WEB)!r})\n"
            "import iso_runner\n"
            f"data_dir = {str(self.tmp)!r}\n"
            f"for i in range(25):\n"
            "    iso_runner._record_failure(data_dir, 'linux', 'ubuntu',\n"
            "                               sys.argv[1] + '-%d.iso' % i, 'hard')\n",
            encoding="utf-8")
        procs = [subprocess.Popen([sys.executable, str(script), tag],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, cwd=str(REPO_ROOT))
                 for tag in ("a", "b")]
        for p in procs:
            _out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)

        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 50,
                         "并发写丢了记录: 期望 50 条, 实得 %d 条" % len(data))


# --------------------------------------------------------------------------- M4
NAME = "ubuntu-24.04.iso"
PAYLOAD = bytes(range(256)) * 16          # 4096 字节, 内容可辨认


class _Resp:
    """requests.get 的最小替身(只实现 failover 用到的那几个接口)。"""

    def __init__(self, status_code, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for c in self._chunks:
            yield c

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError("HTTP %d" % self.status_code)

    def close(self):
        self.closed = True


class _FakeDownloader:
    def __init__(self, verify_result=(True, "ok")):
        self.headers = {}
        self.verify_result = verify_result

    def verify_checksum_smart(self, filepath, checksum_url, stored, dist=None):
        return self.verify_result


class PartSourceIsolationTest(unittest.TestCase):
    """M4: 同一个 .part 不能被两个镜像的字节拼起来。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.filepath = self.dir / NAME
        self.part = self.dir / (NAME + ".part")

    def tearDown(self):
        self._td.cleanup()

    def _run(self, url, resp, verify=(True, "ok")):
        seen = {}

        def _get(u, **kw):
            seen["range"] = (kw.get("headers") or {}).get("Range")
            return resp

        with patch.object(iso_runner.requests, "get", _get):
            ok, _used = iso_runner._download_file_with_failover(
                _FakeDownloader(verify), {"checksum": ""}, [(url, "")],
                NAME, self.dir, self.filepath, head_total=len(PAYLOAD))
        return ok, seen

    def test_switching_source_restarts_from_zero(self):
        """半成品来自 A, 现在要下 B —— 必须不带 Range, 从头下。"""
        self.part.write_bytes(b"X" * 100)
        iso_runner._write_part_src(self.part, "https://a/" + NAME)

        ok, seen = self._run("https://b/" + NAME,
                             _Resp(200, {"Content-Length": str(len(PAYLOAD))},
                                   [PAYLOAD]))
        self.assertTrue(ok)
        self.assertIsNone(seen["range"], "换源不得续传: 请求里不该有 Range")
        self.assertEqual(self.filepath.read_bytes(), PAYLOAD,
                         "落盘内容必须是 B 的完整字节, 不能混入 A 的前缀")

    def test_same_source_still_resumes(self):
        """同一个源必须照旧续传 —— 否则每次断线都要重下 6GB。"""
        self.part.write_bytes(PAYLOAD[:100])
        iso_runner._write_part_src(self.part, "https://a/" + NAME)

        ok, seen = self._run(
            "https://a/" + NAME,
            _Resp(206, {"Content-Range": "bytes 100-4095/4096"},
                  [PAYLOAD[100:]]))
        self.assertTrue(ok)
        self.assertEqual(seen["range"], "bytes=100-")
        self.assertEqual(self.filepath.read_bytes(), PAYLOAD)

    def test_legacy_part_without_marker_is_not_destroyed(self):
        """升级前留下的 .part 没有来源标记。

        无法证明同源, 但也不能把用户已经下了一半的 6GB 直接删掉 ——
        保持旧行为(续传), 由最终 SHA256 兜底。
        """
        self.part.write_bytes(PAYLOAD[:100])
        self.assertFalse(iso_runner._part_src_path(self.part).exists())

        ok, seen = self._run(
            "https://a/" + NAME,
            _Resp(206, {"Content-Range": "bytes 100-4095/4096"},
                  [PAYLOAD[100:]]))
        self.assertTrue(ok)
        self.assertEqual(seen["range"], "bytes=100-")

    def test_marker_removed_after_landing(self):
        self.part.write_bytes(PAYLOAD[:100])
        iso_runner._write_part_src(self.part, "https://a/" + NAME)
        self._run("https://a/" + NAME,
                  _Resp(206, {"Content-Range": "bytes 100-4095/4096"},
                        [PAYLOAD[100:]]))
        self.assertFalse(iso_runner._part_src_path(self.part).exists(),
                         "落定后来源标记必须清掉, 否则污染下一次续传")

    def test_416_without_marker_is_not_landed_unverified(self):
        """L4: 416 把 .part 当完整文件, 但同源性无法证明且没有校验和兜底。

        这时不能落定成"未验签" —— 那等于把来源不明的内容标成"只是没验",
        用户会当它是好的。宁可丢弃重下。
        """
        self.part.write_bytes(PAYLOAD)
        ok, _seen = self._run("https://a/" + NAME, _Resp(416, {}),
                              verify=(None, "校验源不可达"))
        self.assertFalse(ok, "无法证明同源又验不了, 不该判成功")
        self.assertFalse(self.filepath.exists(), "不得落定成未验签文件")
        self.assertFalse(self.part.exists(), "不可信的半成品应被丢弃")

    def test_416_with_checksum_still_lands(self):
        """有校验和时照旧: 校验通过就说明内容是对的, 与来源无关。"""
        self.part.write_bytes(PAYLOAD)
        ok, _seen = self._run("https://a/" + NAME, _Resp(416, {}))
        self.assertTrue(ok)
        self.assertEqual(self.filepath.read_bytes(), PAYLOAD)


class PartSrcInventoryTest(unittest.TestCase):
    """M4: 来源标记不能被当成"过期文件"显示在 UI 上。"""

    def test_suffix_constants_match(self):
        self.assertEqual(iso_runner.PART_SRC_SUFFIX, app.PART_SRC_SUFFIX)
        self.assertEqual(app.PART_SRC_SUFFIX, ".part.src")

    def test_disk_inventory_skips_source_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ddir = root / "linux" / "ubuntu"
            ddir.mkdir(parents=True)
            (ddir / NAME).write_bytes(b"x")
            (ddir / (NAME + ".part")).write_bytes(b"y")
            (ddir / (NAME + ".part.src")).write_text("https://a/" + NAME,
                                                     encoding="utf-8")
            with patch.object(app, "DATA_DIR", root), \
                    patch.object(app, "load_storage_mode", lambda: "classified"):
                inv = app.disk_inventory()
            files = inv[("linux", "ubuntu")]
            names = sorted({f.get("partial_name") or f["name"] for f in files})
            self.assertNotIn(NAME + ".part.src", names)
            self.assertEqual(len(files), 2, "只应有一条完整文件 + 一条半成品: %r" % (files,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
