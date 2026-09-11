#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜像 CVE 基线增量检查 —— 只对「基线之外的新增 High/Critical」告警。

背景:
    iso-hub 镜像当前有 101 条 CVE, **Debian 官方给出修复版本的是 0 条**
    (trixie 100 open / 1 undetermined), 意味着 `apt upgrade` 一条都修不掉,
    且全部经分诊判定为不可达或受限(详见 docs/cve-triage-1.3.12.md)。

结论:
    * 用「CVE 数量阈值」做门禁 -> 永远红 -> 退化为噪音源 -> 等于没有门禁。
    * 逐条分析 101 条 -> 投入产出比极低。
    * **基线快照 + 只对新增告警** -> 关注「变化」而非「绝对数字」。
      与 gpg_ledger 同一思路: 让不可见的变化变可见。

用法:
    python scripts/cve_check.py --input trivy.json [--out artifacts/cve_diff.txt]
    python scripts/cve_check.py --input trivy.json --update-baseline   # 人工确认后刷新基线

退出码:
    0 = 无新增(或仅有条目消失)
    1 = 有新增, 但都是 Medium/Low/Unknown -> warning
    2 = 有新增 High/Critical -> error(应当阻塞)
    3 = 配置/输入异常(基线缺失、扫描结果解析不出 CVE) -> 这是"扫描没跑成", 不是安全结论
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "cve_baseline.json"

# 触发 error(退出码 2) 的等级
ERROR_SEV = {"CRITICAL", "HIGH"}
KNOWN_SEV = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN", "NONE"}

EXIT_OK = 0
EXIT_WARN = 1
EXIT_ERROR = 2
EXIT_CONFIG = 3

_CVE_RE = re.compile(r"^CVE-\d{4}-\d+$", re.I)
_ID_KEYS = ("VulnerabilityID", "vulnerability_id", "id", "CVE", "cve", "name")
_SEV_KEYS = ("Severity", "severity", "sev")


def norm_sev(v) -> str:
    """归一化等级; 无法识别的一律 UNKNOWN(宁可提醒, 不可静默放过)。"""
    if not isinstance(v, str) or not v.strip():
        return "UNKNOWN"
    s = v.strip().upper()
    return s if s in KNOWN_SEV else "UNKNOWN"


