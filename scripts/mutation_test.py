#!/usr/bin/env python3
"""变异测试 runner(P3-④) —— 证明守卫测试真的有牙。

背景:
    测试全绿 ≠ 测试有效。一个恒真的断言永远绿。
    v1.3.3 曾手动做过两轮变异(2/2 捕获), 效果很好; 但一次性验证不代表持续
    有效 —— 新加的测试可能无效而不自知。本脚本把变异测试常态化:
    按 scripts/mutations.json 逐个把真实修复"改坏", 断言对应守卫测试必须变红。

判定标准:
    注入变异后, expect_test 模块必须非零退出(测试被杀死)。
    若仍为 0(全绿) -> 该守卫测试无效, CI 失败, 需重写测试。

用法:
    python scripts/mutation_test.py \
        --mutations scripts/mutations.json \
        --test-cmd-template "{python} -m unittest {module} -v"
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_cmd(template: str, python: str, module: str) -> list:
    """把命令模板拆成参数列表(不用 shell, 规避注入与 bandit B602)。

    Windows 下关闭 posix 转义, 否则路径里的反斜杠会被 shlex 吃掉。
    """
    return shlex.split(template.format(python=python, module=module),
                       posix=(os.name != "nt"))


def run_mutation(mut: dict, test_cmd_template: str, python: str) -> tuple:
    """注入单个变异体并跑目标测试, 返回 (mutant_id, killed, detail)。"""
    target = REPO_ROOT / mut["file"]
    original = target.read_bytes()
    text = original.decode("utf-8")
    find, replace = mut["find"], mut["replace"]

    count = text.count(find)
    if count == 0:
        return mut["id"], False, f"变异锚点不存在于 {mut['file']} —— 代码已重构, 请更新 mutations.json"
    if count > 1:
        return mut["id"], False, f"变异锚点在 {mut['file']} 出现 {count} 次, 无法唯一定位"

    module = mut["expect_test"].replace("/", ".").removesuffix(".py")
    cmd = _build_cmd(test_cmd_template, python, module)
    try:
        target.write_text(text.replace(find, replace), encoding="utf-8")
        r = subprocess.run(cmd, cwd=str(REPO_ROOT),
                           capture_output=True, text=True, timeout=600)
        # 恢复放在断言之前逻辑之外 —— finally 里做
        if r.returncode == 0:
            return mut["id"], False, (
                f"变异体存活: 注入 {mut['desc']} 后 {module} 仍然全绿 —— 守卫测试无效, 需重写")
        return mut["id"], True, f"已杀死: {module} 变红(returncode={r.returncode})"
    except subprocess.TimeoutExpired:
        return mut["id"], False, f"{module} 超时(>600s), 视为未杀死"
    finally:
        target.write_bytes(original)  # 无条件还原


def main() -> int:
    ap = argparse.ArgumentParser(description="iso-hub mutation test runner")
    ap.add_argument("--mutations", default=str(REPO_ROOT / "scripts" / "mutations.json"))
    ap.add_argument("--test-cmd-template", default="{python} -m unittest {module} -v")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    mutations = json.loads(Path(args.mutations).read_text(encoding="utf-8"))
    print(f"共 {len(mutations)} 个变异体, 逐个注入...\n")

    survivors = []
    # 预先跑一遍目标测试确认基线为绿(基线红时变异结果无意义)
    modules = sorted({m["expect_test"].replace("/", ".").removesuffix(".py")
                      for m in mutations})
    for mod in modules:
        cmd = _build_cmd(args.test_cmd_template, args.python, mod)
        r = subprocess.run(cmd, cwd=str(REPO_ROOT),
                           capture_output=True, text=True, timeout=600)
        status = "green" if r.returncode == 0 else "RED"
        print(f"[baseline] {mod}: {status}")
        if r.returncode != 0:
            print(f"\n[FAIL] 基线测试 {mod} 本身是红的, 变异结果不可信。先修基线。")
            return 1

    for mut in mutations:
        mut_id, killed, detail = run_mutation(mut, args.test_cmd_template, args.python)
        print(f"[{'KILLED ' if killed else 'SURVIVED'}] {mut_id}: {detail}")
        if not killed:
            survivors.append(mut_id)

    print(f"\n结果: {len(mutations) - len(survivors)}/{len(mutations)} 被杀死")
    if survivors:
        print(f"[FAIL] 存活变异体: {survivors} —— 对应守卫测试无效")
        return 1
    print("[OK] 所有守卫测试均有牙")
    return 0


if __name__ == "__main__":
    sys.exit(main())
