#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""种子分类前端的契约测试。

项目没有 JS 测试框架(前端"测试"一贯是 Python 断言 HTML 字符串), 因此这里
把**能静态锁死的不变量**固定下来, 防的是"重构时把踩过的坑再踩一遍":

  1. 折叠 key 必须来自后端下发的 fold_key, 不许从 DOM 可见文本拼
     (首页那套 chip|gname 的拼法正是历史 bug 来源: 计数变化/切语言就丢状态)
  2. 种子分类用独立的 localStorage 键, 不污染首页折叠状态
  3. 新接口已接线, 且三个子标签/面板都在
  4. 未分类导出只取前 20 条
  5. i18n 字典里 t() 用到的键必须都存在(中英双语), 防 key 拼错静默显示成 key 名
  6. applyLang 对带 placeholder 的输入框改的是 placeholder 而不是 value
     (原实现只认 data-i18n-ph, 全项目没人用, 结果切语言会把提示文字灌进输入框)
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = REPO_ROOT / "web" / "static" / "index.html"
HTML = HTML_PATH.read_text(encoding="utf-8")

# 取出内联脚本(整页只有一块内联 script)
_blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", HTML, re.S)
JS = _blocks[0] if _blocks else ""

# 只看标签部分(排除 <script> 里的 I18N 字典), 避免字典里的中文干扰文案定位
MARKUP = HTML.split("<script>")[0]


def _fn(name: str) -> str:
    """按名字粗略切出一个 JS 函数的源码(取到下一个顶层 function 之前)。"""
    m = re.search(r"\n(?:async\s+)?function %s\(" % re.escape(name), JS)
    if not m:
        return ""
    rest = JS[m.start():]
    nxt = re.search(r"\n(?:async\s+)?function \w+\(", rest[1:])
    return rest if not nxt else rest[:nxt.start() + 1]


class TestWiring(unittest.TestCase):
    def test_endpoints_used(self):
        self.assertIn("/api/torrent/classify", JS)
        self.assertIn("/api/torrent/categories", JS)

    def test_categories_tab_and_panel_exist(self):
        self.assertIn('data-sub="cat"', HTML)
        self.assertIn('id="torr-cat"', HTML)
        self.assertIn("switchTorr('cat')", HTML)

    def test_switchttor_shows_cat_panel(self):
        body = _fn("switchTorr")
        self.assertIn("torr-cat", body)
        self.assertIn("loadTorrCats", body)

    def test_search_input_wired(self):
        self.assertIn('id="torr-search"', HTML)
        self.assertIn("torrSearchInput()", JS)

    def test_sources_flow_caches_items_before_classify(self):
        body = _fn("loadTorrentSources")
        self.assertIn("LAST_TORRENT_ITEMS", body)     # 改规则重排的数据源
        self.assertIn("classifyAndRender", body)

    def test_classify_failure_falls_back_to_flat_list(self):
        """分组失败不能让种子列表消失。"""
        body = _fn("classifyAndRender")
        self.assertIn("renderTorrItems(LAST_TORRENT_ITEMS)", body)
        self.assertIn("torrClassifyFail", body)


