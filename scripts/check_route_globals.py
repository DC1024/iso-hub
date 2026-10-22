#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 web/api/*.py 里有没有残留的内核裸名字。

codemod 只改写了它认出来的内核符号；漏一个不会在 import 期报错，要等真有人
请求那条路由才 NameError 500（v1.7.0 第一次跑就漏了 TORRENT_AVAILABLE）。
本脚本把"路由模块里 Load 但未在本模块绑定"的名字全部列出来，逐个对照内核清单，
凡是在内核里存在、又没写成 core.X 的，就是漏网。

用法: python scripts/check_route_globals.py
退出码: 0 干净 / 1 有残留
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "web" / "app.py"
API_DIR = ROOT / "web" / "api"

BUILTINS = set(dir(__builtins__)) | {"__file__", "__name__", "__doc__"}


def module_scope_names(tree):
    """模块作用域里绑定的名字（不下钻函数体）。"""
    names = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for a in stmt.names:
                names.add(a.asname or a.name.split(".")[0])
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                names.add(n.id)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
    return names


def local_bindings(fn):
    """函数体内绑定的名字（参数、赋值、for 目标、with as、except as、嵌套 def、import）。"""
    bound = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
            bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
    return bound


def main():
    kernel = module_scope_names(ast.parse(APP.read_text(encoding="utf-8")))
    problems = []
    for f in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        own = module_scope_names(tree)
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            bound = local_bindings(fn) | own | BUILTINS
            for n in ast.walk(fn):
                if not isinstance(n, ast.Name) or not isinstance(n.ctx, ast.Load):
                    continue
                if n.id in bound:
                    continue
                if n.id in kernel:
                    problems.append((f.name, fn.name, n.lineno, n.id))

    if problems:
        print(f"发现 {len(problems)} 处残留内核裸名（应写成 core.<名字>）：")
        for fname, fn, ln, name in problems:
            print(f"  web/api/{fname}:{ln}  {fn}()  ->  {name}")
        return 1
    print("路由层无残留内核裸名 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
