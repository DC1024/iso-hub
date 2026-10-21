#!/usr/bin/env python3
"""前端 innerHTML 插值必须转义 —— 2960 行单文件里埋着 30 处 innerHTML。

为什么需要这道闸:
  页面渲染的数据来源不止一个, 其中**发行版名、文件名、种子标题**都来自外部
  (镜像站清单、DistroWatch RSS、用户填的自定义源)。任何一个 `${x.y}` 忘了包 esc(),
  一条恶意构造的标题就能往页面里塞脚本 —— 而它在本地自测时完全看不出来,
  因为自己的镜像站当然不会返回带尖括号的名字。

  人工审计靠不住: 这次逐处看下来 30 处里 29 处是对的, 唯一漏的
  `data-status="${S}"` 恰好是后端下发的字段, 而且它当前取值可控所以一直没出事 ——
  这种"现在安全、将来不一定"的地方, 正是要靠测试钉住的。

判定口径:
  引用了服务端数据对象的属性、又没经过 esc/t/fmt/time2str 的插值一律算可疑,
  除非显式登记在 ALLOWLIST 里(登记时必须写清为什么安全)。
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HTML = REPO_ROOT / "web" / "static" / "index.html"

# 经过这些函数包裹即视为安全
SANITISERS = ("esc(", "t(", "fmt(", "time2str(", "String(", "shortSrc(")

# 服务端数据对象的常用变量名 —— 引用它们的属性即视为"可能来自外部"
DATA_VARS = r"(?:e|g|s|x|it|tk|c|f|u|p|row|item|src|cat)\s*\."

# 人工复核过、确认安全(数值 / 布尔 / 固定枚举)的裸插值。
# 加新条目必须同时写清理由, 否则这条闸就形同虚设。
ALLOWLIST = {
    "g.file_count": "后端下发的计数, 纯数字",
    "fmt(g.local_total)": "fmt() 输出纯数字 + 单位",
    "s.keep||2": "数字, 且有默认值兜底",
    "s.enabled?'checked':''": "布尔 -> 固定字符串",
    "s.enabled?'on':'btn-s'": "布尔 -> 固定样式类名",
    "s.last_run?(t('previouslyRun')+' '+time2str(s.last_run)):t('neverRun')":
        "时间戳经 time2str 纯数字化",
    "isFav(g.type,g.name)?'on':''": "布尔 -> 固定字符串",
    "isFav(g.type,g.name)?'★':'☆'": "布尔 -> 固定字符",
    "isFav(g.type,g.name)?t('unfavGroup'):t('favGroup')": "i18n 常量",
    "isSubscribed(g.type,g.name)?'on':''": "布尔 -> 固定字符串",
    "isSubscribed(g.type,g.name)?t('subTipOn'):t('subTipOff')": "i18n 常量",
    "isSubscribed(g.type,g.name)?('★ '+t('subscribed')):('☆ '+t('subscribe'))":
        "i18n 常量",
    "isFolded(g.type,g.name)?'▸':'▾'": "布尔 -> 固定字符",
    "isFolded(g.type,g.name)?'display:none':''": "布尔 -> 固定样式",
    "cls": "classifyLine() 的固定四值枚举(ok/warn/er/info)",
    "rows": "在上文逐字段 esc() 拼好的 HTML 片段",
    "strayHtml": "同上, 文件名已 esc()",
    "sizeCell": "本函数内构造, 只含 t() 与 fmt()",
    "i": "数组下标, 纯数字",
}

_INTERP = re.compile(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")


def _html_chunks():
    """取每条 innerHTML / insertAdjacentHTML 赋值语句(可能跨多行)。"""
    lines = HTML.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, 1):
        if "innerHTML" not in line and "insertAdjacentHTML" not in line:
            continue
        chunk, j = line, i
        while j < len(lines) and not re.search(r"[;`]\s*$", chunk.splitlines()[-1]) \
                and j - i < 20:
            chunk += "\n" + lines[j]
            j += 1
        yield i, chunk


def _suspicious():
    out = []
    for lineno, chunk in _html_chunks():
        exprs = [m.group(1).strip() for m in _INTERP.finditer(chunk)]
        # 字符串拼接形式: '+ expr +'
        exprs += [m.group(1).strip()
                  for m in re.finditer(r"\+\s*([^+\n][^\n+]*?)\s*\+", chunk)]
        for raw in exprs:
            expr = re.sub(r"\s+", " ", raw).strip()
            if not expr or expr[0] in "'\"`":
                continue
            # 拼接形式的正则会把三元表达式从中间的 '+' 切开, 括号不配对即残片
            if expr.count("(") != expr.count(")") or expr.count("[") != expr.count("]"):
                continue
            if any(s in expr for s in SANITISERS):
                continue
            if not re.search(DATA_VARS, expr) and not re.search(
                    r"\b(" + "|".join(re.escape(k) for k in ALLOWLIST) + r")\b", expr):
                continue
            if expr in ALLOWLIST:
                continue
            out.append((lineno, expr))
    return out


class TestInnerHtmlEscaping(unittest.TestCase):
    def test_no_unescaped_data_interpolation(self):
        bad = _suspicious()
        self.assertEqual(
            bad, [],
            "以下 innerHTML 插值引用了服务端数据却没有转义:\n"
            + "\n".join(f"  行 {n}: {e}" for n, e in bad)
            + "\n\n要么包 esc(), 要么确认安全后登记进 test_frontend_escaping.ALLOWLIST "
              "并写明理由。")

    def test_allowlist_entries_are_few_and_documented(self):
        """允许清单不该无限膨胀 —— 条目变多说明该考虑换个渲染方式了。"""
        self.assertLessEqual(len(ALLOWLIST), 25)
        for k, why in ALLOWLIST.items():
            self.assertTrue(why.strip(), f"{k} 缺登记理由")

    def test_esc_escapes_every_html_significant_char(self):
        """esc() 本身得真转义五个字符, 少一个就是可绕过的洞。"""
        text = HTML.read_text(encoding="utf-8")
        m = re.search(r"const esc\s*=\s*s\s*=>\s*(.{0,300}?);", text, re.S)
        self.assertIsNotNone(m, "找不到 esc() 实现")
        body = m.group(1)
        for ch in ("&", "<", ">", '"', "'"):
            self.assertIn(ch, body, f"esc() 没有处理 {ch!r}")

    def test_scan_finds_something(self):
        """防止正则失效后上面的断言假通过。"""
        chunks = list(_html_chunks())
        self.assertGreaterEqual(len(chunks), 25, "扫到的 innerHTML 太少, 正则可能坏了")


if __name__ == "__main__":
    unittest.main()
