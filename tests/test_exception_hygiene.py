#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""except Exception 收敛护栏(v1.6.0)。

背景: 全仓库曾有 113 处 `except Exception`。宽捕获的问题是"删了不报错、出事查不到":
可预期的错误(磁盘满、JSON 损坏、网络超时)被和真正的 bug(KeyError、AttributeError、
拼错的变量名)一视同仁地吞掉, 后者本该炸出来却静默劣化。

v1.6.0 的收敛策略:
  * **叶子调用点**(JSON 读写、Docker API、requests 网络、qB API、子进程)收窄到
    可枚举的异常元组, 共享定义集中在 `web/errs.py`;
  * **真正的边界**允许保留宽捕获: 请求处理器 500 兜底、后台守护循环、
    尽力而为的清理、可选模块导入 —— 但同行注释必须写明「边界」理由。

这个测试是只降不升的棘轮:
  1. 生产代码里每一处 `except Exception` 必须在同行注释里带「边界」二字;
  2. 总数不得超过当前基线(BASELINE)。收窄一处就把基线减一, 不许回涨。
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("web", "iso_download", "scripts")
# 当前基线(v1.6.0 收敛后): 全部是带「边界」注释的真边界。再收窄时请同步减小。
# 其中 11 处是"契约型边界"——函数文档明确要求任何异常都不外抛
# (service_state/set_share 等 Docker 查询族、GPG 降级链), 收窄会破坏既定语义。
BASELINE = 39

BROAD_RE = re.compile(r"^\s*except Exception\b")


def _iter_broad_excepts():
    """产出 (相对路径, 行号, 行文本) —— 生产代码里的每一处宽捕获。"""
    for d in SCAN_DIRS:
        for py in sorted((REPO_ROOT / d).rglob("*.py")):
            if py.name == "errs.py":
                continue  # 元组定义模块自身的 docstring 不算
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if BROAD_RE.match(line):
                    yield py.relative_to(REPO_ROOT).as_posix(), lineno, line


class BroadExceptTest(unittest.TestCase):
    def test_every_broad_except_is_a_justified_boundary(self):
        bad = [(f, n, ln.strip()) for f, n, ln in _iter_broad_excepts()
               if "边界" not in ln]
        self.assertEqual(
            bad, [],
            "以下宽捕获缺少「边界」理由注释 —— 要么收窄到可枚举的异常类型"
            "(优先用 web/errs.py 的具名元组), 要么补注释说明为什么它是边界:\n"
            + "\n".join(f"  {f}:{n}: {ln}" for f, n, ln in bad),
        )

    def test_broad_except_count_does_not_regress(self):
        sites = list(_iter_broad_excepts())
        self.assertLessEqual(
            len(sites), BASELINE,
            f"宽捕获数量 {len(sites)} 超过基线 {BASELINE} —— 新增 except Exception "
            f"前请先确认它是真边界; 若收窄了旧点位, 请把 BASELINE 同步调小:\n"
            + "\n".join(f"  {f}:{n}" for f, n, _ in sites),
        )


class ErrsModuleTest(unittest.TestCase):
    """共享元组本身的形态约束: 都是异常类元组, 且不含 Exception 本身(那就白收窄了)。"""

    def test_tuples_are_narrow(self):
        import sys
        sys.path.insert(0, str(REPO_ROOT / "web"))
        import errs

        for name in ("JSON_READ", "JSON_WRITE", "DOCKER_API", "HTTP_REQ", "QB_API"):
            tup = getattr(errs, name, None)
            self.assertIsInstance(tup, tuple, f"errs.{name} 缺失或不是元组")
            self.assertTrue(tup, f"errs.{name} 为空")
            for exc in tup:
                self.assertIsInstance(exc, type, f"errs.{name} 含非类成员: {exc!r}")
                self.assertTrue(issubclass(exc, BaseException),
                                f"errs.{name} 含非异常类: {exc!r}")
                self.assertNotIn(exc, (Exception, BaseException),
                                 f"errs.{name} 不该包含 {exc.__name__} 本身")

    def test_json_tuples_cover_decode_errors(self):
        """JSONDecodeError / UnicodeDecodeError 都必须被 JSON_READ 覆盖(都是 ValueError 子类)。"""
        import json
        import sys
        sys.path.insert(0, str(REPO_ROOT / "web"))
        import errs

        self.assertTrue(issubclass(json.JSONDecodeError, errs.JSON_READ))
        self.assertTrue(issubclass(UnicodeDecodeError, errs.JSON_READ))


if __name__ == "__main__":
    unittest.main()
