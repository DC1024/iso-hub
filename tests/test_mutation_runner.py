#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/mutation_test.py 的守卫 —— 变异 runner 本身也要有牙。

为什么值得单独测：这个脚本的产物是「守卫测试有效」这个结论。它若悄悄失效，
全项目的变异门禁就变成每周打印一次 KILLED 的仪式，谁也不会发现。

重点钉住三件事：

1. **锚点漂移必须报错，不能静默跳过**。v1.7.0 拆分把下载视图搬到 web/api/files.py，
   mutations.json 里两条锚点仍指向 app.py，count=0。旧实现会把它报成「锚点不存在」
   但也算 `killed=False`（看起来像守卫失效），实际上 CI 早就该红。这里钉住
   「锚点不匹配 ⇒ 明确失败」。
2. **必须唯一命中**。同一段代码出现两次时，无法判断变异落在哪一处，结果不可信。
3. **无条件还原**。注入后无论测试超时、崩溃还是被 Ctrl-C，源文件都必须回到原样 ——
   否则一次失败的变异测试会留下一个被改坏的仓库。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "mutation_test.py"
MUTATIONS = REPO_ROOT / "scripts" / "mutations.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import mutation_test as mt  # noqa: E402


class MutationDataTest(unittest.TestCase):
    """mutations.json 自身的完整性 —— 锚点漂移是这里最常见的腐烂方式。"""

    @classmethod
    def setUpClass(cls):
        cls.muts = json.loads(MUTATIONS.read_text(encoding="utf-8"))

    def test_non_empty(self):
        self.assertTrue(self.muts, "mutations.json 为空，变异门禁等于没跑")

    def test_schema(self):
        required = {"id", "file", "find", "replace", "expect_test", "desc"}
        for m in self.muts:
            with self.subTest(id=m.get("id", "<no id>")):
                self.assertTrue(required <= set(m), f"缺字段: {required - set(m)}")

    def test_ids_unique(self):
        ids = [m["id"] for m in self.muts]
        dup = {i for i in ids if ids.count(i) > 1}
        self.assertEqual(dup, set(), f"变异体 id 重复: {dup}")

    def test_anchors_hit_exactly_once(self):
        """每个 find 必须在目标文件里**唯一**命中。

        这是本文件存在的主因：v1.7.0 拆分后两条锚点指向了搬走的代码，
        而 mutation_test.py 只在运行时才报「锚点不存在」。静态钉住能让
        PR 阶段就发现，不必等每周的 cron。
        """
        broken = []
        for m in self.muts:
            target = REPO_ROOT / m["file"]
            if not target.is_file():
                broken.append(f"{m['id']}: 文件不存在 {m['file']}")
                continue
            text = target.read_text(encoding="utf-8").replace("\r\n", "\n")
            count = text.count(m["find"])
            if count != 1:
                broken.append(f"{m['id']}: 在 {m['file']} 命中 {count} 次（应为 1）")
        self.assertEqual(broken, [],
                         "变异锚点已漂移 —— 代码重构后必须同步更新 mutations.json:\n  "
                         + "\n  ".join(broken))

    def test_expect_test_files_exist(self):
        missing = [f"{m['id']}: {m['expect_test']}" for m in self.muts
                   if not (REPO_ROOT / m["expect_test"]).is_file()]
        self.assertEqual(missing, [], "expect_test 指向不存在的文件:\n  "
                         + "\n  ".join(missing))

    def test_find_differs_from_replace(self):
        """find == replace 的变异体什么也没改，必然「存活」，是假阳性来源。"""
        same = [m["id"] for m in self.muts if m["find"] == m["replace"]]
        self.assertEqual(same, [], f"find 与 replace 相同: {same}")


