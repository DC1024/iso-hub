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
import os
import sys
import time
import types
from pathlib import Path

import requests

ALLOWED_TYPES = {"linux", "bsd", "windows", "macos"}
PART_SUFFIX = ".part"
# ISO 存放模式: classified=<type>/<发行版>/ 分类存放; flat=统一单目录。
# 必须与 web/app.py 的 STORAGE_MODES / FLAT_ISO_DIRNAME 保持一致。
STORAGE_MODES = ("classified", "flat")
FLAT_DIRNAME = "iso"

# P1-⑤b: GPG 验证状态账本(同目录模块; 每次验签后落账, 供 /api/health 汇总)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpg_ledger  # noqa: E402


class TruncatedTransfer(Exception):
    """响应流提前结束: 有预期长度但实际写入不足。

    语义: 传输被截断, 已写入的字节仍是**有效前缀**, 应保留 .part 供下次续传,
    不能当作"文件损坏"丢弃 —— 否则一次网络抖动就丢掉全部已下进度。
    """


class CorruptPayload(Exception):
    """传输完成(或长度未知)但内容校验不通过。

    语义: 落盘数据不可信, 应丢弃 .part 重下, 避免下次拿着坏数据续传。
    """



def _safe_dist_dir(download_dir: Path, typ: str, name: str,
                   flat: bool = False) -> Path | None:
    """把 (type, name) 安全解析为该发行版 ISO 的**存放目录**, 拒绝路径穿越/非法字符。

    校验顺序刻意放在模式判断之前: 非法 (type, name) 在任何存放模式下都返回 None,
    避免"切到 flat 就让穿越输入蒙混过关"。返回目录随 `flat` 变化:
      * flat=False(默认, classified) -> download_dir/<type>/<name>/
      * flat=True                    -> download_dir/iso/  (全发行版平铺同一目录)

    `flat` 由 `main()` 经 `--storage-mode` 传入(本文件**不读** settings.json,
    保持 iso_runner 不直接触碰共享配置文件的约束)。
    """
    if not typ or not name or typ not in ALLOWED_TYPES:
        return None
    for comp in (typ, name):
        comp = str(comp)
        if comp != comp.strip() or comp in (".", ".."):
            return None
        if "/" in comp or "\\" in comp:
            return None
    target = (download_dir / FLAT_DIRNAME) if flat else (download_dir / typ / name)
    target = target.resolve()
    try:
        target.relative_to(download_dir.resolve())
    except ValueError:
        return None
    return target


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


