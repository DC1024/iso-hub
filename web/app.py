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
  * GET/POST /api/notify  邮件通知配置(下载完成后发SMTP通知; 密码不回传给前端)
  * POST /api/notify/test 发一封测试邮件(不落盘)

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

from flask import Flask, jsonify, request, send_file, send_from_directory

try:  # 种子下载集成(qBittorrent + DistroWatch 源) —— 可选加载
    from torrent_client import QBClient, qb_config, distro_name_from_torrent  # noqa: PLC0415
    import distro_torrents as dtorrents  # noqa: PLC0415
    TORRENT_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    TORRENT_AVAILABLE = False
    _torrent_import_err = str(e)

# 种子分类: 纯计算模块, 无外部依赖, 因此**不用**可选加载 —— 加载失败属于部署损坏,
# 应当直接暴露而不是静默降级(分类是纯展示功能, 不该被 qBittorrent 可用性绑架)。
import torrent_categories as tcats  # noqa: E402

# settings.json 的共享读写通道(加锁 + 原子写 + 损坏隔离)。
# 放在独立模块而非本文件: distro_torrents.py 是同一个 settings.json 的第二个写方,
# 它必须能 import 到同一把锁 —— 放在 app.py 会形成循环 import。
import config_files  # noqa: E402

# 下载完成后的邮件通知(纯模块: 不读盘不写盘, 配置由本文件读出来传进去)。
# 单向依赖 —— notifier 绝不 import app, 否则循环。
import notifier  # noqa: E402

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
# ISO 存放模式(用户可切换):
#   classified — 按 <类型>/<发行版>/ 分类存放(默认, 便于按发行版浏览/清理)
#   flat       — 全部 ISO 平铺进同一个目录 DATA_DIR/iso, 便于一次性拷走
# 系统自身文件(settings.json / distributions.json / download_failures.json 等)始终在
# DATA_DIR 根目录, 不会落进 ISO 目录 —— 因此 flat 模式下该目录里只有 ISO 文件。
STORAGE_MODES = ("classified", "flat")
FLAT_ISO_DIRNAME = "iso"
# 「下载到本机」票据: 会话 token 走 X-Auth-Token 请求头, 而浏览器顶层导航(点链接下载)
# 带不上自定义头 —— 所以下发文件改用**短时票据**自证身份(见 api_files_ticket)。
# 票据只对应单个文件、限时有效, 且不进浏览器历史里的长效凭据。
DL_TICKET_TTL = int(os.environ.get("ISO_HUB_DL_TICKET_TTL", "600"))  # 秒
DL_TICKET_MAX = 40      # 单次请求最多签发几个(防滥用)
_dl_tickets: dict = {}  # ticket -> (typ, name, fname, expire_ts)


def _is_http_url(url: str) -> bool:
    """仅允许 http/https 协议, 阻断 file://、ftp://、gopher:// 等 SSRF 向量。"""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.netloc != ""
    except Exception:  # noqa: BLE001
        return False


def _flat_iso_dir() -> Path:
    """平铺模式下的统一 ISO 目录(DATA_DIR/iso)。

    该目录只承载 ISO 文件本身: settings.json / distributions.json /
    download_failures.json 等系统文件都在 DATA_DIR 根目录, 不会落进来。
    """
    return (DATA_DIR / FLAT_ISO_DIRNAME).resolve()


def _torrent_fallback_dir() -> Path:
    """种子无法归属到发行版时的落点。

    flat 模式下与镜像下载共用同一个统一目录(DATA_DIR/iso), 让用户在一个目录里
    看到全部 ISO; classified 模式下沿用受控的 DATA_DIR/_torrents/。
    """
    return _flat_iso_dir() if load_storage_mode() == "flat" else (DATA_DIR / "_torrents")


