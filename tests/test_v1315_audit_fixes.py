#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.3.15 审计跟进: 共享凭据明文提示 + PART_SUFFIX 三方契约。

来源: 一次外部代码评审(8 条)。经逐行核实后本轮只采纳两条**零风险**项:

  (1) settings.json 明文存密 —— **不**改存储方式。
      评审建议的 "改用 Docker secrets" 会直接废掉现有功能:
      secrets 是只读挂载、应用无法回写, 而当前设计恰恰依赖
      「网页改共享密码 → 覆盖 compose env」。所以只在共享设置页
      加一条用户可见提示。
      这里锁两件事: 提示必须带 data-i18n 挂钩, 且中英双语齐全
      —— 否则切到英文界面提示原地不动, 或干脆把 key 名显示给用户。

  (2) _entry_status 用字符串拼 part_rel, 与 runner 上报的 #TARGET 路径构成
      跨模块字符串契约; 而同一个 PART_SUFFIX 被三处重复定义
      (iso_download/download_linux.py、web/app.py、web/iso_runner.py),
      只靠注释要求人工保持一致。历史上正是这个契约被破坏, 才出过
      「下载中误报成下载停止」。
      做法: **不动运行时**。把 app.py 改成 import download_linux 看似干净,
      但 download_linux 模块级 `from tqdm import tqdm` + `import requests`,
      轻装环境(只装 web/requirements.txt)会 ImportError → 整个服务起不来;
      (旁证: web/sync_subscriptions.py 在 import 它之前专门 mock 掉 tqdm)
      改用测试把三方钉在一起, 并且**用 runner 自己的 _safe_dist_dir 推导路径**,
      而不是在测试里重抄一遍目录布局 —— 重抄的测试挡不住布局改动。
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402
import iso_runner  # noqa: E402
from iso_download.download_linux import PART_SUFFIX as UPSTREAM_PART_SUFFIX  # noqa: E402

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- (1) 明文提示


class TestSharePlaintextHint(unittest.TestCase):
    """共享设置页必须有「凭据明文存储」提示, 且可被语言切换覆盖。"""

    HINT_KEYS = ("sharePlainWarn",)

    def setUp(self):
        m = re.search(r"const I18N=\{(.*?)\n\};", HTML, re.S)
        self.assertTrue(m, "I18N 字典未找到")
        self.dict_src = m.group(1)

    def test_hint_element_carries_i18n_hook(self):
        """提示文案必须挂在 data-i18n 上(切语言才会跟着变)。"""
        self.assertIn('data-i18n="sharePlainWarn"', HTML,
                      "共享设置页缺少明文存储提示, 或提示没有 i18n 挂钩")

    def test_hint_lives_in_share_card(self):
        """提示要在「共享设置」卡片内, 而不是别处 —— 否则用户看不到。"""
        i_hint = HTML.find('data-i18n="sharePlainWarn"')
        i_card = HTML.find('data-i18n="sharesConfig"')
        i_desc = HTML.find('data-i18n="shareDesc"')
        self.assertGreater(i_card, 0, "找不到共享设置卡片")
        self.assertGreater(i_desc, 0, "找不到共享设置说明")
        self.assertGreater(i_hint, i_desc,
                           "提示应排在共享设置说明之后(同属该卡片尾部)")
        # 卡片尾部与下一张卡片之间不应跨出太多(粗略边界: 2000 字符内)
        self.assertLess(i_hint - i_desc, 2000, "提示离共享设置太远, 可能挂错卡片")

    def test_key_defined_for_both_languages(self):
        for k in self.HINT_KEYS:
            self.assertIn("'%s':{zh:" % k, self.dict_src, "缺少 i18n 键 " + k)
        # 只写中文、漏了 en 的键(本项目历史上出现过)一律拦下
        for m in re.finditer(r"'([A-Za-z0-9_]+)':\{zh:'(?:[^']|\\')*'\}", self.dict_src):
            self.fail("i18n 键 %s 只有中文, 缺少 en" % m.group(1))

    def test_hint_says_where_credentials_live(self):
        """提示必须点明落盘位置, 否则用户无从判断影响面。"""
        seg = self.dict_src[self.dict_src.find("'sharePlainWarn':"):]
        seg = seg[:seg.find("\n")]
        self.assertIn("settings.json", seg, "提示未说明凭据存于 settings.json")
        self.assertIn("明文", seg, "提示未说明是明文存储")