def _record_failure(data_dir, typ: str, name: str, fname: str, kind: str) -> None:
    """把文件级失败写进 download_failures.json(供前端显示「下载失败/下载停止」)。

    kind="hard"    半成品已清理, 下次只能从头下 → 下载失败
    kind="stopped" 半成品保留, 下次可续传     → 下载停止
    """
    try:
        jf = Path(data_dir) / "download_failures.json"
        data = {}
        if jf.exists():
            try:
                data = json.loads(jf.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                data = {}
        data[f"{typ}/{name}/{fname}"] = {"at": int(time.time()), "kind": kind}
        tmp = jf.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(jf)
    except Exception:  # noqa: BLE001
        pass


def _clear_failure(data_dir, typ: str, name: str, fname: str) -> None:
    """下载成功后清除该文件的失败记录。"""
    try:
        jf = Path(data_dir) / "download_failures.json"
        if not jf.exists():
            return
        data = json.loads(jf.read_text(encoding="utf-8")) or {}
        rel = f"{typ}/{name}/{fname}"
        if rel in data:
            data.pop(rel, None)
            tmp = jf.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(jf)
    except Exception:  # noqa: BLE001
        pass


def _resolve_total(resp, have: int, head_total: int) -> int:
    """确定这次传输的**完整文件应有字节数**, 三级兜底, 拿不到返回 0。

    优先级(从最可靠到最兜底):
      1. Content-Range: bytes a-b/TOTAL —— 服务器明确告知全长, 最可信;
      2. Content-Length + have —— 续传时是"本次剩余量", 加上已下部分即全长;
      3. head_total —— 调用方事先 HEAD 探测的大小, 服务器不给长度时的兜底。

    v1.2.8 及以前只看 1/2, 两者都缺失时 total=0, 导致后续完整性检查被
    `if total and ...` 短路跳过 —— 这正是"没下载完就去算校验和"的根因。
    引入第 3 级 + 显式返回 0 的语义(0 = 确实无从判断), 让调用方能区分
    "确定不完整"与"无法判断"。
    """
    # 1) Content-Range 全长
    cr = resp.headers.get("content-range") or ""
    if "/" in cr:
        try:
            n = int(cr.rsplit("/", 1)[-1])
            if n > 0:
                return n
        except ValueError:
            pass
    # 2) Content-Length(+ 已下的 have)
    try:
        cl = int(resp.headers.get("content-length") or 0)
    except ValueError:
        cl = 0
    if cl > 0:
        return have + cl
    # 3) HEAD 预取大小兜底
    if head_total > 0:
        return head_total
    return 0


def _download_file_with_failover(downloader, target_dist: dict, candidates, filename: str,
                                 dist_dir, filepath, head_total: int = 0) -> tuple:
    """逐个候选源下载同一文件, 失败/校验失败自动切换下一候选源。

    返回 (成功与否, 实际使用的下载URL)。全部候选失败返回 (False, None)。

    落盘协议(关键): 下载期间一律写 ``<最终名>.part``, 只有"大小校验 + 校验和"
    全部通过后才 ``os.replace`` 原子改名为最终文件名。这样:

      * 进程被「停止任务」SIGTERM/SIGKILL 杀掉时, 磁盘上留下的是 ``xxx.iso.part``,
        后端 disk_inventory() 能识别为半成品 → 显示「下载停止」而不是「已下载」;
      * 半成品绝不会以最终文件名出现在磁盘上, 杜绝"残缺文件被当成完整 ISO"。

    完整性判据(v1.2.9 修复):
      * 期望长度由 ``_resolve_total()`` 三级兜底解析, 不再是"拿不到就跳过检查";
      * 实际不足 → TruncatedTransfer: **保留 .part 供续传**, 不计为损坏;
      * 长度达标但校验和不符 → CorruptPayload: 丢弃 .part 重下;
      * 长度完全未知且校验和不符 → 无法区分截断与损坏 → **保守保留 .part**,
        只切下一个候选源(宁可多占点磁盘, 也不误删用户已下的进度)。

    head_total: 调用方 HEAD 预取的大小, 长度信息缺失时用于兜底判据。
    """
    part = Path(str(filepath) + PART_SUFFIX)
    last_err = None
    for idx, (url, checksum_url) in enumerate(candidates):
        print(f"  候选源 {idx + 1}/{len(candidates)}: {url}")
        # 断点续传: 已存在的 .part 就是本源的断点位置。
        # 注意每个候选源都要重新评估(不同镜像上的文件可能不同, 故续传只在本源
        # 首次失败后保留, 换源时从该源的断点重新开始)。
        try:
            have = part.stat().st_size if part.exists() else 0
            headers = dict(downloader.headers or {})
            if have:
                headers["Range"] = f"bytes={have}-"
            resp = requests.get(url, headers=headers, stream=True, timeout=60)
            if have and resp.status_code == 206:
                # 服务器支持 Range: 追加写入
                total = _resolve_total(resp, have, head_total)
                if total:
                    print(f"  续传: 从 {have/1024/1024:.1f} MiB 继续 "
                          f"(共 {total/1024/1024:.1f} MiB)")
                else:
                    print(f"  续传: 从 {have/1024/1024:.1f} MiB 继续 "
                          f"(服务器未提供总大小, 将无法核对完整性)")
                mode = "ab"
            elif have and resp.status_code == 200:
                # 服务器不支持 Range(返回 200 全量) → 只能从头下, 截断重写
                total = _resolve_total(resp, 0, head_total)
                print(f"  服务器不支持断点续传, 从头下载 (已丢弃 {have/1024/1024:.1f} MiB)")
                have = 0
                mode = "wb"
            else:
                # 无 .part(全新下载) 或 Range 返回 416(断点已越界)等
                if resp.status_code == 416:
                    raise Exception("断点位置越界(416), 将重下")
                total = _resolve_total(resp, 0, head_total)
                mode = "wb"
                have = 0
            resp.raise_for_status()

            # 下载到 .part; 下载途中被 kill 也会留下 .part 供识别
            with open(part, mode) as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            written = part.stat().st_size

            # ---- 完整性判据(v1.2.9) ----
            # 期望长度已知却不足 → 传输被截断。注意这里**不再跳过检查**:
            # 只有 total 确实为 0(三级兜底全都拿不到)时才无从判断。
            if total and written < total:
                raise TruncatedTransfer(
                    f"传输未完成: 期望 {total}B, 实际 {written}B "
                    f"(缺 {total - written}B, 可续传)")
            if total and written > total:
                # 超出预期长度: 落盘内容不可信, 丢弃重下
                raise CorruptPayload(f"大小超出预期: 期望 {total}B, 实际 {written}B")
            if not total:
                print("  ⚠ 服务器未提供文件总大小, 无法核对完整性, 直接交由校验和判定")

            # 校验和优先跟随当前候选源自身; 无则回退 entry 存储值
            # 必须传 dist=target_dist: 否则 GPG 验签会被静默跳过(漏传 dist 的回归)
            success, msg = downloader.verify_checksum_smart(
                part, checksum_url, target_dist.get("checksum"), dist=target_dist
            )
            gpg_ledger.record_from_msg(target_dist, success, msg)
            if success:
                print(f"  ✓ {msg}")
                # 全部校验通过 → 原子改名为最终文件名(此刻才"看起来"下载完成)
                if filepath.exists():
                    filepath.unlink()
                os.replace(part, filepath)
                return True, url

            # 校验和不符: 区分"尺寸已达标 → 内容损坏"与"尺寸未知 → 无法判定"
            print(f"  ✗ 校验和验证失败: {msg}")
            if total:
                # 尺寸与预期一致却校验不过 → 内容确实损坏, 丢弃避免坏数据被续传
                try:
                    if part.exists():
                        part.unlink()
                except OSError:
                    pass
                print("  半成品尺寸正确但内容校验不符, 已丢弃(避免坏数据被续传)")
            else:
                # 尺寸未知: 可能是截断(可续传)也可能是损坏, 无法区分 → 保守保留
                print("  无法确认是否为传输截断, 保守保留半成品供下次续传")
                raise TruncatedTransfer("校验失败且长度未知, 保守保留半成品")
        except TruncatedTransfer as e:
            # 传输截断: 半成品是有效前缀, **保留**供下次续传, 不当作损坏
            last_err = e
            print(f"  ✗ 传输被截断, 保留半成品待续传: {e}")
        except CorruptPayload as e:
            last_err = e
            print(f"  ✗ 响应内容不可信, 丢弃半成品: {e}")
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
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
    parser.add_argument(
        "--storage-mode", default="classified", choices=list(STORAGE_MODES),
        help="ISO 存放模式: classified=按 <类型>/<发行版>/ 分类(默认), flat=统一单目录",
    )
    args = parser.parse_args()

    selected = json.loads(args.select)
    repo_dir = Path(__file__).resolve().parent.parent / "iso_download"
    sys.path.insert(0, str(repo_dir))

    from download_linux import LinuxDistributionDownloader  # noqa: E402

    downloader = LinuxDistributionDownloader(args.json_file, args.download_dir)
    # 把存放模式透传给上游下载器(其内部 _safe_dist_dir 也按同一模式落盘)
    downloader.storage_mode = args.storage_mode
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
            dist_dir = _safe_dist_dir(Path(downloader.download_dir), entry.get("type", "linux"), entry.get("distribution", ""), flat=args.storage_mode == "flat")
            if dist_dir is None:
                print(f"错误: 发行版 {entry.get('distribution')} 的 type/distribution 不合法, 跳过", file=sys.stderr)
                failed = True
                continue
            dist_dir.mkdir(parents=True, exist_ok=True)
            filepath = dist_dir / fname

            # 依据策略选出有序候选源列表
            candidates = _pick_candidates(args.strategy, entry, downloader.headers)
            primary = candidates[0][0] if candidates else entry["download_url"]

            # 预先 HEAD 探测默认源的目标文件大小, 供 UI 显示下载进度条。
            # 注意上报的是 .part 路径: 下载期间字节都写在 .part 上(完成后才改名为
            # filepath), 后端 running_task() 对该路径 stat() 才能得到真实进度。
            # HEAD 失败时回退到候选源逐个探测(v1.2.9): 只探测第一个源的话, 该源
            # 恰好故障就会让 total=0, UI 进度条失去百分比基准(与"卡 0%"同类症状)。
            total = _head_target_size(primary, downloader.headers)
            if not total:
                for _u, _c in candidates[1:]:
                    total = _head_target_size(_u, downloader.headers)
                    if total:
                        print(f"  默认源未返回大小, 改用候选源探测: {total/1024/1024:.1f} MiB")
                        break
            part_path = Path(str(filepath) + PART_SUFFIX)
            # 标记行由后端拦截收集, 不写入任务日志
            print(f"#TARGET {part_path} {total}")
            if total:
                print(f"  目标大小: {total/1024/1024:.1f} MiB")
            else:
                print("  ⚠ 所有候选源均未返回文件大小, 进度条将无法显示百分比")

            if filepath.exists():
                print(f"文件已存在: {filepath}")
                # 必须传 dist=entry: 否则 GPG 验签被静默跳过(漏传 dist 的回归)
                ok, msg = downloader.verify_checksum_smart(
                    filepath, entry.get("checksum_url"), entry.get("checksum"), dist=entry
                )
                gpg_ledger.record_from_msg(entry, ok, msg)
                if ok:
                    print(f"✓ {msg}")
                    _clear_failure(args.download_dir, entry.get("type", "linux"), name, fname)
                    continue
                print(f"✗ {msg}")
                print("校验和验证失败, 将重新下载")

            ok, used_url = _download_file_with_failover(
                downloader, entry, candidates, fname, dist_dir, filepath, head_total=total
            )
            if not ok:
                failed = True
                # 半成品(.part)保留 → 下次可续传, 记 stopped; 连 .part 都没有记 hard
                kind = "stopped" if part_path.exists() else "hard"
                _record_failure(args.download_dir, entry.get("type", "linux"), name, fname, kind)
            else:
                _clear_failure(args.download_dir, entry.get("type", "linux"), name, fname)

    if failed:
        sys.exit(1)
    print("\n>>> 所有选定条目处理完成")


if __name__ == "__main__":
    main()
