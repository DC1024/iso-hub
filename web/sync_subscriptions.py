#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ISO Hub - 订阅模式同步 runner。

对每个"已启用订阅"的发行版:
  1. 在(官方+自定义合并后的)清单中,按文件名自然排序取最新 N 个条目
  2. 下载这些最新条目
  3. 清理该组目录中不属于最新 N 的过期 ISO(保留用户手动添加的其他文件)

用法:
  sync_subscriptions.py --json-file <distributions.json>
                         --download-dir <dir>
                         --subscriptions '<json>'
                         [--update-first <sources_config.json>]
"""
import argparse
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import requests

ALLOWED_TYPES = {"linux", "bsd", "windows", "macos"}


def natural_key(value: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", value)]


def _safe_dist_dir(download_dir: Path, typ: str, name: str) -> Path | None:
    """把 (type, name) 安全拼接为 download_dir 下的路径, 拒绝路径穿越/非法字符。"""
    if not typ or not name or typ not in ALLOWED_TYPES:
        return None
    for comp in (typ, name):
        comp = str(comp)
        if comp != comp.strip() or comp in (".", ".."):
            return None
        if "/" in comp or "\\" in comp:
            return None
    target = (download_dir / typ / name).resolve()
    try:
        target.relative_to(download_dir.resolve())
    except ValueError:
        return None
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="ISO Hub subscription sync runner")
    parser.add_argument("--json-file", required=True)
    parser.add_argument("--download-dir", required=True)
    parser.add_argument("--subscriptions", required=True)
    parser.add_argument("--update-first", default=None,
                        help="若给定 sources_config.json，先刷新一次官方清单元数据")
    parser.add_argument("--custom-json", default=None,
                        help="若给定，把其中的自定义源条目并入候选池")
    parser.add_argument("--cache-json", default=None,
                        help="自定义源展开缓存(custom_repo_cache.json)，strategy 源从中读取而非实时抓取")
    args = parser.parse_args()

    subs = json.loads(args.subscriptions)
    repo_dir = Path(__file__).resolve().parent.parent / "iso_download"
    sys.path.insert(0, str(repo_dir))

    # 在上游 download_linux 被 import 之前 mock 掉 tqdm。
    # 上游下载器内部用 tqdm 的 \r(回车) 覆盖式进度条输出(无换行),
    # 后端 _spawn_worker 按行读取子进程 stdout 时会被阻塞, 导致日志/进度不实时刷新。
    # 这里用无输出的 stub 替换, 前端进度靠后端跑文件 stat(size/total) 实现。
    _tqdm_stub = types.ModuleType("tqdm")

    class _NoopTqdm:
        def __init__(self, *a, **k):
            pass

        def update(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            pass

        def close(self):
            pass

    _tqdm_stub.tqdm = lambda *a, **k: _NoopTqdm()
    sys.modules["tqdm"] = _tqdm_stub

    # 复用上游下载器（必须在使用前导入，路径已加入 sys.path）
    from download_linux import LinuxDistributionDownloader  # noqa: E402

    # 可选: 先刷新官方源元数据
    if args.update_first:
        print(">>> 步骤1/3 刷新官方源清单元数据 ...", flush=True)
        up = subprocess.run(
            [sys.executable, str(repo_dir / "update_distributions.py"),
             "--config", args.update_first,
             "--output", args.json_file, "--pretty"],
            cwd=str(repo_dir), text=True,
        )
        if up.returncode != 0:
            print("[WARN] 元数据刷新有告警，继续订阅同步", file=sys.stderr)

    # 加载当前清单 + 自定义源,合并为候选池
    data = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    all_entries = data.get("distributions", [])
    if args.custom_json and Path(args.custom_json).exists():
        try:
            custom = json.loads(Path(args.custom_json).read_text(encoding="utf-8"))
            if isinstance(custom, list):
                by_url = {e["download_url"]: e for e in all_entries}
                # strategy 源的展开结果从持久化缓存读取(由 custom_repo_refresh.py 后台写入),
                # 订阅同步进程内不再对镜像站发起实时抓取。
                repo_cache = {}
                if args.cache_json and Path(args.cache_json).exists():
                    try:
                        cached = json.loads(Path(args.cache_json).read_text(encoding="utf-8"))
                        if isinstance(cached, dict):
                            repo_cache = cached
                    except Exception as e:  # noqa: BLE001
                        print(f"[WARN] 读取自定义源缓存失败: {e}", file=sys.stderr)
                for c in custom:
                    if c.get("strategy"):
                        key = json.dumps(
                            {k: v for k, v in c.items() if k != "timeout"},
                            sort_keys=True, ensure_ascii=False, default=str,
                        )
                        hit = repo_cache.get(key)
                        entries = hit.get("entries") if isinstance(hit, dict) else None
                        if isinstance(entries, list):
                            for e in entries:
                                e.setdefault("distribution", c.get("distribution", "?"))
                                e.setdefault("type", c.get("type", "linux"))
                                by_url[e["download_url"]] = e
                        else:
                            print(f"[WARN] 发行版源 {c.get('distribution')} 无缓存, 已跳过(请先刷新自定义源)", file=sys.stderr)
                    elif c.get("download_url"):
                        by_url[c["download_url"]] = c
                all_entries = list(by_url.values())
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 读取自定义源失败: {e}", file=sys.stderr)
    download_dir = Path(args.download_dir)

    failed = False
    for sub in subs:
        if not sub.get("enabled", True):
            continue
        name = sub.get("distribution")
        typ = sub.get("type", "linux")
        try:
            keep = max(1, int(sub.get("keep", 2)))
        except (ValueError, TypeError):
            print(f"[WARN] 订阅 {name} 的 keep 值非法, 使用默认值 2", file=sys.stderr)
            keep = 2
        # 路径穿越防护: distribution/type 必须合法
        target = _safe_dist_dir(download_dir, typ, name)
        if target is None:
            print(f"[WARN] 订阅 {typ}/{name} 的 distribution/type 不合法, 跳过", file=sys.stderr)
            continue
        print(f"\n{'='*60}\n>>> 订阅同步: {typ}/{name} (保留最新 {keep} 版)")

        pool = [
            e for e in all_entries
            if e.get("distribution") == name and e.get("type") == typ
        ]
        if not pool:
            print(f"[WARN] 清单中无 {name} 条目，跳过")
            continue

        # 按文件名自然排序取最新 N 个
        def fname_of(e):
            return e.get("download_url", "").rstrip("/").rsplit("/", 1)[-1]
        pool.sort(key=lambda e: natural_key(fname_of(e)), reverse=True)
        keep_entries = pool[:keep]
        keep_fnames = {fname_of(e) for e in keep_entries}
        print(f"待下载: {len(keep_entries)} 个 -> {[fname_of(e) for e in keep_entries]}")

        # 下载前先 HEAD 探测每个待下载条目的目标文件大小, 打印 #TARGET 哨兵行。
        # 后端 web/app.py 拦截该行填充 task["targets"](path->字节数),
        # 再结合 running_task() 的 stat(size) 计算下载进度百分比。
        # 与手动勾选下载(web/iso_runner.py)走同一套行协议, 保持前端进度条一致。
        # 真实下载路径 = download_dir/typ/name/<URL文件名>, 与 download_linux.py 一致。
        for _e in keep_entries:
            _fn = _e.get("download_url", "").rstrip("/").rsplit("/", 1)[-1]
            _fp = target / _fn
            _sz = 0
            try:
                _r = requests.head(_e["download_url"], timeout=(10, 30), allow_redirects=True)
                _r.raise_for_status()
                _sz = int(_r.headers.get("Content-Length", 0) or 0)
            except Exception:  # noqa: BLE001
                _sz = 0
            print(f"#TARGET {_fp} {_sz}", flush=True)

        # 下载最新 N 个
        downloader = LinuxDistributionDownloader(args.json_file, str(download_dir))
        downloader.cleanup_distribution_dir = lambda *a, **k: None
        downloader.distributions = {"distributions": keep_entries}
        ok = downloader.download_distribution(name, verify_checksum=True)
        if not ok:
            failed = True

        # 清理该组不在最新 N 内的过期 ISO
        # target 已由 _safe_dist_dir 校验, 确定落在 download_dir 内
        removed = []
        # 受保护名单(settings.json 的 protected, 相对路径或文件名)
        protected = set()
        hist = {}
        try:
            stj = json.loads((download_dir / "settings.json").read_text(encoding="utf-8"))
            protected = set(stj.get("protected", []) or [])
            hist = stj.get("manifest_history", {}) or {}
        except Exception:  # noqa: BLE001
            protected = set()
        # B7 修复: 先把当前清单文件名并入历史(只删"曾出现在清单里"的文件, 保护用户自有 ISO)
        known = set(hist.get(f"{typ}/{name}", []) or [])
        know_set_before = set(known)
        for e in pool:
            fn = fname_of(e)
            if fn:
                known.add(fn)
        if known != know_set_before:
            hist[f"{typ}/{name}"] = sorted(known)
            try:
                cur = json.loads((download_dir / "settings.json").read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                cur = {}
            cur["manifest_history"] = hist
            (download_dir / "settings.json").write_text(
                json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        if target.exists():
            for f in target.iterdir():
                if (
                    f.is_file()
                    and f.name not in keep_fnames
                    and f.suffix.lower() in {".iso", ".img", ".qcow2", ".vmdk"}
                ):
                    rel = f"{typ}/{name}/{f.name}"
                    if rel in protected or f.name in protected:
                        print(f"[SKIP] 受保护, 跳过: {rel}")
                        continue
                    # B7 修复: 只删"曾出现在历史清单中"的文件, 保留用户手动放入/种子下载的 ISO
                    if f.name not in known:
                        print(f"[SKIP] 非清单文件, 保留: {rel}")
                        continue
                    try:
                        f.unlink()
                        removed.append(f.name)
                    except OSError as e:
                        print(f"[WARN] 删除失败 {f.name}: {e}", file=sys.stderr)
        print(f"清理过期: {len(removed)} 个 -> {removed}")

    if failed:
        sys.exit(1)
    print("\n>>> 所有订阅同步完成")


if __name__ == "__main__":
    main()