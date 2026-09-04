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
from pathlib import Path


def natural_key(value: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", value)]


def main() -> None:
    parser = argparse.ArgumentParser(description="ISO Hub subscription sync runner")
    parser.add_argument("--json-file", required=True)
    parser.add_argument("--download-dir", required=True)
    parser.add_argument("--subscriptions", required=True)
    parser.add_argument("--update-first", default=None,
                        help="若给定 sources_config.json，先刷新一次官方清单元数据")
    parser.add_argument("--custom-json", default=None,
                        help="若给定，把其中的自定义源条目并入候选池")
    args = parser.parse_args()

    subs = json.loads(args.subscriptions)
    repo_dir = Path(__file__).resolve().parent.parent / "iso_download"
    sys.path.insert(0, str(repo_dir))

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
                sys.path.insert(0, str(repo_dir))
                for c in custom:
                    if c.get("strategy"):
                        # 发行版源: 展开为最新若干 ISO 条目
                        try:
                            from update_distributions import build_entries  # noqa: PLC0415
                            for e in build_entries(dict(c)):
                                e.setdefault("distribution", c.get("distribution", "?"))
                                e.setdefault("type", c.get("type", "linux"))
                                by_url[e["download_url"]] = e
                        except Exception as e:  # noqa: BLE001
                            print(f"[WARN] 发行版源展开失败 {c.get('distribution')}: {e}", file=sys.stderr)
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
        keep = max(1, int(sub.get("keep", 2)))
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

        # 下载最新 N 个
        downloader = LinuxDistributionDownloader(args.json_file, str(download_dir))
        downloader.cleanup_distribution_dir = lambda *a, **k: None
        downloader.distributions = {"distributions": keep_entries}
        ok = downloader.download_distribution(name, verify_checksum=True)
        if not ok:
            failed = True

        # 清理该组不在最新 N 内的过期 ISO
        target = download_dir / typ / name
        removed = []
        # 受保护名单(settings.json 的 protected, 相对路径或文件名)
        protected = set()
        try:
            stj = json.loads((download_dir / "settings.json").read_text(encoding="utf-8"))
            protected = set(stj.get("protected", []) or [])
        except Exception:  # noqa: BLE001
            protected = set()
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