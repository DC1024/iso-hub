#!/usr/bin/env python3
"""v1.3.2 回归测试: 订阅同步进度条卡住。

用户报障: 手动下载完第一个文件后订阅 Arch 组做订阅同步, 进度条**一直卡在 50%**
不再变化, 且表头显示的是那个"已下载完成"的文件名(1.5 GB)。

两个独立缺陷叠加:

(A) **上报路径用错名字**(sync_subscriptions.py)
    订阅同步打印 `#TARGET <最终名> <大小>`, 但 download_linux.py 下载期间把字节
    写在 `<最终名>.part` 上, 完成后才 os.replace 成最终名。后端 running_task()
    对 targets 里的路径做 stat():
      * 文件还没开始下 → 最终名不存在 → size=0(进度恒 0%)
      * 该文件已下载完成 → 读到一个静止的完整大小 → 分子被垫高后不再变化
    实测 1.5GB(已完成) + 1.5GB(下载中) → 恰好卡在 50%。

(B) **聚合把已完成文件也算进来**(前端 poll)
    分子分母都含已完成的 1.5GB, 于是进度条先被垫到 50%, 之后真正在下载的文件
    再怎么增长也推不动指针。加上表头取"size 最大者", 于是一直显示已完成文件。

修复后: 上报 .part 路径; 聚合时把已完成文件从分子分母一起剔除。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
SYNC_SRC = (REPO_ROOT / "web" / "sync_subscriptions.py").read_text(encoding="utf-8")


class TestSyncTargetUsesPartPath(unittest.TestCase):
    """(A) 订阅同步的 #TARGET 必须上报 .part 路径。"""

    def test_imports_part_suffix(self):
        self.assertIn("PART_SUFFIX", SYNC_SRC, "必须从 download_linux 导入 PART_SUFFIX")
        self.assertIn("from download_linux import", SYNC_SRC)

    def test_target_line_uses_part_path(self):
        """回归护栏: 不得再出现 `#TARGET {_fp} ...`(最终名)。"""
        i = SYNC_SRC.index("for _e in keep_entries:")
        body = SYNC_SRC[i:i + 1600]
        self.assertIn("_part_fp", body, "应构造 .part 路径")
        self.assertIn('print(f"#TARGET {_part_fp}', body,
                      "上报的必须是 .part 路径")
        # 明确禁止旧写法
        self.assertNotIn('print(f"#TARGET {_fp}', body,
                         "不得上报最终名(旧 bug)")

    def test_completed_file_pinned_to_local_size(self):
        """已存在完整文件时把目标夹到本地大小, 让它在聚合里天然 100%。"""
        i = SYNC_SRC.index("for _e in keep_entries:")
        body = SYNC_SRC[i:i + 1600]
        self.assertIn("_fp.exists()", body)
        self.assertIn("_local", body)


class TestTrackedSizeFallback(unittest.TestCase):
    """(A2) running_task 的取大小逻辑: .part 不存在时回落到最终名。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_part_exists_prefers_part(self):
        part = self.d / "a.iso.part"
        part.write_bytes(b"x" * 100)
        (self.d / "a.iso").write_bytes(b"y" * 999)   # 旧的完整文件
        self.assertEqual(app._tracked_size(part), 100, "并存时以 .part 为准")

    def test_part_absent_falls_back_to_final(self):
        """.part 已被 os.replace 成最终名 → 进度应算满值, 而不是 0。"""
        final = self.d / "a.iso"
        final.write_bytes(b"y" * 500)
        self.assertEqual(app._tracked_size(self.d / "a.iso.part"), 500)

    def test_neither_exists_is_zero(self):
        self.assertEqual(app._tracked_size(self.d / "nope.iso.part"), 0)

    def test_non_part_missing_is_zero(self):
        """非 .part 路径不做回落, 缺失就是 0。"""
        self.assertEqual(app._tracked_size(self.d / "nope.iso"), 0)

    def test_used_by_running_task(self):
        src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        i = src.index("def running_task")
        body = src[i:i + 2200]
        self.assertIn("_tracked_size(p)", body,
                      "running_task 必须走 _tracked_size 而不是裸 stat")


class TestRunningTaskProgress(unittest.TestCase):
    """(A3) 端到端: 已完成 + 下载中 两个文件, 进度必须随 .part 增长而推进。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        d = self.data / "linux" / "Arch"
        d.mkdir(parents=True)
        self.f0901 = d / "archlinux-2026.09.01-x86_64.iso"
        self.p0801 = d / "archlinux-2026.08.01-x86_64.iso.part"
        self.f0901.write_bytes(b"x" * 1000)     # 已下载完成
        self.p0801.write_bytes(b"y" * 10)       # 下载中
        self._p = [patch.object(app, "DATA_DIR", self.data)]

    def tearDown(self):
        for x in self._p:
            x.stop()
        self._tmp.cleanup()

    def _state(self, targets):
        with patch.object(app, "task", {
            "proc": object(), "kind": "sync", "title": "订阅同步",
            "started": 0, "cancelled": False, "downloads": [], "targets": targets,
        }):
            return app.running_task()

    def test_sizes_track_part_growth(self):
        targets = {
            str(self.f0901) + ".part": 1000,   # 已完成(夹到本地大小)
            str(self.p0801): 1000,             # 下载中
        }
        for grow in (10, 400, 900):
            self.p0801.write_bytes(b"y" * grow)
            info = self._state(targets)
            sizes = [x["size"] for x in info["downloads"]]
            self.assertIn(grow, sizes, f".part 涨到 {grow} 应被 stat 到")
            self.assertIn(1000, sizes, "已完成文件应回落读到最终名的 1000")

    def test_completed_file_not_zero(self):
        """回归: 旧实现下已完成文件的 size 会是 0(因为查的是 .part 不存在)。"""
        targets = {str(self.f0901) + ".part": 1000}
        info = self._state(targets)
        self.assertEqual(info["downloads"][0]["size"], 1000)


class TestFrontendProgressAggregation(unittest.TestCase):
    """(B) 前端聚合: 已完成文件必须从分子分母一起剔除。"""

    def poll_body(self) -> str:
        i = HTML.index("if(tk.downloads&&tk.downloads.length){")
        return HTML[i:i + 1800]

    def test_completed_files_excluded(self):
        body = self.poll_body().replace(" ", "")
        self.assertIn("if(tot>0&&sz>=tot)return;", body,
                      "已完成文件应从本轮进度中剔除")

    def test_falls_back_to_100_when_all_done(self):
        body = self.poll_body().replace(" ", "")
        self.assertIn("total>0?Math.min(100,got/total*100):100", body,
                      "全部完成时应显示 100% 而非 0%")

    def test_current_file_prefers_unfinished(self):
        """表头应优先显示"未完成的那个", 而不是 size 最大的已完成文件。"""
        body = self.poll_body().replace(" ", "")
        self.assertIn("constunfinished=tk.downloads.filter(d=>!(d.total>0&&d.size>=d.total));", body)
        self.assertIn("constpickFrom=unfinished.length?unfinished:tk.downloads;", body)

    def test_no_longer_picks_global_max(self):
        """回归护栏: 不得再对全部 downloads 直接取 size 最大者。"""
        body = self.poll_body().replace(" ", "")
        self.assertNotIn("for(constdof tk.downloads){if(d.size>0", body)


class TestSyncScriptSyntax(unittest.TestCase):
    def test_sync_script_compiles(self):
        import py_compile
        py_compile.compile(str(REPO_ROOT / "web" / "sync_subscriptions.py"),
                           doraise=True)


if __name__ == "__main__":
    unittest.main()
