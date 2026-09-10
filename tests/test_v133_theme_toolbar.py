#!/usr/bin/env python3
"""v1.3.3 回归测试: 切换日/夜模式时工具栏位置漂移。

用户报障: 「一个是夜间模式一个是日间模式，工具条 ui 位置变化了」——两张截图里
同一排工具栏按钮在浅色模式下比深色模式**更宽**，导致整行被挤到换行/下沉。

根因(已在真实页面上用 Chrome 实测复现):
  主题按钮的图标在两套主题间换字形:
      浅色模式 → 🌙 U+1F319  (East Asian Width = **Wide**)
      深色模式 → ☀ U+2600   (East Asian Width = **Narrow/Neutral**)
  Wide 与 Neutral 的字符前进宽度不同, 于是同一个按钮在两种主题下宽度不同。
  实测(真实 index.html, 1400px 视口):
      .actions 宽度  浅色 757.91px / 深色 753.05px   → 相差 4.86px
      主题按钮宽度   浅色  47.86px / 深色  43.00px   → 相差 4.86px
      按钮行高度     浅色  36px    / 深色  37px
  工具栏本身贴着一行的换行边界, 这 ~4.9px 恰好让浅色模式多折一行, 视觉上就是
  "换主题后按钮整体挪位"。

修复(三道保险):
  1. 图标放进 `.icon-slot` —— 固定 width/height, 吸收字形宽度差;
  2. `.actions>button{flex:0 0 auto;white-space:nowrap}` —— 按钮不参与伸缩、不因内容换行;
  3. 按钮的 `title` 改为随语言翻译(此前写死 "theme", 中文界面悬浮提示是英文)。

修复后实测: 两种主题下 .actions 宽 754.98px / 高 36px / y 88.38px 完全一致。

本文件把这些结构性约束锁死, 防止未来有人把图标重新内联回按钮里。
"""

import re
import sys
import unittest
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


class TestEmojiWidthDiffersByTheme(unittest.TestCase):
    """先把"为什么会有这个 bug"的事实本身钉住: 两个图标宽度类别确实不同。

    如果哪天有人把图标换成同宽类别的一对(例如 ☀/☾ 都是中性宽度), 这条会失败,
    提醒我们"特殊的 .icon-slot 保护或许已经不再必要"——但那也应该是一次有意识的改动。
    """

    def test_crescent_moon_is_wide_but_sun_is_not(self):
        moon = "\U0001f319"  # 🌙 浅色模式图标
        sun = "\u2600"       # ☀ 深色模式图标
        self.assertEqual(unicodedata.east_asian_width(moon), "W",
                         "🌙 应为 East Asian Width=Wide(这是宽度差的根源)")
        self.assertNotEqual(unicodedata.east_asian_width(sun), "W",
                            "☀ 应为窄/中性宽度, 与 🌙 不同 —— 这正是 bug 的成因")

    def test_the_two_icons_really_have_different_advance_width(self):
        """宽度类别不同 ⇒ 渲染宽度不同。用 unicodedata 无法量像素, 这里只断言分类差异。"""
        moon = unicodedata.east_asian_width("\U0001f319")
        sun = unicodedata.east_asian_width("\u2600")
        self.assertNotEqual(moon, sun)


class TestIconSlotIsUsed(unittest.TestCase):
    """修复手段 1: 图标必须被 `.icon-slot` 包裹。"""

    def test_css_defines_icon_slot_with_fixed_size(self):
        self.assertIn(".icon-slot{", HTML)
        m = re.search(r"\.icon-slot\{([^}]*)\}", HTML)
        self.assertIsNotNone(m, "未找到 .icon-slot 规则")
        body = m.group(1)
        self.assertIn("width:1.15em", body, "图标槽必须有固定宽度, 否则吸收不了字形宽度差")
        self.assertIn("height:1em", body, "图标槽必须有固定高度")
        self.assertIn("flex:0 0 auto", body, "图标槽不得被 flex 拉伸/压缩")

    def test_theme_button_wraps_icon_in_slot(self):
        """主题按钮内部应是一个 #theme-icon 的 .icon-slot, 而不是裸字形。"""
        m = re.search(r'<button[^>]*id="btnTheme"[^>]*>(.*?)</button>', HTML, re.S)
        self.assertIsNotNone(m, "未找到 btnTheme 按钮")
        inner = m.group(1)
        self.assertIn('class="icon-slot"', inner,
                      "主题按钮的图标必须放在 .icon-slot 里, 否则宽度会随主题变化")
        self.assertIn('id="theme-icon"', inner,
                      "applyTheme 需要定位 #theme-icon 来只改字形、不改按钮")

    def test_theme_button_no_longer_holds_bare_glyph(self):
        """回归护栏: 按钮里除 .icon-slot 之外, 不得再有其他可见文本。

        判据: 把 .icon-slot 整块挖掉之后, 按钮内部应只剩空白。
        (不能简单"剥掉所有标签再看文本"——那会把槽内合法字形也算成裸文本。)
        """
        m = re.search(r'<button[^>]*id="btnTheme"[^>]*>(.*?)</button>', HTML, re.S)
        self.assertIsNotNone(m)
        inner = m.group(1)
        # 图标必须恰好包在一个 .icon-slot 的 span 里
        self.assertRegex(inner, r'<span class="icon-slot"[^>]*>[^<]+</span>',
                         "图标必须包在 .icon-slot 里")
        # 挖掉整个 icon-slot 后, 不应再剩任何可见字符
        outside = re.sub(r'<span class="icon-slot"[^>]*>.*?</span>', "", inner, flags=re.S)
        outside = re.sub(r"<[^>]+>", "", outside).strip()
        self.assertEqual(outside, "",
                         f"btnTheme 在 .icon-slot 之外还有裸文本 {outside!r}")