def _first(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def iter_vulns(data):
    """从多种扫描结果格式里产出 (cve_id, severity)。

    兼容:
      * Trivy 原生 JSON: {"Results":[{"Vulnerabilities":[{"VulnerabilityID","Severity"}]}]}
      * 通用 {"vulnerabilities":[{...}]} / {"cves":[...]}
      * 顶层 list: 元素为 CVE 字符串, 或含 id/severity 的 dict
      * {"cves": {"CVE-xxx": "HIGH", ...}} 映射形式
    """
    items = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if isinstance(data.get("Results"), list):          # Trivy
            for r in data["Results"]:
                if isinstance(r, dict):
                    items.extend(r.get("Vulnerabilities") or [])
        elif isinstance(data.get("results"), list):
            for r in data["results"]:
                if isinstance(r, dict):
                    items.extend(r.get("vulnerabilities") or r.get("Vulnerabilities") or [])
        elif isinstance(data.get("vulnerabilities"), list):
            items = data["vulnerabilities"]
        elif isinstance(data.get("cves"), list):
            items = data["cves"]
        elif isinstance(data.get("cves"), dict):           # {cve: severity}
            for k, v in data["cves"].items():
                yield (str(k).strip().upper(), norm_sev(v))
            return

    for it in items:
        if isinstance(it, str):
            cid = it.strip().upper()
            yield (cid, "UNKNOWN")
        elif isinstance(it, dict):
            cid = _first(it, _ID_KEYS).upper()
            if not cid:
                continue
            yield (cid, norm_sev(_first(it, _SEV_KEYS)))


def load_baseline(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError("基线文件不存在: %s" % path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc.get("entries"), dict):
        raise ValueError("基线文件格式错误: 缺少 entries 对象 (%s)" % path)
    return doc


def diff(baseline_ids, scan):
    """scan: {cve: severity} -> (added:{cve:sev}, removed:set)"""
    added = {c: s for c, s in scan.items() if c not in baseline_ids}
    removed = {c for c in baseline_ids if c not in scan}
    return added, removed


def _rank(sev: str, score):
    """排序用: 先按等级权重, 再按分值降序。"""
    w = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 2, "NONE": 0}
    return (-w.get(sev, 2), -(score if isinstance(score, (int, float)) else 0))


def render(baseline_doc, added, removed, non_cve) -> str:
    entries = baseline_doc.get("entries", {})
    lines = []
    lines.append("镜像 CVE 基线增量检查")
    lines.append("=" * 60)
    lines.append("基线: %s 条 (生成于 %s, %s)"
                 % (len(entries), baseline_doc.get("generated_at", "?"),
                    baseline_doc.get("app_version", "")))
    lines.append("")

    if added:
        blocking = {c: s for c, s in added.items() if s in ERROR_SEV}
        others = {c: s for c, s in added.items() if s not in ERROR_SEV}
        if blocking:
            lines.append("⛔ 新增 High/Critical —— 需要人工分诊 (%d 条)" % len(blocking))
            for c in sorted(blocking, key=lambda x: (_rank(blocking[x], None), x)):
                lines.append("   [%s] %s" % (blocking[c], c))
            lines.append("")
        if others:
            lines.append("⚠ 新增 Medium/Low/Unknown (%d 条)" % len(others))
            for c in sorted(others, key=lambda x: (_rank(others[x], None), x)):
                lines.append("   [%s] %s" % (others[c], c))
            lines.append("")
        lines.append("分诊步骤: 查 Debian Security Tracker 的 trixie 状态与官方影响版本区间, ")
        lines.append("           确认 iso-hub 是否可达; 确认无需处理后执行 --update-baseline。")
        lines.append("")
    else:
        lines.append("✅ 无新增 CVE (基线之外的条目: 0)")
        lines.append("")

    if removed:
        lines.append("ℹ 基线中已不再出现的条目 (%d): 通常是上游修了或扫描器调整了规则" % len(removed))
        for c in sorted(removed):
            lines.append("   - %s (%s)" % (c, entries.get(c, {}).get("pkg", "?")))
        lines.append("")

    if non_cve:
        lines.append("ℹ 扫描结果中 %d 个非 CVE 格式的条目未参与比对(如 GHSA-*):" % len(non_cve))
        for c in sorted(non_cve)[:10]:
            lines.append("   - %s" % c)
        if len(non_cve) > 10:
            lines.append("   ... (共 %d)" % len(non_cve))
        lines.append("")

    return "\n".join(lines)


def update_baseline(baseline_doc, scan, path: Path) -> None:
    """把扫描结果并入基线: 保留已有判定, 新增条目标记 reach=unknown。"""
    entries = baseline_doc.setdefault("entries", {})
    added_n = 0
    for cve, sev in scan.items():
        if cve not in entries:
            entries[cve] = {"pkg": "", "score": None, "severity": sev.lower(),
                            "reach": "unknown", "debian_status": "",
                            "urgency": "", "fixed": "",
                            "note": "经 --update-baseline 并入, 尚未分诊"}
            added_n += 1
    removed_n = 0
    for cve in [c for c in entries if c not in scan]:
        entries[cve]["reach"] = "gone"
        entries[cve]["note"] = "已不在最新扫描结果中(可能已修复)"
        removed_n += 1
    baseline_doc["generated_at"] = datetime.now(
        timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    path.write_text(json.dumps(baseline_doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print("基线已更新: %s (新增 %d 条, 标记消失 %d 条, 合计 %d 条)"
          % (path, added_n, removed_n, len(entries)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CVE 基线增量检查")
    ap.add_argument("--input", required=True, help="扫描结果 JSON (Trivy 原生或简化格式)")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="基线文件路径")
    ap.add_argument("--out", default=None, help="报告输出文件(可选)")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前扫描结果并入基线(人工分诊确认后使用)")
    args = ap.parse_args(argv)

    try:
        baseline_doc = load_baseline(Path(args.baseline))
    except Exception as e:
        print("::error::基线读取失败: %s" % e, file=sys.stderr)
        return EXIT_CONFIG

    try:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except Exception as e:
        print("::error::扫描结果读取/解析失败: %s" % e, file=sys.stderr)
        return EXIT_CONFIG

    scan, non_cve = {}, set()
    for cid, sev in iter_vulns(raw):
        if _CVE_RE.match(cid):
            # 同一 CVE 可能被多个包命中, 取最高的那个等级
            if cid not in scan or _rank(sev, None)[0] < _rank(scan[cid], None)[0]:
                scan[cid] = sev
        else:
            non_cve.add(cid)

    if not scan:
        print("::error::扫描结果里没有解析出任何 CVE —— 扫描器可能没跑成功, "
              "这不是「无漏洞」的结论", file=sys.stderr)
        return EXIT_CONFIG

    if args.update_baseline:
        update_baseline(baseline_doc, scan, Path(args.baseline))
        return EXIT_OK

    added, removed = diff(set(baseline_doc.get("entries", {})), scan)
    report = render(baseline_doc, added, removed, non_cve)
    print(report)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(report, encoding="utf-8")

    if any(s in ERROR_SEV for s in added.values()):
        return EXIT_ERROR
    if added:
        return EXIT_WARN
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
