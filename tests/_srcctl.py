# -*- coding: utf-8 -*-
"""源码级契约测试的取材助手。

v1.7.0 把 54 条路由从 web/app.py 拆到 web/api/*.py 后，一批"用 assertIn 钉住
源码字面量"的测试开始失效 —— 它们只读 app.py，而字面量已经搬到路由模块里。
这类测试**不能删**：它们钉的是真实接线（子进程有没有带 --storage-mode、
downloads.path 是不是 .part 名），一旦扫描面落空，断言就退化成空转，
测试照样绿但什么都没守住。

所以统一从这里取"内核 + 路由层"的合并文本：
  source_tree()             -> app.py + web/api/*.py 全部源码拼接
  route_layer_files()       -> [(相对路径, 源码), ...] 便于按文件断言

注意：拼接文本里每个文件之间插了分隔注释，别指望跨文件的 assertIn 能匹配到
相邻两行；本意是让"整个代码库里出现过这个字面量"这类断言继续有效。
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
API_DIR = WEB_DIR / "api"


def route_layer_files():
    """返回 [(相对仓库根的路径, 源码文本)]，顺序：app.py 在前，api/*.py 按名排序。"""
    out = []
    main = WEB_DIR / "app.py"
    if main.is_file():
        out.append(("web/app.py", main.read_text(encoding="utf-8")))
    if API_DIR.is_dir():
        for f in sorted(API_DIR.glob("*.py")):
            out.append((f"web/api/{f.name}", f.read_text(encoding="utf-8")))
    return out


def source_tree():
    """app.py + web/api/*.py 的合并源码，供 `assertIn` 式契约测试使用。"""
    return "\n\n".join(f"# ==== {rel} ====\n{src}" for rel, src in route_layer_files())


def count_in_tree(needle: str) -> int:
    """字面量在整棵路由层里出现的次数。"""
    return source_tree().count(needle)
