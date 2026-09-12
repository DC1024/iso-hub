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
import time
import types
from pathlib import Path

import requests

ALLOWED_TYPES = {"linux", "bsd", "windows", "macos"}
# ISO 存放模式(与 web/app.py 的 STORAGE_MODES / FLAT_ISO_DIRNAME 一致)。
# 由 --storage-mode 传入(主进程 app.py 决定), 本文件不读 settings.json。
STORAGE_MODES = ("classified", "flat")
FLAT_DIRNAME = "iso"


def _warn(msg: str) -> None:
    """订阅同步是被 Popen 出来的子进程: 它的 stdout 会被主进程按行读取并展示,
    告警走 **stderr** 才不会污染进度流。"""
    print(msg, file=sys.stderr, flush=True)


def _on_settings_quarantined(backup) -> None:
    """settings.json 损坏并被隔离后的告警(子进程版本)。"""
    _warn("[sync] settings.json 不是合法 JSON 对象, 原文件已备份到 %s; "
          "本次以空配置写回。" % backup)

# P1-⑤b: GPG 验证状态账本(同目录模块; 订阅同步路径验签后同样落账)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpg_ledger  # noqa: E402
# settings.json 的共享读写通道(加锁 + 原子写 + 损坏隔离)。
# 注意: 本脚本是被 app.py **Popen 出来的独立进程**, 所以线程锁这部分用不上 ——
# 与主进程的互斥必须要文件锁才做得到。这里真正拿到的是**原子写**: 见下方注释。
import config_files  # noqa: E402


