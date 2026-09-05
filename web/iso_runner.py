#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ISO Hub - 选择部分发行版/版本下载的辅助 runner。

复用上游 download_linux.py 的 LinuxDistributionDownloader，但只下载
命令行传入的选定条目，并禁用其"清理整组目录"的行为（清理由 UI 上
单独的"清理过期"动作触发），避免误删用户未选择的同组 ISO。
"""
import argparse
import json
import sys
from pathlib import Path

import requests


def _head_target_size(url: str, headers: dict) -> int:
    """对下载 URL 发 HEAD 请求获取目标文件字节数(总大小), 用于 UI 进度条。失败返回 0。"""
    try:
        r = requests.head(url, headers=headers, timeout=15, allow_redirects=True)
        if r.status_code < 400 and r.headers.get("content-length"):
            return int(r.headers["content-length"])
    except Exception:  # noqa: BLE001
        pass
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ISO Hub selective download runner")
    parser.add_argument("--json-file", required=True, help="distributions.json path")
    parser.add_argument("--download-dir", required=True, help="ISO download dir")
    parser.add_argument(
        "--select", required=True, help='JSON array of {"distribution","download_url"}'
    )
    args = parser.parse_args()

    selected = json.loads(args.select)
    repo_dir = Path(__file__).resolve().parent.parent / "iso_download"
    sys.path.insert(0, str(repo_dir))

    from download_linux import LinuxDistributionDownloader  # noqa: E402

    downloader = LinuxDistributionDownloader(args.json_file, args.download_dir)
    # 禁用整组清理：只做增量校验/下载，旧文件保留等用户手动清理
    downloader.cleanup_distribution_dir = lambda *a, **k: None

    all_entries = downloader.distributions.get("distributions", [])
    wanted = {(e.get("distribution"), e.get("download_url")) for e in selected}
    subset = [
        e for e in all_entries if (e.get("distribution"), e.get("download_url")) in wanted
    ]

    if not subset:
        print("错误: 没有匹配到任何选定的发行版条目", file=sys.stderr)
        sys.exit(1)

    # 按发行版名分组，逐组调用下载逻辑（组内一次下载多个版本文件）
    names = []
    for entry in subset:
        if entry["distribution"] not in names:
            names.append(entry["distribution"])

    failed = False
    for name in names:
        group = [e for e in subset if e["distribution"] == name]
        downloader.distributions = {"distributions": group}
        print(f"\n{'='*60}\n>>> 任务组: {name}")
        # 预先 HEAD 探测每个目标文件的总大小, 供 UI 显示下载进度条
        for entry in group:
            fname = entry["download_url"].rstrip("/").rsplit("/", 1)[-1]
            target_path = Path(downloader.download_dir) / entry["type"] / entry["distribution"] / fname
            total = _head_target_size(entry["download_url"], downloader.headers)
            # 标记行由后端拦截收集, 不写入任务日志
            print(f"#TARGET {target_path} {total}")
            if total:
                print(f"  目标大小: {total/1024/1024:.1f} MiB")
        ok = downloader.download_distribution(name, verify_checksum=True)
        if not ok:
            failed = True

    if failed:
        sys.exit(1)
    print("\n>>> 所有选定条目处理完成")


if __name__ == "__main__":
    main()
