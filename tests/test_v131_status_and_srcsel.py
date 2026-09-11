#!/usr/bin/env python3
"""v1.3.1 回归测试: 「下载中」状态 + 镜像源下拉框默认「自动」。

覆盖两个用户实际报障:
  (1) 正在下载的文件在列表里显示成「下载停止」——因为状态完全由磁盘推断,
      而"正在下载"与"被中断"都表现为一个 .part 文件。修复: build_distros 把
      运行中任务的活跃 .part 路径快照传给 _entry_status, 命中则判 downloading。
  (2) 文件级"下载优先级"下拉框默认选中了配置里的主源(清华), 而非「自动」——
      因为 srcSelHtml 用 `u===e.download_url ? 'selected'` 选中了主源。
      修复: 默认选「自动」, 只有显式 pin 才选中某源, 并给主源加 "(主源)" 标注。

另外锁定 v1.3.1 的版本号与前端契约。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402
from iso_download.download_linux import PART_SUFFIX  # noqa: E402

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


class DataDirTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        (self.data / "linux" / "Arch").mkdir(parents=True)
        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.data / "distributions.json"),
            patch.object(app, "SETTINGS_JSON", self.data / "settings.json"),
            patch.object(app, "FAILURES_JSON", self.data / "download_failures.json"),
            patch.object(app, "AUTO_SYNC_LAST", {"t": 0}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def part_path(self, fname: str) -> str:
        return str(self.data / "linux" / "Arch" / (fname + PART_SUFFIX))


class TestActiveDownloadPaths(DataDirTestBase):
    """active_download_paths(): 只在任务运行时回报活跃路径, 且涵盖 targets/downloads。"""

    def test_idle_task_returns_empty(self):
        """没有运行中的进程 → 空集合(避免把残留 .part 当下载中)。"""
        with patch.object(app, "task", {"proc": None, "targets": {}, "downloads": []}):
            self.assertEqual(app.active_download_paths(), set())

    def test_running_task_reports_targets_and_downloads(self):
        """运行中: targets 的键 + downloads[].path 都应出现(去重后)。"""
        fake = {
            "proc": object(),  # 任意真值即视为运行中
            "targets": {"/data/linux/Arch/a.iso": {"size": 1}},
            "downloads": [{"path": "/data/linux/Arch/a.iso"},
                          {"path": "/data/linux/Arch/b.iso"},
                          {"path": None}],
        }
        with patch.object(app, "task", fake):
            got = app.active_download_paths()
        self.assertEqual(got, {"/data/linux/Arch/a.iso", "/data/linux/Arch/b.iso"})

    def test_targets_may_be_list(self):
        """targets 若为列表(旧/未来形态)也不能崩。"""
        with patch.object(app, "task", {"proc": object(), "targets": [],
                                        "downloads": []}):
            self.assertEqual(app.active_download_paths(), set())


class TestDownloadingStatus(DataDirTestBase):
    """_entry_status 的 downloading 判定必须最先且准确。"""

    def setUp(self):
        super().setUp()
        self.key = ("linux", "Arch")
        self.fname = "archlinux-2026.09.01-x86_64.iso"
        # 磁盘上只有一个半成品(与真实场景一致)
        self._p = self.data / "linux" / "Arch" / (self.fname + PART_SUFFIX)
        self._p.write_bytes(b"x" * 4096)
        self.local = {"name": self.fname, "partial": True, "size": 4096}

    def test_part_in_active_paths_is_downloading(self):
        """.part 正被写入 → downloading(而不是 partial/stopped)。"""
        ap = frozenset({self.part_path(self.fname)})
        status, size = app._entry_status(self.key, self.fname, self.local, {}, ap)
        self.assertEqual(status, "downloading")
        self.assertEqual(size, 4096, "应带出当前已下载字节数")

    def test_part_not_active_is_partial(self):
        """同一个半成品, 任务没在跑 → 仍是可续传的 partial(旧的"下载停止"语义)。"""
        status, _ = app._entry_status(self.key, self.fname, self.local, {}, frozenset())
        self.assertEqual(status, "partial")

    def test_downloading_beats_failure_record(self):
        """即便历史上有 stopped 失败记录, 只要此刻在下载就该报 downloading。"""
        failures = {f"linux/Arch/{self.fname}": {"kind": "stopped"}}
        ap = frozenset({self.part_path(self.fname)})
        status, _ = app._entry_status(self.key, self.fname, self.local, failures, ap)
        self.assertEqual(status, "downloading")

    def test_completed_file_wins_over_active(self):
        """完整文件已落盘时, 即使路径在活跃集合里也是 downloaded(完成优先)。"""
        complete = {"name": self.fname, "partial": False, "size": 999}
        ap = frozenset({self.part_path(self.fname)})
        status, _ = app._entry_status(self.key, self.fname, complete, {}, ap)
        self.assertEqual(status, "downloaded")

    def test_other_files_active_does_not_flip_this_one(self):
        """活跃集合里有别的文件, 不应把本文件判成 downloading。"""
        ap = frozenset({self.part_path("some-other.iso")})
        status, _ = app._entry_status(self.key, self.fname, self.local, {}, ap)
        self.assertEqual(status, "partial")

    def test_active_path_uses_exact_data_dir_form(self):
        """活跃路径必须由 DATA_DIR 拼出, 否则线上永远匹配不上(静默失效)。"""
        with patch.object(app, "DATA_DIR", self.data):
            expect = str(self.data / "linux" / "Arch" / (self.fname + PART_SUFFIX))
        self.assertIn(expect, self.part_path(self.fname))


class TestBuildDistrosWiring(DataDirTestBase):
    """build_distros 必须真的把活跃路径接进 _entry_status, 并输出 pin 字段。"""

    def test_entry_carries_pin_field(self):
        (self.data / "distributions.json").write_text(json.dumps({
            "updated_at": 0,
            "distributions": [{
                "distribution": "Arch", "type": "linux", "category": "Arch",
                "download_url": "https://mirrors.tuna.tsinghua.edu.cn/archlinux/x.iso",
                "pin": "https://mirrors.ustc.edu.cn/archlinux/x.iso",
            }]
        }), encoding="utf-8")
        out = app.build_distros()
        # build_distros 返回 {"updated_at":..., "groups":[{name,type,entries:[...]}]}
        entries = [e for g in out["groups"] for e in g.get("entries", [])]
        hit = [e for e in entries if e.get("distribution") == "Arch"]
        self.assertTrue(hit, "应能找到 Arch 条目")
        self.assertEqual(hit[0].get("pin"),
                         "https://mirrors.ustc.edu.cn/archlinux/x.iso")

    def test_build_distros_reads_active_paths(self):
        """build_distros 内部要调用 active_download_paths (否则状态永远不含 downloading)。"""
        src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        i = src.index("def build_distros")
        body = src[i:i + 1200]
        self.assertIn("active_download_paths()", body)
        self.assertIn("active_paths", body)


class TestFrontendDownloadingContract(unittest.TestCase):
    """前端必须真的认识 downloading 状态, 否则后端算了也白算。"""

    def test_i18n_keys_present(self):
        for k in ("'downloading'", "'downloadingTip'"):
            self.assertIn(k, HTML, f"缺少 i18n key {k}")

    def test_row_handles_downloading(self):
        i = HTML.index("const isRun=")
        body = HTML[i:i + 500]
        self.assertIn("'downloading'", body, "isRun 需按 downloading 判定")

    def test_badge_run_style_exists(self):
        self.assertIn(".badge.run", HTML, "缺少 .badge.run 样式")
        self.assertIn("@keyframes runpulse", HTML, "缺少脉冲动画")

    def test_row_filter_accepts_downloading(self):
        """筛选映射与删除勾选都要涵盖 downloading, 否则会出现勾不上/筛不出。"""
        self.assertIn("downloading:['downloading']", HTML.replace(" ", ""))
        self.assertIn("['downloading','downloaded','partial','stopped','failed']",
                      HTML.replace(" ", "").replace('"', "'"))


class TestMirrorDropdownDefault(unittest.TestCase):
    """srcSelHtml: 默认必须选中「自动」, 主源只是标注, pin 才选中。"""

    def srcsel_body(self) -> str:
        i = HTML.index("function srcSelHtml(")
        return HTML[i:i + 900]

    def test_default_is_empty_value(self):
        """未 pin 时默认选 value='' 的「自动」项。"""
        body = self.srcsel_body()
        compact = body.replace(" ", "")
        self.assertIn("!pin?'selected'", compact,
                      "默认应按 pin 为空来选「自动」")
        self.assertIn("value=\"\"" .replace(" ", ""), compact,
                      "「自动」项的 value 必须是空串")

    def test_no_longer_selects_by_download_url(self):
        """回归护栏: 不得再出现 `u===e.download_url?...selected` 这种默认选中主源的写法。"""
        body = self.srcsel_body()
        compact = body.replace(" ", "")
        self.assertNotIn("u===e.download_url?'selected'", compact)
        self.assertNotIn('u===e.download_url?"selected"', compact)
        self.assertNotIn("u===e.download_url?\"selected\"", body.replace(" ", ""))

    def test_primary_source_is_labeled_not_selected(self):
        """主源要带 "(主源)" 文案, 但只有 pin 命中才 selected。"""
        body = self.srcsel_body()
        self.assertIn("srcPrimary", body, "主源应加标注")
        self.assertIn("isPin", body)

    def test_single_source_still_hidden(self):
        """只有一个源时仍不渲染下拉框(保持原行为)。"""
        body = self.srcsel_body()
        self.assertIn("urls.length<2", body.replace(" ", ""))


class TestVersionBumped(unittest.TestCase):
    def test_app_version_is_current(self):
        self.assertIn("'1.3.12'", HTML)
        self.assertNotIn("'1.2.9'\n", HTML.split("APP_VERSION")[1][:40])


if __name__ == "__main__":
    unittest.main()
