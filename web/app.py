#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ISO Hub - 网页版 Linux 发行版 ISO 自动更新器

对上游 Sowevo/iso_download（纯 CLI）做 Web 封装：
  * GET  /                前端页面
  * GET  /api/distros     清单分组 + 本地磁盘状态
  * POST /api/download    下载选定条目(后台任务)
  * POST /api/update-meta 抓取镜像站刷新发行版清单
  * POST /api/prune       清理某组"不在当前清单"的过期 ISO
  * POST /api/stop        终止当前任务
  * GET  /api/state       当前任务状态(含实时文件大小)
  * GET  /api/logs        增量拉取任务日志

环境变量:
  ISO_REPO_DIR  上游脚本目录   (默认 /app/iso_download)
  ISO_DATA_DIR  数据卷(清单+ISO)(默认 /data)
  ISO_HUB_PORT  监听端口       (默认 8080)
"""
import os
import sys
import json
import re
import time
import shlex
import base64
import hashlib
import urllib.parse
import configparser
from time import struct_time  # 供 _sched_matches 类型注解
import threading
import subprocess
from pathlib import Path
from collections import deque

from flask import Flask, jsonify, request, send_from_directory

try:  # 种子下载集成(qBittorrent + DistroWatch 源) —— 可选加载
    from torrent_client import QBClient, qb_config, distro_name_from_torrent  # noqa: PLC0415
    import distro_torrents as dtorrents  # noqa: PLC0415
    TORRENT_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    TORRENT_AVAILABLE = False
    _torrent_import_err = str(e)

# --------------------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("ISO_REPO_DIR", "/app/iso_download"))
DATA_DIR = Path(os.environ.get("ISO_DATA_DIR", "/data"))
HOST, PORT = "0.0.0.0", int(os.environ.get("ISO_HUB_PORT", "8080"))  # nosec B104
PY = sys.executable
JSON_FILE = DATA_DIR / "distributions.json"
DEFAULT_JSON = REPO_DIR / "distributions.json"
CUSTOM_JSON = DATA_DIR / "custom_sources.json"      # 用户自定义镜像源(独立持久化,不随 update-meta 覆盖)
CUSTOM_CACHE_JSON = DATA_DIR / "custom_repo_cache.json"  # 自定义发行版源展开结果的持久化缓存(仅后台刷新写入)
SUBS_JSON = DATA_DIR / "subscriptions.json"          # 订阅配置: 自动拉最新+删旧
SETTINGS_JSON = DATA_DIR / "settings.json"           # 网络共享开关+凭据(网页可改, 覆盖 compose env)
FAILURES_JSON = DATA_DIR / "download_failures.json"  # 下载失败记录 {rel: {"at":ts,"kind":"hard"|"stopped"}}
# 下载中的半成品后缀: 下载器一律写 <最终名>.part, 校验通过后才原子改名为最终名。
# 必须与 web/iso_runner.py 的 PART_SUFFIX、iso_download/download_linux.py 保持一致。
PART_SUFFIX = ".part"
SHARE_CONTAINERS = {"samba": "iso-hub-samba", "webdav": "iso-hub-webdav"}
ISO_SUFFIXES = {".iso", ".img", ".qcow2", ".vmdk"}
# 下载目录类型白名单(路径穿越防护)
ALLOWED_TYPES = {"linux", "bsd", "windows", "macos"}


def _is_http_url(url: str) -> bool:
    """仅允许 http/https 协议, 阻断 file://、ftp://、gopher:// 等 SSRF 向量。"""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.netloc != ""
    except Exception:  # noqa: BLE001
        return False


def _safe_join(typ: str, name: str) -> Path | None:
    """把 (type, name) 安全拼接为 DATA_DIR 下的路径, 拒绝路径穿越/非法字符。

    校验规则:
      * typ 必须在 {linux,bsd,windows,macos} 白名单内
      * typ/name 均不得为空、不得含 / 或 \\、不得为 . 或 ..
      * resolve 后仍必须位于 DATA_DIR 内(最终兜底)
    非法输入返回 None。
    """
    if not typ or not name or typ not in ALLOWED_TYPES:
        return None
    for comp in (typ, name):
        comp = str(comp)
        if comp != comp.strip() or comp in (".", ".."):
            return None
        if "/" in comp or "\\" in comp:
            return None
    target = (DATA_DIR / typ / name).resolve()
    try:
        target.relative_to(DATA_DIR.resolve())
    except ValueError:
        return None
    return target

DATA_DIR.mkdir(parents=True, exist_ok=True)
if not JSON_FILE.exists() and DEFAULT_JSON.exists():
    import shutil
    shutil.copyfile(DEFAULT_JSON, JSON_FILE)


