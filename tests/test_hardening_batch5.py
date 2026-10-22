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
ISO_DL = REPO_ROOT / "iso_download"
# iso_download 先入 path, 再把 WEB 顶到最前 —— web/ 里的模块名(app / iso_runner)
# 更容易被同名文件顶掉, 保持它优先。
sys.path.insert(0, str(ISO_DL))
sys.path.insert(0, str(WEB))

import app  # noqa: E402
import config_files  # noqa: E402
import download_linux as DL  # noqa: E402
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


# ============================================================== M5: 多源 failover
def _dist(**over):
    """构造一条最小清单条目; 默认就是"单源旧格式"。"""
    d = {
        "type": "linux",
        "distribution": "ubuntu",
        "download_url": "https://a/" + NAME,
        "checksum_url": "",
        "checksum": "",
    }
    d.update(over)
    return d


class CandidateSourcesTest(unittest.TestCase):
    """M5: 把清单条目展开成有序候选源。

    Web 侧一直有多源 failover, CLI 过去只认 download_url 一个地址 —— 主源挂掉
    就直接跳过, 同一个清单在两个入口行为不同。
    """

    def setUp(self):
        self.dl = DL.LinuxDistributionDownloader.__new__(DL.LinuxDistributionDownloader)

    def test_legacy_single_url_degrades_to_one_candidate(self):
        """没有 download_urls 的旧清单必须退化成单源, 与修复前完全一致。"""
        self.assertEqual(self.dl.candidate_sources(_dist()),
                         [("https://a/" + NAME, None)])

    def test_download_urls_order_is_priority(self):
        got = self.dl.candidate_sources(_dist(
            download_urls=["https://a/x", "https://b/x", "https://c/x"],
            checksum_urls=["https://a/S", "https://b/S", "https://c/S"]))
        self.assertEqual([u for u, _ in got],
                         ["https://a/x", "https://b/x", "https://c/x"])
        self.assertEqual([c for _, c in got],
                         ["https://a/S", "https://b/S", "https://c/S"])

    def test_pin_moves_source_and_its_checksum_to_front(self):
        """用户在 UI 上手动选过源 -> 置顶, 且 checksum_url 必须跟着一起搬。

        只搬 url 不搬 checksum, 会让置顶的源拿**别的镜像**的 SHA256SUMS 去验,
        结果是"校验不符"→ 把刚下好的文件删掉重来, 比不置顶还糟。
        """
        got = self.dl.candidate_sources(_dist(
            download_urls=["https://a/x", "https://b/x", "https://c/x"],
            checksum_urls=["https://a/S", "https://b/S", "https://c/S"],
            pin="https://c/x"))
        self.assertEqual([u for u, _ in got],
                         ["https://c/x", "https://a/x", "https://b/x"])
        self.assertEqual(got[0][1], "https://c/S")

    def test_pin_not_in_list_is_ignored(self):
        """pin 指向清单更新后已不存在的源 -> 忽略, 顺序不变。"""
        got = self.dl.candidate_sources(_dist(
            download_urls=["https://a/x", "https://b/x"],
            checksum_urls=[], pin="https://gone/x"))
        self.assertEqual([u for u, _ in got], ["https://a/x", "https://b/x"])

    def test_missing_checksum_urls_are_padded_with_none(self):
        """checksum_urls 比 download_urls 短时补 None, 不能错位。"""
        got = self.dl.candidate_sources(_dist(
            download_urls=["https://a/x", "https://b/x"],
            checksum_urls=["https://a/S"]))
        self.assertEqual(got, [("https://a/x", "https://a/S"), ("https://b/x", None)])


