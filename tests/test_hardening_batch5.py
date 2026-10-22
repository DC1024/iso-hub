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

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"
sys.path.insert(0, str(WEB))

import config_files  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