class TestActionsRowIsRigid(unittest.TestCase):
    """修复手段 2: 工具栏按钮不做弹性伸缩、不因内容换行。"""

    def test_actions_buttons_are_non_flexing(self):
        m = re.search(r"\.actions>button\{([^}]*)\}", HTML)
        self.assertIsNotNone(m, "未找到 .actions>button 规则 —— 缺失则按钮仍可能被内容撑变")
        body = m.group(1)
        self.assertIn("flex:0 0 auto", body)
        self.assertIn("white-space:nowrap", body)


class TestApplyThemeTargetsIconSlot(unittest.TestCase):
    """修复手段 1 的 JS 侧: applyTheme 只改字形, 并同步翻译 title。"""

    def test_apply_theme_prefers_icon_slot(self):
        m = re.search(r"function applyTheme\(theme\)\{(.*?)\n\}", HTML, re.S)
        self.assertIsNotNone(m, "未找到 applyTheme 定义")
        body = m.group(1).replace(" ", "").replace("\n", "")
        self.assertIn("querySelector('#theme-icon')", body,
                      "applyTheme 必须优先更新 #theme-icon(槽内字形), 而不是整个按钮")
        self.assertIn("||document.querySelector('#btnTheme')", body,
                      "需要保留对旧结构的回退, 避免局部升级时按钮失灵")

    def test_apply_theme_syncs_translated_title(self):
        body = HTML.replace(" ", "")
        self.assertIn("btn.title=t('theme')", body,
                      "主题按钮的悬浮提示应随语言翻译(此前写死 title='theme')")

    def test_theme_button_title_is_translatable(self):
        """按钮的 title 必须是可翻译的(data-i18n-title), 且不再写死英文占位。

        坑: 不能用 `'title="theme"' not in tag` 来判定 —— `data-i18n-title="theme"`
        本身就包含 `title="theme"` 这个子串, 会造成假失败。这里改用属性级匹配:
        逐个取出 title 属性, 断言它不等于旧的写死值 "theme"。
        """
        m = re.search(r'<button[^>]*id="btnTheme"[^>]*>', HTML, re.S)
        self.assertIsNotNone(m)
        tag = m.group(0)
        self.assertIn('data-i18n-title="theme"', tag,
                      "主题按钮的 title 必须走 data-i18n-title 以便翻译")
        # 取真正的 title 属性(要求 = 前不是 - 或其他字母)
        tm = re.search(r'(?<![\w-])title="([^"]*)"', tag)
        self.assertIsNotNone(tm, "btnTheme 应有 title 初始值")
        self.assertNotEqual(tm.group(1), "theme",
                            'title 不应再写死英文 "theme"; 应给一句中文默认值并交给 i18n 刷新')


class TestApplyLangHandlesTitle(unittest.TestCase):
    """data-i18n-title 需要真的被 applyLang 处理, 否则 title 永远不会更新。"""

    def test_apply_lang_handles_i18n_title(self):
        body = HTML.replace(" ", "")
        self.assertIn("data-i18n-title", body)
        # applyLang 里应存在对 dataset.i18nTitle 的处理
        self.assertRegex(HTML, r"i18nTitle",
                         "applyLang 未处理 data-i18n-title, title 翻译不会生效")


if __name__ == "__main__":
    unittest.main()
