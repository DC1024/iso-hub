#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路由层拆分（v1.7.0）的结构守卫。

为什么单独一个测试文件：拆 Blueprint 出过的两类事故都不会让现有用例变红 ——

1. **打桩静默失效**：视图搬走后若写成 `from app import log`，`patch.object(app,"log")`
   改的是 app 的全局，视图读的是自己模块的副本。1022 个用例照绿，桩没打上。
2. **内核裸名残留**：codemod 漏改写一个符号（v1.7.0 真漏了 TORRENT_AVAILABLE），
   import 期不报错，要等真有人请求那条路由才 NameError 500。

这两类都靠静态扫描钉住，不依赖跑通业务逻辑。
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


class TestRouteLayerHygiene(unittest.TestCase):
    """scripts/ 下的两个静态检查必须通过。"""

    def _run(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            encoding="utf-8", errors="replace")

    def test_no_bare_kernel_names_in_routes(self):
        """路由模块里不得出现未改写的内核裸名（会 NameError 500）。"""
        r = self._run("check_route_globals.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_patch_object_reaches_route_layer(self):
        """patch.object(app, X) 必须穿透到 web/api/*.py（否则桩静默失效）。"""
        r = self._run("check_patch_reaches_routes.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestRouteLayerShape(unittest.TestCase):
    """拆分后的结构契约。"""

    def test_app_no_longer_holds_routes(self):
        """app.py 不应再有 @app.get/@app.post 业务路由（钩子除外）。

        留一条都不行：有残留说明拆分没拆干净，两处并存最容易被改错地方。
        """
        src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("@app.get(", src)
        self.assertNotIn("@app.post(", src)
        self.assertNotIn("@app.delete(", src)

    def test_hooks_stay_in_kernel(self):
        """before/after hook 必须留在 app.py —— 蓝图路由也要经过它们。"""
        src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        self.assertIn("@app.before_request", src)
        self.assertIn("@app.after_request", src)

    def test_every_route_module_installs(self):
        """每个 web/api/*.py 都要有 install()，否则它的路由根本没注册。"""
        api_dir = REPO_ROOT / "web" / "api"
        mods = [f for f in api_dir.glob("*.py") if not f.name.startswith("_")]
        self.assertGreaterEqual(len(mods), 9, "路由模块数量异常")
        for f in mods:
            src = f.read_text(encoding="utf-8")
            self.assertIn("def install(", src, f"{f.name} 缺 install()")

    def test_routes_forbid_importing_kernel_symbols(self):
        """路由模块不得 `from app import X` / `import app as X`。

        一旦这么写，符号在 import 期固化，patch.object(app, X) 立刻失效。
        允许的出现位置只有注释与 docstring 里的说明文字。

        唯一豁免 `_ctx.py` 的 `import app as mod`：它是兜底分支，只有在"没有请求
        上下文、且 app 不在 sys.modules 里"（脚本直调）时才执行，正常请求期走
        current_app.extensions，不会二次加载内核。这是刻意的、有注释的例外。
        """
        api_dir = REPO_ROOT / "web" / "api"
        for f in sorted(api_dir.glob("*.py")):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0].strip()
                if not code:
                    continue
                if f.name == "_ctx.py" and code == "import app as mod":
                    continue
                self.assertFalse(
                    code.startswith("from app import") or code.startswith("import app"),
                    f"{f.name}:{i} 不得直接 import 内核：{line.strip()}")


if __name__ == "__main__":
    unittest.main()
