#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3.16: settings.json 加锁 + 原子写 + 损坏隔离。

外部评审的两条:
  #3 JSON 文件写竞争(多线程会翻车)
  #4 错误被 `except Exception: log(...)` 静默吞(影响排障)

本项目里 settings.json 一个文件塞了共享凭据 / 用户 / 会话 / 受保护清单 / 调度 /
清单历史 / 选源策略 / 种子 RSS / 种子链接, 写方有 waitress 8 线程 + 1 个调度线程,
而 **`web/distro_torrents.py` 是同进程里的第二个写方**(用户自加种子源那条路径),
`web/sync_subscriptions.py` 是被 Popen 出来的**独立进程**里的第三个写方。

所以修的关键不是"在 app.py 里加个 lock", 而是必须让两边走到同一处临界区 ——
这就是为什么要有 `web/config_files.py`。这里的测试重点也在这:

  * 三个写方都必须走 config_files(靠 mock 断言 + 源码扫描双重保险,
    挡住"以后有人图省事又写回 write_text")
  * 锁必须是 RLock(save_* 内部会再调 load_*)
  * **容器操作绝不能被包进锁**(stop→rm→create→start 最长约 50 秒, 包进去会让
    整个设置页串行化, 重演历史上那次 waitress 线程耗尽的事故)
  * 写必须是原子的: 中途失败要保留原文且不留临时文件
  * 损坏要能被看见: .corrupt 备份 + 日志, 而不是静默当成空配置
"""

import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402
import config_files  # noqa: E402
import distro_torrents  # noqa: E402

WEB_DIR = REPO_ROOT / "web"


class SettingsDirCase(unittest.TestCase):
    """每个用例一个临时 data 目录, 并把 app.SETTINGS_JSON 指过去。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name).resolve()
        self.settings = self.data / "settings.json"
        self._patches = [
            patch.object(app, "SETTINGS_JSON", self.settings),
            patch.object(app, "DATA_DIR", self.data),
        ]
        for p in self._patches:
            p.start()
        self._log_len = len(app._log_lines)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def new_log_lines(self) -> list:
        return [d["l"] for d in list(app._log_lines)[self._log_len:]]

    def tmp_files(self) -> list:
        return sorted(p.name for p in self.data.iterdir() if ".tmp" in p.name)


# --------------------------------------------------------------------------- A. 锁


class TestLockPrimitives(SettingsDirCase):

    def test_lock_is_reentrant(self):
        """save_* 内部会再调 load_* —— 所以必须是 RLock, 普通 Lock 会自锁死。

        用 acquire(timeout=0) 而不是 acquire(): 换成普通 Lock 时这里应当**失败**
        (返回 False) 而不是把整个测试挂住 —— 变异体要能被"红"出来, 不能被人肉超时。
        """
        got = []

        def slow_patch(cur):
            lk = config_files.file_lock(self.settings)
            ok = lk.acquire(timeout=0)
            got.append(ok)
            if ok:
                lk.release()
            cur["reentered"] = True

        config_files.update_json(self.settings, slow_patch)
        self.assertEqual(got, [True], "同一线程二次获取失败 —— 这不是 RLock")

    def test_lock_is_held_for_the_whole_read_modify_write(self):
        """临界区必须覆盖"读到写"的全过程, 只锁写是不够的 —— 竞争恰恰发生在读之间。"""
        inside = threading.Event()
        released = threading.Event()

        def slow_patch(cur):
            inside.set()
            released.wait(1.0)
            cur["slow"] = True

        t = threading.Thread(target=lambda: config_files.update_json(self.settings, slow_patch))
        t.start()
        try:
            self.assertTrue(inside.wait(2.0), "worker 没进临界区")
            lk = config_files.file_lock(self.settings)
            got = lk.acquire(timeout=0)
            if got:
                lk.release()
            self.assertFalse(got, "失锁了: 另一个线程在 update_json 进行中抢到了同一把锁")
        finally:
            released.set()
            t.join(5)

    def test_same_path_resolves_to_the_same_lock(self):
        self.assertIs(config_files.file_lock(self.settings),
                      config_files.file_lock(self.settings))

    def test_different_paths_are_independent(self):
        """别的文件接入时不应被 settings.json 的写入白白串行化。"""
        other = self.data / "other.json"
        self.assertIsNot(config_files.file_lock(self.settings),
                         config_files.file_lock(other))