class FailoverTest(unittest.TestCase):
    """M5: 主源挂掉要能换源, 而不是整条跳过。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.dl = DL.LinuxDistributionDownloader.__new__(DL.LinuxDistributionDownloader)
        self.dl.download_dir = self.root
        self.dl.storage_mode = "classified"
        self.calls = []

    def tearDown(self):
        self._td.cleanup()

    def _drive(self, dist, statuses):
        """用假的 _download_from_url 驱动主流程, 记录它依次试了哪些源。"""
        self.dl.distributions = {"distributions": [dist]}
        seq = list(statuses)

        def _fake(target_dist, url, cs_url, dist_dir, filepath, filename, verify):
            self.calls.append(url)
            return seq.pop(0) if seq else "ok"

        self.dl._download_from_url = _fake
        return self.dl.download_distribution("ubuntu")

    def test_first_source_fails_second_succeeds(self):
        ok = self._drive(_dist(download_urls=["https://a/" + NAME, "https://b/" + NAME],
                               checksum_urls=["https://a/S", "https://b/S"]),
                         ["next", "ok"])
        self.assertTrue(ok, "换源成功后必须算下载成功")
        self.assertEqual(self.calls, ["https://a/" + NAME, "https://b/" + NAME])

    def test_all_sources_failing_returns_false(self):
        ok = self._drive(
            _dist(download_urls=["https://a/" + NAME, "https://b/" + NAME,
                                 "https://c/" + NAME]),
            ["next", "next", "next"])
        self.assertFalse(ok)
        self.assertEqual(len(self.calls), 3, "三个候选源都该被试过")

    def test_fatal_stops_without_trying_remaining_sources(self):
        """磁盘满这类失败换源也没用 —— 不该把剩下几个源再撞一遍。"""
        ok = self._drive(
            _dist(download_urls=["https://a/" + NAME, "https://b/" + NAME,
                                 "https://c/" + NAME]),
            ["fatal"])
        self.assertFalse(ok)
        self.assertEqual(self.calls, ["https://a/" + NAME])

    def test_single_source_behaviour_unchanged(self):
        """旧清单(单源)失败一次就结束 —— 与修复前完全一致。"""
        ok = self._drive(_dist(), ["next"])
        self.assertFalse(ok)
        self.assertEqual(self.calls, ["https://a/" + NAME])

    def test_existing_file_is_not_downloaded_again(self):
        """已存在且判定通过时不发起任何下载, 直接计成功。"""
        ddir = self.root / "linux" / "ubuntu"
        ddir.mkdir(parents=True)
        (ddir / NAME).write_bytes(PAYLOAD)
        self.dl._already_have = lambda *a, **k: True
        self.assertTrue(self._drive(_dist(), []))
        self.assertEqual(self.calls, [])


class UnverifiedLedgerCliTest(unittest.TestCase):
    """M5②: CLI 侧也要落 download_unverified.json, 且键必须与 Web 侧一致。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.dl = DL.LinuxDistributionDownloader.__new__(DL.LinuxDistributionDownloader)
        self.dl.download_dir = self.root

    def tearDown(self):
        self._td.cleanup()

    def _read(self):
        p = self.root / "download_unverified.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def test_cli_and_web_write_the_same_key(self):
        """同一个文件从 CLI 下和从 Web 下必须落同一个键。

        键不一致的后果不是报错, 而是 UI 上出现两条记录、徽标时有时无 ——
        正是"两个入口行为不同"这一类缺陷。
        """
        iso_runner._record_unverified(self.root, "linux", "ubuntu", NAME, "web 侧")
        self.dl._record_unverified(_dist(), NAME, "cli 侧")
        self.assertEqual(list(self._read()), ["linux/ubuntu/" + NAME])

    def test_cli_overwrites_web_record_on_same_key(self):
        """键相同才能互相覆盖, 否则会各占一条。"""
        iso_runner._record_unverified(self.root, "linux", "ubuntu", NAME, "web 侧")
        self.dl._record_unverified(_dist(), NAME, "cli 侧")
        self.assertEqual(self._read()["linux/ubuntu/" + NAME]["reason"], "cli 侧")

    def test_clear_removes_marker(self):
        self.dl._record_unverified(_dist(), NAME, "x")
        self.dl._clear_unverified(_dist(), NAME)
        self.assertEqual(self._read(), {}, "校验通过后必须撤掉徽标")

    def test_existing_file_with_unreachable_checksum_is_recorded(self):
        """校验源不可达: 文件保留不重下, 但必须落一条未验签。

        修复前 CLI 只打印一行 ⚠ 就当成功了 —— 于是同一个文件从 CLI 下没有徽标、
        从 Web 下有, 用户无法区分"验过"和"没验过"。
        """
        f = self.root / NAME
        f.write_bytes(PAYLOAD)
        self.dl.verify_checksum_smart = lambda *a, **k: (None, "校验源不可达")
        self.assertTrue(self.dl._already_have(f, _dist(), True),
                        "校验源不可达 ≠ 校验失败, 不得触发重下")
        rec = self._read()
        self.assertIn("linux/ubuntu/" + NAME, rec)
        self.assertEqual(rec["linux/ubuntu/" + NAME]["state"], "unverified")

    def test_existing_file_verified_clears_stale_marker(self):
        """上一轮没验成, 这一轮验过了 -> 徽标必须消失。"""
        f = self.root / NAME
        f.write_bytes(PAYLOAD)
        self.dl._record_unverified(_dist(), NAME, "上一轮没验成")
        self.dl.verify_checksum_smart = lambda *a, **k: (True, "校验通过")
        self.assertTrue(self.dl._already_have(f, _dist(), True))
        self.assertEqual(self._read(), {})

    def test_already_have_passes_dist_to_smart_verify(self):
        """校验必须带上 dist —— 没有它就拿不到该发行版自己的 checksum_url。

        test_gpg_dist_handoff 的模块注释里记着: 这段功能代码一直存在, 但生产
        路径从未传 dist, 所以从未生效过。这里把它钉住。
        """
        seen = {}
        dist = _dist(checksum_url="https://a/S")

        def _verify(filepath, checksum_url, stored, dist=None):
            seen["dist"] = dist
            seen["checksum_url"] = checksum_url
            return (True, "ok")

        f = self.root / NAME
        f.write_bytes(PAYLOAD)
        self.dl.verify_checksum_smart = _verify
        self.dl._already_have(f, dist, True)
        self.assertIs(seen["dist"], dist)
        self.assertEqual(seen["checksum_url"], "https://a/S")