def _safe_join(typ: str, name: str) -> Path | None:
    """把 (type, name) 安全解析为该发行版 ISO 的**存放目录**, 拒绝路径穿越/非法字符。

    校验规则(两种存放模式完全一致, 与存放位置无关):
      * typ 必须在 {linux,bsd,windows,macos} 白名单内
      * typ/name 均不得为空、不得含 / 或 \\、不得为 . 或 ..
      * classified 下 resolve 后仍必须位于 DATA_DIR 内(最终兜底)

    返回目录随 `load_storage_mode()` 变化:
      * classified(默认) -> DATA_DIR/<type>/<name>/
      * flat             -> DATA_DIR/iso/  (全发行版平铺同一目录)

    校验顺序刻意放在模式判断**之前**: 非法 (type, name) 在任何模式下都返回 None,
    避免"切到 flat 就让穿越输入蒙混过关"。
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
    if load_storage_mode() == "flat":
        return _flat_iso_dir()
    target = (DATA_DIR / typ / name).resolve()
    try:
        target.relative_to(DATA_DIR.resolve())
    except ValueError:
        return None
    return target

# import 副作用加固(P0 环境守卫): 目录建不出来不得炸掉 import。
# CI 实证: GitHub runner 的 / 不可写, 此处致命会让 7 个测试模块连 import 都过不去;
# 本地 Windows 则会悄悄在盘根建 C:\data, 掩盖环境问题。失败推迟到真正写盘时暴露。
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError as _mkdir_err:  # noqa: BLE001
    print(f"⚠ 数据目录不可用({DATA_DIR}): {_mkdir_err} —— 推迟到实际写盘时再报错",
          flush=True)
if not JSON_FILE.exists() and DEFAULT_JSON.exists():
    import shutil
    try:
        shutil.copyfile(DEFAULT_JSON, JSON_FILE)
    except OSError as _copy_err:  # noqa: BLE001
        print(f"⚠ 初始配置拷贝失败: {_copy_err}", flush=True)

# P1-⑤b: GPG 验证状态账本(同目录模块, /api/health 暴露 never_invoked)
sys.path.insert(0, str(BASE_DIR))
import gpg_ledger  # noqa: E402


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
    if load_storage_mode() == "flat":
        return _flat_disk_inventory()
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


def _flat_disk_inventory() -> dict:
    """平铺模式的磁盘清单: 扫描统一的 ISO 目录, 按**清单文件名**归属到 (type, name)。

    平铺目录里没有类型/发行版子目录, 归属信息只能来自清单本身 ——
    文件名 -> (type, name) 的映射由 distributions.json(含用户自定义源)建立。
    目录里清单没有的文件(用户手放 / 种子下载 / 已被淘汰的旧版)在平铺模式下无法
    归属, 直接忽略: 它们仍可经「下载到本机」的相对路径方式取回, 只是不会出现在
    发行版分组里, 也不会被「清理过期」误删。
    """
    inv = {}
    iso_dir = _flat_iso_dir()
    if not iso_dir.is_dir():
        return inv
    fname_key: dict = {}
    try:
        for e in load_json().get("distributions", []):
            url = e.get("download_url", "") or ""
            fn = url.rstrip("/").rsplit("/", 1)[-1]
            if fn:
                fname_key.setdefault(fn, (e.get("type", "linux"),
                                          e.get("distribution", "?")))
    except Exception as e:  # noqa: BLE001  清单不可读时宁可不归属, 也不报错
        log(f"[WARN] 平铺模式建立文件名归属失败: {e}")
    for f in iso_dir.iterdir():
        if not f.is_file():
            continue
        partial = _partial_base_name(f.name)
        base = partial or f.name
        key = fname_key.get(base)
        if key is None:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        rec = {"name": base, "size": st.st_size, "mtime": st.st_mtime,
               "partial": bool(partial)}
        if partial:
            rec["partial_name"] = f.name
        inv.setdefault(key, []).append(rec)
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
    # 运行中的任务正写这个 .part → 下载中(而非"下载停止")。
    # 路径必须走 _safe_join 跟随存放模式: flat 模式下 runner 上报的是
    # <DATA_DIR>/iso/xxx.iso.part, 这里若仍按 <type>/<name>/ 拼, 就永远匹配不上
    # active_paths, 正在下载的文件会被误报成「下载停止」(与 v1.3.1 同源的坑)。
    _dir = _safe_join(key[0], key[1])
    part_rel = str(_dir / (fname + PART_SUFFIX)) if _dir else ""
    if part_rel and part_rel in active_paths:
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
        # 「下载到本机」只对**完整**文件开放: 最终名只在下载器校验通过原子改名后才存在,
        # 半成品名字是 .part, 不会被 inventory 收进这里的 candidates。
        # 由后端下发这个布尔值(而不是让前端从 local_size/status 猜), 与 fold_key 的约
        # 定一致: 事实由后端算, 前端只渲染。
        downloadable = any(not f.get("partial") for f in candidates)
        groups[key]["entries"].append(
            {
                "distribution": name,
                "type": typ,
                "filename": fname,
                "rel": f"{typ}/{name}/{fname}",
                "downloadable": downloadable,
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


def _redact_password(d: dict) -> dict:
    """脱敏: 密码永不回传给前端, 只用一个布尔告诉 UI「有没有配过」。

    与邮件通知(notifier.redact)同一套约定 —— 前端密码框留空 = 沿用已保存的那份,
    因此后端必须把「已配过但不回传」这个状态说清楚, 否则用户会以为密码丢了。
    """
    safe = dict(d or {})
    had = bool(str(safe.pop("password", "") or ""))
    safe["password_set"] = had
    return safe


def load_shares() -> dict:
    """读取共享设置, 缺失键回退环境变量默认。

    v1.3.16: 解析失败不再用裸 except 静默吞掉 —— 统一走 `load_settings_all()`,
    它会写日志说明文件被判为损坏、以及应当去看 `.corrupt` 备份。
    """
    data = load_settings_all()
    out = {}
    for k, dft in DEFAULT_SHARES.items():
        s = dict(dft)
        s.update({kk: vv for kk, vv in data.get(k, {}).items()})
        out[k] = s
    return out


def save_shares(shares: dict) -> None:
    """写回共享设置, 保留 protected 等其它顶层键。

    v1.3.16: 走 `config_files.update_json` —— 在同一把 json 锁里完成
    读 → 合并 → 原子写。旧实现是无锁的 read + write_text, 两条写线程交错时
    后写的整份覆盖先写的, 丢掉一次更新且**没有任何报错**(典型受害者:
    改共享密码与加保护项几乎同时点保存)。
    """
    config_files.update_json(SETTINGS_JSON, shares,
                             on_quarantine=_settings_quarantined)


def load_qb_settings() -> dict:
    """读取 qBittorrent 设置, 缺失键回退环境变量默认。

    v1.3.16: 解析失败不再静默返回默认值, 原因走 `load_settings_all()` 写日志。
    """
    data = load_settings_all()
    out = dict(DEFAULT_QB)
    out.update({k: v for k, v in data.get("qb", {}).items() if k in out})
    return out


def _qb_is_external(qb: dict) -> bool:
    """判断 qBittorrent 是否指向用户自行部署的外部实例(非 iso-hub 配套 sidecar)。

    判断依据: url 被用户显式保存过(url_saved), 且指向非默认内部地址
    (http://qbittorrent:8080)。外部 QB 由用户自己管理, iso-hub 无法改其
    用户名/密码, 只能用它来登录连接 —— 改凭据时只需保存, 不得写 sidecar conf 或重启。
    """
    _url = (qb.get("url") or "").rstrip("/")
    _saved = bool(qb.get("url_saved", False))
    return _saved and (_url != (DEFAULT_QB.get("url") or "").rstrip("/"))


def _probe_qb_connection(qb: dict, timeout: float = 3.0) -> dict:
    """探测**外部** qBittorrent 的真实连接/登录状态, 供面板显示。

    外部 QB 是用户自行部署的容器, iso-hub 没有它的 Docker 状态可查(查了也必然是
    not_deployed/unknown, 会误导成"请检查 socket-proxy")。面向用户的正确指标是
    "能不能连上并登录", 所以这里直接调它的 Web API 试一次登录。

    返回 {state, detail}, state 取值:
      connected   已连上且登录成功
      bad_auth    地址可达, 但用户名/密码不对
      unreachable 地址/端口不可达, 或 HTTP 异常
      unknown     未启用或未配置完整(地址/用户名/密码), 不做探测
    """
    url = (qb.get("url") or "").rstrip("/")
    user = (qb.get("username") or "").strip()
    pwd = qb.get("password") or ""
    if not qb.get("enabled", False):
        return {"state": "unknown", "detail": "未启用"}
    if not url or not user or not pwd:
        return {"state": "unknown", "detail": "地址或凭据未填写完整"}
    try:
        cli = QBClient(url, user, pwd)
        code, text = cli._request("POST", "/api/v2/auth/login",
                                  {"username": user, "password": pwd}, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"state": "unreachable", "detail": f"{type(e).__name__}: {e}"}
    # _request 内部网络异常返回 code=0(且 text 形如 "连接失败: ...")
    if code == 0:
        return {"state": "unreachable", "detail": str(text)[:200]}
    if code in (401, 403):
        return {"state": "bad_auth", "detail": f"HTTP {code}"}
    if code in (200, 204):
        t = (text or "").strip().lower() if isinstance(text, str) else ""
        if any(bad in t for bad in ("fails.", "forbidden", "unauthorized", "invalid")):
            return {"state": "bad_auth", "detail": str(text)[:200]}
        return {"state": "connected", "detail": "已登录"}
    return {"state": "unreachable", "detail": f"HTTP {code}"}


def save_qb_settings(qb: dict) -> None:
    """写回 qBittorrent 设置, 保留其它顶层键。

    v1.3.16: 与 save_shares 同样走 `config_files.update_json`(加锁 + 原子写)。
    """
    config_files.update_json(SETTINGS_JSON, {"qb": qb},
                             on_quarantine=_settings_quarantined)


# ---------- 通用 settings.json 读写 (保护名单与共享并存, 不互相覆盖) ----------
# v1.3.16 起, 这一组读写的三项保证由 web/config_files.py 提供(详见该模块文档):
#   * **独立的** json 锁 —— _lock 是保护内存状态的, 包进来会重演历史上那次死锁
#   * 先写临时文件再 os.replace, 不再 write_text 裸写(写一半被打断会留残缺文件)
#   * 内容损坏时被隔离到 .corrupt 并写日志, 不再静默当成"空配置"
def _settings_quarantined(backup) -> None:
    """settings.json 损坏并被隔离后的告警。

    由 update_json 在**释放 json 锁之后**回调 —— 这里可以安全调用 log()(它要 _lock),
    反过来若在锁内调就会与"持 _lock 再进 save_*"的调用链形成环路死锁。
    """
    log("[settings] %s 不是合法 JSON 对象, 原文件已备份到 %s;"
        " 本次以空配置写回。请检查其中的凭据/用户/会话/保护列表是否需要恢复。"
        % (SETTINGS_JSON.name, backup))


def load_settings_all() -> dict:
    """读取整个 settings.json, 缺失返回 {}。

    v1.3.16: 内容损坏时**不再静默当没看见**。读取故意**不**挪动文件(并发读会打架),
    而是写一条日志说明处境: 下一次任何保存会把原件备份成 settings.json.corrupt 再重写。
    """
    try:
        return config_files.read_json_raw(SETTINGS_JSON)
    except config_files.CorruptJsonFile:
        log("[settings] %s 不是合法 JSON 对象, 已按空配置处理;"
            " 下一次保存会将原件备份为 %s.corrupt。请检查该文件。"
            % (SETTINGS_JSON.name, SETTINGS_JSON.name))
        return {}
    except Exception as e:  # noqa: BLE001  读盘失败/权限/解码异常: 保持不外抛
        log("[settings] 读取 %s 失败: %s" % (SETTINGS_JSON.name, e))
        return {}


def save_settings_all(data: dict) -> None:
    """写整个 settings.json(合并已存在键)。

    v1.3.16 之前这里的 docstring 自称"原子写", 实际是 read_text → 内存合并 →
    write_text 三步裸操作, 两个问题都很实在:
      ① **非原子**: 写一半被中断(进程被 kill / 磁盘满)会留下残缺文件, 之后每次
         读取都只能靠 except 兜底 —— 这正是一条条"静默返回 {}"的来由。
      ② **无锁**: waitress 8 线程 + 1 个调度线程共享这一个文件, 两条写线程交错时
         后写的整份覆盖先写的, 丢掉一次更新且毫无报错。
    现在统一由 `config_files.update_json` 在同一把 json 锁内完成三步, 并先写
    临时文件再 os.replace。

    锁的范围只有"读 → 合并 → 写"这段纯磁盘 IO。**绝不**把容器操作(改共享密码要去
    stop → rm → create → start samba, 最长约 50 秒)包进来, 否则整个设置页会被串行化。
    """
    config_files.update_json(SETTINGS_JSON, data,
                             on_quarantine=_settings_quarantined)


def load_source_strategy() -> str:
    """读取全局选源策略(A 固定优先级 / B 实测选最快)。默认 A, 可被环境变量覆盖。"""
    return (load_settings_all().get("source_strategy") or
            os.environ.get("ISO_HUB_SOURCE_STRATEGY", "A")).upper()


def save_source_strategy(s: str) -> None:
    save_settings_all({"source_strategy": s.upper()})


def load_storage_mode() -> str:
    """读取 ISO 存放模式: `classified`(按类型/发行版分类, 默认) 或 `flat`(统一单目录)。

    与选源策略同款的回退链: settings.json -> 环境变量(ISO_HUB_STORAGE_MODE) -> 默认。
    任何非法/缺失值都收敛为 classified, 保证调用方永远拿到两个合法值之一。
    """
    raw = (load_settings_all().get("storage_mode") or
           os.environ.get("ISO_HUB_STORAGE_MODE", "classified"))
    mode = str(raw).strip().lower()
    return mode if mode in STORAGE_MODES else "classified"


def save_storage_mode(mode: str) -> None:
    """写回存放模式(只接受两个合法值 —— 调用方已校验, 这里再兜一层)。"""
    m = str(mode).strip().lower()
    if m not in STORAGE_MODES:
        m = "classified"
    save_settings_all({"storage_mode": m})


# --------------------------------------------------------------------------- 邮件通知配置
# 存 settings.json 的 notify 段, 读写一律走 load_settings_all / save_settings_all
# (= config_files.update_json), 不在这里另开写通道。
NOTIFY_KEY = "notify"
# 端口默认给 465 —— 2026-09-12 在两台实例上实测: Lighthouse(宿主+容器)的 25 端口
# 出站被云厂商封死, 465/587 通; Z4Pro 家宽三个都通。25 一律不推荐。
NOTIFY_FALLBACK = {
    "hard": "下载失败（不可续传）",
    "stopped": "下载停止（半成品保留，可续传）",
}


def load_notify_config() -> dict:
    """读出并归一化邮件通知配置(脏数据/半截配置一律收敛成合法 dict)。"""
    return notifier.normalize(load_settings_all().get(NOTIFY_KEY) or {})


def save_notify_config(cfg: dict) -> dict:
    """写回邮件通知配置, 返回落盘后的规范化结果。"""
    full = notifier.normalize(cfg)
    save_settings_all({NOTIFY_KEY: full})
    return full


def notify_cfg_from_body(body: dict, base: dict = None) -> dict:
    """把前端提交的局部字段叠加到现有配置上。

    密码单独处理: 提交空串表示「不改」(UI 上密码框留空 = 沿用已保存的), 否则会出现
    「只想改收件人结果把密码清了」这种事故。
    """
    merged = dict(base if base is not None else load_notify_config())
    patch = {k: v for k, v in body.items() if k in notifier.DEFAULTS}
    if not str(patch.get("password") or ""):
        patch.pop("password", None)
    return notifier.merge(merged, patch)


def _notify_entries(snapshot: dict) -> list:
    """任务快照 -> 邮件明细 [{filename, size, failed, reason}]。

    失败判定**不看**进程退出码: iso_runner 是「尽力而为」地跑完所有条目的, 单个文件
    失败时整体退出码仍然可能是 0。真正的账本是 `download_failures.json`
    (`write_fail_record` 写的), 这里以它为准, 并按文件名兜一层 —— 账本的 key 是
    runner 侧拼的相对路径, 与 `task["downloads"]` 里的绝对路径不一定同形。
    """
    failures = load_failures() or {}
    entries = []
    for d in snapshot.get("downloads") or []:
        p = Path(str(d.get("path") or ""))
        if p.name.endswith(PART_SUFFIX):
            p = p.with_name(p.name[: -len(PART_SUFFIX)])
        name = str(d.get("filename") or p.name)
        rec = failures.get(str(p)) or failures.get(name)
        if not rec:
            for k, v in failures.items():
                if str(k) == p.name or str(k).endswith("/" + p.name):
                    rec = v
                    break
        size = 0
        try:
            size = p.stat().st_size if p.exists() else 0
        except OSError:
            size = 0
        if rec:
            entries.append({"filename": name, "size": size, "failed": True,
                            "reason": NOTIFY_FALLBACK.get(str(rec.get("kind") or ""),
                                                          "下载未完成")})
        else:
            entries.append({"filename": name, "size": size, "failed": False,
                            "reason": ""})
    return entries


def notify_download_finished(snapshot: dict) -> None:
    """后台线程: 给一次「镜像列表下载」任务发结果邮件。

    这里**绝不抛异常** —— 邮件是附加功能, 发不出去最多记一行日志, 绝不能把任务线程
    拖死或改变已落地的下载结果。种子下载(kind=torrent)不走这里: qBittorrent 自带
    邮件通知, 再发一遍只是重复打扰。
    """
    try:
        if snapshot.get("kind") != "download" or snapshot.get("cancelled"):
            return
        cfg = load_notify_config()
        entries = _notify_entries(snapshot)
        if not notifier.should_notify(cfg, entries):
            return
        dur = None
        if snapshot.get("finished") and snapshot.get("started"):
            dur = snapshot["finished"] - snapshot["started"]
        subject, body = notifier.build_report(
            snapshot.get("title") or "下载任务", entries,
            snapshot.get("exit_code"), dur)
        ok, detail = notifier.send(cfg, subject, body)
        log("[邮件] " + detail if ok else "[邮件] 发送失败: " + detail)
    except Exception as e:  # noqa: BLE001
        log("[邮件] 通知异常: %r" % (e,))


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
    except Exception as e:  # noqa: BLE001
        # v1.3.16: 会话写盘失败会让"重启后仍保持登录"静默失效 —— 之前是裸 except: pass。
        # 这里不向上抛(登录流程不该因为持久化失败而 500), 但必须留下线索。
        try:
            log("[auth] 会话持久化失败(重启后将需要重新登录): %s" % e)
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

# ---- Docker 查询失败日志降噪 -------------------------------------------------
# 背景: 未挂载 docker.sock 且未设置 DOCKER_HOST 的部署里, 每一次容器状态查询都会抛
# FileNotFoundError。启动收敛线程会对 3 个 sidecar 重试 12 轮(间隔 5 秒), 设置页的
# /api/shares 与 /api/qb/settings 每次访问也各查一轮 —— 面板日志被同一条错误刷满,
# 真正有用的信息反而被淹没。
# 策略: 同一 (对象, 异常类型) 在 _DOCKER_ERR_TTL 内只输出首条, 之后静默并累计次数;
# 首条附带可操作提示, 让用户知道这是"未接 Docker"而不是"功能坏了"。
# 只影响日志输出, 不改变任何返回值语义(仍是 unknown / None), 故不影响既有行为。
_DOCKER_ERR_TTL = 300.0
_DOCKER_ERR_SEEN: dict[str, tuple[float, int]] = {}


def _log_docker_once(key: str, msg: str) -> None:
    """Docker 相关告警的降噪输出: 同 key 在 TTL 内只打首条, 其余静默并计数。"""
    now = time.time()
    with _lock:
        prev = _DOCKER_ERR_SEEN.get(key)
        if prev and now - prev[0] < _DOCKER_ERR_TTL:
            _DOCKER_ERR_SEEN[key] = (prev[0], prev[1] + 1)
            return
        _DOCKER_ERR_SEEN[key] = (now, 1)
    log(msg)


def _docker_failure_hint(err: BaseException) -> str:
    """按异常类型给出"该怎么办"的一句话提示(只附在首条日志上)。"""
    if isinstance(err, FileNotFoundError):
        return ("；iso-hub 未连接到 Docker, 面板将无法启停共享/种子容器。"
                "如需该功能, 请为 iso-hub 容器挂载 /var/run/docker.sock, "
                "或设置环境变量 DOCKER_HOST 指向 socket-proxy。")
    if isinstance(err, (ConnectionRefusedError, ConnectionResetError, TimeoutError)):
        return "；Docker 接口不可达, 请检查 socket-proxy 是否运行。"
    return ""


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
        _log_docker_once(f"state|{name}|{type(e).__name__}",
                         f"[docker] 查询容器 {name} 异常: {e!r}" + _docker_failure_hint(e))
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
        _log_docker_once(f"policy|{name}|{type(e).__name__}",
                         f"[docker] 查询容器 {name} 重启策略异常: {e!r}" + _docker_failure_hint(e))
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
        _log_docker_once(f"raw|{name}|{type(e).__name__}",
                         f"[docker] 查询容器 {name} 异常: {e!r}" + _docker_failure_hint(e))
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
    # 任务真正落地后(result 已写好)才发邮件。快照此刻的 task 字段 —— 紧接着用户可能
    # 立刻发起下一个任务, 直接拿 cur(全局 dict)会读到别人的数据。
    # 只认 download: 种子下载由 qBittorrent 自己通知, sync/meta 也不该发信打扰。
    if cur.get("kind") == "download" and not cur["cancelled"]:
        snap = {k: cur.get(k) for k in
                ("kind", "title", "downloads", "targets",
                 "started", "finished", "exit_code", "cancelled")}
        threading.Thread(target=notify_download_finished, args=(snap,),
                         daemon=True).start()


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
        "--storage-mode", load_storage_mode(),
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
    设置 ISO_HUB_REQUIRE_LOGIN 后, 除登录/自身状态接口外的所有 API 均需登录会话或 X-Auth-Token(强制登录)。

    例外: /api/files/get(下载到本机) 放行 —— 浏览器顶层导航带不上 X-Auth-Token,
    该路由**自带**短时票据鉴权(票据由 /api/files/ticket 在校验会话后签发),
    无票/过期票一律 403, 因此放行不等于开公网。
    """
    # 登录/登出/获取自身状态接口始终放行
    if request.path in ("/api/user/login", "/api/user/me", "/api/user/logout"):
        return None
    if REQUIRE_LOGIN:
        # 强制登录: 页面壳子/静态/健康检查放行(前端靠 /api/user/me 判断是否弹登录遮罩), 其余 API 一律需登录
        if request.method == "GET" and (request.path == "/" or request.path.startswith("/static/") or request.path == "/api/health" or request.path == "/api/files/get"):
            return None
        if request.headers.get("X-Auth-Token") == AUTH_TOKEN:
            return None
        if _valid_session():
            return None
        return jsonify({"error": "unauthorized"}), 401
    # 原逻辑: 仅设置 ISO_HUB_TOKEN 时拦截写操作 API
    if not AUTH_TOKEN:
        return None
    if request.method == "GET" and (request.path == "/" or request.path.startswith("/static/") or request.path == "/api/health" or request.path == "/api/files/get"):
        return None
    if request.headers.get("X-Auth-Token") == AUTH_TOKEN:
        return None
    # 兼容用户登录会话
    if _valid_session():
        return None
    return jsonify({"error": "unauthorized"}), 401


@app.after_request
def no_cache_api(resp):
    """所有 API 响应禁用缓存, 避免前端「刷新列表」拿到浏览器缓存的旧数据而无反应。

    例外: /api/files/get —— 下载大文件必须允许浏览器缓存分片, 否则 Range 续传失效。
    """
    if request.path.startswith("/api/") and request.path != "/api/files/get":
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
    # P1-⑤b: 暴露 GPG 状态账本 —— never_invoked 非空 = 有发行版从未获得验签机会
    return jsonify({"ok": True, "gpg_ledger": gpg_ledger.build_summary(JSON_FILE)})


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
        "--storage-mode", load_storage_mode(),
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


@app.get("/api/storage-mode")
def api_storage_mode_get():
    """读取 ISO 存放模式。flat 模式下附带统一目录的绝对路径, 供 UI 提示用户去哪里找文件。"""
    mode = load_storage_mode()
    return jsonify({"mode": mode,
                    "dir": str(_flat_iso_dir()) if mode == "flat" else ""})


@app.post("/api/storage-mode")
def api_storage_mode_set():
    """切换 ISO 存放模式。只影响**之后**的新下载, 已有文件不会迁移。"""
    body = request.get_json(force=True, silent=True) or {}
    mode = str(body.get("mode") or "").strip().lower()
    if mode not in STORAGE_MODES:
        return jsonify({"error": f"mode 只能是 {' 或 '.join(STORAGE_MODES)}"}), 400
    save_storage_mode(mode)
    log(f"[设置] ISO 存放模式改为 {mode}"
        + ("(统一目录 %s, 仅对新下载生效)" % _flat_iso_dir() if mode == "flat" else
           "(按类型/发行版分类, 仅对新下载生效)"))
    return jsonify({"ok": True, "mode": mode})


# --------------------------------------------------------------------------- 邮件通知
@app.get("/api/notify")
def api_notify_get():
    """读取邮件通知配置。**密码不回传**, 只用 password_set 告诉 UI 有没有配过。"""
    cfg = load_notify_config()
    return jsonify({"config": notifier.redact(cfg),
                    "ready": notifier.is_ready(cfg),
                    "missing": notifier.missing_fields(cfg)})


@app.post("/api/notify")
def api_notify_set():
    """保存邮件通知配置。局部提交即可(只传要改的字段), 密码留空表示不改。"""
    body = request.get_json(force=True, silent=True) or {}
    cfg = save_notify_config(notify_cfg_from_body(body))
    log(f"[设置] 邮件通知已更新(启用={cfg['enabled']}, "
        f"服务器={cfg['smtp_host']}:{cfg['smtp_port']}/{cfg['security']})")
    return jsonify({"ok": True, "config": notifier.redact(cfg),
                    "ready": notifier.is_ready(cfg),
                    "missing": notifier.missing_fields(cfg)})


@app.post("/api/notify/test")
def api_notify_test():
    """发一封测试邮件(**不落盘**)——先验证凭据能不能通, 再决定要不要保存。

    正文用表单里当前的值; 密码留空时沿用已保存的那份。
    """
    body = request.get_json(force=True, silent=True) or {}
    submitted = body.get("config") if isinstance(body.get("config"), dict) else body
    cfg = notify_cfg_from_body(submitted)
    subject, text = notifier.build_test_mail(
        "%s:%s (%s)" % (cfg["smtp_host"], cfg["smtp_port"], cfg["security"]))
    ok, detail = notifier.send(cfg, subject, text)
    log("[邮件] 测试邮件已发送" if ok else "[邮件] 测试邮件发送失败: " + detail)
    return (jsonify({"ok": ok, "detail": detail}), 200 if ok else 400)


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


# --------------------------------------------------------------------------- 下载到本机
def _resolve_local_file(typ: str, name: str, fname: str) -> Path | None:
    """把 (type, distribution, filename) 解析为磁盘上的**完整**文件路径, 非法则 None。

    与删除不同, 这里**不要求文件属于当前清单** —— 种子下载/用户手动放入的 ISO
    同样应该能拉回本机(它们本就在同一批目录里)。因此安全边界只依赖路径约束:

      * typ 白名单 + 无分隔符 (_safe_join)
      * fname 非空、不含 / 或 \\、不为 . 或 ..
      * fname 不得是半成品名(.part/.aria2/.!qB/.tmp) —— 那是未完成的字节,
        而"最终名"只在下载器校验通过原子改名后才出现
      * resolve() 后必须仍在目标目录内 —— 兜住 symlink 指向外部的场景
    """
    if not fname or "/" in fname or "\\" in fname or fname in (".", ".."):
        return None
    if _partial_base_name(fname):
        return None
    target = _safe_join(typ, name)
    if target is None:
        return None
    fp = target / fname
    try:
        fp.resolve().relative_to(target.resolve())
    except ValueError:
        return None
    return fp if fp.is_file() else None


def _resolve_rel_path(rel: str) -> Path | None:
    """把 DATA_DIR 内的相对路径解析为真实文件, 非法/越界/半成品则 None。

    用于「下载到本机」的第二种寻址: 种子/手动放入的文件不一定落在
    <type>/<distro>/ 的 catalog 结构里(如 /data/_torrents/), 只能靠相对路径
    定位。安全边界与 _resolve_local_file 一致: 必须仍在 DATA_DIR 内、不得是
    半成品、不得用 .. 越界。
    """
    if not rel:
        return None
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or rel.startswith("\\") or rel in (".", ".."):
        return None
    if any(part in (".", "..") for part in rel.split("/")):
        return None
    fp = (DATA_DIR / rel).resolve()
    try:
        fp.relative_to(DATA_DIR.resolve())
    except ValueError:
        return None
    if not fp.is_file():
        return None
    if _partial_base_name(fp.name):
        return None
    return fp


def _resolve_dl_target(typ: str, name: str, fname: str) -> Path | None:
    """「下载到本机」统一寻址: 三种形态走不同解析器。

    * typ == "_rel": fname 是 DATA_DIR 内的相对路径(种子/任意文件)
    * typ == "_abs": fname 是容器内绝对路径, 且**必须**是 qB 当前报告为已完成的
                     种子文件(name=种子 hash)。客户端无法凭空伪造 —— 伪造路径
                     过不了 qB 复核, 避免退化成任意文件读。
    * 否则:          (type, distribution, filename) catalog 三元组(走 _safe_join)
    """
    if typ == "_rel":
        return _resolve_rel_path(fname)
    if typ == "_abs":
        return _resolve_abs_qb(name, fname)
    return _resolve_local_file(typ, name, fname)


def _resolve_abs_qb(h: str, path: str) -> Path | None:
    """_abs 寻址复核: 路径必须与 qB 此刻报告的已完成文件**逐字符串相等**。

    外部 qB 的下载目录不在 DATA_DIR 内(典型: 独立部署的 qB 写 /downloads),
    只要把该目录挂载进 iso-hub 容器(路径与 qB 的 save_path 一致), 就能下载。
    若路径在容器内不可见或 qB 未报告, 一律拒绝。
    """
    if not h or not path:
        return None
    p = Path(path)
    if not p.is_file() or _partial_base_name(p.name):
        return None
    try:
        entries, _sk = _torrent_completed_files([h])
    except Exception:  # noqa: BLE001
        return None
    for kind, v in entries:
        if kind == "abs" and v == str(p):
            return p
    return None


def _torrent_completed_files(hashes: list) -> tuple[list, list]:
    """给定种子 hash 列表, 返回 (可下载条目列表, 跳过说明列表)。

    条目是 (kind, value) 二元组:
      * ("rel", 相对路径) —— 文件在 DATA_DIR 内, 走 _rel 票据;
      * ("abs", 绝对路径) —— 文件在 DATA_DIR 外但容器内真实可见(外部 qB 把
        下载目录挂进容器的形态), 走 _abs 票据(下载时还会再过一次 qB 复核)。
    只挑**已完成**的文件(progress>=1 或 is_seed), 非半成品。
    依赖 qBittorrent sidecar; 未启用/不可用时返回空(由调用方决定如何提示)。
    """
    entries, skipped = [], []
    ok, _err = _ensure_qb_enabled()
    if not ok or not TORRENT_AVAILABLE:
        return entries, skipped
    try:
        qb = _qb()
        toks = qb.list_torrents() or []
        by_hash = {str(t.get("hash", "")).lower(): t for t in toks}
        for h in hashes:
            h = str(h or "").strip().lower()
            t = by_hash.get(h)
            if not t:
                skipped.append(f"{h or '(空)'}: 种子不存在或 qBittorrent 未连接")
                continue
            save_path = (t.get("save_path") or "").rstrip("/")
            if not save_path:
                skipped.append(f"{t.get('name', h)}: 未知保存路径")
                continue
            try:
                files = qb.torrent_files(h) or []
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{t.get('name', h)}: 列举文件失败: {e}")
                continue
            found = 0
            hidden = 0
            for f in files:
                prog = float(f.get("progress") or 0)
                if prog < 1.0 and not f.get("is_seed"):
                    continue  # 未完成: 不提供下载
                fp = (Path(save_path) / (f.get("name") or "")).resolve()
                try:
                    rel = fp.relative_to(DATA_DIR.resolve())
                except ValueError:
                    # DATA_DIR 外(外部 qB 的典型形态): 容器内真实可见才提供,
                    # 否则计入 hidden, 给出可行动的提示而不是笼统的"没有文件"
                    if fp.is_file() and not _partial_base_name(fp.name):
                        entries.append(("abs", str(fp)))
                        found += 1
                    else:
                        hidden += 1
                    continue
                if _resolve_rel_path(str(rel)):
                    entries.append(("rel", str(rel).replace("\\", "/")))
                    found += 1
                else:
                    hidden += 1
            if not found:
                skipped.append(
                    f"{t.get('name', h)}: "
                    + (f"{hidden} 个已完成文件不在 iso-hub 数据目录"
                       "(外部 qB 需把下载目录按原路径挂载进容器)"
                       if hidden else "没有已完成的可下载文件"))
    except Exception as e:  # noqa: BLE001
        log(f"[下载] 列举种子文件失败: {e!r}")
    return entries, skipped


def _issue_ticket_for(typ: str, name: str, fname: str) -> tuple:
    """解析并签发单张票据; 成功返回 (票据dict, None), 失败返回 (None, 跳过原因)。"""
    fp = _resolve_dl_target(typ, name, fname)
    if fp is None:
        return None, f"{fname or '(空文件名)'}: 文件不存在或参数非法"
    try:
        size = fp.stat().st_size
    except OSError as e:
        return None, f"{fname}: {e}"
    rel = str(fp.resolve().relative_to(DATA_DIR.resolve())).replace("\\", "/") if typ == "_rel" else None
    return {
        "url": "/api/files/get?t=" + _issue_dl_ticket(typ, name, fname),
        "filename": fp.name, "type": typ, "distribution": name,
        "size": size, "rel": rel,
    }, None


def _dl_tickets_purge() -> None:
    """清理过期票据。每次都清一遍 —— 票据量极小, 不需要定时器。"""
    now = time.time()
    for k in [k for k, v in _dl_tickets.items() if v[3] <= now]:
        _dl_tickets.pop(k, None)


def _issue_dl_ticket(typ: str, name: str, fname: str) -> str:
    """签发一个只对应单个文件的限时票据。"""
    _dl_tickets_purge()
    tok = secrets.token_urlsafe(24)
    _dl_tickets[tok] = (typ, name, fname, time.time() + DL_TICKET_TTL)
    return tok


@app.post("/api/files/ticket")
def api_files_ticket():
    """为「把服务器上已下载的文件拉到本机」签发短时下载票据。

    为什么不能直接把链接指到文件: 会话 token 走 X-Auth-Token **请求头**, 而浏览器
    点链接下载是顶层导航, 带不上自定义头 → 拿不到凭据。把会话 token 塞进 URL 更糟:
    它是 7 天有效的长效凭据, 会留在浏览器历史/代理日志里。所以用票据 ——
    随机、限时、只对一个文件有效, 泄露面最小。

    请求体: {"items": [{"type","distribution","filename"}, ...]}
    返回:   {"tickets": [{"url","filename","type","distribution","size"}], "skipped": [...]}
    """
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items") or []
    if not items:
        return jsonify({"error": "没有选择任何文件"}), 400
    if len(items) > DL_TICKET_MAX:
        return jsonify({"error": f"一次最多下载 {DL_TICKET_MAX} 个文件"}), 400

    tickets, skipped = [], []
    for it in items:
        # 形态 1: 种子下载(按 hash 枚举已完成文件, 后端走 qBittorrent)
        h = it.get("hash")
        if h:
            entries, hskip = _torrent_completed_files([h])
            skipped.extend(hskip)
            h = str(h).strip().lower()
            for kind, val in entries:
                # DATA_DIR 内的走 _rel; 目录外(外部 qB 挂载形态)的走 _abs,
                # _abs 票据在下载时还会再对照 qB 报告复核一次, 防伪路径
                tk, sk = (_issue_ticket_for("_abs", h, val) if kind == "abs"
                          else _issue_ticket_for("_rel", "", val))
                if tk is not None:
                    tickets.append(tk)
                else:
                    skipped.append(sk)
            continue
        # 形态 2: 相对路径(种子/手动放入的任意文件, 不依赖 catalog)
        rel = it.get("rel")
        if rel:
            tk, sk = _issue_ticket_for("_rel", "", str(rel))
            if tk is not None:
                tickets.append(tk)
            else:
                skipped.append(sk)
            continue
        # 形态 3: catalog 三元组(历史形态, 保持兼容)
        tk, sk = _issue_ticket_for(
            str(it.get("type") or "").strip(),
            str(it.get("distribution") or "").strip(),
            str(it.get("filename") or "").strip())
        if tk is not None:
            tickets.append(tk)
        else:
            skipped.append(sk)

    if tickets:
        user = _valid_session() or ("token" if AUTH_TOKEN else "匿名")
        log(f"[下载] {user} 请求下载 {len(tickets)} 个文件到本机")
    return jsonify({"ok": bool(tickets), "tickets": tickets, "skipped": skipped})


@app.get("/api/files/get")
def api_files_get():
    """凭票据下发文件(支持 Range, 因此浏览器可暂停/续传)。

    票据**可重复使用直到过期** —— 断点续传/连接重试会让浏览器对同一 URL 发多次
    请求(带 Range 头), 一次性票据会直接掐断续传。代价是票据在 TTL 内可重放,
    所以 TTL 默认只有 10 分钟。
    """
    tok = request.args.get("t", "")
    _dl_tickets_purge()
    hit = _dl_tickets.get(tok) if tok else None
    if not hit:
        return jsonify({"error": "下载链接无效或已过期, 请在面板上重新点击下载"}), 403
    typ, name, fname, _exp = hit
    # 二次校验: 票据里存的是 (type, distro, 文件名) 分量而不是路径, 必须重新过一遍约束
    fp = _resolve_dl_target(typ, name, fname)
    if fp is None:
        return jsonify({"error": f"文件不存在: {fname}"}), 404
    resp = send_file(fp, as_attachment=True, download_name=fname, conditional=True)
    # 大文件必须允许浏览器缓存分片, 否则续传会从头开始(no-store 是续传杀手)。
    # 该路径同时被 after_request 的 no-cache 规则排除, 见 no_cache_api。
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


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
        st = service_state(SHARE_CONTAINERS[proto])
        s["container"] = st
        # managed = 是否 iso-hub 配套部署的容器: 能检测到(非 not_deployed/unknown)即为配套,
        # 凭据可管理; 用户自行部署的外部容器 iso-hub 检测不到, 无法改凭据。
        s["managed"] = st not in ("not_deployed", "unknown")
    return jsonify({"shares": {k: _redact_password(v) for k, v in shares.items()}})


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
            _pw = str(p["password"]).strip()
            if _pw:  # 空 = 不改: 前端密码框留空即沿用已保存的那份
                cur["password"] = _pw
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
    return jsonify({"ok": True, "shares": {
        k: {**_redact_password(v), "container": (_st := service_state(SHARE_CONTAINERS[k])),
            "managed": _st not in ("not_deployed", "unknown")}
        for k, v in shares.items()}})


@app.get("/api/qb/settings")
def api_qb_settings_get():
    """获取 qBittorrent 设置及容器实时四态状态。"""
    qb = load_qb_settings()
    # 标记 url 是否被用户显式保存过(区别于默认的内部 sidecar 地址 http://qbittorrent:8080)。
    # 前端据此决定输入框是否回显 url —— 未自定义时留空, 由用户按占位提示自行填写。
    try:
        _raw = json.loads(SETTINGS_JSON.read_text(encoding="utf-8")) if SETTINGS_JSON.exists() else {}
        qb["url_saved"] = bool((_raw.get("qb") or {}).get("url"))
    except Exception:  # noqa: BLE001
        qb["url_saved"] = False
    # 区分「配套容器」与「外部容器」: 只要 url 未被用户指向外部(仍是默认内部 sidecar 地址,
    # 或从未自定义), iso-hub 就能管理该 qb 容器, 可改凭据并同步到 sidecar; 若用户填了外部
    # qBittorrent 地址, 那是自行部署的容器, iso-hub 无法改其用户名/密码 → managed=False。
    qb["managed"] = not _qb_is_external(qb)
    # 容器状态只在**配套** sidecar 场景才有意义。外部 QB 是用户自行部署的容器, iso-hub 没有
    # 它可查(查的是同名配套 sidecar, 必然 not_deployed/unknown): 既会污染日志(未接 Docker 的
    # 部署每次访问都抛 FileNotFoundError), 又会让界面误报"请检查 socket-proxy"。
    # 故外部 QB 不查询, 直接标记未部署; 界面看 conn 判断"能否连上并登录"。
    qb["container"] = service_state(QB_CONTAINER) if qb["managed"] else "not_deployed"
    # 外部 QB 真正要看的是"能否连上并登录这个外部实例" —— 直接探测一次并回传, 面板据此显示
    # 「已连接 / 凭据错误 / 无法连接」, 而不是误导性的"请检查 socket-proxy"。
    qb["conn"] = _probe_qb_connection(qb) if not qb["managed"] else None
    return jsonify({"ok": True, "qb": _redact_password(qb)})


@app.post("/api/qb/settings")
def api_qb_settings_post():
    """保存 qBittorrent 设置并启停/重启容器。body: {enabled?:bool, username?:str, password?:str}"""
    body = request.get_json(force=True, silent=True) or {}
    qb = load_qb_settings()
    changed = False
    cred_changed = False
    if "username" in body:
        qb["username"] = str(body["username"]).strip()
        changed = True
        cred_changed = True
    if "password" in body:
        _pw = str(body["password"]).strip()
        if _pw:  # 空 = 不改: 前端密码框留空即沿用已保存的那份
            qb["password"] = _pw
            changed = True
            cred_changed = True
    if "url" in body:
        # 允许用户指向外部 qBittorrent(如自行部署的实例), 而非默认的 sidecar 内部地址
        qb["url"] = str(body["url"]).strip().rstrip("/") or qb.get("url", "")
        changed = True

    # 启用或保持启用时，必须提供非空凭据
    will_be_enabled = bool(body["enabled"]) if "enabled" in body else qb.get("enabled", False)
    if will_be_enabled and (not qb.get("username") or not qb.get("password")):
        return jsonify({"error": "启用 qBittorrent 必须提供非空用户名和密码"}), 400

    # 计算是否指向外部 QB(用户自行部署, 非配套 sidecar)。url_saved 需从原始 settings 判断,
    # 与 GET 保持一致: 用户显式保存过 url 才视为已自定义; 若本次请求带 url 字段同样视为已保存。
    # 必须在 enabled 分支**之前**算好: 外部 QB 启停时不应去操作配套 sidecar 容器。
    try:
        _raw = json.loads(SETTINGS_JSON.read_text(encoding="utf-8")) if SETTINGS_JSON.exists() else {}
        qb["url_saved"] = bool((_raw.get("qb") or {}).get("url")) or ("url" in body)
    except Exception:  # noqa: BLE001
        qb["url_saved"] = bool("url" in body)
    _is_ext = _qb_is_external(qb)

    enabled_changed = False
    if "enabled" in body:
        want = bool(body["enabled"])
        if want != qb.get("enabled", False):
            # 外部 QB(用户自行部署): 根本没有 iso-hub 配套的 sidecar 容器可启停。
            # 这里的 enabled 只表示"是否用这个外部 QB 做种子下载", 只落盘开关状态即可,
            # 绝不能去 start/stop QB_CONTAINER —— 那个容器不存在(或不属于 iso-hub),
            # 否则必定失败并误报"容器状态未知(请检查 socket-proxy 是否运行)"。
            # 这与用户预期完全不符: 外部 QB 不需要 socket-proxy, iso-hub 只走 Web API 登录连接。
            if _is_ext:
                log(f"[qB] 外部 qBittorrent({qb.get('url')}) 开关 -> enabled={want}, 不操作配套 sidecar 容器")
            else:
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

    # 仅当"凭据"变更且当前是启用状态、且是配套 sidecar 时, 才同步密码到 sidecar 容器并重启。
    # 外部 QB(用户自行部署): 改凭据只是为了登录连接, 只保存到配置即可, 不写 conf 不重启
    # (根本没有 iso-hub 配套容器可重启)。
    if cred_changed and qb.get("enabled", False) and not _is_ext:
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

    # 与 GET 一致: 外部 QB 不查配套 sidecar 容器(查了必然 not_deployed/unknown, 只会污染
    # 日志并让界面误报"请检查 socket-proxy")。复用上面已算好的 _is_ext, 避免重复判断。
    qb["managed"] = not _is_ext
    qb["container"] = service_state(QB_CONTAINER) if not _is_ext else "not_deployed"
    return jsonify({"ok": True, "qb": _redact_password(qb)})


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


MAX_CLASSIFY_ITEMS = 5000        # 入参上限: 防超大数组打内存/撑爆返回体
_CLASSIFY_FIELDS = ("title", "url", "pubDate", "source", "builtin")


@app.post("/api/torrent/classify")
def api_torrent_classify():
    """把种子条目按分类分组返回。**纯计算**: 不抓外网、不落盘、不依赖 qBittorrent。

    刻意不做 _ensure_qb_enabled 检查: 分类只影响展示, 不该因为没部署
    qbittorrent sidecar 就用不了(/api/torrent/sources 的检查不要照抄到这里)。

    入参: {"items": [{"title","url","pubDate","source","builtin"}, ...]}
    出参: {"ok": true, "categories": [...], "counts": {...}}  (+ 可选 warnings)
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    items = body.get("items")
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "items 必须是数组"}), 400
    if len(items) > MAX_CLASSIFY_ITEMS:
        return jsonify({"ok": False,
                        "error": "items 过多(上限 %d 条)" % MAX_CLASSIFY_ITEMS}), 400

    # 字段白名单 + 类型校验: 只取认识的字段, 非字符串一律归一化, 缺 title 的条目丢弃
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = it.get("url") if isinstance(it.get("url"), str) else ""
        title = it.get("title") if isinstance(it.get("title"), str) else ""
        title = title.strip() or url.strip()
        if not title:
            continue
        clean.append({
            "title": title,
            "url": url,
            "pubDate": it.get("pubDate") if isinstance(it.get("pubDate"), str) else "",
            "source": it.get("source") if isinstance(it.get("source"), str) else "",
            "builtin": bool(it.get("builtin")),
        })

    report = []
    try:
        cats = tcats.load_categories(report=report)     # 每次实时读, 不缓存
        res = tcats.classify(clean, cats)
        res["show_empty"] = tcats.load_show_empty()
    except Exception as e:  # noqa: BLE001
        log(f"[种子] 分类失败: {e!r}")
        return jsonify({"ok": False, "error": f"分类失败: {e}"}), 500

    res["ok"] = True
    if report:      # 配置损坏必须可见, 不能静默当成"没有分类"
        res["warnings"] = report
        log(f"[种子] 分类配置告警: {report}")
    return jsonify(res)


@app.get("/api/torrent/categories")
def api_torrent_categories_get():
    """读取分类配置(设置页回显)。返回合并结果 + 原始用户配置 + 损坏告警。"""
    report = []
    try:
        merged = tcats.load_categories(report=report)
        user = tcats.load_user_doc()
    except Exception as e:  # noqa: BLE001
        log(f"[种子] 读取分类配置失败: {e!r}")
        return jsonify({"ok": False, "error": f"读取分类配置失败: {e}"}), 500
    res = {"ok": True, "categories": merged,
           "user_categories": user["user_categories"],
           "overrides": user["overrides"],
           "show_empty": user["show_empty"]}
    if report:
        res["warnings"] = report
    return jsonify(res)


@app.post("/api/torrent/categories")
def api_torrent_categories_post():
    """保存用户分类配置。

    信任边界: 入参一律当不可信输入, 只认 user_categories/overrides/show_empty,
    其余键丢弃; 校验不过返回 400 而不是写坏配置。
    预置分类**不可删除**, 只能通过 overrides 禁用。
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    doc = {
        "user_categories": body.get("user_categories") or [],
        "overrides": body.get("overrides") or {},
        "show_empty": bool(body.get("show_empty", False)),
    }
    try:
        tcats.save_user_doc(doc)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        log(f"[种子] 保存分类配置失败: {e!r}")
        return jsonify({"ok": False, "error": f"保存分类配置失败: {e}"}), 500

    report = []
    merged = tcats.load_categories(report=report)
    res = {"ok": True, "categories": merged, "show_empty": doc["show_empty"],
           "user_categories": tcats.load_user_doc()["user_categories"],
           "overrides": doc["overrides"]}
    if report:
        res["warnings"] = report
    return jsonify(res)


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

    保存路径随 ISO 存放模式走, 与镜像下载保持一致:
      * classified(默认) — 能推断发行版则 /data/<type>/<发行版>/, 否则 /data/_torrents/
      * flat             — 一律落到统一目录 /data/iso/(与镜像下载同一个目录)
    保存路径经 qBittorrent `torrents/add` 的 `savepath` 参数下发(见 QBClient.add_torrent)。
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
            # distro/typ 非法(含 ../ 等): 回退到受控目录, 不信任用户输入
            fallback = _torrent_fallback_dir()
            fallback.mkdir(parents=True, exist_ok=True)
            save_path = str(fallback)
            log(f"[种子] 拒绝非法保存路径 {typ}/{distro}, 回退到 {save_path}")
    else:
        fallback = _torrent_fallback_dir()
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