class BuildCmdTest(unittest.TestCase):
    """命令模板拆参数 —— 不走 shell，避免注入与路径被吃掉。"""

    def test_basic_split(self):
        got = mt._build_cmd("{python} -m unittest {module} -v",
                            "/usr/bin/python3", "tests.test_x")
        self.assertEqual(got, ["/usr/bin/python3", "-m", "unittest", "tests.test_x", "-v"])

    def test_not_a_shell(self):
        """`;` 必须作为普通字符留在某个参数里，不能被当成命令分隔符。

        走 list + 不设 shell=True 才是关键；`shlex.split` 仍会按空格切分
        （所以 `a;rm -rf /` 变成 `['a;rm', '-rf', '/']`），但那个 `;` 永远
        只是字符串里的一颗字符，不会被 shell 解释。断言要钉的是这一点，
        而不是"整串保持完整"。
        """
        got = mt._build_cmd("{python} -m unittest {module}", "py", "a;rm -rf /")
        self.assertEqual(got[0], "py")
        # 没有任何一个参数被"吃掉"或变成独立命令；分号留在参数内部
        self.assertTrue(any(";" in a for a in got),
                        f"分号应作为参数内的普通字符保留: {got}")
        self.assertNotIn("rm", [a for a in got if a == "rm"],
                         "rm 不应被提升为独立命令")