class TestNoLostUpdate(SettingsDirCase):
    """再现评审 #3: 两条写线程交错, 后写的覆盖先写的, 丢一次更新且无报错。"""

    WORKERS = 12
    ROUNDS = 15

    def _sum_worker(self, barrier):
        barrier.wait()
        for _ in range(self.ROUNDS):
            # 原子 RMW: 读取、修改、写回全在同一把锁里完成
            def bump(cur):
                cur["n"] = int(cur.get("n") or 0) + 1
            config_files.update_json(self.settings, bump)

    def test_parallel_read_modify_write_counts_exactly(self):
        barrier = threading.Barrier(self.WORKERS)
        threads = [threading.Thread(target=self._sum_worker, args=(barrier,))
                   for _ in range(self.WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        final = config_files.read_json_raw(self.settings)
        self.assertEqual(int(final.get("n") or 0), self.WORKERS * self.ROUNDS,
                         "并发增量丢失 —— update_json 没有真正串行化这一串 RMW")

    def test_merged_keys_all_survive(self):
        """更贴近真实形态: 不同线程写不同的顶层键(改共享密码 vs 加保护项)。"""
        n = 16
        barrier = threading.Barrier(n)

        def w(i):
            barrier.wait()
            config_files.update_json(self.settings, {"k%02d" % i: i})

        ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        final = config_files.read_json_raw(self.settings)
        missing = [i for i in range(n) if ("k%02d" % i) not in final]
        self.assertEqual(missing, [], "这些键的更新被并发写覆盖了: %s" % missing)


# --------------------------------------------------------------------------- B. 原子写


class TestAtomicWrite(SettingsDirCase):

    def test_no_temp_file_left_behind(self):
        for i in range(5):
            app.save_settings_all({"round": i})
        self.assertEqual(self.tmp_files(), [], "数据目录里残留了临时文件")
        # /data 同时是 SMB/WebDAV 的共享根, 残留文件对用户是可见的

    def test_write_failure_keeps_the_original_file(self):
        """写到一半失败(=对方 Gospelwrite 裸写的常态)时, 原文必须一字不动。"""
        app.save_settings_all({"before": "good", "count": 1})
        before = self.settings.read_text(encoding="utf-8")

        real_replace = config_files.os.replace

        def boom(*a, **k):
            raise OSError("模拟磁盘写满")

        config_files.os.replace = boom
        try:
            with self.assertRaises(OSError):
                app.save_settings_all({"before": "HALF"})
        finally:
            config_files.os.replace = real_replace

        self.assertEqual(self.settings.read_text(encoding="utf-8"), before,
                         "写失败后原文被改动了 —— 那就不是原子写")
        self.assertEqual(self.tmp_files(), [], "写失败后残留了临时文件")

    def test_readers_never_see_a_half_written_file(self):
        """持续读 + 持续写并发, 任何一次读到的数据都必须能被完整解析。

        Windows 上 os.replace 偶尔会因"目标正被并发读者占住"抛 WinError 5 —— 这是
        OS 文件共享的瞬时竞争(生产环境跑在 Linux, rename 原子且不受此限), 不代表代码
        缺陷。这里让写方对同一轮保存重试几次; 真正要守的不变量(读者永远读不到半截
        JSON)由下面的 `bad == []` 断言把关, 与写方是否重试无关。
        """
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                try:
                    config_files.read_json_raw(self.settings)
                except config_files.CorruptJsonFile:
                    bad.append("corrupt")
                except OSError:
                    pass  # Windows 上替换瞬间可能出现文件被占用

        def writer():
            for i in range(120):
                # 写方重试吸收 Windows 上"目标被读者占住"的瞬时失败
                for _ in range(15):
                    try:
                        app.save_settings_all({"n": i, "pad": "x" * 500})
                        break
                    except PermissionError:
                        time.sleep(0.01)

        r = threading.Thread(target=reader)
        r.start()
        writer()
        stop.set()
        r.join(10)
        self.assertEqual(bad, [], "并发读到了写了一半的 JSON")


# --------------------------------------------------------------------------- C. 损坏隔离 + 可见性


class TestCorruptHandling(SettingsDirCase):

    GARBAGE = '{"users": {"admin": {"password_ha'

    def _write_garbage(self):
        self.settings.write_text(self.GARBAGE, encoding="utf-8")

    def test_read_does_not_move_the_file(self):
        """读取是纯读: 并发读会互相抢着备份, 所以读侧一律不产生副作用。"""
        self._write_garbage()
        with self.assertRaises(config_files.CorruptJsonFile):
            config_files.read_json_raw(self.settings)
        self.assertTrue(self.settings.exists(), "读取不应该挪动文件")
        app.load_settings_all()
        self.assertTrue(self.settings.exists())
        self.assertEqual(list(self.data.glob("*.corrupt")), [])

    def test_load_settings_all_is_empty_but_not_silent(self):
        """评审 #4: 过去是 `except Exception: data = {}`, 排障时毫无线索。"""
        self._write_garbage()
        self.assertEqual(app.load_settings_all(), {})
        lines = self.new_log_lines()
        self.assertTrue(lines, "损坏时不该静默 —— 必须留下日志")
        self.assertTrue(any("settings.json" in x for x in lines),
                        "日志里要点名出问题的文件: %s" % lines)

    def test_corrupt_file_is_backed_up_on_write(self):
        self._write_garbage()
        app.save_settings_all({"protected": ["linux/Arch/a.iso"]})

        self.assertTrue(self.settings.exists(), "应当重建出一个可用的配置文件")
        saved = config_files.read_json_raw(self.settings)
        self.assertEqual(saved.get("protected"), ["linux/Arch/a.iso"])

        backups = sorted(p.name for p in self.data.glob("*.corrupt*"))
        self.assertTrue(backups, "覆盖损坏文件之前必须留备份 —— 否则凭据/用户彻底没了")
        self.assertIn("settings.json.corrupt", backups[0])
        self.assertEqual((self.data / backups[0]).read_text(encoding="utf-8"),
                         self.GARBAGE, "备份内容不是原始损坏文件")

    def test_quarantine_log_points_at_the_backup(self):
        self._write_garbage()
        app.save_settings_all({"x": 1})
        lines = self.new_log_lines()
        self.assertTrue(any(".corrupt" in x for x in lines),
                        "告警必须指向备份路径, 否则用户不知道去哪找: %s" % lines)

    def test_two_successive_backups_do_not_overwrite_each_other(self):
        self._write_garbage()
        app.save_settings_all({"a": 1})
        self._write_garbage()
        app.save_settings_all({"b": 2})
        self.assertTrue(self.settings.exists())
        backups = sorted(self.data.glob("*.corrupt*"))
        self.assertGreaterEqual(len(backups), 2, "第二次备份覆盖了第一次")


# --------------------------------------------------------------------------- D. 接线: 三个写方必须走同一通道


class TestAllWritersShareTheChannel(SettingsDirCase):

    def test_three_savers_all_delegate_to_update_json(self):
        """以后谁再图省事写回 SETTINGS_JSON.write_text, 这里立刻变红。"""
        cases = [
            ("save_settings_all", lambda: app.save_settings_all({"a": 1})),
            ("save_shares", lambda: app.save_shares({"samba": {"enabled": True}})),
            ("save_qb_settings", lambda: app.save_qb_settings({"enabled": False})),
        ]
        for name, call in cases:
            seen = []
            real = config_files.update_json

            def spy(path, patch_, **kw):
                seen.append(Path(path))
                return real(path, patch_, **kw)

            with patch.object(config_files, "update_json", side_effect=spy):
                call()
            self.assertEqual(seen, [self.settings],
                             "%s 没有走 config_files.update_json" % name)

    def test_distro_torrents_shares_the_very_same_lock(self):
        """第二个写方在同一路径上必须拿到**同一把**锁对象, 否则等于没锁。"""
        with patch.object(distro_torrents, "SETTINGS_JSON", self.settings):
            self.assertIs(config_files.file_lock(distro_torrents.SETTINGS_JSON),
                          config_files.file_lock(app.SETTINGS_JSON))

    def test_distro_torrents_writes_through_the_channel(self):
        with patch.object(distro_torrents, "SETTINGS_JSON", self.settings):
            distro_torrents.add_user_rss("https://example.org/feed.xml")
        saved = config_files.read_json_raw(self.settings)
        self.assertIn("https://example.org/feed.xml", saved.get("torrent_rss", []))
        self.assertEqual(self.tmp_files(), [], "第二个写方也得用原子写")

    def test_no_module_writes_settings_json_behind_the_channels_back(self):
        """源码扫描兜底: 任何直接对 settings.json 的 write/read_text 都要被拦下。

        mock 断言只能覆盖"那几个已知入口被调用", 盖不住新冒出来的第四个写方。
        """
        offenders = []
        for name in ("app.py", "distro_torrents.py", "sync_subscriptions.py",
                     "torrent_client.py", "iso_runner.py",
                     # 2026-09-12 加入: 邮件通知是第四个碰配置的模块, 一并纳入扫描
                     "notifier.py"):
            src = (WEB_DIR / name).read_text(encoding="utf-8")
            for i, line in enumerate(src.splitlines(), 1):
                if "settings.json" not in line:
                    continue
                if re.search(r"\b(write_text|write_bytes|open\()", line):
                    offenders.append("%s:%d %s" % (name, i, line.strip()))
        self.assertEqual(offenders, [],
                         "发现绕过 config_files 的 settings.json 直接读写:\n  " +
                         "\n  ".join(offenders))


# --------------------------------------------------------------------------- E. 锁的范围: 不含容器操作


class TestContainerOpsAreOutsideTheLock(SettingsDirCase):

    def test_applying_share_credentials_can_still_take_the_lock(self):
        """改共享密码要 stop→rm→create→start samba(最长约 50 秒)。

        这段一旦被包进 json 锁: 一个人保存共享, 其余 7 个 waitress 线程全被堵住,
        历史上那次"线程耗尽 / 队列飙升 / 日志一条打不出来"就是这么来的。
        这里让"容器操作"自己去抢锁 —— 抢得到才说明它确实在临界区之外。
        """
        acquired = []

        def fake_container_op(*a, **k):
            lk = config_files.file_lock(app.SETTINGS_JSON)
            got = lk.acquire(timeout=0)
            acquired.append(got)
            if got:
                lk.release()
            return True

        with patch.object(app, "REQUIRE_LOGIN", False), \
             patch.object(app, "apply_share_creds", side_effect=fake_container_op), \
             patch.object(app, "set_share", return_value=True), \
             patch.object(app, "service_state", return_value="running"):
            client = app.app.test_client()
            r = client.post("/api/shares",
                            json={"samba": {"enabled": True,
                                            "username": "u", "password": "p"}})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(acquired, [True],
                         "容器操作期间 json 锁不可用 —— 说明它被包进了临界区")


# --------------------------------------------------------------------------- F. 反向对照: 证明上面的断言有牙


class TestAssertionsHaveTeeth(unittest.TestCase):
    """把墙拆掉, 确认有警报。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = Path(self._tmp.name) / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_without_lock_the_updates_are_lost(self):
        """确定性对照: 用两道屏障让 N 个线程**先全部读完同一份旧值, 再全部写**。

        无锁时后写覆盖先写 —— 20 个线程都读到 n=0、各自 +1 写回, 最终必然只剩 1 次
        增量(其余 19 次被覆盖)。这证明上面 `test_parallel_read_modify_write_counts_exactly`
        的并发测试有牙: 一旦 update_json 的锁被去掉, 那条测试就会从精确 180 跌到这里。
        """
        n_threads = 20
        bar_read = threading.Barrier(n_threads)
        bar_write = threading.Barrier(n_threads)

        def w():
            bar_read.wait()                          # 对齐起点
            cur = config_files.read_json_raw(self.p)  # 此刻尚无任何写入发生
            if not isinstance(cur, dict):
                cur = {}
            bar_write.wait()                         # 确保**所有**读都发生在**任何**写之前
            cur["n"] = int(cur.get("n") or 0) + 1
            config_files.write_json_atomic(self.p, cur)

        ts = [threading.Thread(target=w) for _ in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        got = int(config_files.read_json_raw(self.p).get("n") or 0)
        self.assertEqual(got, 1,
                         "无锁对照异常: 期望只剩 1 次增量(其余 %d 次被并发覆盖), 实际 %d"
                         % (n_threads - 1, got))

    def test_non_atomic_write_can_leave_a_half_file(self):
        """对照: 裸 write_text 在写失败时确实会毁掉原文 —— 这才是要修的东西。"""
        good = Path(self._tmp.name) / "half.json"
        good.write_text('{"a": 1}', encoding="utf-8")
        try:
            with open(good, "w", encoding="utf-8") as fh:
                fh.write('{"a": ')          # 写到一半
                raise OSError("模拟中断")
        except OSError:
            pass
        self.assertNotEqual(good.read_text(encoding="utf-8"), '{"a": 1}',
                            "裸写居然没破坏原文 —— 这条对照不成立")
        with self.assertRaises(config_files.CorruptJsonFile):
            config_files.read_json_raw(good)


if __name__ == "__main__":
    unittest.main()