def _migrate_distribution_fields() -> None:
    """字段级增量补齐 /data/distributions.json 中缺失的配置字段。

    背景: 旧版本只在 data 副本"不存在"时才从镜像内置配置复制一次, 此后镜像升级
    新增的字段(如 gpg_verify/gpg_key_url/gpg_key_fingerprint)永远同步不进运行时,
    导致这些功能在已部署环境上静默失效。

    本函数每次启动调用: 以 download_url 为条目标识, 把内置配置里"存在而 data 副本
    缺失"的字段逐个补齐。只补缺失, 绝不覆盖 data 副本已有值, 也绝不删除用户
    自定义条目 —— 保证幂等, 不破坏用户数据。
    """
    try:
        default = json.loads(DEFAULT_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # 内置配置读不到, 无从迁移
    builtin = default.get("distributions", [])
    if not builtin:
        return
    try:
        data = json.loads(JSON_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data_list = data.setdefault("distributions", [])
    # 用 download_url 建立内置索引(避免同名发行版多条目错配)
    builtin_index = {e.get("download_url"): e for e in builtin if e.get("download_url")}
    changed_entries = 0
    changed_fields = 0
    for entry in data_list:
        src = builtin_index.get(entry.get("download_url"))
        if not src:
            continue  # 用户自定义条目, 完整保留
        added = 0
        for field, value in src.items():
            if field in entry:
                continue  # data 副本已有该字段, 不覆盖
            entry[field] = value  # 只补缺失字段
            added += 1
        if added:
            changed_entries += 1
            changed_fields += added
    if changed_fields:
        tmp = JSON_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(JSON_FILE)
        log(f"配置迁移: 补齐 {changed_entries} 个条目的 {changed_fields} 个缺失字段 -> {JSON_FILE}")


_migrate_distribution_fields()

# --------------------------------------------------------------------------- state
# D1 修复: 用 RLock 代替 Lock —— stop_task() 在 with _lock: 块内调用 log(), 而 log()
# 内部也要 with _lock:, 普通 Lock 在同一线程内二次 acquire 会永久死锁, 占满 waitress 线程。
_lock = threading.RLock()
_log_lines = deque(maxlen=4000)
_log_seq = 0
task = {"proc": None}


def log(msg: str) -> None:
    global _log_seq
    # L2 修复: 序号自增放进锁内, waitress 多线程下不再重复/乱序
    with _lock:
        _log_seq += 1
        _log_lines.append({"i": _log_seq, "t": time.time(), "l": str(msg).rstrip()})


def human(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- json / disk
def load_json() -> dict:
    """读取发行版清单并合并自定义源，失败时回退上游默认 JSON。"""
    base = None
    for p in (JSON_FILE, DEFAULT_JSON):
        if p.exists():
            try:
                base = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception as e:  # noqa: BLE001
                log(f"[WARN] 解析 {p} 失败: {e}")
    base = base or {"distributions": []}
    base["distributions"] = merge_custom_entries(base.get("distributions", []))
    return base


def meta_updated_at() -> float:
    try:
        return JSON_FILE.stat().st_mtime
    except OSError:
        return 0.0


def write_fail_record(rel: str, kind: str = "hard") -> None:
    """记录一次下载失败到 download_failures.json: {rel: {"at": ts, "kind": kind}}。

    由各下载 runner 在文件级失败时调用(子进程写)。kind 语义由调用方决定:
      * "hard"    — 不可续传的失败(半成品已被清理), 下次只能从头下 → 下载失败
      * "stopped" — 已停止/被中断, 半成品保留, 下次可续传 → 下载停止
    """
    try:
        data = {}
        if FAILURES_JSON.exists():
            data = json.loads(FAILURES_JSON.read_text(encoding="utf-8")) or {}
        data[rel] = {"at": int(time.time()), "kind": kind}
        tmp = FAILURES_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(FAILURES_JSON)
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] 写下载失败记录失败: {e}")


def load_failures() -> dict:
    """读取下载失败记录 {rel: {"at": ts, "kind": kind}}。"""
    try:
        if FAILURES_JSON.exists():
            data = json.loads(FAILURES_JSON.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] 解析下载失败记录失败: {e}")
    return {}


def clear_failure(rel: str) -> None:
    """下载成功后清除该文件的失败记录。"""
    try:
        data = load_failures()
        if rel in data:
            data.pop(rel, None)
            tmp = FAILURES_JSON.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(FAILURES_JSON)
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] 清除下载失败记录失败: {e}")


def disk_inventory() -> dict:
    """扫描数据卷 -> {(type, name): [files]}

    同时识别下载中的半成品文件(.part / .aria2 / .!qB / *.tmp), 它们在条目里
    以 partial 标记返回, 供前端区分「下载失败 / 下载停止」与「未下载」。

    重要: 半成品文件只以"还原后的目标名 + partial=True"的形式出现一次,
    绝不把 xxx.iso.part 这个原始名再当成普通文件收录 —— 否则它会被
    build_distros() 判为"不在清单中"的 stray(过期文件), UI 上显示成
    "不在最新清单元数据中"(通常是已被更新淘汰的旧版 ISO) 并给出
    「清理过期」按钮, 而它其实是正在下载/可续传的半成品。
    """
    inv = {}
    if not DATA_DIR.exists():
        return inv
    for tdir in DATA_DIR.iterdir():
        if not tdir.is_dir() or tdir.name == ".git":
            continue
        for ddir in tdir.iterdir():
            if ddir.is_dir():
                key = (tdir.name, ddir.name)
                inv.setdefault(key, [])
                for f in ddir.iterdir():
                    if not f.is_file():
                        continue
                    # 半成品交给下一趟统一处理(跳过其原始名)
                    if _partial_base_name(f.name):
                        continue
                    try:
                        st = f.stat()
                        inv[key].append({"name": f.name, "size": st.st_size,
                                         "mtime": st.st_mtime, "partial": False})
                    except OSError:
                        pass
                # 半成品: 单独一趟, 把 xxx.iso.part 归到 xxx.iso 名下
                for f in ddir.iterdir():
                    if not f.is_file():
                        continue
                    base = _partial_base_name(f.name)
                    if not base:
                        continue
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    inv[key].append({"name": base, "size": st.st_size,
                                     "mtime": st.st_mtime, "partial": True,
                                     "partial_name": f.name})
    return inv


def _partial_base_name(fname: str) -> str:
    """把半成品文件名还原成目标文件名; 不是半成品返回空串。

    识别: name.part / name.aria2 / name.!qB / name.tmp
    (download_linux 与 failover runner 都先写目标名再落盘, 半成品仅此几类后缀)
    """
    low = fname.lower()
    for suf in (".part", ".aria2", ".!qb", ".tmp"):
        if low.endswith(suf):
            return fname[: -len(suf)]
    return ""


# --------------------------------------------------------------------------- custom sources / subscriptions
def load_custom_sources() -> list:
    """读取用户自定义镜像源(纯 download_url 条目)。"""
    if CUSTOM_JSON.exists():
        try:
            data = json.loads(CUSTOM_JSON.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as e:  # noqa: BLE001
            log(f"[WARN] 解析自定义源失败: {e}")
    return []


def save_custom_sources(items: list) -> None:
    CUSTOM_JSON.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- 自定义源自动刷新设置 (独立开关, 默认关闭不主动访问源服务器) ----------
CUSTOM_AUTO_REFRESH_DEFAULT = False
CUSTOM_REFRESH_INTERVAL_DEFAULT = 86400  # 秒, 默认每天(仅在开关开启时生效)


def load_custom_auto_refresh() -> dict:
    """读取自定义源自动刷新设置。缺失时回退默认(关闭 + 间隔 86400 秒)。"""
    raw = load_settings_all()
    enabled = raw.get("custom_source_auto_refresh", CUSTOM_AUTO_REFRESH_DEFAULT)
    try:
        interval = int(raw.get("custom_source_refresh_interval", CUSTOM_REFRESH_INTERVAL_DEFAULT))
    except (ValueError, TypeError):
        interval = CUSTOM_REFRESH_INTERVAL_DEFAULT
    if interval < 60:  # 最小 60 秒, 防止误设过短导致高频抓取
        interval = 60
    return {"enabled": bool(enabled), "interval": interval}


def save_custom_auto_refresh(enabled: bool | None, interval: int | None) -> dict:
    """保存自定义源自动刷新设置, 返回合并后的最新值。"""
    cur = load_custom_auto_refresh()
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    if interval is not None:
        try:
            cur["interval"] = max(60, int(interval))
        except (ValueError, TypeError):
            pass
    save_settings_all({
        "custom_source_auto_refresh": cur["enabled"],
        "custom_source_refresh_interval": cur["interval"],
    })
    return cur


def _custom_source_list() -> list:
    """返回所有 strategy 类型的自定义源(需后台展开的发行版源)。"""
    return [c for c in load_custom_sources() if c.get("strategy")]


def load_subscriptions() -> list:
    """订阅配置: 每个元素 {distribution,type,keep,enabled,last_run}"""
    if SUBS_JSON.exists():
        try:
            data = json.loads(SUBS_JSON.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as e:  # noqa: BLE001
            log(f"[WARN] 解析订阅配置失败: {e}")
    return []


def save_subscriptions(items: list) -> None:
    SUBS_JSON.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# 自定义源展开结果采用「持久化落盘缓存」, 请求线程内只读缓存、绝不发起网络请求。
# 展开动作仅由后台 runner(custom_repo_refresh.py) 执行后写回该 JSON 文件,
# 结构与 custom_repo_refresh.py 保持一致: {"<cache_key>": {"entries": [...], "updated_at": <ts>}}
_CUSTOM_REPO_CACHE: dict = {}      # 进程内存缓存: 磁盘缓存文件按 mtime 失效后重新读取
_CUSTOM_REPO_CACHE_MTIME: float | None = None


def _custom_repo_cache_key(item: dict) -> str:
    """计算自定义源展开结果的键。除 timeout 外的所有配置字段都决定抓取结果, 故一并纳入。"""
    return json.dumps({k: v for k, v in item.items() if k != "timeout"},
                      sort_keys=True, ensure_ascii=False, default=str)


def load_custom_repo_cache() -> dict:
    """读取自定义源展开缓存文件。结构异常时返回空 dict 并记日志。

    带 mtime 失效的进程内缓存: 后台 runner 以子进程写回新文件后, 本进程检测到
    mtime 变化会重新读盘, 避免 Web 列表永远停留在刷新前的旧结果。
    """
    global _CUSTOM_REPO_CACHE, _CUSTOM_REPO_CACHE_MTIME
    try:
        mtime = CUSTOM_CACHE_JSON.stat().st_mtime
    except OSError:
        _CUSTOM_REPO_CACHE = {}
        _CUSTOM_REPO_CACHE_MTIME = None
        return {}
    if _CUSTOM_REPO_CACHE_MTIME is not None and abs(_CUSTOM_REPO_CACHE_MTIME - mtime) < 1e-6:
        return _CUSTOM_REPO_CACHE
    try:
        data = json.loads(CUSTOM_CACHE_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            log(f"[自定义源] 缓存文件结构异常(非对象): {CUSTOM_CACHE_JSON}")
            data = {}
        _CUSTOM_REPO_CACHE = data
        _CUSTOM_REPO_CACHE_MTIME = mtime
        return data
    except Exception as e:  # noqa: BLE001
        log(f"[自定义源] 解析缓存文件失败 {CUSTOM_CACHE_JSON}: {e}")
        _CUSTOM_REPO_CACHE = {}
        _CUSTOM_REPO_CACHE_MTIME = None
        return {}


def expand_custom_repo(item: dict, timeout: int = 40) -> list:
    """把一个自定义"发行版源"条目展开为若干具体 ISO 条目(只读持久化缓存)。

    支持与官方 sources_config.json 相同的 strategy:
      dated_directory / flat_listing / versioned_flat_listing / static
    请求线程内只命中磁盘缓存并返回缓存条目; 未命中(该源尚未被后台刷新过)时返回空列表,
    提示用户在「自定义源」面板手动触发刷新或开启自动刷新。**绝不在请求线程内同步抓取网络。**
    """
    strategy = item.get("strategy", "")
    if strategy not in ("dated_directory", "flat_listing", "versioned_flat_listing", "static"):
        return []
    # SSRF 防护: 只接受 http/https 协议(与后台 runner 的过滤保持一致)
    listing_url = item.get("listing_url", "")
    if listing_url and not _is_http_url(listing_url):
        log(f"[自定义源] 拒绝非 http/https 的 listing_url: {listing_url}")
        return []
    cache_key = _custom_repo_cache_key(item)
    hit = load_custom_repo_cache().get(cache_key)
    entries = hit.get("entries") if isinstance(hit, dict) else None
    return list(entries) if isinstance(entries, list) else []


def merge_custom_entries(entries: list) -> list:
    """把自定义源条目合并进官方清单,按 (distribution,download_url) 去重。

    自定义条目分两种:
      * 普通直链(download_url): 直接合并
      * 发行版源(strategy=...): 读取持久化缓存(custom_repo_cache.json)里的展开条目后合并
        未命中缓存(尚无后台刷新记录)时不产生任何条目, 也不发起网络请求。
    """
    merged = {e["download_url"]: e for e in entries}
    for c in load_custom_sources():
        if c.get("strategy"):
            for e in expand_custom_repo(c):
                merged[e["download_url"]] = e
        elif c.get("download_url"):
            merged[c["download_url"]] = c
    return list(merged.values())


def active_download_paths() -> set:
    """快照"当前正准备写入的 .part 路径"集合, 供状态判定用。

    v1.3.1 新增: 此前列表状态完全由磁盘推断, 无法区分"下载被中断"和
    "此刻正在下载" —— 正在下载的文件同样表现为一个 .part 文件, 于是被显示成
    「下载停止」。这里从运行中任务的 targets/downloads 取活跃路径。

    必须在 **_lock 之外**调用(见 D2 教训: 锁内做磁盘 IO 会拖垮 waitress)。
    本函数自身只持有锁读内存字段, 不做任何 stat()。
    """
    with _lock:
        if not task.get("proc"):
            return set()
        paths = set()
        for p in (task.get("targets") or {}):
            paths.add(str(p))
        for d in (task.get("downloads") or []):
            if d.get("path"):
                paths.add(str(d["path"]))
    return paths


def _entry_status(key: tuple, fname: str, local: dict | None, failures: dict,
                  active_paths: frozenset = frozenset()) -> tuple:
    """判定条目下载状态, 返回 (status, partial_size)。

    status 取值:
      * "downloaded"  — 完整文件已落盘
      * "downloading" — 该文件的 .part 正被运行中的任务写入 → 下载中
      * "partial"     — 发现半成品(.part 等), 任务中断过, 可尝试续传
      * "stopped"     — 有失败记录且 kind=stopped(半成品曾保留) → 下载停止
      * "failed"      — 有失败记录且 kind=hard(半成品已清理) → 下载失败
      * "none"        — 从未下载过

    v1.3.1: "downloading" 必须**最先**判定 —— 只有它来自运行中任务的实时信息,
    其余状态都是事后推断。没有这一步时, 正在下载的文件会因为"磁盘上有个 .part"
    而被误报成「下载停止」(用户实际遇到的现象)。

    partial 判定优先于失败记录: 只要半成品还在, 就说明可续传 → 归入「下载停止」语义。

    注意 local 的传入约定(build_distros 保证): 若同名的完整文件与 .part 半成品
    同时存在, local 会是"二者中较新的那个"。因此:
      * local 为 partial → 半成品比完整文件更新(或压根没有完整文件) → 下载被中断
      * local 为完整文件 → 半成品已落定 → 已下载
    """
    if local and not local.get("partial"):
        return "downloaded", 0
    # 运行中的任务正写这个 .part → 下载中(而非"下载停止")
    part_rel = str(DATA_DIR / key[0] / key[1] / (fname + PART_SUFFIX))
    if part_rel in active_paths:
        return "downloading", int(local.get("size") or 0) if local else 0
    rel = f"{key[0]}/{key[1]}/{fname}"
    rec = failures.get(rel) or {}
    if local and local.get("partial"):
        # 半成品存在: 无论历史失败记录为何, 都视为可续传的中断
        return "partial", int(local.get("size") or 0)
    if rec:
        kind = rec.get("kind")
        return ("stopped" if kind == "stopped" else "failed"), 0
    return "none", 0


def build_distros() -> dict:
    data = load_json()
    inv = disk_inventory()
    failures = load_failures()
    # v1.3.1: 活跃下载路径快照(锁外获取, 只读内存), 用于把"正在下载"从"下载停止"里分出来
    active_paths = frozenset(active_download_paths())
    groups = {}
    for e in data.get("distributions", []):
        name, typ = e.get("distribution", "?"), e.get("type", "linux")
        key = (typ, name)
        groups.setdefault(key, {"name": name, "type": typ, "entries": []})
        url = e.get("download_url", "")
        fname = url.rstrip("/").rsplit("/", 1)[-1] if url else "?"
        files = inv.get(key, [])
        # 同名可能同时存在完整文件与 .part 半成品。取"最近改动"的那个作为代表:
        # 半成品更新说明下载正在/曾在进行(显示下载停止), 完整文件更新说明已落定(已下载)。
        # 旧实现固定优先完整文件, 会把"下载中断后残留的旧文件 + 正在写的 .part"
        # 误判成已下载。
        candidates = [f for f in files if f["name"] == fname]
        local = max(candidates, key=lambda f: f.get("mtime") or 0) if candidates else None
        status, partial_size = _entry_status(key, fname, local, failures, active_paths)
        groups[key]["entries"].append(
            {
                "distribution": name,
                "type": typ,
                "filename": fname,
                "download_url": url,
                "download_urls": e.get("download_urls", []),
                "checksum_url": e.get("checksum_url", ""),
                "checksum_urls": e.get("checksum_urls", []),
                "checksum": e.get("checksum", ""),
                "pin": e.get("pin", ""),
                "local_size": local["size"] if local else 0,
                "local_mtime": int(local["mtime"]) if local else 0,
                "status": status,
                "partial_size": partial_size,
            }
        )

    result = []
    for (typ, name), g in groups.items():
        expected = {e["filename"] for e in g["entries"]}
        # 半成品(partial)不是 stray: 它对应清单里的目标文件, 由失败记录/半成品
        # 本身表达状态。只有"确实不属于本清单"的完整文件才算过期文件。
        strays = [
            f for f in inv.get((typ, name), [])
            if f["name"] not in expected and not f.get("partial")
        ]
        # 本地占用: 每个物理文件只算一次(半成品与同名完整文件可能是两条记录)
        local_total = 0
        _seen_files = set()
        for f in inv.get((typ, name), []):
            _k = f.get("partial_name") or f["name"]
            if _k in _seen_files:
                continue
            _seen_files.add(_k)
            local_total += f["size"]
        result.append(
            {
                "name": name,
                "type": typ,
                "entries": g["entries"],
                "stray_files": strays,
                "local_total": local_total,
                "file_count": len(g["entries"]),
            }
        )
    result.sort(key=lambda g: (g["type"], g["name"]))
    return {"updated_at": int(meta_updated_at()), "groups": result}


# --------------------------------------------------------------------------- network shares (SMB / WebDAV)
DEFAULT_SHARES = {
    "samba": {"enabled": False, "username": os.environ.get("SAMBA_USER", "iso"),
              "password": os.environ.get("SAMBA_PASS", "iso123"), "port": os.environ.get("SAMBA_PORT", "1445")},
    "webdav": {"enabled": False, "username": os.environ.get("WEBDAV_USER", "iso"),
               "password": os.environ.get("WEBDAV_PASS", "iso123"), "port": os.environ.get("WEBDAV_PORT", "8081")},
}


# --------------------------------------------------------------------------- qBittorrent sidecar
DEFAULT_QB = {
    "enabled": False,
    "username": os.environ.get("QB_USER", "admin"),
    "password": os.environ.get("QB_PASS", "adminadmin"),
    "port": os.environ.get("QB_PORT", "8090"),
    "url": os.environ.get("QB_URL", "http://qbittorrent:8080"),
}
QB_CONTAINER = "iso-hub-qbittorrent"
QB_CONF_PATH = Path("/qb-config/qBittorrent/qBittorrent.conf")


def load_shares() -> dict:
    """读取共享设置, 缺失键回退环境变量默认。"""
    data = {}
    if SETTINGS_JSON.exists():
        try:
            data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            data = {}
    out = {}
    for k, dft in DEFAULT_SHARES.items():
        s = dict(dft)
        s.update({kk: vv for kk, vv in data.get(k, {}).items()})
        out[k] = s
    return out


def save_shares(shares: dict) -> None:
    cur = load_settings_all()
    cur.update(shares)  # 保留 protected 等其它顶层键
    SETTINGS_JSON.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def load_qb_settings() -> dict:
    """读取 qBittorrent 设置, 缺失键回退环境变量默认。"""
    data = {}
    if SETTINGS_JSON.exists():
        try:
            data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            data = {}
    out = dict(DEFAULT_QB)
    out.update({k: v for k, v in data.get("qb", {}).items() if k in out})
    return out


def save_qb_settings(qb: dict) -> None:
    cur = load_settings_all()
    cur["qb"] = qb
    SETTINGS_JSON.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 通用 settings.json 读写 (保护名单与共享并存, 不互相覆盖) ----------
def load_settings_all() -> dict:
    """读取整个 settings.json, 缺失返回 {}。"""
    if not SETTINGS_JSON.exists():
        return {}
    try:
        data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        data = {}
    return data


def save_settings_all(data: dict) -> None:
    """原子写整个 settings.json(合并已存在键)。"""
    cur = load_settings_all()
    cur.update(data)
    SETTINGS_JSON.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def load_source_strategy() -> str:
    """读取全局选源策略(A 固定优先级 / B 实测选最快)。默认 A, 可被环境变量覆盖。"""
    return (load_settings_all().get("source_strategy") or
            os.environ.get("ISO_HUB_SOURCE_STRATEGY", "A")).upper()


def save_source_strategy(s: str) -> None:
    save_settings_all({"source_strategy": s.upper()})


def load_protected() -> list:
    """受保护文件名列表(存 settings.json 的 protected 键, 相对路径 type/name/文件).iso)。"""
    return list(load_settings_all().get("protected", []) or [])


# B7 修复: 历史清单文件名持久化, 用于清理时只删"曾经出现在清单里"的文件, 保护用户自有 ISO
MANIFEST_HISTORY_KEY = "manifest_history"


def _manifest_history() -> dict:
    """读取历史清单 { "type/name": [文件名,...] }, 缺失返回 {}。"""
    h = load_settings_all().get(MANIFEST_HISTORY_KEY) or {}
    return h if isinstance(h, dict) else {}


def _record_manifest_history(entries: list) -> None:
    """把当前清单里的文件名并入历史(去重累积), 有新文件名才落盘。"""
    hist = _manifest_history()
    changed = False
    for e in entries:
        typ = e.get("type", "linux")
        name = e.get("distribution", "?")
        url = e.get("download_url", "")
        fname = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        if not fname:
            continue
        key = f"{typ}/{name}"
        st = set(hist.get(key, []) or [])
        if fname not in st:
            st.add(fname)
            hist[key] = sorted(st)
            changed = True
    if changed:
        save_settings_all({MANIFEST_HISTORY_KEY: hist})


def _is_known_file(typ: str, name: str, fname: str) -> bool:
    """判断文件名是否曾在历史清单中出现过(可安全清理)。"""
    return fname in set(_manifest_history().get(f"{typ}/{name}", []) or [])


def save_protected(lst: list) -> None:
    """写受保护名单。"""
    save_settings_all({"protected": list(lst)})


# ---------- 定时任务 / 调度 (用户自建) ----------
SCHEDULE_KEY = "schedules"


def load_schedules() -> list:
    return list(load_settings_all().get(SCHEDULE_KEY, []) or [])


def save_schedules(lst: list) -> None:
    save_settings_all({SCHEDULE_KEY: lst})


def _sched_matches(s: dict, now: struct_time | None = None) -> bool:
    """当前时间是否命中该调度规则。now: time.struct_time。"""
    now = now or time.localtime()
    typ = s.get("type", "daily")
    try:
        hour, minute = (s.get("time") or "00:00").split(":")[:2]
        hour, minute = int(hour), int(minute)
    except (ValueError, TypeError):
        hour, minute = 0, 0
    if now.tm_hour != hour or now.tm_min != minute:
        return False
    if typ == "daily":
        return True
    if typ == "weekly":
        return now.tm_wday == int(s.get("day_of_week", 0))  # Mon=0..Sun=6
    if typ == "monthly":
        return now.tm_mday == int(s.get("day_of_month", 1))
    if typ == "yearly":
        return now.tm_mon == int(s.get("month_of_year", 1)) and now.tm_mday == int(s.get("day_of_month", 1))
    if typ == "once":
        return False
    return False


# ---------- 用户登录 / 会话 token (单管理员, 标准库实现) ----------
import secrets  # noqa: E402

USER_STORE_KEY = "users"
SESSION_TTL = int(os.environ.get("ISO_HUB_SESSION_TTL", str(7 * 24 * 3600)))  # 默认7天
SESSION_KEY = "sessions"  # settings.json 里持久化会话的键
# 内存会话: token -> (username, expire_ts)；同时持久化到磁盘, 容器重建/进程重启后恢复
_sessions = {}


def _sessions_persist() -> None:
    """把内存会话原子写盘到 settings.json（容器重建后 token 仍有效）。"""
    try:
        save_settings_all({SESSION_KEY: dict(_sessions)})
    except Exception:  # noqa: BLE001
        pass


def _sessions_load() -> None:
    """启动时从磁盘恢复会话(忽略已过期项), 并清理磁盘过期项。"""
    global _sessions
    try:
        saved = load_settings_all().get(SESSION_KEY) or {}
        now = int(time.time())
        _sessions = {k: v for k, v in saved.items() if isinstance(v, (list, tuple)) and len(v) == 2 and v[1] > now}
        if len(saved) != len(_sessions):
            _sessions_persist()
    except Exception:  # noqa: BLE001
        _sessions = {}


def _hash_pw(password: str, salt: str) -> str:
    """PBKDF2 派生口令哈希。"""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt.encode("utf-8"), 120_000).hex()


def load_users() -> dict:
    """读取用户表: {username: {password_hash, salt, created_at}}。"""
    return dict(load_settings_all().get(USER_STORE_KEY, {}) or {})


def save_users(users: dict) -> None:
    save_settings_all({USER_STORE_KEY: users})


def seed_admin():
    """若未播种管理员且设置了 ISO_HUB_ADMIN_USER/PASS, 则创建之。"""
    u = os.environ.get("ISO_HUB_ADMIN_USER", "").strip()
    p = os.environ.get("ISO_HUB_ADMIN_PASS", "").strip()
    if not u or not p:
        return
    users = load_users()
    if u in users:
        return
    salt = secrets.token_hex(16)
    users[u] = {"password_hash": _hash_pw(p, salt), "salt": salt,
                "created_at": int(time.time())}
    save_users(users)
    log(f"[用户] 已从环境变量播种管理员账号: {u}")


def _check_login(username: str, password: str) -> bool:
    return _verify_login(username, password) == "ok"


def _verify_login(username: str, password: str) -> str:
    """校验登录, 返回精确结果: 'ok' | 'no_user' | 'bad_pass'。

    分开返回两种失败原因, 让前端能提示"用户名不存在"还是"密码错误"。
    注意: 这会暴露账号是否存在(用户枚举)。本应用是自建私有面板、公网通常还有
    反代/防火墙兜底, 可读性优先; 如后续要抗枚举, 可改为统一话术+恒定耗时。
    """
    u = load_users().get(username)
    if not u:
        return "no_user"
    ok = secrets.compare_digest(_hash_pw(password, u.get("salt", "")),
                                u.get("password_hash", ""))
    return "ok" if ok else "bad_pass"


def _issue_token(username: str) -> str:
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = (username, int(time.time()) + SESSION_TTL)
    _sessions_persist()
    return tok


def _valid_session() -> str | None:
    """校验 X-Auth-Token 是否有效会话 token, 返回 username 或 None。"""
    tok = request.headers.get("X-Auth-Token", "")
    if not tok:
        return None
    hit = _sessions.get(tok)
    if not hit and _sessions:  # 内存没命中: 从磁盘恢复(容器重建后 token 持久化)
        _sessions_load()
        hit = _sessions.get(tok)
    if not hit:
        return None
    user, exp = hit
    if time.time() > exp:
        _sessions.pop(tok, None)
        _sessions_persist()
        return None
    return user


# Docker Engine API 连接方式:
#   * DOCKER_HOST=tcp://host:port 时, 走 TCP 连 socket-proxy(主容器不再持有裸 docker.sock)
#   * 否则回退到既有 Unix socket 路径(DOCKER_SOCK), 存量部署零影响
# 设计意图: 主容器为 slim 精简镜像, 不安装 docker CLI / docker Python 包,
# 直接用 Python 标准库(socket + http.client)直连 Docker REST API。
DOCKER_HOST = os.environ.get("DOCKER_HOST", "")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")


def _docker_conn(timeout):
    """按 DOCKER_HOST 创建 Docker Engine API 连接: tcp:// 走 HTTP, 否则走 Unix socket。"""
    import socket
    import http.client
    if DOCKER_HOST.startswith("tcp://"):
        return http.client.HTTPConnection(DOCKER_HOST[6:], timeout=timeout)
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.connect(DOCKER_SOCK)
    return conn


def _docker_request(method, path, body, timeout):
    """通过 docker.sock 或 DOCKER_HOST(TCP) 调 Docker REST API, 主容器内无需 docker CLI。"""
    import json
    payload = json.dumps(body).encode() if body is not None else None
    conn = _docker_conn(timeout)
    try:
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
    finally:
        conn.close()
    return type("R", (), {"status": resp.status, "text": data.decode()})()


# sidecar 服务四态: 与前端 UI 映射约定(见需求文档 N4)
#   running       运行中
#   stopped       已停止(容器存在但未运行)
#   not_deployed  未部署(容器根本不存在 —— compose profile 未启用)
#   unknown       未知(Docker API 不通/查询失败, 如 socket-proxy 挂掉; 不能误报未部署)
SERVICE_STATES = ("running", "stopped", "not_deployed", "unknown")
# Docker 容器 State.Status 值 -> 服务四态(created/restarting/paused/dead 均归为 stopped)
_CONTAINER_STATUS_TO_STATE = {"running": "running", "created": "stopped", "restarting": "stopped",
                              "paused": "stopped", "exited": "stopped", "dead": "stopped"}


def service_state(name: str) -> str:
    """查询 sidecar 容器并归并为四态之一。任何异常/API 不通都返回 unknown, 绝不抛错。

    懒加载原则: 只在设置页/相关接口被调用时才查询 Docker; 查询失败降级为 unknown,
    不崩溃、不影响主流程。
    """
    try:
        r = _docker_request("GET", f"/containers/{name}/json", None, 15)
        if r.status == 404:
            return "not_deployed"  # 容器不存在 -> compose profile 未启用
        if r.status != 200:
            log(f"[docker] 查询容器 {name} 状态失败: HTTP {r.status}, body={r.text[:500]!r}")
            return "unknown"
        payload = json.loads(r.text)
        st = payload.get("State", {}).get("Status")
        if not st:
            log(f"[docker] 容器 {name} 返回异常结构: State={payload.get('State')!r}")
            return "unknown"
        return _CONTAINER_STATUS_TO_STATE.get(st, "stopped")
    except Exception as e:  # noqa: BLE001
        log(f"[docker] 查询容器 {name} 异常: {e!r}")
        return "unknown"


def container_restart_policy(name: str) -> str | None:
    """读取容器的 RestartPolicy.Name(如 unless-stopped / no / on-failure)。

    用途: 识别"容器在运行但自愈能力已丢失"的静默不一致 —— 例如运维执行
    `docker compose up -d` 触发 recreate 后, compose 里写死的 `restart: no`
    会重新生效, 而容器仍在运行、面板仍显示绿灯, 直到宿主重启才暴露。

    查询失败/容器不存在一律返回 None, 由调用方**保守跳过**(不据此动作),
    避免在 Docker API 不可达时误判。
    """
    try:
        r = _docker_request("GET", f"/containers/{name}/json", None, 15)
        if r.status != 200:
            return None
        payload = json.loads(r.text)
        policy = ((payload.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name")
        return policy or None
    except Exception as e:  # noqa: BLE001
        log(f"[docker] 查询容器 {name} 重启策略异常: {e!r}")
        return None


def share_container_state(name: str) -> str | None:
    """返回 sidecar 容器原始运行状态: running/created/exited/None。失败时记录日志。

    保持旧语义(供启动同步等内部逻辑与既有测试使用); 面向 UI 的四态判定用 service_state。
    """
    try:
        r = _docker_request("GET", f"/containers/{name}/json", None, 15)
        if r.status != 200:
            log(f"[docker] 查询容器 {name} 状态失败: HTTP {r.status}, body={r.text[:500]!r}")
            return None
        payload = json.loads(r.text)
        st = payload.get("State", {}).get("Status")
        if not st:
            log(f"[docker] 容器 {name} 返回异常结构: State={payload.get('State')!r}")
        return st or None
    except Exception as e:  # noqa: BLE001
        log(f"[docker] 查询容器 {name} 异常: {e!r}")
        return None


def set_share(proto: str, enabled: bool) -> bool:
    """通过 docker.sock 启动/停止对应 sidecar 容器。

    两步缺一不可: start/stop 负责"当下", RestartPolicy 负责"持久化"(让状态在
    宿主/compose 重启后不反弹)。因此**任一步失败都必须视为未完全成功** ——
    否则会留下 running + restart=no 的静默不一致: 面板显示绿灯, 但宿主重启后
    容器不会被拉起(见 _converge_sidecars 对启用态的反向收敛)。
    """
    cname = SHARE_CONTAINERS.get(proto)
    if not cname:
        return False
    try:
        if enabled:
            # 先启动, 再恢复自动重启策略(启用时让容器随 compose 自启)
            r1 = _docker_request("POST", f"/containers/{cname}/start", None, 20)
            r2 = _docker_request("POST", f"/containers/{cname}/update", {"RestartPolicy": {"Name": "unless-stopped"}}, 20)
        else:
            # 停用: 设 restart=no 防止自动拉起, 再停止
            r1 = _docker_request("POST", f"/containers/{cname}/update", {"RestartPolicy": {"Name": "no"}}, 20)
            r2 = _docker_request("POST", f"/containers/{cname}/stop", None, 20)
        # start/stop 对已处于目标状态的容器返回 304, update 正常返回 200/204
        ok1 = r1.status in (200, 204, 304)
        ok2 = r2.status in (200, 204, 304)
        if ok1 and ok2:
            return True
        # 区分"完全失败"与"部分成功": 关键是把"当下能不能用"和"重启后会不会失效"分开说,
        # 让用户第一时间知道现在到底能不能用(用户最怕的是状态不明)。
        # 启用态: 步骤1=启动(决定当下可用性), 步骤2=自愈策略(决定重启后能否自启)。
        # 停用态: 步骤1=重启策略设为 no, 步骤2=停止。
        if enabled:
            if ok1:
                log(f"[共享] {proto} 启用部分成功: 容器已启动, 但自动重启策略未生效(步骤2: HTTP {r2.status})。"
                    f"当前可用, 重启后不会自动恢复, 将在下次 iso-hub 启动时自动修复。")
            else:
                log(f"[共享] {proto} 启用失败: 容器启动失败(步骤1: HTTP {r1.status}), 配置未变更, 当前不可用。")
        else:
            if ok2:
                log(f"[共享] {proto} 停用部分成功: 容器已停止, 但重启策略未设为 no(步骤1: HTTP {r1.status})。"
                    f"当前已停用, 宿主重启后可能被自动拉起, 将在下次 iso-hub 启动时自动修复。")
            else:
                log(f"[共享] {proto} 停用失败: 容器停止失败(步骤2: HTTP {r2.status}), 配置未变更。")
        return False
    except Exception:  # noqa: BLE001
        return False


def _sync_disabled_shares() -> None:
    """单次同步共享 sidecar 容器状态: 若默认/配置为 disabled 但容器仍在运行, 则停止它。

    这样即使 docker compose up 时自动拉起了 samba/webdav, 首次启动也会立即把它们停掉,
    让用户在 Web 面板里手动启用并设置凭据。

    注意: 这是"单次快照"版本, 只适合在容器已确定存在的场景直接调用(如测试/手动)。
    启动路径请用 _sync_disabled_shares_converge(), 它会重试以覆盖 compose 并发启动窗口。
    """
    try:
        shares = load_shares()
        for proto in ("samba", "webdav"):
            if not shares.get(proto, {}).get("enabled", True):
                cname = SHARE_CONTAINERS.get(proto)
                if not cname:
                    continue
                st = share_container_state(cname)
                if st == "running":
                    log(f"[共享] 启动同步: {proto} 当前为禁用但容器在运行, 正在停止")
                    set_share(proto, False)
    except Exception:  # noqa: BLE001
        pass


def _make_qb_pbkdf2(password: str) -> str:
    """生成 qBittorrent WebUI 接受的 PBKDF2 密码哈希。"""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return "@ByteArray(" + base64.b64encode(salt).decode() + ":" + base64.b64encode(key).decode() + ")"


def _set_qb_password(username: str, password: str) -> bool:
    """把用户名/密码写入挂载的 qBittorrent.conf, 禁用 LocalHostAuth/HostHeaderValidation。"""
    if not QB_CONF_PATH.exists():
        return False
    try:
        c = configparser.ConfigParser(strict=False, allow_no_value=True)
        c.optionxform = str
        c.read(QB_CONF_PATH, encoding="utf-8")
        if not c.has_section("Preferences"):
            c.add_section("Preferences")
        c.set("Preferences", "WebUI\\Username", username)
        c.set("Preferences", "WebUI\\Password_PBKDF2", _make_qb_pbkdf2(password))
        c.set("Preferences", "WebUI\\LocalHostAuth", "false")
        c.set("Preferences", "WebUI\\HostHeaderValidation", "false")
        with open(QB_CONF_PATH, "w", encoding="utf-8") as f:
            c.write(f)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[qB] 写入密码失败: {e!r}")
        return False


def set_qb(enabled: bool, username: str, password: str) -> bool:
    """启动/停止 qBittorrent sidecar, 并在首次启用时写入固定密码。"""
    try:
        if enabled:
            # 1. 启动容器
            r1 = _docker_request("POST", f"/containers/{QB_CONTAINER}/start", None, 20)
            if r1.status not in (200, 204, 304):
                log(f"[qB] 启用失败: 容器启动失败(步骤1: HTTP {r1.status}), 配置未变更, 当前不可用。")
                return False
            # 2. 恢复自动重启策略(与 start 同等重要: 缺失会留下 running + restart=no,
            #    面板绿灯但宿主重启后不自启, 故必须校验, 不能静默忽略)
            r2 = _docker_request("POST", f"/containers/{QB_CONTAINER}/update", {"RestartPolicy": {"Name": "unless-stopped"}}, 20)
            if r2.status not in (200, 204, 304):
                log(f"[qB] 启用部分成功: 容器已启动, 但自动重启策略未生效(步骤2: HTTP {r2.status})。"
                    f"当前可用, 重启后不会自动恢复, 将在下次 iso-hub 启动时自动修复。")
                return False
            # 3. 等待 conf 生成并写入密码
            for _ in range(15):
                if QB_CONF_PATH.exists() and _set_qb_password(username, password):
                    break
                time.sleep(1)
            else:
                log("[qB] 启用后未能在 15 秒内写入 qBittorrent.conf")
                return False
            # 4. 重启使新密码生效
            rr = _docker_request("POST", f"/containers/{QB_CONTAINER}/restart", None, 30)
            return rr.status in (200, 204, 304)
        else:
            ru = _docker_request("POST", f"/containers/{QB_CONTAINER}/update", {"RestartPolicy": {"Name": "no"}}, 20)
            r = _docker_request("POST", f"/containers/{QB_CONTAINER}/stop", None, 20)
            ok_ru = ru.status in (200, 204, 304)
            ok_r = r.status in (200, 204, 304)
            if ok_ru and ok_r:
                return True
            if ok_r:
                # 停止成功(当下已停用), 但重启策略未设为 no -> 重启后可能被自动拉起
                log(f"[qB] 停用部分成功: 容器已停止, 但重启策略未设为 no(步骤1: HTTP {ru.status})。"
                    f"当前已停用, 宿主重启后可能被自动拉起, 将在下次 iso-hub 启动时自动修复。")
            else:
                log(f"[qB] 停用失败: 容器停止失败(步骤2: HTTP {r.status}), 配置未变更。")
            return False
    except Exception as e:  # noqa: BLE001
        log(f"[qB] 启停异常: {e!r}")
        return False


def _sync_disabled_qb() -> None:
    """单次同步 qBittorrent 容器状态: 若配置为 disabled 但容器在运行, 则停止它。

    同 _sync_disabled_shares, 单次快照只适合容器已确定存在的场景; 启动路径用收敛版。
    """
    try:
        qb = load_qb_settings()
        if not qb.get("enabled", True):
            st = share_container_state(QB_CONTAINER)
            if st == "running":
                log("[qB] 启动同步: qBittorrent 当前为禁用但容器在运行, 正在停止")
                set_qb(False, qb.get("username", ""), qb.get("password", ""))
    except Exception:  # noqa: BLE001
        pass


def _heal_enabled_sidecar(cname: str, start_fn, handled: set[str]) -> bool:
    """对"配置为启用"的 sidecar 做反向收敛, 返回 True 表示本轮仍需继续观察。

    补齐 _converge_disabled_sidecars 只管"禁用"的半边缺口。两种不一致:

      1) 容器在运行, 但 RestartPolicy != unless-stopped
         成因: compose 里 sidecar 写死 `restart: no`, 任何 recreate(镜像更新 /
         --force-recreate / down+up) 都会让它重新生效; 而容器仍在跑、面板仍绿灯,
         要到宿主重启才暴露成"共享消失"。
         处理: 只补 update, **绝不碰运行状态**(不停止、不重启), 因此不会违背
         "启用的容器不得被停止"这一既有约束。

      2) 容器存在但未运行
         成因: 宿主重启后 restart=no 的容器不会被拉起, 而面板配置仍是"启用"。
         处理: 调 start_fn(True) 拉起 —— 配置 enabled 即表达"期望在运行"。

    保守原则: 运行状态或重启策略查询失败(返回 None) 时一律跳过, 绝不据此启停,
    避免 Docker API 不可达(socket-proxy 未就绪)时误动作。
    """
    st = share_container_state(cname)
    if st is None:
        return True  # 容器尚未出现或查询失败 -> 下一轮再看
    if st == "running":
        policy = container_restart_policy(cname)
        if policy is None:
            # 策略查询失败(代理暂不可达 / Docker API 抖动): 不误判为"已一致",
            # 留待下一轮重试, 避免漏掉真正需要补自愈策略的容器
            log(f"[启动收敛] {cname} 在运行但重启策略查询失败(代理可能暂不可达), 留待下一轮重试")
            return True
        if policy == "unless-stopped":
            return False  # 已一致
        log(f"[启动收敛] {cname} 在运行但重启策略为 {policy}, 补设为 unless-stopped")
        _docker_request("POST", f"/containers/{cname}/update",
                        {"RestartPolicy": {"Name": "unless-stopped"}}, 20)
        return False
    # stopped / created / exited / dead 等: 配置要求启用, 予以拉起
    if cname in handled:
        return True  # 已发起过启动, 等待生效, 不重复冲击
    log(f"[启动收敛] {cname} 配置为启用但未运行(状态={st}), 正在启动")
    start_fn(True)
    handled.add(cname)
    return True


def _converge_disabled_sidecars(attempts: int = 12, interval: float = 5.0) -> None:
    """启动时收敛同步: 反复检查禁用中的 sidecar, 直到它们确实停止或次数耗尽。

    为什么需要重试(修复启动时序竞态):
      `docker compose up -d` 会并发创建多个容器, 主容器与 sidecar(samba/webdav/qbittorrent)
      没有 depends_on 关系, 谁先起来不确定。旧实现只在应用启动最早期做"一次性快照"判断:
      此刻 sidecar 往往尚未创建(查询返回 404/None), 判断被跳过; 随后 compose 才把它们拉起,
      于是长期停留在"配置为禁用, 但容器在运行"的不一致状态。

    收敛策略: 每轮对所有禁用 sidecar 检查一次, 发现 running 就发起停止; 只要还有
    未就绪(容器尚未出现 / 停止尚未生效)的禁用容器就继续下一轮, 全部落定才退出。

    幂等保护: 每个容器只发起一次停止请求(记入 handled), 避免同一容器被反复 stop。
    停止请求返回后若下一轮仍见 running, 说明停止未生效(如 restart 策略未改成功),
    此时只记录日志不再重复请求, 防止对 Sidecar 的无效冲击。

    任何异常都不向外抛, 避免影响启动流程。
    """
    pending_handled: set[str] = set()  # 已发起停止的容器名, 防重复 stop
    try:
        for i in range(max(1, attempts)):
            pending = False
            # --- 共享 sidecar: samba / webdav ---
            try:
                shares = load_shares()
                for proto in ("samba", "webdav"):
                    cname = SHARE_CONTAINERS.get(proto)
                    if not cname:
                        continue
                    if shares.get(proto, {}).get("enabled", True):
                        # 已启用: 反向收敛 —— 补自愈能力 / 拉起停摆的容器
                        if _heal_enabled_sidecar(cname, lambda on, p=proto: set_share(p, on),
                                                 pending_handled):
                            pending = True
                        continue
                    st = share_container_state(cname)
                    if st == "running":
                        if cname in pending_handled:
                            # 上一轮已请求停止但仍见 running: 不再重复请求, 仅提示
                            log(f"[共享] 启动收敛: {proto} 停止请求已发出但仍在运行, 等待生效")
                            pending = True
                            continue
                        log(f"[共享] 启动收敛({i + 1}/{attempts}): {proto} 配置为禁用但容器在运行, 正在停止")
                        ok = set_share(proto, False)
                        if ok:
                            pending_handled.add(cname)
                        pending = True
                    elif st is None:
                        # 容器尚未出现(Docker API 未响应)或代理暂不可达 —— 下一轮再看
                        pending = True
            except Exception as e:  # noqa: BLE001
                log(f"[共享] 启动收敛检查异常: {e!r}")

            # --- 种子 sidecar: qBittorrent ---
            try:
                qb = load_qb_settings()
                if qb.get("enabled", True):
                    # 已启用: 反向收敛
                    if _heal_enabled_sidecar(
                        QB_CONTAINER,
                        lambda on: set_qb(on, qb.get("username", ""), qb.get("password", "")),
                        pending_handled,
                    ):
                        pending = True
                else:
                    st = share_container_state(QB_CONTAINER)
                    if st == "running":
                        if QB_CONTAINER in pending_handled:
                            log("[qB] 启动收敛: qBittorrent 停止请求已发出但仍在运行, 等待生效")
                            pending = True
                        else:
                            log(f"[qB] 启动收敛({i + 1}/{attempts}): qBittorrent 配置为禁用但容器在运行, 正在停止")
                            if set_qb(False, qb.get("username", ""), qb.get("password", "")):
                                pending_handled.add(QB_CONTAINER)
                            pending = True
                    elif st is None:
                        pending = True
            except Exception as e:  # noqa: BLE001
                log(f"[qB] 启动收敛检查异常: {e!r}")

            if not pending:
                log(f"[启动收敛] 第 {i + 1} 轮: 所有禁用 sidecar 状态已一致, 结束")
                return
            if i < attempts - 1:
                time.sleep(interval)
        log(f"[启动收敛] 已尝试 {attempts} 轮, 个别 sidecar 可能仍未就绪; 后续在设置页手动操作即可")
    except Exception as e:  # noqa: BLE001
        log(f"[启动收敛] 异常: {e!r}")


def start_sidecar_convergence() -> threading.Thread:
    """在后台守护线程里运行 _converge_disabled_sidecars, 不阻塞 web 服务启动。

    收敛过程最长约 attempts*interval(默认 60 秒)。若放在主线程, waitress 会推迟监听端口,
    健康检查可能失败、用户看到白屏。放后台线程则服务立即可用, 状态由收敛逻辑异步纠正。
    """
    t = threading.Thread(target=_converge_disabled_sidecars, name="sidecar-converge", daemon=True)
    t.start()
    return t


# 每个 sidecar 容器内用于凭据的环境变量名 (samba 用; webdav 已改用挂载的 webdav.yml 明文)
CRED_ENVS = {
    "samba": ("SAMBA_USER", "SAMBA_PASS"),
    "webdav": ("WEBDAV_USER", "WEBDAV_PASS"),
}


def _recreate_samba(username: str, password: str) -> bool:
    """重建 samba sidecar 以应用新凭据(凭据在 cmd 的 -u/-s 参数里, 只能重建)。
    以旧容器配置为基准: stop->rm->create(新 cmd)->start, 保留挂载/端口/网络/标签。"""
    # L3 修复: 凭据字符白名单校验, 拒绝会破坏 samba command 语义的字符
    if not username or not password:
        return False
    if any(ch in (username + password) for ch in (" ", ";", "\n", "\r", '"', "'", "\\", ",")):
        log("[共享] 拒绝含特殊字符的共享凭据(用户名/密码不能含空格、分号、逗号、引号、反斜杠)")
        return False
    cname = SHARE_CONTAINERS["samba"]
    try:
        r = _docker_request("GET", f"/containers/{cname}/json", None, 20)
        if r.status != 200:
            return False
        spec = json.loads(r.text)
        cfg = spec.get("Config", {})
        host = spec.get("HostConfig", {}) or {}
        nw = spec.get("NetworkSettings", {}) or {}
        nets = nw.get("Networks", {}) or {}

        # 安全重建 cmd: 不使用正则/字符串拆分, 直接构造已知安全的参数列表,
        # 避免用户名/密码中的 shell/regex 元字符被二次解析。
        samba_cmd = [
            "-p",
            "-u", f"{username};{password}",
            "-s", f"iso;/srv/iso;no;no;no;{username},{password}",
        ]

        body = {
            "Image": cfg.get("Image", "dperson/samba:latest"),
            "Cmd": samba_cmd,
            "Env": cfg.get("Env", []),
            "Labels": cfg.get("Labels", {}),
            "HostConfig": {
                "Binds": [f"{m['Source']}:{m['Destination']}" + (":ro" if m.get("Mode") == "ro" else "")
                          for m in spec.get("Mounts", [])],
                "RestartPolicy": {"Name": "unless-stopped"},
                "PortBindings": host.get("PortBindings") or {},
            },
            "NetworkingConfig": {"EndpointsConfig": nets},
        }
        _docker_request("POST", f"/containers/{cname}/stop", None, 30)
        _docker_request("DELETE", f"/containers/{cname}?force=1", None, 20)
        # 注意: 容器名必须在 URL query 里, 不是 body; 否则创建随机名孤儿容器
        cr = _docker_request("POST", f"/containers/create?name={cname}", body, 30)
        if cr.status not in (200, 201, 409):
            log(f"[共享] samba 重建失败 HTTP {cr.status}: {cr.text[:200]}")
            return False
        _docker_request("POST", f"/containers/{cname}/start", None, 20)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[共享] samba 凭据同步异常: {e!r}")
        return False


def _webdav_conf_path() -> Path:
    """webdav 配置文件路径。

    默认 /data/webdav.yml(旧版单文件 bind); 新版 compose 把 ./webdav-config 同时挂给
    iso-hub(/webdav-config) 与 webdav(/config), 并用 WEBDAV_CONF 指向后者,
    以规避 LinuxServer/runc 在部分内核上单文件 bind 报 "not a directory" 的问题。
    """
    return Path(os.environ.get("WEBDAV_CONF") or str(DATA_DIR / "webdav.yml"))


# webdav.yml 初始模板。
# 仓库根目录的 webdav.yml 只是一份示例, 部署时并不会自动落到宿主机的 ./webdav-config/,
# 而 compose 里 webdav 容器以 `-c /config/webdav.yml` 启动 —— 目录为空时容器读不到配置,
# 会立刻退出并被重启策略反复拉起。这里在配置文件缺失时按模板兜底生成, 保证 sidecar 始终可读。
_WEBDAV_CONF_TEMPLATE = """# WebDAV 配置 (hacdias/webdav) —— 由 iso-hub 自动生成, 可手动编辑
address: 0.0.0.0
port: 6065
prefix: /dav
directory: /data
permissions: R            # 只读 (R); 想可写改成 CRUD
users:
  - username: {username}
    password: {password}
    permissions: R        # 只读, 覆盖全局默认(可写则删这行)
log:
  format: console
  outputs:
    - stderr
"""


def _yaml_quote(value: str) -> str:
    """把值包装成 YAML 安全的双引号标量; 含特殊字符时强制加引号。"""
    # 双引号标量内转义双引号和反斜杠
    inner = value.replace("\\", "\\\\").replace('"', '\\"')
    # 若含 YAML 特殊字符或首尾空白则加引号
    if not inner or inner != inner.strip() or any(ch in inner for ch in ":#{}[]|>&*%@,!'`\""):
        return f'"{inner}"'
    return inner


def _ensure_webdav_conf(username: str, password: str) -> bool:
    """webdav.yml 不存在或为空文件时按内置模板生成一份; 已存在且非空则原样保留。

    返回 True 表示文件此刻可用(已存在/非空或刚刚生成成功)。
    """
    cfg_path = _webdav_conf_path()
    if cfg_path.exists() and cfg_path.stat().st_size > 0:
        return True
    if not username or not password:
        log("[共享] webdav 配置缺失且无可用凭据, 跳过自动生成")
        return False
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            _WEBDAV_CONF_TEMPLATE.format(username=_yaml_quote(username), password=_yaml_quote(password)),
            encoding="utf-8",
        )
        log(f"[共享] 已自动生成 webdav 配置文件: {cfg_path}")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[共享] 自动生成 webdav 配置文件失败: {e!r}")
        return False


def _bootstrap_webdav_conf() -> None:
    """启动时兜底: 若 webdav.yml 不存在, 按 settings.json 中的凭据生成一份。

    这样即使用户从未在面板里改过共享密码, 全新部署的 webdav sidecar 也有可读配置,
    不会因为 `-c /config/webdav.yml` 找不到文件而崩溃重启。
    """
    try:
        wd = load_shares().get("webdav", {})
        _ensure_webdav_conf(wd.get("username", ""), wd.get("password", ""))
    except Exception as e:  # noqa: BLE001
        log(f"[共享] 启动时生成 webdav 配置异常: {e!r}")


def _apply_webdav_creds(username: str, password: str) -> bool:
    """webdav 凭据在挂载的 webdav.yml 里, 改写文件 + 重启容器即生效, 无需重建。"""
    # 凭据中不能含换行/回车, 否则既破坏 YAML 也允许注入新键
    if not username or not password or "\n" in username or "\r" in username or "\n" in password or "\r" in password:
        log("[共享] webdav 凭据不能含换行符")
        return False
    try:
        cfg_path = _webdav_conf_path()
        # 首次部署时 ./webdav-config 可能是空目录, 或 webdav.yml 被截断成 0 字节:
        # 先兜底生成/覆盖空文件, 再走改写逻辑
        if (not cfg_path.exists() or cfg_path.stat().st_size == 0) and not _ensure_webdav_conf(username, password):
            log(f"[共享] webdav 配置文件缺失/为空且无法生成: {cfg_path}")
            return False

        lines = cfg_path.read_text(encoding="utf-8").splitlines()
        new_lines = []
        replaced_user = replaced_pass = False
        for line in lines:
            m = re.match(r"^(\s*username:\s*)([^\n]*)", line)
            if m and not replaced_user:
                new_lines.append(f"{m.group(1)}{_yaml_quote(username)}")
                replaced_user = True
                continue
            m = re.match(r"^(\s*password:\s*)([^\n]*)", line)
            if m and not replaced_pass:
                new_lines.append(f"{m.group(1)}{_yaml_quote(password)}")
                replaced_pass = True
                continue
            new_lines.append(line)
        if not (replaced_user and replaced_pass):
            log("[共享] webdav.yml 中未找到 username/password 行")
            return False
        cfg_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        _docker_request("POST", f"/containers/{SHARE_CONTAINERS['webdav']}/restart", None, 30)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[共享] webdav 凭据同步异常: {e!r}")
        return False


def apply_share_creds(proto: str, username: str, password: str) -> bool:
    """把网页设置的账号/密码同步到 sidecar 容器。"""
    if proto == "webdav":
        return _apply_webdav_creds(username, password)
    return _recreate_samba(username, password)


# --------------------------------------------------------------------------- task runner (共享工作线程)
def _spawn_worker() -> None:
    """把当前全局 task 记录启动为后台进程,并逐行写入日志。"""
    # B2 修复: 入口处捕获当前 task 引用, 全程只写 cur, 避免与新任务竞态时误清新任务的 proc/exit_code
    with _lock:
        cur = task
    # cmd 存的是 list,直接使用; 若为字符串(兼容旧数据)则用 shlex 按 shell 规则拆分
    cmd = shlex.split(cur["cmd"]) if isinstance(cur["cmd"], str) else cur["cmd"]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            # L8 修复: 新建进程组, 停止任务时能连带终止孙进程(如 update_distributions.py)
            start_new_session=True,
        )
    except Exception as e:  # L7 修复: Popen 失败(命令不存在等)不再静默死亡
        with _lock:
            cur["proc"] = None
            cur["exit_code"] = 127
            cur["finished"] = time.time()
        log(f"[任务启动失败] {e!r}")
        return
    with _lock:
        cur["proc"] = proc
    for raw in proc.stdout:
        line = raw.rstrip("\n").rstrip("\r")
        if not line:
            continue
        # 捕获 iso_runner 打印的目标文件大小标记: #TARGET <path> <bytes>
        if line.startswith("#TARGET "):
            try:
                _, _p, _s = line.split(" ", 2)
                _targets = {}
                with _lock:
                    _targets.update(cur["targets"])
                _targets[_p] = int(_s)
                with _lock:
                    cur["targets"] = _targets
            except (ValueError, IndexError):
                pass
            continue
        log(line)
    code = proc.wait()
    with _lock:
        cur["proc"] = None
        cur["exit_code"] = code
        cur["finished"] = time.time()
    if cur["cancelled"]:
        log("[任务已取消]")
    else:
        log(f"[任务结束] 退出码 {code}")


def start_task(kind: str, title: str, cmd: list, downloads=None) -> bool:
    """启动一个任务,同一时间只允许一个。cmd 为 list。"""
    global task
    with _lock:
        if task.get("proc") and task["proc"].poll() is None:
            return False
        task = {
            "kind": kind,
            "title": title,
            # cmd 直接存 list,避免字符串 split 拆坏含空格的 --select JSON 参数
            "cmd": list(cmd),
            "started": time.time(),
            "finished": None,
            "exit_code": None,
            "cancelled": False,
            "downloads": downloads or [],
            "targets": {},   # path -> 目标字节数(由 iso_runner 打印 #TARGET 收集)
            "proc": None,
        }
    log(f"[任务开始] {title}")
    threading.Thread(target=_spawn_worker, daemon=True).start()
    return True


# --------------------------------------------------------------------------- auto-sync scheduler
AUTO_SYNC_INTERVAL = int(os.environ.get("ISO_HUB_SYNC_INTERVAL", "86400"))  # 秒,默认每天
# L6 修复: 初始化为当前时间, 避免存在订阅时每次容器重启后 30s 内必然触发一次全量同步
AUTO_SYNC_LAST = {"t": time.time()}
# 自定义源自动刷新: 独立开关(默认关闭), 开启后才按间隔调度; last 初始化为当前时间避免重启立即触发
CUSTOM_REFRESH_LAST = {"t": time.time()}


def _run_sync_cmd() -> list:
    subs = [s for s in load_subscriptions() if s.get("enabled", True)]
    if not subs:
        return []
    sub_json = json.dumps(subs, ensure_ascii=False)
    return [
        PY, str(BASE_DIR / "sync_subscriptions.py"),
        "--json-file", str(JSON_FILE),
        "--download-dir", str(DATA_DIR),
        "--subscriptions", sub_json,
        "--update-first", str(REPO_DIR / "sources_config.json"),
        "--custom-json", str(CUSTOM_JSON),
        "--cache-json", str(CUSTOM_CACHE_JSON),
    ]


def _run_custom_repo_refresh_cmd() -> list:
    """自定义源展开 runner 的命令行(仅 strategy 源, 写回 custom_repo_cache.json)。"""
    return [
        PY, str(BASE_DIR / "custom_repo_refresh.py"),
        "--custom-json", str(CUSTOM_JSON),
        "--cache-json", str(CUSTOM_CACHE_JSON),
    ]


def refresh_custom_repo_cache() -> bool:
    """后台(子进程)刷新自定义源展开缓存。复用 start_task 的同一时间只允许一个任务锁。

    返回 True 表示任务已启动; False 表示有其它任务在运行或没有需要展开的源。
    """
    if running_task():
        return False
    if not _custom_source_list():
        return False
    cmd = _run_custom_repo_refresh_cmd()
    ok = start_task(
        "custom-refresh",
        f"刷新自定义源: {len(_custom_source_list())} 个发行版源",
        cmd,
        [],
    )
    return ok


def schedule_auto_sync() -> None:
    """后台线程: 检查用户自建调度 + 传统间隔 + 自定义源自动刷新, 到点且无任务运行则执行。"""
    def _trigger(msg: str):
        cmd = _run_sync_cmd()
        idle = task.get("proc") is None or task["proc"].poll() is not None
        if not cmd or not idle:
            return
        n = len([s for s in load_subscriptions() if s.get("enabled", True)])
        start_task("sync", msg, cmd, [])
        log(f"[自动同步] {msg}: 已启动, {n} 个发行版")

    def _trigger_custom_refresh():
        cfg = load_custom_auto_refresh()
        idle = task.get("proc") is None or task["proc"].poll() is not None
        if not cfg.get("enabled") or not idle or not _custom_source_list():
            return
        # 间隔由用户设置控制(默认 86400s); 只在开关开启时命中
        if time.time() - CUSTOM_REFRESH_LAST["t"] < cfg.get("interval", CUSTOM_REFRESH_INTERVAL_DEFAULT):
            return
        CUSTOM_REFRESH_LAST["t"] = time.time()
        ok = refresh_custom_repo_cache()
        if ok:
            log(f"[自定义源] 自动刷新已启动 (间隔 {cfg.get('interval', CUSTOM_REFRESH_INTERVAL_DEFAULT)//3600} 小时)")

    def _loop():
        while True:
            try:
                now = time.localtime()
                # 用户自建调度
                for s in load_schedules():
                    if not s.get("enabled", True):
                        continue
                    if s.get("type") == "once":
                        # 一次性: 命中时刻且尚未运行过则执行
                        if s.get("scheduled_at") and int(time.time()) >= int(s["scheduled_at"]) and not s.get("last_run"):
                            _trigger(f"定时任务[{s.get('name','?')}] (一次性)")
                            lst = load_schedules()
                            for x in lst:
                                if x.get("id") == s.get("id"):
                                    x["last_run"] = int(time.time())
                            save_schedules(lst)
                        continue
                    if _sched_matches(s, now):
                        if not s.get("last_run") or int(time.time()) - int(s.get("last_run", 0)) >= 60:
                            _trigger(f"定时任务[{s.get('name','?')}]")
                            lst = load_schedules()
                            for x in lst:
                                if x.get("id") == s.get("id"):
                                    x["last_run"] = int(time.time())
                            save_schedules(lst)
                # 传统间隔兜底
                idle = task.get("proc") is None or task["proc"].poll() is not None
                if idle and time.time() - AUTO_SYNC_LAST["t"] >= AUTO_SYNC_INTERVAL:
                    AUTO_SYNC_LAST["t"] = time.time()
                    _trigger(f"自动订阅同步 (间隔 {AUTO_SYNC_INTERVAL//3600} 小时)")
                # 自定义源自动刷新(独立开关, 默认关闭)
                _trigger_custom_refresh()
            except Exception as e:  # noqa: BLE001
                log(f"[自动同步] 异常: {e}")
            time.sleep(30)
    threading.Thread(target=_loop, daemon=True).start()
    log(f"[自动同步] 调度器已启动 (用户自建调度 + 间隔 {AUTO_SYNC_INTERVAL//3600} 小时)")


def running_task() -> dict | None:
    # D2 修复: 绝不把阻塞磁盘 IO(Path.stat/p.exists) 放在 _lock 内。
    # 旧实现 with _lock: 内逐文件 stat(), 在慢盘/网络盘或目标文件正被写入时,
    # 会把 /api/logs、/api/stop 和 _spawn_worker 的 log() 全卡在同一把锁上,
    # 叠加锁内死锁即造成 waitress 线程耗尽、队列飙升、应用日志一条打不出来。
    with _lock:
        if not task.get("proc"):
            return None
        dl = list(task.get("downloads", []))
        targets = dict(task.get("targets", {}))
        # 订阅同步等场景 downloads 可能为空但 targets 已由 #TARGET 填充:
        # 从 targets 派生 downloads, 让前端进度条能显示
        if not dl and targets:
            dl = [{"filename": str(Path(p).name), "path": str(p)} for p in targets]
        info = {
            "kind": task["kind"],
            "title": task["title"],
            "started": task["started"],
            "cancelled": task["cancelled"],
            "downloads": [],
        }
    # 锁外做磁盘 IO(每文件一次 stat, 失败按 0 处理)
    for d in dl:
        p = Path(d["path"])
        size = _tracked_size(p)
        info["downloads"].append({"filename": d["filename"], "path": str(p),
                                  "size": size, "total": targets.get(str(p), 0)})
    return info


def _tracked_size(p: Path) -> int:
    """取"该条目当前已落盘的字节数"。

    v1.3.2: targets 的键是 .part 路径(下载期间字节写在这里), 但要区分两种
    "没有 .part" 的情形:
      * 尚未开始下载  → 0
      * 已下载完成    → .part 已被 os.replace 成最终名, 进度应算满值
    因此 .part 不存在时回落到同名最终文件; 两者并存时以更可信的 .part 为准
    (并存说明半成品没落定, 最终名那份是旧的/待覆盖)。
    """
    try:
        if p.exists():
            return p.stat().st_size
        if p.name.endswith(PART_SUFFIX):
            final = p.with_name(p.name[: -len(PART_SUFFIX)])
            if final.exists():
                return final.stat().st_size
    except OSError:
        pass
    return 0


def stop_task() -> bool:
    with _lock:
        p = task.get("proc")
        if not p or p.poll() is not None:
            return False
        task["cancelled"] = True
        log("[收到停止请求，正在终止进程…]")
        try:
            # L8 修复: 终止整个进程组(含孙进程), 避免订阅同步内的 update_distributions 成孤儿继续写清单
            import signal
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            p.terminate()

        def _kill():
            try:
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    import signal
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    p.kill()
        threading.Thread(target=_kill, daemon=True).start()
        return True


# --------------------------------------------------------------------------- flask app
app = Flask(__name__, static_folder="static", static_url_path="/static")
AUTH_TOKEN = os.environ.get("ISO_HUB_TOKEN", "").strip()
# 强制登录开关: ISO_HUB_REQUIRE_LOGIN=1 时, 除登录/自身状态接口外所有 API 均需登录会话或 X-Auth-Token
REQUIRE_LOGIN = os.environ.get("ISO_HUB_REQUIRE_LOGIN", "1").strip().lower() in ("1", "true", "yes", "on")


@app.before_request
def require_auth():
    """设置 ISO_HUB_TOKEN 后,除页面/静态/健康检查外的所有 API 需携带 X-Auth-Token 或有效登录会话。
    设置 ISO_HUB_REQUIRE_LOGIN 后, 除登录/自身状态接口外的所有 API 均需登录会话或 X-Auth-Token(强制登录)。"""
    # 登录/登出/获取自身状态接口始终放行
    if request.path in ("/api/user/login", "/api/user/me", "/api/user/logout"):
        return None
    if REQUIRE_LOGIN:
        # 强制登录: 页面壳子/静态/健康检查放行(前端靠 /api/user/me 判断是否弹登录遮罩), 其余 API 一律需登录
        if request.method == "GET" and (request.path == "/" or request.path.startswith("/static/") or request.path == "/api/health"):
            return None
        if request.headers.get("X-Auth-Token") == AUTH_TOKEN:
            return None
        if _valid_session():
            return None
        return jsonify({"error": "unauthorized"}), 401
    # 原逻辑: 仅设置 ISO_HUB_TOKEN 时拦截写操作 API
    if not AUTH_TOKEN:
        return None
    if request.method == "GET" and (request.path == "/" or request.path.startswith("/static/") or request.path == "/api/health"):
        return None
    if request.headers.get("X-Auth-Token") == AUTH_TOKEN:
        return None
    # 兼容用户登录会话
    if _valid_session():
        return None
    return jsonify({"error": "unauthorized"}), 401


@app.after_request
def no_cache_api(resp):
    """所有 API 响应禁用缓存, 避免前端「刷新列表」拿到浏览器缓存的旧数据而无反应。"""
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.get("/")
def index():
    # 禁用缓存, 防止前端更新后浏览器仍加载旧 index.html(导致输入框缺失/交互失效)
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True})


@app.get("/api/distros")
def api_distros():
    return jsonify(build_distros())


@app.get("/api/state")
def api_state():
    t = running_task()
    return jsonify({"running": t is not None, "task": t})


@app.get("/api/logs")
def api_logs():
    # L1 修复: 非数字 after 参数兜底为 0, 不再抛 500
    try:
        after = int(request.args.get("after", 0))
    except (ValueError, TypeError):
        after = 0
    with _lock:
        lines = [x for x in _log_lines if x["i"] > after]
        final_seq = _log_seq
    return jsonify({"after": final_seq, "lines": lines})


@app.post("/api/download")
def api_download():
    body = request.get_json(force=True, silent=True) or {}
    entries = body.get("entries") or []
    if not entries:
        return jsonify({"error": "没有选择任何条目"}), 400
    # 重新从当前清单校验，防止提交伪造数据
    # 前端每条目传 {distribution, download_url}; download_url 是用户选定的源URL
    # (可为主源 download_url, 也可为 download_urls 里的某个镜像), 据此匹配对应条目
    cur = load_json()
    wanted = {(e.get("distribution"), e.get("download_url")) for e in entries}
    cand_index = {}   # (distribution, 任意候选url) -> entry
    for e in cur.get("distributions", []):
        cand_index[(e["distribution"], e["download_url"])] = e
        for c in e.get("download_urls", []):
            cand_index[(e["distribution"], c)] = e
    matched = [cand_index[w] for w in wanted if w in cand_index]
    if not matched:
        return jsonify({"error": "所选条目不在当前发行版清单中，请先刷新列表"}), 400

    # 用户对某个文件手动指定的源URL(不存在则空 = 跟随全局策略自动选)
    chosen = {e.get("distribution"): e.get("download_url", "") for e in entries}
    download_payload = []
    seen = set()
    for e in matched:
        fname = e["download_url"].rstrip("/").rsplit("/", 1)[-1]
        # 路径穿越防护: 通过白名单校验确保目标目录在 DATA_DIR 内
        target = _safe_join(e.get("type", "linux"), e.get("distribution", ""))
        if target is None:
            return jsonify({"error": f"非法的发行版类型/名称: {e.get('type')}/{e.get('distribution')}"}), 400
        # 下载期间字节写在 <最终名>.part 上(iso_runner 的 .part 原子落盘协议),
        # 且 #TARGET 哨兵上报的也是 .part 路径。这里必须同样用 .part 路径,
        # 才能让 running_task() 的 stat(size) 与 targets 的 key 对得上,
        # 否则进度条永远 0%(size=0 且 total 取不到)。
        path = str(target / (fname + PART_SUFFIX))
        if (e["distribution"], fname) not in seen:
            seen.add((e["distribution"], fname))
            download_payload.append({"filename": fname, "path": path})

    select_json = json.dumps(
        [{"distribution": e["distribution"], "download_url": e["download_url"],
          "pin": chosen.get(e["distribution"]) or ""} for e in matched],
        ensure_ascii=False,
    )
    cmd = [
        PY, str(BASE_DIR / "iso_runner.py"),
        "--json-file", str(JSON_FILE),
        "--download-dir", str(DATA_DIR),
        "--select", select_json,
        "--strategy", load_source_strategy(),
    ]
    names = sorted({e["distribution"] for e in matched})
    ok = start_task("download", f"下载: {'、'.join(names)}（{len(matched)} 个文件）", cmd, download_payload)
    if not ok:
        return jsonify({"error": "已有任务在运行"}), 409
    return jsonify({"ok": True, "files": len(matched)})


@app.post("/api/update-meta")
def api_update_meta():
    cmd = [
        PY, str(REPO_DIR / "update_distributions.py"),
        "--config", str(REPO_DIR / "sources_config.json"),
        "--output", str(JSON_FILE),
        "--pretty",
    ]
    ok = start_task("meta", "抓取镜像站，刷新发行版清单元数据", cmd)
    if not ok:
        return jsonify({"error": "已有任务在运行"}), 409
    # 自定义源独立存储，不随 update-meta 覆盖；UI 读取时再合并
    return jsonify({"ok": True})


@app.get("/api/custom-sources")
def api_custom_sources():
    return jsonify({"sources": load_custom_sources()})


@app.get("/api/source-strategy")
def api_source_strategy_get():
    return jsonify({"strategy": load_source_strategy()})


@app.post("/api/source-strategy")
def api_source_strategy_set():
    body = request.get_json(force=True, silent=True) or {}
    s = (body.get("strategy") or "A").strip().upper()
    if s not in ("A", "B"):
        return jsonify({"error": "strategy 只能是 A 或 B"}), 400
    save_source_strategy(s)
    log(f"[设置] 选源策略改为 {s}")
    return jsonify({"ok": True, "strategy": s})


@app.post("/api/custom-sources")
def api_custom_sources_add():
    body = request.get_json(force=True, silent=True) or {}
    distribution = (body.get("distribution") or "").strip()
    typ = (body.get("type") or "linux").strip()
    if not distribution:
        return jsonify({"error": "需要 distribution(发行版名)"}), 400
    if typ not in ALLOWED_TYPES:
        return jsonify({"error": f"type 必须是 {', '.join(sorted(ALLOWED_TYPES))} 之一"}), 400
    items = load_custom_sources()
    strategy = (body.get("strategy") or "").strip()
    if strategy:
        # 发行版源: 追踪该镜像站目录的最新版本
        if strategy not in ("dated_directory", "flat_listing", "versioned_flat_listing", "static"):
            return jsonify({"error": f"不支持的 strategy: {strategy}"}), 400
        listing_url = (body.get("listing_url") or "").strip()
        download_template = (body.get("download_template") or "").strip()
        if not listing_url or not download_template:
            return jsonify({"error": "发行版源需要 listing_url 与 download_template"}), 400
        # SSRF 防护: listing_url 与下载模板必须为 http/https
        if not _is_http_url(listing_url):
            return jsonify({"error": "listing_url 必须是 http/https 链接"}), 400
        if download_template and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", download_template):
            if not _is_http_url(download_template):
                return jsonify({"error": "download_template 若含绝对 URL 协议, 必须是 http/https"}), 400
        checksum_template = (body.get("checksum_template") or "").strip()
        if checksum_template and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", checksum_template):
            if not _is_http_url(checksum_template):
                return jsonify({"error": "checksum_template 若含绝对 URL 协议, 必须是 http/https"}), 400
        # 判断重复: 同名 + 同 strategy + 同 listing_url
        if any(c.get("strategy") == strategy and c.get("distribution") == distribution
               and c.get("listing_url") == listing_url for c in items):
            return jsonify({"error": "该发行版源已存在"}), 400
        try:
            max_entries = max(1, int(body.get("max_entries") or 1))
        except (ValueError, TypeError):
            max_entries = 1
        entry = {
            "distribution": distribution,
            "type": typ,
            "strategy": strategy,
            "listing_url": listing_url,
            "version_regex": (body.get("version_regex") or "").strip(),
            "artifact_regex": (body.get("artifact_regex") or "").strip(),
            "download_template": download_template,
            "checksum_template": checksum_template,
            "max_entries": max_entries,
        }
        if strategy == "static":
            vs = body.get("versions")
            if isinstance(vs, list) and vs:
                entry["versions"] = [str(v).strip() for v in vs]
        items.append(entry)
        save_custom_sources(items)
        # 不再在请求线程内实时抓取校验: 落盘后由后台任务展开(若当前有任务运行则稍后手动刷新)
        refresh_custom_repo_cache()
        log(f"[自定义源] 添加发行版源 {distribution}({strategy}) <- {listing_url} (待后台展开)")
        return jsonify({"ok": True, "sources": items, "expanded": 0, "pending_refresh": True})
    # 普通直链
    url = (body.get("download_url") or "").strip()
    if not url:
        return jsonify({"error": "需要 download_url 或 strategy"}), 400
    if not _is_http_url(url):
        return jsonify({"error": "download_url 必须是 http/https 链接"}), 400
    if any(c.get("download_url") == url for c in items):
        return jsonify({"error": "该地址已存在"}), 400
    checksum_url = (body.get("checksum_url") or "").strip()
    if checksum_url and not _is_http_url(checksum_url):
        return jsonify({"error": "checksum_url 必须是 http/https 链接"}), 400
    items.append({
        "distribution": distribution,
        "type": typ,
        "download_url": url,
        "checksum_url": checksum_url,
        "checksum": (body.get("checksum") or "").strip(),
    })
    save_custom_sources(items)
    log(f"[自定义源] 添加 {distribution} <- {url}")
    return jsonify({"ok": True, "sources": items})


@app.delete("/api/custom-sources")
def api_custom_sources_del():
    url = request.args.get("url", "")
    distribution = request.args.get("distribution", "")
    strategy = request.args.get("strategy", "")
    if url:
        items = [c for c in load_custom_sources() if c.get("download_url") != url]
    elif distribution and strategy:
        items = [c for c in load_custom_sources()
                 if not (c.get("distribution") == distribution and c.get("strategy") == strategy)]
    else:
        return jsonify({"error": "需要 url 或 distribution+strategy"}), 400
    save_custom_sources(items)
    log(f"[自定义源] 删除 {url or f'{distribution}({strategy})'}")
    return jsonify({"ok": True, "sources": items})


@app.post("/api/custom-sources/refresh")
def api_custom_sources_refresh():
    """手动触发自定义源展开(后台子进程, 非阻塞)。复用 start_task 的单一任务锁。"""
    if running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    sources = _custom_source_list()
    if not sources:
        return jsonify({"error": "没有 strategy 类型的自定义源"}), 400
    ok = refresh_custom_repo_cache()
    if not ok:
        return jsonify({"error": "已有任务在运行"}), 409
    return jsonify({"ok": True, "count": len(sources)})


@app.get("/api/custom-sources/auto-refresh")
def api_custom_auto_refresh_get():
    """读取自定义源自动刷新设置(独立开关, 默认关闭)。"""
    return jsonify({"custom_source_auto_refresh": load_custom_auto_refresh()})


@app.post("/api/custom-sources/auto-refresh")
def api_custom_auto_refresh_set():
    """保存自定义源自动刷新设置。body: {enabled?:bool, interval?:int(秒)}"""
    body = request.get_json(force=True, silent=True) or {}
    enabled = body.get("enabled")
    interval = body.get("interval")
    # 显式只接受 bool/None; 避免字符串 'false' 被当成真值
    if enabled is not None and not isinstance(enabled, bool):
        return jsonify({"error": "enabled 必须是布尔值"}), 400
    if interval is not None:
        try:
            interval = int(interval)
        except (ValueError, TypeError):
            return jsonify({"error": "interval 必须是整数(秒)"}), 400
    cfg = save_custom_auto_refresh(enabled, interval)
    log(f"[自定义源] 自动刷新设置已保存: enabled={cfg['enabled']}, interval={cfg['interval']}s")
    return jsonify({"ok": True, "custom_source_auto_refresh": cfg})


@app.get("/api/subscriptions")
def api_subscriptions():
    return jsonify({"subscriptions": load_subscriptions()})


@app.post("/api/subscriptions")
def api_subscriptions_save():
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("subscriptions") or []
    save_subscriptions(items)
    log(f"[订阅] 保存 {len(items)} 条订阅配置")
    return jsonify({"ok": True})


@app.post("/api/sync-subscriptions")
def api_sync_subscriptions():
    """对所有已启用订阅执行: 刷新清单 -> 下载最新 N -> 删旧版。"""
    if running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    subs = [s for s in load_subscriptions() if s.get("enabled", True)]
    if not subs:
        return jsonify({"error": "没有已启用的订阅"}), 400
    sync_cmd = _run_sync_cmd()
    ok = start_task(
        "sync",
        f"订阅同步: {len(subs)} 个发行版(自动拉最新+删旧)",
        sync_cmd,
        [],
    )
    if not ok:
        return jsonify({"error": "已有任务在运行"}), 409
    return jsonify({"ok": True, "count": len(subs)})


@app.post("/api/prune")
def api_prune():
    body = request.get_json(force=True, silent=True) or {}
    name, typ = body.get("distribution"), body.get("type")
    if not name or not typ:
        return jsonify({"error": "缺少 distribution/type"}), 400
    # H1 修复: 路径穿越防护, target 必须安全地落在 DATA_DIR 内
    target = _safe_join(typ, name)
    if target is None:
        return jsonify({"error": "非法的 distribution/type 参数"}), 400
    if running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    cur = load_json()
    # B7 修复: 记录当前清单文件名到历史(累积), 供清理时判断哪些文件"曾经属于清单"
    _record_manifest_history(cur.get("distributions", []))
    expected = {
        e["download_url"].rstrip("/").rsplit("/", 1)[-1]
        for e in cur.get("distributions", [])
        if e.get("distribution") == name and e.get("type") == typ
    }
    removed, skipped = [], []
    protected = set(load_protected())
    if target.exists():
        for f in target.iterdir():
            if f.is_file() and f.name not in expected and f.suffix.lower() in ISO_SUFFIXES:
                rel = f"{typ}/{name}/{f.name}"
                if rel in protected or f.name in protected:
                    skipped.append(f"{f.name}: 受保护, 跳过")
                    continue
                # B7 修复: 只删除"曾出现在历史清单中"的文件, 保护用户手动放入/种子下载的 ISO
                if not _is_known_file(typ, name, f.name):
                    skipped.append(f"{f.name}: 非清单文件, 保留")
                    continue
                try:
                    f.unlink()
                    removed.append(f.name)
                except OSError as e:
                    skipped.append(f"{f.name}: {e}")
    log(f"[清理] {typ}/{name}: 删除 {len(removed)} 个过期文件，跳过 {len(skipped)}")
    return jsonify({"ok": True, "removed": removed, "skipped": skipped})


@app.post("/api/delete-files")
def api_delete_files():
    """删除清单内已下载的 ISO 文件。

    请求体: {"items": [{"type": "linux", "distribution": "Ubuntu", "filename": "xxx.iso"}, ...]}

    安全约束(逐条对应):
      1. 路径穿越: (type, distribution) 经 _safe_join 校验, 必须落在 DATA_DIR 内
      2. 文件名: 拒绝空/含分隔符/为 . 或 ..; 且最终路径必须仍在目标目录内
      3. 只删清单内文件: 文件名必须属于该发行版**当前清单**(expected), 否则拒绝
         —— 防止误删用户手动放入的 ISO; 若要删除非清单文件请用「清理过期」接口
      4. **受保护(🔒锁定)文件一律拒绝删除**: 无任何绕过参数。锁定优先级高于手动删除,
         用户必须先点解锁按钮才可删除。命中时以 locked 列表单独返回, 供前端给出明确指引。
      5. 任务互斥: 有下载任务在跑时拒绝, 避免边下边删造成状态错乱
    """
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items") or []
    if not items:
        return jsonify({"error": "没有选择任何文件"}), 400
    if running_task():
        return jsonify({"error": "已有任务在运行, 请稍后再试"}), 409

    cur = load_json()
    # 按 (type, distribution) 归集当前清单里的合法文件名
    expected: dict[tuple[str, str], set[str]] = {}
    for e in cur.get("distributions", []):
        url = e.get("download_url", "") or ""
        fname = url.rstrip("/").rsplit("/", 1)[-1]
        if not fname:
            continue
        expected.setdefault((e.get("type", "linux"), e.get("distribution", "")), set()).add(fname)

    protected = set(load_protected())
    removed, skipped, locked = [], [], []
    for it in items:
        typ = str(it.get("type") or "").strip()
        name = str(it.get("distribution") or "").strip()
        fname = str(it.get("filename") or "").strip()
        label = f"{fname or '(空文件名)'}"
        if not fname or "/" in fname or "\\" in fname or fname in (".", ".."):
            skipped.append(f"{label}: 非法文件名")
            continue
        target = _safe_join(typ, name)
        if target is None:
            skipped.append(f"{label}: 非法的 type/distribution")
            continue
        # 只允许删除当前清单内声明的文件(防误删用户自有 ISO)
        if fname not in expected.get((typ, name), set()):
            skipped.append(f"{label}: 不在当前清单内, 已拒绝(如需清理请用「清理过期」)")
            continue
        rel = f"{typ}/{name}/{fname}"
        # 锁定文件: 硬拒绝, 无 force 后门
        if rel in protected or fname in protected:
            locked.append({"type": typ, "distribution": name, "filename": fname})
            continue
        # 前端传的是"目标文件名"(如 xxx.iso), 但下载中的半成品实际叫
        # xxx.iso.part。删除必须连带处理半成品: 否则"下载停止"的文件点删除会
        # 报"文件不存在"被跳过(用户明明看得到它占着空间)。
        fp = (target / fname)
        part_fp = target / (fname + PART_SUFFIX)
        # 二次确认最终路径仍在目标目录内(防 symlink / 拼接绕过)
        for _p in (fp, part_fp):
            try:
                _p.resolve().relative_to(target.resolve())
            except ValueError:
                skipped.append(f"{label}: 路径越界, 已拒绝")
                break
        else:
            victims = [p for p in (fp, part_fp) if p.is_file()]
            if not victims:
                skipped.append(f"{label}: 文件不存在")
                continue
            try:
                for _p in victims:
                    _p.unlink()
                # 半成品被删掉后, 残留的"下载停止"失败记录也要清掉,
                # 否则 UI 仍会显示「下载停止」而文件已经没了。
                if part_fp in victims:
                    clear_failure(f"{typ}/{name}/{fname}")
                removed.append(fname + (PART_SUFFIX if fp not in victims else ""))
            except OSError as e:
                skipped.append(f"{label}: {e}")

    ok = not locked
    for lk in locked:
        skipped.append(f"{lk['filename']}: 文件已被锁定, 已跳过")
    resp = {"ok": ok, "removed": removed, "skipped": skipped, "locked": locked}
    if locked:
        resp["error"] = f"{len(locked)} 个文件已被锁定"
    return jsonify(resp)


# ---------- 受保护/锁定文件 ----------
def _rel_of_file(abs_path: Path, typ: str = "", name: str = "") -> str:
    """把磁盘文件绝对路径规约成相对路径 type/name/文件名。"""
    try:
        return str(abs_path.relative_to(DATA_DIR)).replace("\\", "/")
    except ValueError:
        # 不在 DATA_DIR 下时, 退化为 typ/name/文件名 或仅文件名
        return f"{typ}/{name}/{abs_path.name}".lstrip("/") if typ and name else abs_path.name


@app.get("/api/protected")
def api_protected():
    lst = load_protected()
    return jsonify({"protected": lst})


@app.post("/api/protected")
def api_protected_toggle():
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path")  # 相对路径 type/name/文件名 或 文件名
    if not path:
        return jsonify({"error": "缺少 path"}), 400
    lst = load_protected()
    if path in lst:
        lst = [p for p in lst if p != path]
        removed = True
    else:
        lst.append(path)
        removed = False
    save_protected(lst)
    return jsonify({"ok": True, "protected": lst, "removed": removed})


# ---------- 定时任务 / 调度 ----------
def _sched_id() -> str:
    return secrets.token_hex(4)


@app.get("/api/schedules")
def api_schedules():
    return jsonify({"schedules": load_schedules()})


@app.post("/api/schedules")
def api_schedules_add():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip() or "定时任务"
    typ = body.get("type") or "daily"
    sched = {
        "id": body.get("id") or _sched_id(),
        "name": name,
        "type": typ,
        "time": body.get("time") or "00:00",
        "day_of_week": int(body.get("day_of_week", 0) or 0),
        "day_of_month": int(body.get("day_of_month", 1) or 1),
        "month_of_year": int(body.get("month_of_year", 1) or 1),
        "scheduled_at": int(body.get("scheduled_at") or 0),  # 一次性调度用的时间戳
        "enabled": bool(body.get("enabled", True)),
        "last_run": 0,
    }
    lst = load_schedules()
    lst = [x for x in lst if x.get("id") != sched["id"]]  # 更新或新增
    lst.append(sched)
    save_schedules(lst)
    return jsonify({"ok": True, "schedules": lst})


@app.delete("/api/schedules")
def api_schedules_del():
    sid = request.args.get("id")
    if not sid:
        return jsonify({"error": "缺少 id"}), 400
    lst = [x for x in load_schedules() if x.get("id") != sid]
    save_schedules(lst)
    return jsonify({"ok": True, "schedules": lst})


@app.post("/api/schedules/run")
def api_schedules_run():
    """手动触发一次订阅同步。"""
    if running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    cmd = _run_sync_cmd()
    if not cmd:
        return jsonify({"error": "无启用的订阅"}), 400
    n = len([s for s in load_subscriptions() if s.get("enabled", True)])
    start_task("sync", f"手动订阅同步: {n} 个发行版", cmd, [])
    return jsonify({"ok": True})


# ---------- 用户登录 / 会话 token ----------
@app.get("/api/user/me")
def api_user_me():
    """返回当前登录状态(不强制要求已登录)。附带 require_login/auth_configured 标志供前端判定是否弹登录遮罩。"""
    username = _valid_session()
    return jsonify({
        "authenticated": bool(username),
        "username": username,
        # B5 修复: 只有强制登录模式下前端才应弹遮罩; 未设 token 时 API 本就敞开, 不应强凑 UX
        "require_login": REQUIRE_LOGIN,
        "auth_configured": bool(AUTH_TOKEN) or bool(REQUIRE_LOGIN),
    })


@app.post("/api/user/login")
def api_user_login():
    body = request.get_json(force=True, silent=True) or {}
    u = (body.get("username") or "").strip()
    p = body.get("password") or ""
    # 首次登录且尚未播种: 允许用 ISO_HUB_ADMIN_USER/PASS 播种
    if not load_users() and not (os.environ.get("ISO_HUB_ADMIN_USER", "").strip() or ""):
        # 无可播种账号, 允许首个登录者创建管理员(设置页用相同用户名/密码建账号)
        # B5 修复: 仅限 localhost 请求放行首用户建号, 防止公网访客顺手抢注管理员
        if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
            return jsonify({"error": "管理员账号未设置, 请先在服务器本机或通过环境变量创建"}), 403
        if not u or not p:
            return jsonify({"error": "缺少用户名或密码"}), 400
        salt = secrets.token_hex(16)
        users = load_users()
        users[u] = {"password_hash": _hash_pw(p, salt), "salt": salt,
                    "created_at": int(time.time())}
        save_users(users)
        log(f"[用户] 首次创建管理员账号: {u}")
        return jsonify({"ok": True, "token": _issue_token(u), "username": u})
    v = _verify_login(u, p)
    if v == "no_user":
        return jsonify({"error": "用户名不存在", "code": "no_user"}), 401
    if v == "bad_pass":
        return jsonify({"error": "密码错误", "code": "bad_pass"}), 401
    return jsonify({"ok": True, "token": _issue_token(u), "username": u})


@app.post("/api/user/logout")
def api_user_logout():
    tok = request.headers.get("X-Auth-Token")
    if tok and tok in _sessions:
        _sessions.pop(tok, None)
        _sessions_persist()
    return jsonify({"ok": True})


@app.post("/api/user/password")
def api_user_password():
    """修改当前登录用户密码(需会话 token)。"""
    # H3 修复: 用 _valid_session 统一校验(含过期检查), 而非裸 _sessions.get(过期也放行)
    username = _valid_session()
    if not username:
        return jsonify({"error": "未登录"}), 401
    body = request.get_json(force=True, silent=True) or {}
    old = body.get("old_password") or ""
    nw = body.get("new_password") or ""
    if not nw:
        return jsonify({"error": "新密码不能为空"}), 400
    if not _check_login(username, old):
        return jsonify({"error": "当前密码错误"}), 401
    salt = secrets.token_hex(16)
    users = load_users()
    users[username] = {"password_hash": _hash_pw(nw, salt), "salt": salt,
                       "created_at": users.get(username, {}).get("created_at", int(time.time()))}
    save_users(users)
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    return jsonify({"ok": stop_task()})


@app.get("/api/shares")
def api_shares():
    shares = load_shares()
    # 附带每个 sidecar 容器实时四态(running/stopped/not_deployed/unknown)
    for proto, s in shares.items():
        s["container"] = service_state(SHARE_CONTAINERS[proto])
    return jsonify({"shares": shares})


@app.post("/api/shares")
def api_shares_save():
    body = request.get_json(force=True, silent=True) or {}
    shares = load_shares()
    for proto in ("samba", "webdav"):
        if proto not in body:
            continue
        p = body[proto] or {}
        cur = shares[proto]
        cred_changed = False
        if "username" in p:
            cur["username"] = str(p["username"]).strip()
            cred_changed = True
        if "password" in p:
            cur["password"] = str(p["password"]).strip()
            cred_changed = True
        if "port" in p:
            cur["port"] = str(p["port"]).strip()
        if cred_changed:
            # 同步新凭据到运行中的 sidecar 容器(更新 env + 重启)
            ok = apply_share_creds(proto, cur["username"], cur["password"])
            if not ok:
                # L4 修复: 此处尚未 save_shares, 凭据并未落盘 —— 修正与实际不符的错误文案
                return jsonify({"error": f"{proto} 凭据同步到容器失败, 设置未保存, 请检查 sidecar 是否运行"}), 500
        if "enabled" in p:
            # 启停 sidecar 容器; 未部署时给出命令提示(应用读不到宿主 compose 文件, 无法自动创建)
            want = bool(p["enabled"])
            cur["enabled"] = want
            ok = set_share(proto, want)
            if not ok:
                st = service_state(SHARE_CONTAINERS[proto])
                if st == "not_deployed":
                    return jsonify({"error": f"{proto} 未部署: 请用 docker compose --profile share up -d 先创建该容器"}), 500
                if st == "unknown":
                    return jsonify({"error": f"{proto} 容器状态未知(请检查 socket-proxy 是否运行)"}), 500
                return jsonify({"error": f"{proto} 容器操作失败(是否已部署 sidecar?)"}), 500
    save_shares(shares)
    log(f"[共享] 设置已保存: SMB={shares['samba']['enabled']} WebDAV={shares['webdav']['enabled']}")
    return jsonify({"ok": True, "shares": {k: {**v, "container": service_state(SHARE_CONTAINERS[k])} for k, v in shares.items()}})


@app.get("/api/qb/settings")
def api_qb_settings_get():
    """获取 qBittorrent 设置及容器实时四态状态。"""
    qb = load_qb_settings()
    qb["container"] = service_state(QB_CONTAINER)
    return jsonify({"ok": True, "qb": qb})


@app.post("/api/qb/settings")
def api_qb_settings_post():
    """保存 qBittorrent 设置并启停/重启容器。body: {enabled?:bool, username?:str, password?:str}"""
    body = request.get_json(force=True, silent=True) or {}
    qb = load_qb_settings()
    changed = False
    if "username" in body:
        qb["username"] = str(body["username"]).strip()
        changed = True
    if "password" in body:
        qb["password"] = str(body["password"]).strip()
        changed = True

    # 启用或保持启用时，必须提供非空凭据
    will_be_enabled = bool(body["enabled"]) if "enabled" in body else qb.get("enabled", False)
    if will_be_enabled and (not qb.get("username") or not qb.get("password")):
        return jsonify({"error": "启用 qBittorrent 必须提供非空用户名和密码"}), 400

    enabled_changed = False
    if "enabled" in body:
        want = bool(body["enabled"])
        if want != qb.get("enabled", False):
            ok = set_qb(want, qb["username"], qb["password"])
            if not ok:
                st = service_state(QB_CONTAINER)
                if st == "not_deployed":
                    return jsonify({"error": "qBittorrent 未部署: 请用 docker compose --profile bt up -d 先创建该容器"}), 500
                if st == "unknown":
                    return jsonify({"error": "qBittorrent 容器状态未知(请检查 socket-proxy 是否运行)"}), 500
                return jsonify({"error": "qBittorrent 容器操作失败，请检查是否已部署 sidecar"}), 500
            qb["enabled"] = want
            changed = True
            enabled_changed = True

    # 如果凭据变了且当前是启用状态(或刚启用)，同步密码到容器
    if changed and qb.get("enabled", False):
        if not enabled_changed:
            # 仅修改凭据：直接写 conf 并重启
            if QB_CONF_PATH.exists() and _set_qb_password(qb["username"], qb["password"]):
                rr = _docker_request("POST", f"/containers/{QB_CONTAINER}/restart", None, 30)
                if rr.status not in (200, 204, 304):
                    return jsonify({"error": "qBittorrent 凭据已保存，但重启容器失败"}), 500
            else:
                return jsonify({"error": "qBittorrent 凭据保存失败，请检查 qb-config 是否正确挂载"}), 500
        save_qb_settings(qb)
    elif changed:
        save_qb_settings(qb)

    qb["container"] = service_state(QB_CONTAINER)
    return jsonify({"ok": True, "qb": qb})


# --------------------------------------------------------------------------- 种子下载 (qBittorrent + DistroWatch)
def _qb() -> QBClient:
    qb_cfg = load_qb_settings()
    return QBClient(qb_cfg.get("url"), qb_cfg.get("username"), qb_cfg.get("password"))


def _ensure_qb_enabled() -> tuple[bool, tuple | None]:
    """检查 qBittorrent 是否已启用；未启用时返回 (False, (response, status))。"""
    qb = load_qb_settings()
    if not qb.get("enabled", False):
        return False, (jsonify({"error": "qBittorrent 未启用，请在「设置」中启用并设置账号密码"}), 403)
    return True, None


@app.get("/api/torrent/sources")
def api_torrent_sources():
    """扫描 DistroWatch 官方种子源 + 用户自加 RSS/链接, 返回可下载的种子列表。"""
    ok, err = _ensure_qb_enabled()
    if not ok:
        return err
    if not TORRENT_AVAILABLE:
        return jsonify({"error": "种子模块未加载: " + (_torrent_import_err or "")}), 500
    try:
        res = dtorrents.scan_sources()
        return jsonify({"ok": True, "source": res.get("source"),
                        "reports": res.get("reports", []),
                        "items": res.get("items", []),
                        "user_rss": dtorrents.get_user_rss(),
                        "user_links": dtorrents.get_user_links()})
    except Exception as e:  # noqa: BLE001
        log(f"[种子] 扫描种子源失败: {e!r}")
        return jsonify({"error": f"扫描种子源失败: {e}"}), 500


@app.get("/api/torrent/info")
def api_torrent_info():
    """查询 qBittorrent 连接状态 + 传输信息 + 种子列表(含本地是否已存在)."""
    ok, err = _ensure_qb_enabled()
    if not ok:
        return err
    if not TORRENT_AVAILABLE:
        return jsonify({"error": "种子模块未加载"}), 500
    try:
        qb = _qb()
        ver = qb.version()
        if not ver:
            cfg = qb_config()
            return jsonify({"ok": False, "error": f"无法连接 qBittorrent ({cfg['QB_URL']}), 请确认已部署 sidecar 且凭据正确",
                            "config": {k: (cfg[k][:12] + "..." if k == "QB_PASS" else cfg[k]) for k in cfg}}), 200
        transfer = qb.transfer_info()
        toks = qb.list_torrents()
        # 标记本地是否已存在同名文件
        inv = disk_inventory()
        names = {f["name"] for fs in inv.values() for f in fs}
        for t in toks:
            t["local_has"] = bool(set(t.get("name", "").split()) & names) or any(
                f in names for f in _torrent_file_names(t))
        return jsonify({"ok": True, "connected": True, "version": ver,
                        "transfer": transfer, "torrents": toks})
    except Exception as e:  # noqa: BLE001
        log(f"[种子] 查询 qBittorrent 状态失败: {e!r}")
        return jsonify({"ok": False, "error": f"查询失败: {e}"}), 500


def _torrent_file_names(t: dict) -> list:
    """从 qBittorrent 种子内容字段尽量还原文件名, 用于本地存在性判断。"""
    n = (t.get("name") or "").strip()
    if not n:
        return []
    # name 可能形如  ubuntu-26.04.1-desktop-amd64.iso 或 目录/文件.iso
    base = n.rsplit("/", 1)[-1].replace(".torrent", "")
    # 剥离可能的 hash/扩展
    return [base] if base else []


@app.post("/api/torrent/add")
def api_torrent_add():
    """把种子(URL/磁力)交给 qBittorrent 下载。
    body: {urls:[...], distro?, type?, category?}
    若能推断发行版, 保存路径设为 /data/<type>/<发行版>/; 否则存 /data/_torrents/。
    """
    ok, err = _ensure_qb_enabled()
    if not ok:
        return err
    if not TORRENT_AVAILABLE:
        return jsonify({"error": "种子模块未加载"}), 500
    body = request.get_json(force=True, silent=True) or {}
    urls = [u for u in (body.get("urls") or []) if u and str(u).strip()]
    if not urls:
        return jsonify({"error": "未提供任何种子 URL/磁力链接"}), 400
    urls = [str(u).strip() for u in urls]
    for u in urls:
        if u.startswith("magnet:"):
            continue
        if not _is_http_url(u):
            return jsonify({"error": f"非法的种子链接(仅允许 http/https/magnet): {u[:80]}"}), 400
    # 推断保存路径
    save_path = None
    # 后端优先从 URL 文件名推断发行版(可靠), 前端传来的 distro 可能是完整文件名(如
    # "NetBSD-9.5-amd64.iso")而非发行版名, 若直接当目录名会用错(类型也恒为 linux)。
    distro = (body.get("distro") or "").strip()
    typ = (body.get("type") or "linux").strip()
    probe = urls[0].rsplit("/", 1)[-1]
    dname, dtype = distro_name_from_torrent(probe)
    if dname:
        # 官方种子列表/标准 .torrent 文件名 → 用推断出的发行版名(更准确)
        distro, typ = dname, dtype
    elif not distro:
        # 推断失败且前端没给 distro → 用文件名主体兜底, 归入 linux 类型
        base = probe.rsplit(".", 1)[0] if "." in probe else probe
        distro = base
    if distro:
        # H2 修复: 路径穿越防护, 只有通过白名单校验才允许指向该保存路径
        target = _safe_join(typ, distro)
        if target is not None:
            target.mkdir(parents=True, exist_ok=True)
            save_path = str(target)
        else:
            # distro/typ 非法(含 ../ 等): 回退到受控的 _torrents 目录, 不信任用户输入
            fallback = DATA_DIR / "_torrents"
            fallback.mkdir(parents=True, exist_ok=True)
            save_path = str(fallback)
            log(f"[种子] 拒绝非法保存路径 {typ}/{distro}, 回退到 {save_path}")
    else:
        fallback = DATA_DIR / "_torrents"
        fallback.mkdir(parents=True, exist_ok=True)
        save_path = str(fallback)
    try:
        qb = _qb()
        r = qb.add_torrent(urls, save_path=save_path, category=body.get("category") or "iso-hub")
        if not r.get("ok"):
            return jsonify({"error": f"qBittorrent 添加失败: {r.get('error')}"}), 502
        log(f"[种子] 添加 {len(urls)} 个下载, 保存到 {save_path}")
        return jsonify({"ok": True, "save_path": save_path, "distro": distro, "type": typ})
    except Exception as e:  # noqa: BLE001
        log(f"[种子] 添加失败: {e!r}")
        return jsonify({"error": f"添加失败: {e}"}), 500


@app.post("/api/torrent/delete")
def api_torrent_delete():
    """从 qBittorrent 删除种子。body: {hashes:[...], delete_files?:bool}"""
    ok, err = _ensure_qb_enabled()
    if not ok:
        return err
    if not TORRENT_AVAILABLE:
        return jsonify({"error": "种子模块未加载"}), 500
    body = request.get_json(force=True, silent=True) or {}
    hashes = [str(h) for h in (body.get("hashes") or []) if h]
    if not hashes:
        return jsonify({"error": "未提供种子 hash"}), 400
    delete_files = bool(body.get("delete_files"))
    try:
        qb = _qb()
        ok = qb.delete_torrents(hashes, delete_files)
        log(f"[种子] 删除 {len(hashes)} 个, 同时删文件={delete_files}")
        return jsonify({"ok": ok})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"删除失败: {e}"}), 500


