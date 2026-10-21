#!/usr/bin/env python3
"""API 文档必须与代码里的路由对得上。

此前项目没有 API 文档 —— 想知道某个按钮发了什么请求只能去读 4236 行 app.py,
或者开浏览器抓包。现在 wiki/API.md 列了全部路由, 但文档会腐烂: 加一个路由忘写
文档, 几个月后就成了"文档里没有的接口到底存不存在"。

双向钉住:
  1. 代码里的每个路由, 文档里必须出现
  2. 文档表格里写的每个路由, 代码里必须真的存在(防止删了路由文档还留着)
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_PY = REPO_ROOT / "web" / "app.py"
API_MD = REPO_ROOT / "wiki" / "API.md"

ROUTE_RE = re.compile(r'@app\.(get|post|put|delete)\("([^"]+)"')
# 文档表格里的路由行: `| GET | `/api/xxx` | ... |` 或 `| GET / POST | `/api/xxx` | ... |`
# (方法列是纯文本, 路径列才带反引号 —— 按实际排版写, 别照着想象改正则)
DOC_ROW_RE = re.compile(r"\|\s*([A-Z][A-Z/ ]*?)\s*\|\s*`([^`]+)`\s*\|")


def _code_routes() -> set:
    text = APP_PY.read_text(encoding="utf-8")
    return {(m.group(1).upper(), m.group(2)) for m in ROUTE_RE.finditer(text)}


def _doc_routes() -> set:
    text = API_MD.read_text(encoding="utf-8")
    out = set()
    for methods, path in DOC_ROW_RE.findall(text):
        if not path.startswith("/"):
            continue
        for m in re.split(r"\s*/\s*", methods.strip()):
            if m in ("GET", "POST", "PUT", "DELETE"):
                out.add((m, path))
    return out


class TestApiDocsCoverRoutes(unittest.TestCase):
    def test_every_route_is_documented(self):
        missing = sorted(_code_routes() - _doc_routes())
        self.assertEqual(
            missing, [],
            "以下路由在 app.py 里存在但 API.md 没写:\n  "
            + "\n  ".join(f"{m} {p}" for m, p in missing))

    def test_docs_do_not_invent_routes(self):
        ghosts = sorted(_doc_routes() - _code_routes())
        self.assertEqual(
            ghosts, [],
            "以下路由写在 API.md 里但 app.py 中不存在:\n  "
            + "\n  ".join(f"{m} {p}" for m, p in ghosts))

    def test_route_count_is_sane(self):
        """防止正则失效后两个集合一起变空、上面的断言假通过。"""
        self.assertGreaterEqual(len(_code_routes()), 40)
        self.assertGreaterEqual(len(_doc_routes()), 40)

    def test_auth_header_is_documented(self):
        """认证头是 X-Auth-Token 而非 Bearer, 写错的人一律 401 还以为是服务坏了。"""
        text = API_MD.read_text(encoding="utf-8")
        self.assertIn("X-Auth-Token", text)
        self.assertIn("Bearer", text, "应明说不是 Bearer, 否则照着写的人必踩坑")


if __name__ == "__main__":
    unittest.main()
