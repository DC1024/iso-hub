#!/usr/bin/env python3
"""镜像源深度解析巡检(季度) —— 抓取逻辑是否仍然健壮。

与 endpoint_smoke.py 的分工:
    * endpoint_smoke(每日): 只查 URL 活性(404/410 失效), 查不出「页面还在但
      DOM 变了导致正则静默匹配不到」这类解析层失效。
    * 本脚本(季度): 对 sources_config.json 里每个非 static source 跑
      **真实的版本正则 + 真实的 build_entries() 解析链**, 与基线快照对比。
      页面改版 -> 正则匹配 0 个版本 / builder 抛异常 / 条目数归零, 都会被抓出来。

判定分级(与 cve_check.py 同思路, 关注「变化」而非绝对数字):
    * BROKEN(error, 退出码 2): 版本正则匹配 0 个 / build_entries 抛
      SourceBuilderError / 解析条目数为 0 —— 解析链已断, 必须修。
    * SUSPECT(warning, 退出码 1): 列表页拉取异常(超时/连接失败/HTTP 错误) ——
      可能是 runner 网络或源站抖动, 也可能是反爬升级, 人工复核。
      新 source 无基线 / 基线缺失 -> 首跑提示 --update-baseline。
    * DIFF(info, 不影响退出码): 条目数/最新版本与基线不同 —— 上游自然推进
      (出新版/归档旧版), 只是让变化可见, 不算故障。

退出码:
    0 = 全部正常(仅 DIFF/info)
    1 = 存在 SUSPECT 或首跑无基线 -> workflow 发 warning
    2 = 存在 BROKEN -> workflow 升级为 error(cron 失败会发邮件)
    3 = 配置/输入异常(配置文件读不出、基线 JSON 坏了) -> 必须当故障处理

用法:
    python scripts/scrape_check.py [--config iso_download/sources_config.json]
                                   [--baseline scripts/scrape_baseline.json]
                                   [--out artifacts/scrape_result.json]
                                   [--update-baseline]
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ISO_DL_DIR = REPO_ROOT / "iso_download"
sys.path.insert(0, str(ISO_DL_DIR))

import update_distributions as ud  # noqa: E402  (复用真实抓取/解析链)

DEFAULT_CONFIG = ISO_DL_DIR / "sources_config.json"
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "scrape_baseline.json"
DEFAULT_OUT = REPO_ROOT / "artifacts" / "scrape_result.json"

SCHEMA = 1


def _probe_source(source: dict) -> dict:
    """对一个 source 做两阶段探测, 返回结构化结果(不抛网络异常)。"""
    name = source.get("distribution", "?")
    strategy = source.get("strategy", "?")
    result = {"distribution": name, "strategy": strategy,
              "listing_url": source.get("listing_url", ""),
              "status": "ok", "versions_total": None, "latest": None,
              "entries": None, "detail": ""}

    if strategy == "static":
        result["status"] = "skipped"
        result["detail"] = "static 策略无实时列表页, 由 endpoint_smoke 覆盖"
        return result

    # 阶段 A: 版本正则探测(直接量「页面还能不能被正则读懂」)。
    # 仅对声明了 version_regex 的策略(dated_directory / versioned_flat_listing 等)
    # 做; flat_listing 单页直出 artifact, 没有版本列表, 只靠阶段 B 的解析条目数。
    if "version_regex" in source:
        try:
            html = ud.fetch_text(source["listing_url"],
                                 timeout=source.get("timeout", 30))
            pattern = re.compile(source["version_regex"])
            versions = [m.groupdict().get("value", "") for m in pattern.finditer(html)]
            versions = [v.strip("/") for v in versions if v]
            result["versions_total"] = len(versions)
            ordered = ud.extract_ordered_unique(versions)
            result["latest"] = ordered[0] if ordered else None
            if not versions:
                result["status"] = "broken"
                result["detail"] = (f"版本正则匹配 0 个(页面可能改版): "
                                    f"{source['listing_url']}")
                return result
        except Exception as exc:  # 网络层/HTTP 层失败 -> 存疑而非判死
            result["status"] = "suspect"
            result["detail"] = f"列表页拉取失败: {type(exc).__name__}: {exc}"
            return result

    # 阶段 B: 完整解析链(与 update_distributions.py 主流程同一函数)
    try:
        entries = ud.build_entries(source)
        result["entries"] = len(entries)
        if not entries:
            result["status"] = "broken"
            result["detail"] = "build_entries 返回 0 条(解析链已断)"
    except Exception as exc:
        result["status"] = "broken"
        result["detail"] = f"build_entries 异常: {type(exc).__name__}: {exc}"
    return result


def _diff(baseline: dict, result: dict) -> list:
    """对比单 source 与基线, 返回 info 行(自然推进, 不算故障)。

    基线 key 用 listing_url(唯一)而非 distribution 名 —— Fedora/Ubuntu
    各有两个不同列表页的 source, 按 distribution 做 key 会互相覆盖。
    """
    old = baseline.get(result["listing_url"])
    if not isinstance(old, dict):
        return []
    lines = []
    if old.get("entries") is not None and result["entries"] is not None \
            and old["entries"] != result["entries"]:
        lines.append(f"{result['distribution']}: 条目数 {old['entries']} -> "
                     f"{result['entries']}")
    if old.get("latest") and result["latest"] \
            and old["latest"] != result["latest"]:
        lines.append(f"{result['distribution']}: 最新版本 {old['latest']} -> "
                     f"{result['latest']}")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="iso-hub scrape robustness check")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--update-baseline", action="store_true",
                    help="用本次(成功的)探测结果落/更新基线后退出 0")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    try:
        sources = json.loads(cfg_path.read_text(encoding="utf-8")).get("sources", [])
    except Exception as exc:
        print(f"⛔ 配置读取失败 {cfg_path}: {exc}")
        return 3
    if not sources:
        print(f"⛔ 配置为空: {cfg_path}")
        return 3

    baseline_path = Path(args.baseline)
    baseline = {}
    if baseline_path.exists():
        try:
            doc = json.loads(baseline_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"⛔ 基线 JSON 损坏 {baseline_path}: {exc} "
                  f"(删掉重建: --update-baseline)")
            return 3
        # 基线文件结构: {"schema":1, "sources": {name: {versions_total/latest/entries}}}
        baseline = doc.get("sources", {}) if isinstance(doc, dict) else {}
    else:
        print(f"[提示] 基线不存在({baseline_path}), 本次为首跑: "
              f"全部结果将归为 SUSPECT, 确认无误后加 --update-baseline 落基线")

    results = [_probe_source(s) for s in sources]
    broken = [r for r in results if r["status"] == "broken"]
    suspect = [r for r in results if r["status"] == "suspect"]
    skipped = [r for r in results if r["status"] == "skipped"]
    ok = [r for r in results if r["status"] == "ok"]

    diffs = []
    for r in ok:
        diffs.extend(_diff(baseline, r))

    # ---- 输出 ----
    print(f"共巡检 {len(results)} 个 source: "
          f"正常 {len(ok)}, 跳过 {len(skipped)}, "
          f"存疑 {len(suspect)}, 损坏 {len(broken)}")
    for r in ok:
        print(f"  [OK]     {r['distribution']:<10} {r['strategy']:<22} "
              f"版本 {r['versions_total']} 个, 最新 {r['latest']}, "
              f"解析 {r['entries']} 条")
    for r in broken:
        print(f"  [BROKEN] {r['distribution']:<10} {r['detail']}")
    for r in suspect:
        print(f"  [SUSPECT]{r['distribution']:<10} {r['detail']}")
    for d in diffs:
        print(f"  [DIFF]   {d}")

    # 结果 JSON 落盘(artifact), 供人工复核与 CI summary
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "schema": SCHEMA, "checked_at": date.today().isoformat(),
        "results": results, "diffs": diffs,
        "broken": [r["distribution"] for r in broken],
        "suspect": [r["distribution"] for r in suspect],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {out}")

    if args.update_baseline:
        good = {r["listing_url"]: {"distribution": r["distribution"],
                                   "versions_total": r["versions_total"],
                                   "latest": r["latest"], "entries": r["entries"]}
                for r in ok}
        baseline_path.write_text(json.dumps(
            {"schema": SCHEMA, "updated_at": date.today().isoformat(),
             "sources": good}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已更新({len(good)} 个 source) -> {baseline_path}")
        return 0

    # 退出码: BROKEN=2 > SUSPECT=1 > 正常=0
    if broken:
        return 2
    if suspect or not baseline_path.exists():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
