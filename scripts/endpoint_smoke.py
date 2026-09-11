#!/usr/bin/env python3
"""端点连通性冒烟(P2-③) —— 提前发现清单里的 URL 失效。

用途:
    「Fedora 42 CHECKSUM 已归档 404」「清华 Arch 只剩 2026.07+」这类清单失效,
    会导致对应发行版长期静默降级。本脚本每日在 CI cron 里对内置清单的所有 URL
    做连通性探测, 结果写 stdout + artifacts 文件, 供 workflow 汇报告警。

设计要点:
    * 只做探测不阻塞开发 —— workflow 里 continue-on-error, 失败仅发告警。
    * 兼容两种配置形态: 单 URL 字段(download_url/checksum_url/...)
      与多镜像数组字段(download_urls/checksum_urls)。
    * HEAD 被部分镜像拒绝时回退 GET(带 Range 头, 只拉首个字节)。
    * 404/410 视为失效; 403/429 等反爬限流视为"存疑", 单独归入 suspect,
      不与真 404 混淆(避免告警噪音)。

用法:
    python scripts/endpoint_smoke.py [--config iso_download/distributions.json]
                                     [--out artifacts/smoke_result.txt]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "iso_download" / "distributions.json"

# 视为"失效"(清单需要更新)的状态码
DEAD_CODES = {404, 410}
# 视为"存疑"(可能是反爬/限流, 不判定失效)的状态码
SUSPECT_CODES = {403, 405, 418, 429}
# 大面积失效阈值: 真失效占比超过它时升级为 error(退出码 2)。
# 场景: 清华 Arch 整批下架这类"整站级"事件, 普通 warning 会被噪音淹没。
ERROR_RATIO = 0.30

UA = {"User-Agent": "iso-hub-endpoint-smoke/1.0 (endpoint health probe)",
      "Accept": "*/*"}


def _urls_of(entry: dict) -> list:
    """收集一个条目里的全部待探测 URL(单值字段 + 多镜像数组)。"""
    urls = []
    for key in ("download_url", "checksum_url", "signature_url", "gpg_key_url"):
        v = entry.get(key)
        if isinstance(v, str) and v.startswith("http"):
            urls.append(v)
    for key in ("download_urls", "checksum_urls"):
        vs = entry.get(key)
        if isinstance(vs, list):
            urls.extend(u for u in vs if isinstance(u, str) and u.startswith("http"))
    # 去重保序
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def probe(url: str) -> str:
    """探测单个 URL, 返回 'ok' / 'dead' / 'suspect'。"""
    for attempt, method in enumerate(("head", "get")):
        try:
            kwargs = {"timeout": 20, "allow_redirects": True, "headers": UA}
            if method == "get":
                kwargs["headers"] = {**UA, "Range": "bytes=0-0"}
            r = requests.request(method, url, **kwargs)
            if r.status_code < 400:
                return "ok"
            if r.status_code in DEAD_CODES:
                return "dead"
            if r.status_code in SUSPECT_CODES:
                return "suspect"
            return "suspect"
        except requests.RequestException:
            if attempt == 0:
                time.sleep(2)  # HEAD 网络异常时再试一次 GET
    return "suspect"


def main() -> int:
    ap = argparse.ArgumentParser(description="iso-hub endpoint smoke")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--out", default="artifacts/smoke_result.txt")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    entries = cfg.get("distributions", [])
    dead, suspect = [], []
    checked = 0
    for e in entries:
        label = f"{e.get('distribution', '?')}/{e.get('version', '')}".rstrip("/")
        for url in _urls_of(e):
            checked += 1
            verdict = probe(url)
            if verdict == "dead":
                dead.append(f"{label}: {url} -> 端点失效(404/410, 可能已归档/下架)")
            elif verdict == "suspect":
                suspect.append(f"{label}: {url} -> 存疑(反爬/限流, 人工复核)")
            time.sleep(0.3)  # 对镜像站友好

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = dead + suspect
    out.write_text("\n".join(lines), encoding="utf-8")

    print(f"共探测 {checked} 个端点: "
          f"正常 {checked - len(dead) - len(suspect)}, "
          f"失效 {len(dead)}, 存疑 {len(suspect)}")
    if dead:
        print("\n".join(dead))
    if suspect:
        print("\n".join(suspect))
    # 退出码:
    #   0  全部正常(或仅存疑)
    #   1  存在"真失效"端点 -> workflow 发 warning 告警
    #   2  真失效占比 > ERROR_RATIO -> 大面积失效, workflow 升级为 error 告警
    if checked and len(dead) / checked > ERROR_RATIO:
        ratio = len(dead) / checked
        print(f"⛔ 大面积失效: {len(dead)}/{checked} ({ratio:.0%}) 超过 "
              f"{ERROR_RATIO:.0%} 阈值 —— 疑似整站下架/归档, 升级为 ERROR")
        return 2
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