class TestFoldKeyContract(unittest.TestCase):
    """折叠 key 一律来自后端, 前端不拼字符串。"""

    def test_uses_backend_fold_key(self):
        self.assertIn("cat.fold_key", _fn("renderTorrGroups"))
        self.assertIn("data-fold-key", _fn("renderTorrGroups"))

    def test_toggle_reads_dataset_not_dom_text(self):
        body = _fn("toggleTcat")
        # key 的唯一来源必须是 dataset.foldKey(后端下发的值原样落到属性上)
        self.assertRegex(body, r"const key\s*=\s*head\.dataset\.foldKey")
        # 关键: 不许像首页 toggleGroupFold 那样从 .chip / .g-name 的文本拼 key
        self.assertNotIn(".chip", body)
        self.assertNotIn(".g-name", body)
        self.assertNotIn(".tcat-name", body)
        # 折叠箭头的字形(textContent='▸'/'▾')是允许的, 但要锁死它没有被
        # 反向拿来拼 key —— 即 key 变量不得由 textContent 参与赋值。
        self.assertNotRegex(body, r"key\s*(\+?=)[^;\n]*textContent")

    def test_fold_key_fallback_is_stable_id_not_text(self):
        """后端 fold_key 缺失时的兜底只能用稳定 id, 不能用可见文本。"""
        body = _fn("renderTorrGroups")
        self.assertRegex(body, r"cat\.fold_key\s*\|\|")
        # 兜底表达式里不许出现 name / textContent / querySelector
        m = re.search(r"const foldKey\s*=\s*([^;\n]+);", body)
        self.assertTrue(m, "foldKey 赋值未找到")
        fallback = m.group(1)
        for banned in ("name", "textContent", "innerHTML", "querySelector"):
            self.assertNotIn(banned, fallback, "foldKey 兜底用了 " + banned)

    def test_independent_localstorage_key(self):
        """不能和首页折叠共用键, 否则两边互相踩。"""
        self.assertIn("iso_hub_tcat_folded", JS)
        m = re.search(r"const FOLD_KEY='([^']+)'", HTML)
        self.assertTrue(m, "首页 FOLD_KEY 未找到")
        self.assertNotEqual(m.group(1), "iso_hub_tcat_folded")

    def test_language_switch_rerenders_without_losing_fold(self):
        """切语言必须重渲染分组(名字在后端载荷里), 但 key 不含本地化文本。"""
        self.assertIn("renderTorrGroups", _fn("applyLang"))
        self.assertIn("renderTorrCats", _fn("applyLang"))
        body = _fn("renderTorrGroups")
        self.assertNotIn("fold_key: 'tcat:' +", body)   # 不许本地拼 key
        self.assertNotIn('fold_key:"tcat:"+', body)


class TestExport(unittest.TestCase):
    def test_export_takes_top_20(self):
        body = _fn("torrExportUncat")
        self.assertIn("slice(0,20)", body.replace(" ", ""))

    def test_export_targets_uncategorized_only(self):
        body = _fn("torrExportUncat")
        self.assertIn("__uncategorized__", body)

    def test_export_button_only_on_uncategorized_group(self):
        body = _fn("renderTorrGroups")
        self.assertIn("isU&&items.length", body.replace(" ", ""))