class RunMutationTest(unittest.TestCase):
    """注入 / 还原 / 判定。全部在临时目录里造目标文件，不碰真实源码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # 让 run_mutation 在临时目录里解析 "target.py"，不碰真实源码；
        # 它的 subprocess 也用 REPO_ROOT 当 cwd，所以探测脚本能直接读到 target.py
        self._orig_root = mt.REPO_ROOT
        mt.REPO_ROOT = self.root
        self.addCleanup(setattr, mt, "REPO_ROOT", self._orig_root)

    def _write(self, name: str, text: str) -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def _mut(self, **kw) -> dict:
        base = {"id": "t", "file": "target.py", "find": "BAD",
                "replace": "MUTANT", "expect_test": "tests/test_x.py", "desc": "d"}
        base.update(kw)
        return base

    def test_anchor_missing_is_not_killed(self):
        """锚点找不到 ⇒ 明确报未杀死，且理由要说清是锚点问题（不是守卫无效）。"""
        self._write("target.py", "GOOD\n")
        mut_id, killed, detail = mt.run_mutation(self._mut(), "{python} -c pass", "py")
        self.assertFalse(killed)
        self.assertIn("锚点", detail)
        self.assertIn("重构", detail, "提示要指向「代码变了，请更新 JSON」，别让人去查守卫测试")

    def test_anchor_ambiguous_is_not_killed(self):
        self._write("target.py", "BAD\nBAD\n")
        _mid, killed, detail = mt.run_mutation(self._mut(), "{python} -c pass", "py")
        self.assertFalse(killed)
        self.assertIn("2 次", detail)

    # 夹具必须**包含锚点 BAD**，否则 run_mutation 在锚点检查处就提前返回，
    # 注入/超时/还原三条分支一条都走不到 —— 测试会以假绿通过。
    BASE = "GOOD\nBAD\n"

    def test_killed_when_test_turns_red(self):
        """注入后目标测试非零退出 ⇒ 判定为已杀死。"""
        target = self._write("target.py", self.BASE)
        # 用一个真的会失败的 python 命令：文件里出现 MUTANT 才返回 1
        probe = self._write("probe.py",
                            "import sys,pathlib\n"
                            "t=pathlib.Path('target.py').read_text(encoding='utf-8')\n"
                            "sys.exit(1 if 'MUTANT' in t else 0)\n")
        mut_id, killed, detail = mt.run_mutation(
            self._mut(), "{python} " + str(probe), sys.executable)
        self.assertTrue(killed, detail)
        self.assertIn("已杀死", detail)
        self.assertEqual(target.read_text(encoding="utf-8"), self.BASE,
                         "跑完必须还原源文件")

    def test_survived_when_test_stays_green(self):
        """注入后测试仍绿 ⇒ 存活，说明该守卫测试没牙。"""
        self._write("target.py", self.BASE)
        probe = self._write("probe.py", "import sys; sys.exit(0)\n")
        _mid, killed, detail = mt.run_mutation(
            self._mut(), "{python} " + str(probe), sys.executable)
        self.assertFalse(killed)
        self.assertIn("变异体存活", detail,
                      "必须是因为「测试没变红」而存活，不能是锚点缺失导致的提前返回")

    def test_injection_really_happened(self):
        """确认注入那一步真的落到了盘上 —— 否则上面几条可能只是空转。"""
        self._write("target.py", self.BASE)
        seen = self.root / "seen.txt"
        probe = self._write(
            "probe.py",
            "import pathlib\n"
            "t = pathlib.Path('target.py').read_text(encoding='utf-8')\n"
            f"pathlib.Path({str(seen)!r}).write_text(t, encoding='utf-8')\n"
            "raise SystemExit(1)\n")
        mt.run_mutation(self._mut(), "{python} " + str(probe), sys.executable)
        self.assertEqual(seen.read_text(encoding="utf-8"), "GOOD\nMUTANT\n",
                         "子进程看到的应是注入后的内容")

    def test_source_restored_after_survive(self):
        original = self.BASE + "KEEP THIS EXACT\n"
        target = self._write("target.py", original)
        probe = self._write("probe.py", "import sys; sys.exit(0)\n")
        _mid, killed, detail = mt.run_mutation(
            self._mut(), "{python} " + str(probe), sys.executable)
        self.assertNotIn("锚点", detail, "夹具应命中锚点，不能走提前返回分支")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_source_restored_on_timeout(self):
        """超时也必须还原 —— 否则一次中断留下改坏的仓库。

        这是最容易漏的分支：`except TimeoutExpired` 里若忘了写文件，
        只有 finally 兜底；而 finally 存在的前提是有人记得写。

        这里让替身直接抛 TimeoutExpired，而不是真起一个 sleep 子进程去撞
        0.2s 超时。后者在 Windows 上偶发永久卡死：subprocess.run 在 kill()
        之后还要 communicate() 一次去收读线程，等不到管道 EOF 就无限 join
        （全量套件里连挡了三批门禁，隔离单跑却 4.6s 全过）。被测的分支就是
        `except subprocess.TimeoutExpired`，替身抛出来一样能走到。
        """
        original = self.BASE + "KEEP THIS EXACT\n"
        target = self._write("target.py", original)

        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 600))

        orig = mt.subprocess.run
        mt.subprocess.run = fake_run
        try:
            _mid, killed, detail = mt.run_mutation(
                self._mut(), "{python} -m unittest fake_module", sys.executable)
        finally:
            mt.subprocess.run = orig

        self.assertFalse(killed)
        self.assertIn("超时", detail)
        self.assertEqual(target.read_text(encoding="utf-8"), original,
                         "超时后没有还原源文件 —— 仓库会被改坏")

    def test_crlf_anchor_matches_lf_json(self):
        """目标文件是 CRLF、JSON 按 LF 维护时也要能命中。

        仓库里确实有 CRLF 文件（Windows 侧编辑过），不统一换行会让锚点
        「明明看起来一样却找不到」。
        """
        target = self.root / "target.py"
        target.write_bytes(b"line1\r\nBAD\r\nline3\r\n")
        probe = self._write("probe.py", "import sys; sys.exit(0)\n")
        _mid, killed, detail = mt.run_mutation(
            self._mut(), "{python} " + str(probe), sys.executable)
        self.assertNotIn("锚点", detail, f"CRLF 文件应能命中 LF 锚点: {detail}")
        self.assertEqual(target.read_bytes(), b"line1\r\nBAD\r\nline3\r\n",
                         "还原必须保持原始字节（含换行符）")


class CliTest(unittest.TestCase):
    """命令行入口的基本契约。"""

    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                           capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertEqual(r.returncode, 0)
        self.assertIn("mutation", r.stdout.lower())

    def test_missing_mutations_file_fails_loudly(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--mutations", str(REPO_ROOT / "nope.json")],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.assertNotEqual(r.returncode, 0, "配置缺失必须非零退出，不能静默当作没有变异体")


if __name__ == "__main__":
    unittest.main()