@app.post("/api/torrent/rss/add")
def api_torrent_rss_add():
    ok, err = _ensure_qb_enabled()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    if not _is_http_url(url):
        return jsonify({"error": "RSS 地址必须是有效的 http/https 链接"}), 400
    try:
        lst = dtorrents.add_user_rss(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    log(f"[种子] 添加 RSS 源: {url}")
    return jsonify({"ok": True, "user_rss": lst})


@app.post("/api/torrent/rss/remove")
def api_torrent_rss_remove():
    ok, err = _ensure_qb_enabled()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    lst = dtorrents.remove_user_rss(url)
    log(f"[种子] 移除 RSS 源: {url}")
    return jsonify({"ok": True, "user_rss": lst})


@app.post("/api/torrent/link/add")
def api_torrent_link_add():
    ok, err = _ensure_qb_enabled()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    if url.startswith("magnet:"):
        pass
    elif not _is_http_url(url):
        return jsonify({"error": "链接必须是有效的 http/https 磁力链接"}), 400
    try:
        lst = dtorrents.add_user_link(url, (body.get("distro") or ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    log(f"[种子] 添加手动链接: {url}")
    return jsonify({"ok": True, "user_links": lst})


@app.post("/api/torrent/link/remove")
def api_torrent_link_remove():
    ok, err = _ensure_qb_enabled()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    lst = dtorrents.remove_user_link(url)
    log(f"[种子] 移除手动链接: {url}")
    return jsonify({"ok": True, "user_links": lst})


if __name__ == "__main__":
    log(f"ISO Hub 启动  |  清单: {JSON_FILE}  数据目录: {DATA_DIR}")
    _sessions_load()  # 从磁盘恢复持久化会话(容器重建后 token 仍有效)
    seed_admin()  # 若设置 ISO_HUB_ADMIN_USER/PASS 则播种管理员
    # 启动收敛: compose 并发拉起 sidecar 存在时序竞态, 用后台线程重试直到状态一致,
    # 替代旧的一次性快照(_sync_disabled_shares/_sync_disabled_qb 仍保留供手动/测试调用)
    _bootstrap_webdav_conf()  # 首次部署时 ./webdav-config 为空, 按当前设置生成 webdav.yml
    start_sidecar_convergence()
    schedule_auto_sync()  # 订阅自动同步调度器(默认每天; ISO_HUB_SYNC_INTERVAL 可改秒数)
    if os.environ.get("ISO_HUB_DEV"):
        # 即使开发模式也不开启 debug, 避免 Werkzeug 调试器暴露任意代码执行
        app.run(host=HOST, port=PORT, debug=False)
    else:
        from waitress import serve
        print(f"serving on http://{HOST}:{PORT}")
        serve(app, host=HOST, port=PORT, threads=8)
