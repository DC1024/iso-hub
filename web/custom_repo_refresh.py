#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ISO Hub - 自定义发行版源展开 runner(后台子进程, 不占 waitress 线程)。

读取 custom_sources.json 中所有 strategy 类型的自定义源, 逐个展开为具体 ISO 条目,
把结果持久化写入 custom_repo_cache.json。

/app.py 的 /api/distros 请求线程只读取 custom_repo_cache.json(绝不发起网络请求);
本 runner 由用户手动触发或定时调度触发, 以 subprocess 方式运行, 与 Web 请求线程隔离。

用法:
  custom_repo_refresh.py --custom-json <custom_sources.json>
                         --cache-json <custom_repo_cache.json>
                         [--repo-dir <iso_download 目录>]
"""
import argparse
import json
import sys
import time
from pathlib import Path

STRATEGIES = ("dated_directory", "flat_listing", "versioned_flat_listing", "static")


def cache_key(item: dict) -> str:
    """与 app.py 的 _custom_repo_cache_key 保持一致: 除 timeout 外的配置字段决定抓取结果。"""
    return json.dumps({k: v for k, v in item.items() if k != "timeout"},
                      sort_keys=True, ensure_ascii=False, default=str)


def _is_http_url(url: str) -> bool:
    """仅允许 http/https 协议(与 app.py 保持一致的 SSRF 防线)。"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.netloc != ""
    except Exception:  # noqa: BLE001
        return False


def expand_source(item: dict) -> list:
    """展开单个自定义源, 复用上游 update_distributions.build_entries。"""
    from update_distributions import build_entries  # noqa: PLC0415
    # 复制后交给 build_entries(其 build_from_* 会读取 source.get("timeout", 30) 控制读超时)
    source = dict(item)
    entries = build_entries(source)
    for e in entries:
        e.setdefault("distribution", source.get("distribution", "?"))
        e.setdefault("type", source.get("type", "linux"))
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description="ISO Hub custom distro source expander")
    parser.add_argument("--custom-json", required=True)
    parser.add_argument("--cache-json", required=True)
    parser.add_argument("--repo-dir", default=None,
                        help="iso_download 目录(含 update_distributions.py), 默认按脚本位置推导")
    args = parser.parse_args()

    custom_path = Path(args.custom_json)
    cache_path = Path(args.cache_json)

    if args.repo_dir:
        repo_dir = Path(args.repo_dir)
    else:
        repo_dir = Path(__file__).resolve().parent.parent / "iso_download"
    sys.path.insert(0, str(repo_dir))

    # 读取自定义源
    try:
        custom = json.loads(custom_path.read_text(encoding="utf-8"))
        if not isinstance(custom, list):
            custom = []
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 读取自定义源失败: {e}", file=sys.stderr)
        custom = []

    sources = [c for c in custom if c.get("strategy") in STRATEGIES]
    if not sources:
        print(">>> 无 strategy 类型的自定义源, 无需展开")
        return

    # 读取既有的展开缓存(刷新失败时保留旧结果, 避免清空已展示列表)
    existing: dict = {}
    try:
        if cache_path.exists():
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 读取缓存文件失败, 忽略旧缓存: {e}", file=sys.stderr)

    cache: dict = {}
    ok_count = 0
    for c in sources:
        key = cache_key(c)
        dist = c.get("distribution", "?")
        listing = c.get("listing_url", "")
        if listing and not _is_http_url(listing):
            print(f"[WARN] 拒绝非 http/https 的 listing_url, 跳过 {dist}: {listing}", file=sys.stderr)
            if key in existing:
                cache[key] = existing[key]
            continue
        print(f"{'=' * 60}\n>>> 展开发行版源: {dist} ({c.get('strategy')}) <- {listing}")
        try:
            entries = expand_source(c)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 展开失败 {dist}: {e!r}", file=sys.stderr)
            if key in existing:
                cache[key] = existing[key]
            continue
        if not entries:
            print(f"[WARN] 展开为空 {dist}, 保留旧缓存(若有)", file=sys.stderr)
            if key in existing:
                cache[key] = existing[key]
            continue
        cache[key] = {"entries": entries, "updated_at": int(time.time())}
        ok_count += 1
        print(f">>> 展开 {len(entries)} 个条目: {[e.get('download_url', '').rsplit('/', 1)[-1] for e in entries]}")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f">>> 自定义源展开完成: {ok_count}/{len(sources)} 个源成功, 缓存写入 {cache_path}")
    if ok_count < len(sources):
        sys.exit(1)


if __name__ == "__main__":
    main()