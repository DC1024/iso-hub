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
import time
import types
from pathlib import Path

import requests

# 在上游 download_linux 被 import 之前 mock 掉 tqdm。
# 上游用 tqdm 的 \r(回车) 覆盖式进度条输出(无换行), 会阻塞后端按行读取子进程 stdout,
# 导致日志/状态不实时刷新(进度条卡 0%)、停止任务后才一次性 flush。这里用无输出的 stub 替换,
# 前端进度条靠后端跑文件 stat(size/total) 实现, 不依赖 tqdm 的细粒度进度。
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


def _head_target_size(url: str, headers: dict) -> int:
    """对下载 URL 发 HEAD 请求获取目标文件字节数(总大小), 用于 UI 进度条。失败返回 0。"""
    try:
        r = requests.head(url, headers=headers, timeout=15, allow_redirects=True)
        if r.status_code < 400 and r.headers.get("content-length"):
            return int(r.headers["content-length"])
    except Exception:  # noqa: BLE001
        pass
    return 0


def _pick_candidates(strategy: str, entry: dict, headers: dict):
    """根据选源策略, 返回有序的 [(url, checksum_url), ...] 候选列表。

    策略 A（固定优先级）: 保持 sources_config.json 里 mirrors 的配置顺序
      （清华在前, 官方源兜底）, 不额外请求。
    策略 B（实测选优）: 对每个候选源发 HEAD 探测, 按响应耗时排序,
      最快可达源排最前; 若全部探测失败, 回退到配置顺序。

    候选列表来源: entry 的 download_urls/checksum_urls（由 update_distributions.py
    按各镜像模板生成）。无多源时退化为单一 download_url。
    """
    urls = entry.get("download_urls") or [entry["download_url"]]
    cs_urls = entry.get("checksum_urls") or []
    cs = cs_urls + [None] * (len(urls) - len(cs_urls))  # 校验和不足的镜像补 None

    # 用户手动指定的源URL(entry.pin): 存在且是候选之一 → 强制移到最前
    pin = (entry.get("pin") or "").strip()
    if pin and pin in urls:
        i = urls.index(pin)
        urls = [urls[i]] + urls[:i] + urls[i + 1:]
        cs = [cs[i]] + cs[:i] + cs[i + 1:]

    if strategy != "B":
        # 策略 A: 固定优先级(配置顺序, pin 已置顶)
        return list(zip(urls, cs))

    # 策略 B: HEAD 实测各候选源, 选最快可达源
    # 若用户手动指定了源(pin 已置顶), 只要它可达就优先使用, 不参与速度排序
    pin = (entry.get("pin") or "").strip()
    scored = []
    pinned_ok = None
    for u, c in zip(urls, cs):
        try:
            t0 = time.monotonic()
            r = requests.head(u, headers=headers, timeout=8, allow_redirects=True)
            dt = time.monotonic() - t0
            if r.status_code < 400:
                if u == pin:
                    pinned_ok = (u, c)   # 用户指定的源可达, 直接置顶
                else:
                    scored.append((dt, u, c))
                print(f"  [策略B] 可达 {dt*1000:.0f}ms  {u}")
            else:
                print(f"  [策略B] HTTP {r.status_code} 跳过  {u}")
        except Exception:  # noqa: BLE001
            print(f"  [策略B] 不可达 跳过  {u}")
    if pinned_ok:
        rest = [x for x in sorted(scored, key=lambda x: x[0])]
        return [pinned_ok] + [(u, c) for _, u, c in rest]
    if scored:
        scored.sort(key=lambda x: x[0])
        return [(u, c) for _, u, c in scored]
    return list(zip(urls, cs))


def _download_file_with_failover(downloader, target_dist: dict, candidates, filename: str,
                                 dist_dir, filepath) -> tuple:
    """逐个候选源下载同一文件, 失败/校验失败自动切换下一候选源。

    返回 (成功与否, 实际使用的下载URL)。全部候选失败返回 (False, None)。
    """
    last_err = None
    for idx, (url, checksum_url) in enumerate(candidates):
        # 清除上一候选留下的不完整文件
        if filepath.exists():
            try:
                filepath.unlink()
            except Exception:  # noqa: BLE001
                pass
        print(f"  候选源 {idx + 1}/{len(candidates)}: {url}")
        try:
            resp = requests.get(url, headers=downloader.headers, stream=True, timeout=60)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            if total and filepath.stat().st_size != total:
                raise Exception(f"大小不匹配: 期望 {total}B, 实际 {filepath.stat().st_size}B")
            # 校验和优先跟随当前候选源自身; 无则回退 entry 存储值
            success, msg = downloader.verify_checksum_smart(
                filepath, checksum_url, target_dist.get("checksum")
            )
            if success:
                print(f"  ✓ {msg}")
                return True, url
            raise Exception(f"校验和验证失败: {msg}")
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  ✗ 候选源失败, 尝试下一源: {e}")
    if last_err is not None:
        print(f"  ✗ 该文件所有候选源均失败: {last_err}")
    return False, None


def main() -> None:
    parser = argparse.ArgumentParser(description="ISO Hub selective download runner")
    parser.add_argument("--json-file", required=True, help="distributions.json path")
    parser.add_argument("--download-dir", required=True, help="ISO download dir")
    parser.add_argument(
        "--select", required=True, help='JSON array of {"distribution","download_url"}'
    )
    parser.add_argument(
        "--strategy", default="A", choices=["A", "B"],
        help="多源选源策略: A=固定优先级(配置顺序, 默认), B=实测选最快可达源",
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
    pinmap = {e.get("distribution"): e.get("pin") or "" for e in selected}
    wanted = {(e.get("distribution"), e.get("download_url")) for e in selected}
    subset = [
        {**e, "pin": pinmap.get(e.get("distribution")) or ""}
        for e in all_entries if (e.get("distribution"), e.get("download_url")) in wanted
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
        print(f"\n{'='*60}\n>>> 任务组: {name}（选源策略: {'A 固定优先级' if args.strategy == 'A' else 'B 实测选优'}）")

        for entry in group:
            fname = entry["download_url"].rstrip("/").rsplit("/", 1)[-1]
            dist_dir = Path(downloader.download_dir) / entry["type"] / entry["distribution"]
            dist_dir.mkdir(parents=True, exist_ok=True)
            filepath = dist_dir / fname

            # 依据策略选出有序候选源列表
            candidates = _pick_candidates(args.strategy, entry, downloader.headers)
            primary = candidates[0][0] if candidates else entry["download_url"]

            # 预先 HEAD 探测默认源的目标文件大小, 供 UI 显示下载进度条
            total = _head_target_size(primary, downloader.headers)
            # 标记行由后端拦截收集, 不写入任务日志
            print(f"#TARGET {filepath} {total}")
            if total:
                print(f"  目标大小: {total/1024/1024:.1f} MiB")

            if filepath.exists():
                print(f"文件已存在: {filepath}")
                ok, msg = downloader.verify_checksum_smart(
                    filepath, entry.get("checksum_url"), entry.get("checksum")
                )
                if ok:
                    print(f"✓ {msg}")
                    continue
                print(f"✗ {msg}")
                print("校验和验证失败, 将重新下载")

            ok, used_url = _download_file_with_failover(
                downloader, entry, candidates, fname, dist_dir, filepath
            )
            if not ok:
                failed = True

    if failed:
        sys.exit(1)
    print("\n>>> 所有选定条目处理完成")


if __name__ == "__main__":
    main()
