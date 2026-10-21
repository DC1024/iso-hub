#!/usr/bin/env python3
"""P1 守卫: index.html 的行内事件处理器里禁止出现动态拼接的 esc() 值。

背景(审查发现): 旧写法 onclick="fn('...esc(x)...')" 里的 esc() 只做 HTML 实体
转义(' -> &#39;), 而浏览器**编译行内 JS 之前**会先解码属性实体 —— 单引号字符串
照样被 RSS 标题/抓取文件名中的一个 ' 截断, 即任意 JS 执行(会话 token 在
localStorage, 等于管理员会话被窃取)。

正确模式: 动态值放 data-* 属性(esc() 在双引号 HTML 属性上下文是安全的),
按钮带 data-act, 由 document 级委托监听器分发(见 index.html 顶部 dispatcher)。

本测试静态扫描所有 on\\w+="..." 属性段, 断言其中不再出现 esc( / ${ 拼接。
"""

import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "web" / "static" / "index.html") \
    .read_text(encoding="utf-8")


class NoEscInInlineHandlersTest(unittest.TestCase):
    def test_no_dynamic_values_inside_inline_event_handlers(self):
        offenders = []
        # 匹配 on<event>="..." 属性段(不含跨行, 本文件的行内属性都在单行内)
        for m in re.finditer(r'on[a-z]+\s*=\s*"([^"]*)"', HTML):
            body = m.group(1)
            if "esc(" in body or "${" in body:
                offenders.append(body[:120])
        self.assertEqual(
            offenders, [],
            "行内事件处理器里出现动态拼接值(esc()/${...}) —— "
            "实体解码发生在 JS 编译之前, 单引号字符串可被截断(XSS)。"
            "请改用 data-* 属性 + document 级委托分发(data-act)。")

    def test_delegation_dispatcher_exists(self):
        """委托分发器必须在: 新按钮靠它路由, 没有它 data-act 按钮全部失效。"""
        self.assertIn("button[data-act]", HTML)
        self.assertIn("input[data-change-act]", HTML)
        for act in ("'protect'", "'dlfile'", "'delone'", "'prune'", "'subscribe'",
                    "'unprotect'", "'rssrm'", "'linkrm'", "'torradd'", "'torrdel'",
                    "'delSched'", "'srcEdit'", "'srcRepoDel'", "'srcDel'"):
            self.assertIn(f"case {act}:", HTML, f"缺少分发分支 {act}")


if __name__ == "__main__":
    unittest.main()