class CleanupKeepsPartSourceTest(unittest.TestCase):
    """M4 收口: 清理过时文件时不能把 .part.src 单独删掉。

    .part 被保留、标记被删掉, 下一轮就会走"无标记 → 照旧续传"的旧分支,
    正好把 M4 堵上的跨源拼接重新打开。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.dl = DL.LinuxDistributionDownloader.__new__(DL.LinuxDistributionDownloader)
        self.dl.download_dir = self.root
        self.ddir = self.root / "linux" / "ubuntu"
        self.ddir.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_part_and_its_marker_survive_cleanup(self):
        # 注意真实文件名: part_src_path(part) 是 str(part) + ".part.src",
        # 而 part 本身已经以 .part 结尾, 所以半成品的标记是 x.iso.part.part.src。
        part = self.ddir / (NAME + ".part")
        marker = self.ddir / (NAME + ".part.part.src")
        part.write_bytes(b"x" * 10)
        marker.write_text("https://a/" + NAME, encoding="utf-8")
        self.dl.cleanup_distribution_dir(self.ddir, [NAME])
        self.assertTrue(part.exists())
        self.assertTrue(marker.exists(),
                        "标记被删 → 下一轮退化成无标记续传, 跨源拼接的洞又开了")

    def test_orphan_marker_is_cleaned(self):
        """半成品已经没了, 孤立的标记必须清掉, 否则永久堆积。"""
        marker = self.ddir / (NAME + ".part.part.src")
        marker.write_text("https://a/" + NAME, encoding="utf-8")
        self.dl.cleanup_distribution_dir(self.ddir, [NAME])
        self.assertFalse(marker.exists())

    def test_stale_iso_still_deleted(self):
        """别把清理逻辑改坏了: 真正过时的 ISO 必须照删。"""
        (self.ddir / "old.iso").write_bytes(b"x")
        self.dl.cleanup_distribution_dir(self.ddir, [NAME])
        self.assertFalse((self.ddir / "old.iso").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