class TestI18n(unittest.TestCase):
    """i18n 键必须齐全 —— 缺键会静默把 key 名显示给用户。"""

    def setUp(self):
        block = re.search(r"const I18N=\{(.*?)\n\};", HTML, re.S)
        self.assertTrue(block, "I18N 字典未找到")
        self.dict_src = block.group(1)
        # 注意: 正则只有一个捕获组, findall 返回的是字符串列表, 不能直接喂 dict()
        self.keys = set(re.findall(r"'([A-Za-z0-9_]+)':\{zh:", self.dict_src))
        self.assertTrue(len(self.keys) > 100, "I18N 解析异常, 仅取到 %d 个键" % len(self.keys))
        self.assertIn("torrCatTab", self.keys, "新键未进入解析结果")

    def test_used_keys_all_defined(self):
        used = set(re.findall(r"\bt\('([A-Za-z0-9_]+)'\)", JS))
        used |= set(re.findall(r'data-i18n="([A-Za-z0-9_]+)"', HTML))
        missing = sorted(used - self.keys)
        self.assertEqual(missing, [], "以下 i18n 键被使用但没有定义: %s" % missing)

    def test_new_category_keys_are_all_used(self):
        """本次新增的键不许只定义不使用(死文案)。

        不去做全量孤儿检测: 老代码里已有 57 个历史死键, 一次性断言必然红,
        只能靠白名单维持 —— 那种测试维护成本高于收益。这里只锁本次新增的。
        """
        used = set(re.findall(r"\bt\('([A-Za-z0-9_]+)'\)", JS))
        used |= set(re.findall(r'data-i18n="([A-Za-z0-9_]+)"', HTML))
        used |= set(re.findall(r'data-i18n-title="([A-Za-z0-9_]+)"', HTML))
        prefixes = ("torrCat", "torrUncat", "torrSearchPh", "torrNoMatch", "torrClassifyFail")
        mine = sorted(k for k in self.keys if k.startswith(prefixes))
        self.assertGreaterEqual(len(mine), 15, "新增键数量异常: %d" % len(mine))
        self.assertEqual(sorted(k for k in mine if k not in used), [],
                         "新增的 i18n 键定义了却没用上")

    def test_new_torrent_category_keys_bilingual(self):
        for k in ("torrCatTab", "torrCatTitle", "torrCatShowEmpty", "torrCatHint",
                  "torrCatUserTitle", "torrCatAdd", "torrCatSave", "torrCatSaveHint",
                  "torrCatNamePh", "torrCatKeywordPh", "torrCatExcludePh",
                  "torrSearchPh", "torrNoMatch", "torrUncatExport",
                  "torrUncatExported", "torrUncatExportFail", "torrClassifyFail",
                  "torrCatNoUser", "torrCatSaved", "torrCatSaveFail",
                  "torrCatPresetTitle"):
            self.assertIn("'%s':" % k, self.dict_src, "缺少 %s" % k)
        # 每个新键都必须同时有 en(防只写中文)
        for m in re.finditer(r"'([A-Za-z0-9_]+)':\{zh:'(?:[^']|\\')*'\}", self.dict_src):
            self.fail("i18n 键 %s 只有中文, 缺少 en" % m.group(1))

    def test_new_ui_text_is_i18n_backed(self):
        """新文案必须带 data-i18n 挂钩(不是"不许出现中文")。

        本项目惯例: HTML 里直接写中文当首屏默认值, 启动后由 applyLang 覆盖
        (如既有按钮 ``data-i18n="torrRssAdd">➕ 添加 RSS``)。真正要防的是
        **只有中文、没有 i18n 挂钩** —— 那种文案切到英文界面会原地不动。
        """
        for sample in ("🏷️ 种子分类", "显示空分类", "关键词按", "我的分类",
                       "新增分类", "保存分类设置", "保存后回到", "搜索种子标题"):
            idx = MARKUP.find(sample)
            self.assertGreaterEqual(idx, 0, "文案丢失: " + sample)
            lt = MARKUP.rfind("<", 0, idx)
            self.assertGreaterEqual(lt, 0, "文案不在标签内: " + sample)
            tag = MARKUP[lt:MARKUP.find(">", lt) + 1]
            self.assertIn("data-i18n=", tag,
                          "文案没有 i18n 挂钩, 切语言不会变: %s → %s" % (sample, tag[:110]))

    def test_classify_failure_toast_key_defined(self):
        """分类失败必须给用户可见反馈(而不是静默退回扁平列表)。"""
        self.assertIn("torrClassifyFail", JS)
        self.assertIn("'torrClassifyFail':", self.dict_src)


class TestI18nInputPlaceholder(unittest.TestCase):
    """applyLang 对输入框必须改 placeholder(而不是 value)。"""

    def test_applylang_prefers_placeholder(self):
        body = _fn("applyLang")
        self.assertIn("hasAttribute('placeholder')", body)
        self.assertIn("el.placeholder=v", body)

    def test_all_i18n_inputs_have_placeholder(self):
        for m in re.finditer(r"<(input|textarea)[^>]*data-i18n=\"[^\"]+\"[^>]*>", HTML):
            self.assertIn("placeholder=", m.group(0),
                          "带 data-i18n 的输入框必须同时有 placeholder: " + m.group(0)[:80])


class TestCategoriesEditor(unittest.TestCase):
    def test_collects_preset_overrides(self):
        body = _fn("collectTorrCats")
        self.assertIn("overrides", body)
        self.assertIn("data-preset", HTML)
        self.assertIn("cat-en", body)          # 启用开关

    def test_user_category_id_generated_client_side_with_prefix(self):
        body = _fn("addUserCat")
        self.assertIn("user_", body)

    def test_save_rerenders_without_refetch(self):
        body = _fn("saveTorrCats")
        self.assertIn("classifyAndRender", body)
        self.assertNotIn("loadTorrentSources", body)   # 不许重抓外网

    def test_show_empty_switch_present(self):
        self.assertIn('id="cat-show-empty"', HTML)
        self.assertIn("show_empty", _fn("collectTorrCats"))


if __name__ == "__main__":
    unittest.main()