def natural_key(value: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", value)]


def _failures_path(download_dir: Path) -> Path:
    return Path(download_dir) / "download_failures.json"


def _record_failure(download_dir: Path, typ: str, name: str, fname: str, kind: str) -> None:
    """记录文件级下载失败(供前端显示「下载失败 / 下载停止」)。"""
    try:
        jf = _failures_path(download_dir)
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


def _clear_failure(download_dir: Path, typ: str, name: str, fname: str) -> None:
    try:
        jf = _failures_path(download_dir)
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


def _last_run_verified(downloader, entry: dict, fp: Path) -> bool:
    """判断某文件本次下载后是否已通过校验(通过则说明是完整文件, 不是半成品)。"""
    if not fp.exists():
        return False
    try:
        # 必须传 dist=entry: 否则 GPG 验签被静默跳过(漏传 dist 的回归)
        ok, msg = downloader.verify_checksum_smart(
            fp, entry.get("checksum_url"), entry.get("checksum"), dist=entry
        )
        gpg_ledger.record_from_msg(entry, ok, msg)
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


def _safe_dist_dir(download_dir: Path, typ: str, name: str,
                   flat: bool = False) -> Path | None:
    """把 (type, name) 安全解析为该发行版 ISO 的**存放目录**, 拒绝路径穿越/非法字符。

    校验顺序刻意放在模式判断之前 —— 非法 (type, name) 在任何模式下都返回 None。
      * flat=False(默认) -> download_dir/<type>/<name>/
      * flat=True        -> download_dir/iso/  (全发行版平铺同一目录)
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
    parser.add_argument("--storage-mode", default="classified", choices=list(STORAGE_MODES),
                        help="ISO 存放模式: classified=按类型/发行版分类(默认), flat=统一单目录")
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
    from download_linux import LinuxDistributionDownloader, PART_SUFFIX  # noqa: E402

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
        target = _safe_dist_dir(download_dir, typ, name, flat=args.storage_mode == "flat")
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
        #
        # v1.3.2 修复(进度条卡住根因): 这里上报的路径必须是 **.part 路径**。
        # download_linux.py 在下载期间把字节写在 <最终名>.part 上, 完成后才
        # os.replace 成最终名。旧实现上报最终名, 于是 running_task() 对最终名
        # stat(): 文件尚不存在 → size=0(进度恒 0%); 若该文件已下载完成, 则读到
        # 一个静止的完整大小 → 分子被垫高后不再变化(实测卡在 50%)。
        # 两种症状同源: 上报的名字和真正在增长的文件不是同一个。
        for _e in keep_entries:
            _fn = _e.get("download_url", "").rstrip("/").rsplit("/", 1)[-1]
            _fp = target / _fn
            _part_fp = target / (_fn + PART_SUFFIX)
            _sz = 0
            try:
                _r = requests.head(_e["download_url"], timeout=(10, 30), allow_redirects=True)
                _r.raise_for_status()
                _sz = int(_r.headers.get("Content-Length", 0) or 0)
            except Exception:  # noqa: BLE001
                _sz = 0
            # 已存在完整文件 → 本轮不会重新下载(download_linux 会先校验再跳过),
            # 它的贡献恒为满值。此时把目标大小夹到本地实际大小, 让这一条在聚合里
            # 天然是 100%, 而不是"本地 1.5GB / 远端 1.5GB"这种看似在下载的幻象。
            try:
                if _fp.exists() and not _part_fp.exists():
                    _local = _fp.stat().st_size
                    if _local > 0:
                        _sz = _local
            except OSError:
                pass
            print(f"#TARGET {_part_fp} {_sz}", flush=True)

        # 下载最新 N 个
        downloader = LinuxDistributionDownloader(args.json_file, str(download_dir))
        # 与 target 用同一模式落盘(否则本脚本的簿记路径与真实落盘路径会分叉)
        downloader.storage_mode = args.storage_mode
        downloader.cleanup_distribution_dir = lambda *a, **k: None
        downloader.distributions = {"distributions": keep_entries}
        # 逐文件记录失败: 订阅同步用的是上游 download_distribution(多文件循环),
        # 无法从返回值区分是哪个文件失败, 故先快照再逐条比对。
        # 注意: 下载中途数据写在 <最终名>.part 上, 完成后才改名为 _fp。
        # 所以快照要把两处大小相加, 否则"下载中 .part 增长"会被当成无变化。
        def _size_of(fp):
            n = 0
            for p in (fp, fp.with_name(fp.name + ".part")):
                try:
                    n += p.stat().st_size if p.exists() else 0
                except OSError:
                    pass
            return n

        _before = {}
        for _e in keep_entries:
            _fn = _e.get("download_url", "").rstrip("/").rsplit("/", 1)[-1]
            _before[_fn] = _size_of(target / _fn)
        ok = downloader.download_distribution(name, verify_checksum=True)
        if not ok:
            failed = True
        for _e in keep_entries:
            _fn = _e.get("download_url", "").rstrip("/").rsplit("/", 1)[-1]
            _fp = target / _fn
            _part = _fp.with_name(_fp.name + ".part")
            try:
                _after = _fp.stat().st_size if _fp.exists() else -1
            except OSError:
                _after = -1
            _part_exists = _part.exists()
            if _after >= 0 and _part_exists:
                # 最终文件与 .part 并存(异常残留): 半成品未落定 → 下载停止
                _record_failure(download_dir, typ, name, _fn, "stopped")
            elif _after < 0 and _part_exists:
                # 只留下 .part: 下载被中断/停止, 可续传 → 下载停止
                _record_failure(download_dir, typ, name, _fn, "stopped")
            elif _after < 0:
                # 文件未落盘且无半成品: 完全没能下载 → 下载失败(不可续传)
                _record_failure(download_dir, typ, name, _fn, "hard")
            elif _size_of(_fp) != _before.get(_fn, -1) and not _last_run_verified(downloader, _e, _fp):
                # 本次有变动但校验未过 → 下载失败
                _record_failure(download_dir, typ, name, _fn, "hard")
            else:
                _clear_failure(download_dir, typ, name, _fn)

        # 清理该组不在最新 N 内的过期 ISO
        # target 已由 _safe_dist_dir 校验, 确定落在 download_dir 内
        removed = []
        # 受保护名单(settings.json 的 protected, 相对路径或文件名)
        protected = set()
        hist = {}
        # v1.3.16: 解析失败不再静默回退 —— protected 被当成"空的"会让本轮清理
        # 删掉用户本来受保护的文件, 这种事必须留下线索。
        # 注意这里**不**挪动文件(读取路径保持无副作用), 备份发生在真正要覆盖它的写入里。
        try:
            stj = config_files.read_json_raw(download_dir / "settings.json")
            protected = set(stj.get("protected", []) or [])
            hist = stj.get("manifest_history", {}) or {}
        except config_files.CorruptJsonFile:
            protected, hist = set(), {}
            _warn("[sync] settings.json 不是合法 JSON 对象, protected 名单按空处理 —— "
                  "本轮过期清理将只能依赖 manifest_history 之外的判据。")
        except Exception as e:  # noqa: BLE001  读盘/权限异常
            protected, hist = set(), {}
            _warn("[sync] 读取 settings.json 失败: %s" % e)
        # B7 修复: 先把当前清单文件名并入历史(只删"曾出现在清单里"的文件, 保护用户自有 ISO)
        known = set(hist.get(f"{typ}/{name}", []) or [])
        know_set_before = set(known)
        for e in pool:
            fn = fname_of(e)
            if fn:
                known.add(fn)
        if known != know_set_before:
            hist[f"{typ}/{name}"] = sorted(known)
            # v1.3.16: 走共享通道。这里过去是 `write_text` 裸写 —— 本进程一旦在写途中
            # 被中断(比如用户在 UI 上停止订阅同步), settings.json 就会留下半截内容,
            # 而**主进程**下一次读取只能靠 except 兜底, 于是凭据/用户/保护列表看起来
            # 全部"消失"。改成原子替换后, 主进程要么看到旧内容、要么看到完整新内容。
            # 跨进程互斥做不到(需要文件锁), 但那条竞争的最坏后果只是 manifest_history
            # 少记一次, 且随时会被下一次同步补回来。
            config_files.update_json(download_dir / "settings.json",
                                     {"manifest_history": hist},
                                     on_quarantine=_on_settings_quarantined)
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