# --------------------------------------------------------------------------- (2) PART_SUFFIX 契约


class PartContractTestBase(unittest.TestCase):
    """DATA_DIR 一律用 resolve() 后的路径。

    runner 侧 _safe_dist_dir() 返回的是 resolve() 结果, 而 app 侧是裸拼接;
    两者要在**同一形态**下比较才有意义(线上 Linux 上本来就没有这条差异)。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name).resolve()
        (self.data / "linux" / "Arch").mkdir(parents=True)
        self.key = ("linux", "Arch")
        self.fname = "archlinux-2026.09.01-x86_64.iso"
        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.data / "distributions.json"),
            patch.object(app, "SETTINGS_JSON", self.data / "settings.json"),
            patch.object(app, "FAILURES_JSON", self.data / "download_failures.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    # -- 用 runner 自己的代码推导路径(关键: 不在测试里重抄布局) --
    def runner_part_path(self) -> Path:
        dist_dir = iso_runner._safe_dist_dir(self.data, self.key[0], self.key[1])
        self.assertIsNotNone(dist_dir, "runner 拒绝了这个 (type, name)")
        filepath = dist_dir / self.fname
        return Path(str(filepath) + iso_runner.PART_SUFFIX)

    def app_index(self, fname: str) -> str:
        """app 侧拼法(与 _entry_status 内 part_rel 同构)。

        注意这里用的是 app.PART_SUFFIX、runner_part_path() 用的是
        iso_runner.PART_SUFFIX —— 两侧各自取自己模块的常量, 而不是在测试里
        另抄一个常量, 否则测试会跟着"被抄的那个"一起走偏。
        """
        return str(self.data / self.key[0] / self.key[1] / (fname + app.PART_SUFFIX))


class TestPartSuffixDefinitionsAgree(PartContractTestBase):
    """三处重复定义的常量必须同值 —— 任一处分叉都会让状态判定静默失效。"""

    def test_three_modules_share_one_value(self):
        self.assertEqual(
            app.PART_SUFFIX, iso_runner.PART_SUFFIX,
            "web/app.py 与 web/iso_runner.py 的 PART_SUFFIX 已分叉")
        self.assertEqual(
            app.PART_SUFFIX, UPSTREAM_PART_SUFFIX,
            "web/app.py 与 iso_download/download_linux.py 的 PART_SUFFIX 已分叉")

    def test_value_is_the_expected_suffix(self):
        """值本身也要锁死: 三处一起改成别的后缀同样是事故。"""
        self.assertEqual(UPSTREAM_PART_SUFFIX, ".part")


class TestRunnerLayoutMatchesAppIndex(PartContractTestBase):
    """runner 推导出的 .part 路径必须能被 app 的活跃集合命中。"""

    def test_string_forms_are_identical(self):
        """线上(Linux) 比较的就是字符串, 这里直接比字符串。"""
        self.assertEqual(str(self.runner_part_path()), self.app_index(self.fname),
                         "runner 与 app 拼出的 .part 路径不一致(跨模块契约被改坏)")

    def test_relative_layout_is_type_name_filename(self):
        """锁住相对布局: <download_dir>/<type>/<name>/<file>.part"""
        rel = self.runner_part_path().relative_to(self.data)
        self.assertEqual(rel, Path(self.key[0]) / self.key[1] / (self.fname + ".part"))

    def test_active_path_leads_to_downloading(self):
        """端到端: runner 上报的路径进 active_paths 后, app 必须判 downloading。"""
        (self.data / self.key[0] / self.key[1] / (self.fname + app.PART_SUFFIX)).write_bytes(b"x" * 2048)
        fake_task = {
            "proc": object(),  # 任意真值 = 任务在跑
            "targets": {str(self.runner_part_path()): {"size": 2048}},
            "downloads": [],
        }
        with patch.object(app, "task", fake_task):
            active = app.active_download_paths()
        local = {"name": self.fname, "partial": True, "size": 2048}
        status, size = app._entry_status(self.key, self.fname, local, {}, frozenset(active))
        self.assertEqual(status, "downloading",
                         "runner 的 #TARGET 路径没被 app 认出来 —— 会退回"
                         "「下载停止」误报(v1.3.1 的老问题)")
        self.assertEqual(size, 2048)

    def test_disk_inventory_reports_the_same_partial_name(self):
        """磁盘扫描出的半成品名也必须还原成同一个 fname(不然查表miss)。"""
        (self.data / self.key[0] / self.key[1] / (self.fname + app.PART_SUFFIX)).write_bytes(b"x" * 512)
        inv = app.disk_inventory()
        names = [f["name"] for f in inv.get(self.key, [])]
        self.assertIn(self.fname, names, "半成品没被还原成目标名: %s" % names)


class TestContractTestHasTeeth(PartContractTestBase):
    """反向对照: 布局或后缀一变, 上面的断言必须真的会红。"""

    def test_wrong_layout_is_not_matched(self):
        """漏掉 type 这一层的路径 + 正确的活跃集合 → 绝不能判成 downloading。"""
        (self.data / self.key[0] / self.key[1] / (self.fname + app.PART_SUFFIX)).write_bytes(b"x" * 16)
        bogus = str(self.data / self.key[1] / (self.fname + app.PART_SUFFIX))  # 少了 linux/
        local = {"name": self.fname, "partial": True, "size": 16}
        status, _ = app._entry_status(self.key, self.fname, local, {}, frozenset({bogus}))
        self.assertEqual(status, "partial",
                         "错误布局的路径竟被当成活跃 —— 说明这个契约测试没有牙")

    def test_other_suffix_is_not_matched(self):
        """后缀换了(比如 .part.tmp)就不该命中, 保证后缀参与比较。"""
        (self.data / self.key[0] / self.key[1] / (self.fname + app.PART_SUFFIX)).write_bytes(b"x" * 16)
        wrong = str(self.runner_part_path()) + ".tmp"
        local = {"name": self.fname, "partial": True, "size": 16}
        status, _ = app._entry_status(self.key, self.fname, local, {}, frozenset({wrong}))
        self.assertEqual(status, "partial")

    def test_runner_rejects_traversal(self):
        """契约的另一半: runner 侧目录拼接必须继续拒绝穿越。"""
        self.assertIsNone(iso_runner._safe_dist_dir(self.data, "linux", ".."))
        self.assertIsNone(iso_runner._safe_dist_dir(self.data, "linux", "a/b"))
        self.assertIsNone(iso_runner._safe_dist_dir(self.data, "bogus", "Arch"))


class TestPlatformNoteForStringCompare(unittest.TestCase):
    """说明为什么字符串比较在这套测试里成立(留个记录, 不做事)。"""

    def test_posix_expectation_documented(self):
        # app._entry_status 用的是 `part_rel in active_paths` 字符串比较;
        # 生产是 Linux, 两侧都由同一 DATA_DIR 拼出, 没有大小写/短名差异。
        # 本套测试用 resolve() 后的 DATA_DIR, 因此 Windows 上也能得到
        # 与生产一致的字符串形态。若哪天 runner 改成返回未 resolve 的路径,
        # TestRunnerLayoutMatchesAppIndex.test_string_forms_are_identical 会先红。
        self.assertIn(os.name, ("posix", "nt"))
