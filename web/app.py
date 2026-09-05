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
from time import struct_time  # 供 _sched_matches 类型注解
import threading
import subprocess
from pathlib import Path
from collections import deque

from flask import Flask, jsonify, request, send_from_directory

# --------------------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("ISO_REPO_DIR", "/app/iso_download"))
DATA_DIR = Path(os.environ.get("ISO_DATA_DIR", "/data"))
HOST, PORT = "0.0.0.0", int(os.environ.get("ISO_HUB_PORT", "8080"))
PY = sys.executable
JSON_FILE = DATA_DIR / "distributions.json"
DEFAULT_JSON = REPO_DIR / "distributions.json"
CUSTOM_JSON = DATA_DIR / "custom_sources.json"      # 用户自定义镜像源(独立持久化,不随 update-meta 覆盖)
SUBS_JSON = DATA_DIR / "subscriptions.json"          # 订阅配置: 自动拉最新+删旧
SETTINGS_JSON = DATA_DIR / "settings.json"           # 网络共享开关+凭据(网页可改, 覆盖 compose env)
SHARE_CONTAINERS = {"samba": "iso-hub-samba", "webdav": "iso-hub-webdav"}
ISO_SUFFIXES = {".iso", ".img", ".qcow2", ".vmdk"}

DATA_DIR.mkdir(parents=True, exist_ok=True)
if not JSON_FILE.exists() and DEFAULT_JSON.exists():
    import shutil
    shutil.copyfile(DEFAULT_JSON, JSON_FILE)

# --------------------------------------------------------------------------- state
_lock = threading.Lock()
_log_lines = deque(maxlen=4000)
_log_seq = 0
_logs = {"lines": _log_lines, "seq": _log_seq}
task = {"proc": None, "info": None}


def log(msg: str) -> None:
    global _log_seq
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


def disk_inventory() -> dict:
    """扫描数据卷 -> {(type, name): [files]}"""
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
                    if f.is_file():
                        try:
                            st = f.stat()
                            inv[key].append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
                        except OSError:
                            pass
    return inv


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


def _custom_repo_dir() -> Path:
    """上游 iso_download 目录,含 update_distributions.py 的 build_from_* 展开函数。"""
    return REPO_DIR if (REPO_DIR / "update_distributions.py").exists() else BASE_DIR.parent / "iso_download"


def expand_custom_repo(item: dict, timeout: int = 40) -> list:
    """把一个自定义"发行版源"条目展开为若干具体 ISO 条目(复用 update_distributions 的解析策略)。

    支持与官方 sources_config.json 相同的 strategy:
      dated_directory / flat_listing / versioned_flat_listing / static
    返回普通 ISO 条目列表(distribution/download_url/checksum_url/checksum),供候选池合并。
    失败时返回空列表并记日志。
    """
    try:
        if item.get("strategy") not in ("dated_directory", "flat_listing", "versioned_flat_listing", "static"):
            return []
        sys.path.insert(0, str(_custom_repo_dir()))
        from update_distributions import build_entries  # noqa: PLC0415
        entries = build_entries(dict(item))
        # 展开后的条目可能缺 distribution/type,补上
        for e in entries:
            e.setdefault("distribution", item.get("distribution", "?"))
            e.setdefault("type", item.get("type", "linux"))
        return entries
    except Exception as e:  # noqa: BLE001
        log(f"[自定义源] 发行版源展开失败 {item.get('distribution')}: {e!r}")
        return []


def merge_custom_entries(entries: list) -> list:
    """把自定义源条目合并进官方清单,按 (distribution,download_url) 去重。

    自定义条目分两种:
      * 普通直链(download_url): 直接合并
      * 发行版源(strategy=...): 实时抓取解析为最新若干 ISO 条目后合并
    """
    merged = {e["download_url"]: e for e in entries}
    for c in load_custom_sources():
        if c.get("strategy"):
            for e in expand_custom_repo(c):
                merged[e["download_url"]] = e
        elif c.get("download_url"):
            merged[c["download_url"]] = c
    return list(merged.values())


def build_distros() -> dict:
    data = load_json()
    inv = disk_inventory()
    groups = {}
    for e in data.get("distributions", []):
        name, typ = e.get("distribution", "?"), e.get("type", "linux")
        key = (typ, name)
        groups.setdefault(key, {"name": name, "type": typ, "entries": []})
        url = e.get("download_url", "")
        fname = url.rstrip("/").rsplit("/", 1)[-1] if url else "?"
        local = next((f for f in inv.get(key, []) if f["name"] == fname), None)
        groups[key]["entries"].append(
            {
                "filename": fname,
                "download_url": url,
                "checksum_url": e.get("checksum_url", ""),
                "checksum": e.get("checksum", ""),
                "local_size": local["size"] if local else 0,
                "local_mtime": int(local["mtime"]) if local else 0,
            }
        )

    result = []
    for (typ, name), g in groups.items():
        expected = {e["filename"] for e in g["entries"]}
        strays = [f for f in inv.get((typ, name), []) if f["name"] not in expected]
        local_total = sum(f["size"] for f in inv.get((typ, name), []))
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
    "samba": {"enabled": True, "username": os.environ.get("SAMBA_USER", "iso"),
              "password": os.environ.get("SAMBA_PASS", "iso123"), "port": os.environ.get("SAMBA_PORT", "1445")},
    "webdav": {"enabled": True, "username": os.environ.get("WEBDAV_USER", "iso"),
               "password": os.environ.get("WEBDAV_PASS", "iso123"), "port": os.environ.get("WEBDAV_PORT", "8081")},
}


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


def load_protected() -> list:
    """受保护文件名列表(存 settings.json 的 protected 键, 相对路径 type/name/文件).iso)。"""
    return list(load_settings_all().get("protected", []) or [])


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
import hashlib  # noqa: E402
import secrets  # noqa: E402

USER_STORE_KEY = "users"
SESSION_TTL = int(os.environ.get("ISO_HUB_SESSION_TTL", str(7 * 24 * 3600)))  # 默认7天
# 内存会话: token -> (username, expire_ts)
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
    u = load_users().get(username)
    if not u:
        return False
    return secrets.compare_digest(_hash_pw(password, u.get("salt", "")),
                                  u.get("password_hash", ""))


def _issue_token(username: str) -> str:
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = (username, int(time.time()) + SESSION_TTL)
    return tok


def _valid_session() -> str | None:
    """校验 X-Auth-Token 是否有效会话 token, 返回 username 或 None。"""
    tok = request.headers.get("X-Auth-Token", "")
    if not tok:
        return None
    hit = _sessions.get(tok)
    if not hit:
        return None
    user, exp = hit
    if time.time() > exp:
        _sessions.pop(tok, None)
        return None
    return user


DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")


def _docker_request(method, path, body, timeout):
    """通过 docker.sock 调 Docker REST API, 主容器内无需 docker CLI。"""
    import socket
    import http.client
    payload = json.dumps(body).encode() if body is not None else None
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.connect(DOCKER_SOCK)
    conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return type("R", (), {"status": resp.status, "text": data.decode()})()


def share_container_state(name: str) -> str | None:
    """返回 sidecar 容器运行状态: running/created/exited/None。"""
    try:
        r = _docker_request("GET", f"/containers/{name}/json", None, 15)
        if r.status != 200:
            return None
        st = json.loads(r.text).get("State", {}).get("Status")
        return st or None
    except Exception:  # noqa: BLE001
        return None


def set_share(proto: str, enabled: bool) -> bool:
    """通过 docker.sock 启动/停止对应 sidecar 容器。"""
    cname = SHARE_CONTAINERS.get(proto)
    if not cname:
        return False
    try:
        if enabled:
            # 先恢复自动重启策略(启用时让容器随 compose 自启), 再启动
            _docker_request("POST", f"/containers/{cname}/start", None, 20)
            _docker_request("POST", f"/containers/{cname}/update", {"RestartPolicy": {"Name": "unless-stopped"}}, 20)
        else:
            # 停用: 设 restart=no 防止自动拉起, 再停止
            _docker_request("POST", f"/containers/{cname}/update", {"RestartPolicy": {"Name": "no"}}, 20)
            _docker_request("POST", f"/containers/{cname}/stop", None, 20)
        return True
    except Exception:  # noqa: BLE001
        return False


# 每个 sidecar 容器内用于凭据的环境变量名 (samba 用; webdav 已改用挂载的 webdav.yml 明文)
CRED_ENVS = {
    "samba": ("SAMBA_USER", "SAMBA_PASS"),
    "webdav": ("WEBDAV_USER", "WEBDAV_PASS"),
}


def _recreate_samba(username: str, password: str) -> bool:
    """重建 samba sidecar 以应用新凭据(凭据在 cmd 的 -u/-s 参数里, 只能重建)。
    以旧容器配置为基准: stop->rm->create(新 cmd)->start, 保留挂载/端口/网络/标签。"""
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

        # 重拼 cmd 的 -u 与 -s 参数里的用户/密码
        cmd = list(cfg.get("Cmd", []))
        joined = " ".join(cmd)
        joined = re.sub(r"-u\s+\S+;\S+", f"-u {username};{password}", joined)
        # -s 形如  iso;/srv/iso;no;no;no;user,pass  替换末尾 user,pass
        joined = re.sub(r"(;[^;]+;no;no;no;)[^,]+,[^\s]+", rf"\g<1>{username},{password}", joined)
        cmd = joined.split(" ")

        body = {
            "Image": cfg.get("Image", "dperson/samba:latest"),
            "Cmd": cmd,
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


def _apply_webdav_creds(username: str, password: str) -> bool:
    """webdav 凭据在挂载的 webdav.yml 里(主容器可写 /data/webdav.yml),
    改写文件 + 重启容器即生效, 无需重建。"""
    try:
        cfg_path = DATA_DIR / "webdav.yml"
        if not cfg_path.exists():
            log(f"[共享] webdav 配置文件缺失: {cfg_path}")
            return False
        text = cfg_path.read_text(encoding="utf-8")
        text = re.sub(r"(username:\s*)\S+", rf"\g<1>{username}", text, count=1)
        text = re.sub(r"(password:\s*)\S+", rf"\g<1>{password}", text, count=1)
        cfg_path.write_text(text, encoding="utf-8")
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
    global task
    cmd = task["cmd"].split(" ") if isinstance(task["cmd"], str) else task["cmd"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )
    with _lock:
        task["proc"] = proc
    for raw in proc.stdout:
        line = raw.rstrip("\n").rstrip("\r")
        if line:
            log(line)
    code = proc.wait()
    with _lock:
        task["proc"] = None
        task["exit_code"] = code
        task["finished"] = time.time()
    if task["cancelled"]:
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
            "cmd": " ".join(str(c) for c in cmd),
            "started": time.time(),
            "finished": None,
            "exit_code": None,
            "cancelled": False,
            "downloads": downloads or [],
            "proc": None,
        }
    log(f"[任务开始] {title}")
    threading.Thread(target=_spawn_worker, daemon=True).start()
    return True


# --------------------------------------------------------------------------- auto-sync scheduler
AUTO_SYNC_INTERVAL = int(os.environ.get("ISO_HUB_SYNC_INTERVAL", "86400"))  # 秒,默认每天
AUTO_SYNC_LAST = {"t": 0}


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
    ]


def schedule_auto_sync() -> None:
    """后台线程: 检查用户自建调度 + 传统间隔, 到点且无任务运行则自动订阅同步。"""
    def _trigger(msg: str):
        cmd = _run_sync_cmd()
        idle = task.get("proc") is None or task["proc"].poll() is not None
        if not cmd or not idle:
            return
        n = len([s for s in load_subscriptions() if s.get("enabled", True)])
        start_task("sync", msg, cmd, [])
        log(f"[自动同步] {msg}: 已启动, {n} 个发行版")

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
            except Exception as e:  # noqa: BLE001
                log(f"[自动同步] 异常: {e}")
            time.sleep(30)
    threading.Thread(target=_loop, daemon=True).start()
    log(f"[自动同步] 调度器已启动 (用户自建调度 + 间隔 {AUTO_SYNC_INTERVAL//3600} 小时)")


def running_task() -> dict | None:
    with _lock:
        if not task.get("proc"):
            return None
        info = {
            "kind": task["kind"],
            "title": task["title"],
            "started": task["started"],
            "cancelled": task["cancelled"],
            "downloads": [],
        }
        # 实时汇报每个目标文件当前大小（含正在写入的）
        for d in task.get("downloads", []):
            p = Path(d["path"])
            size = 0
            try:
                size = p.stat().st_size if p.exists() else 0
            except OSError:
                pass
            info["downloads"].append({"filename": d["filename"], "path": str(p), "size": size})
        return info


def stop_task() -> bool:
    with _lock:
        p = task.get("proc")
        if not p or p.poll() is not None:
            return False
        task["cancelled"] = True
        log("[收到停止请求，正在终止进程…]")
        p.terminate()

        def _kill():
            try:
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001
                p.kill()
        threading.Thread(target=_kill, daemon=True).start()
        return True


# --------------------------------------------------------------------------- flask app
app = Flask(__name__, static_folder="static", static_url_path="/static")
AUTH_TOKEN = os.environ.get("ISO_HUB_TOKEN", "").strip()
# 强制登录开关: ISO_HUB_REQUIRE_LOGIN=1 时, 除登录/自身状态接口外所有 API 均需登录会话或 X-Auth-Token
REQUIRE_LOGIN = os.environ.get("ISO_HUB_REQUIRE_LOGIN", "").strip().lower() in ("1", "true", "yes", "on")


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


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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
    after = int(request.args.get("after", 0))
    with _lock:
        lines = [x for x in _log_lines if x["i"] > after]
    return jsonify({"after": _log_seq, "lines": lines})


@app.post("/api/download")
def api_download():
    body = request.get_json(force=True, silent=True) or {}
    entries = body.get("entries") or []
    if not entries:
        return jsonify({"error": "没有选择任何条目"}), 400
    # 重新从当前清单校验，防止提交伪造数据
    cur = load_json()
    wanted = {(e.get("distribution"), e.get("download_url")) for e in entries}
    matched = [e for e in cur.get("distributions", []) if (e["distribution"], e["download_url"]) in wanted]
    if not matched:
        return jsonify({"error": "所选条目不在当前发行版清单中，请先刷新列表"}), 400

    download_payload = []
    seen = set()
    for e in matched:
        fname = e["download_url"].rstrip("/").rsplit("/", 1)[-1]
        path = str(DATA_DIR / e["type"] / e["distribution"] / fname)
        if (e["distribution"], fname) not in seen:
            seen.add((e["distribution"], fname))
            download_payload.append({"filename": fname, "path": path})

    select_json = json.dumps(
        [{"distribution": e["distribution"], "download_url": e["download_url"]} for e in matched],
        ensure_ascii=False,
    )
    cmd = [
        PY, str(BASE_DIR / "iso_runner.py"),
        "--json-file", str(JSON_FILE),
        "--download-dir", str(DATA_DIR),
        "--select", select_json,
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


@app.post("/api/custom-sources")
def api_custom_sources_add():
    body = request.get_json(force=True, silent=True) or {}
    distribution = (body.get("distribution") or "").strip()
    typ = (body.get("type") or "linux").strip()
    if not distribution:
        return jsonify({"error": "需要 distribution(发行版名)"}), 400
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
        # 判断重复: 同名 + 同 strategy + 同 listing_url
        if any(c.get("strategy") == strategy and c.get("distribution") == distribution
               and c.get("listing_url") == listing_url for c in items):
            return jsonify({"error": "该发行版源已存在"}), 400
        entry = {
            "distribution": distribution,
            "type": typ,
            "strategy": strategy,
            "listing_url": listing_url,
            "version_regex": (body.get("version_regex") or "").strip(),
            "artifact_regex": (body.get("artifact_regex") or "").strip(),
            "download_template": download_template,
            "checksum_template": (body.get("checksum_template") or "").strip(),
            "max_entries": int(body.get("max_entries") or 1),
        }
        if strategy == "static":
            vs = body.get("versions")
            if isinstance(vs, list) and vs:
                entry["versions"] = [str(v).strip() for v in vs]
        # 试解析一次,确认配置可用
        exp = expand_custom_repo(entry, timeout=40)
        if not exp:
            return jsonify({"error": "解析该发行版源失败,请检查 listing_url/正则/模板"}), 400
        items.append(entry)
        save_custom_sources(items)
        log(f"[自定义源] 添加发行版源 {distribution}({strategy}) <- {listing_url} ({len(exp)} 个条目)")
        return jsonify({"ok": True, "sources": items, "expanded": len(exp)})
    # 普通直链
    url = (body.get("download_url") or "").strip()
    if not url:
        return jsonify({"error": "需要 download_url 或 strategy"}), 400
    if any(c.get("download_url") == url for c in items):
        return jsonify({"error": "该地址已存在"}), 400
    items.append({
        "distribution": distribution,
        "type": typ,
        "download_url": url,
        "checksum_url": (body.get("checksum_url") or "").strip(),
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
    if running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    cur = load_json()
    expected = {
        e["download_url"].rstrip("/").rsplit("/", 1)[-1]
        for e in cur.get("distributions", [])
        if e.get("distribution") == name and e.get("type") == typ
    }
    target = DATA_DIR / typ / name
    removed, skipped = [], []
    protected = set(load_protected())
    if target.exists():
        for f in target.iterdir():
            if f.is_file() and f.name not in expected and f.suffix.lower() in ISO_SUFFIXES:
                rel = f"{typ}/{name}/{f.name}"
                if rel in protected or f.name in protected:
                    skipped.append(f"{f.name}: 受保护, 跳过")
                    continue
                try:
                    f.unlink()
                    removed.append(f.name)
                except OSError as e:
                    skipped.append(f"{f.name}: {e}")
    log(f"[清理] {typ}/{name}: 删除 {len(removed)} 个过期文件，跳过 {len(skipped)}")
    return jsonify({"ok": True, "removed": removed, "skipped": skipped})


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
    """返回当前登录状态(不强制要求已登录)。"""
    tok = request.headers.get("X-Auth-Token")
    username = _sessions.get(tok, (None, 0))[0] if tok else None
    return jsonify({"authenticated": bool(username), "username": username})


@app.post("/api/user/login")
def api_user_login():
    body = request.get_json(force=True, silent=True) or {}
    u = (body.get("username") or "").strip()
    p = body.get("password") or ""
    # 首次登录且尚未播种: 允许用 ISO_HUB_ADMIN_USER/PASS 播种
    if not load_users() and not (os.environ.get("ISO_HUB_ADMIN_USER", "").strip() or ""):
        # 无可播种账号, 允许首个登录者创建管理员(设置页用相同用户名/密码建账号)
        if not u or not p:
            return jsonify({"error": "缺少用户名或密码"}), 400
        salt = secrets.token_hex(16)
        users = load_users()
        users[u] = {"password_hash": _hash_pw(p, salt), "salt": salt,
                    "created_at": int(time.time())}
        save_users(users)
        log(f"[用户] 首次创建管理员账号: {u}")
        return jsonify({"ok": True, "token": _issue_token(u), "username": u})
    if not _check_login(u, p):
        return jsonify({"error": "用户名或密码错误"}), 401
    return jsonify({"ok": True, "token": _issue_token(u), "username": u})


@app.post("/api/user/logout")
def api_user_logout():
    tok = request.headers.get("X-Auth-Token")
    if tok and tok in _sessions:
        _sessions.pop(tok, None)
    return jsonify({"ok": True})


@app.post("/api/user/password")
def api_user_password():
    """修改当前登录用户密码(需会话 token)。"""
    tok = request.headers.get("X-Auth-Token")
    info = _sessions.get(tok)
    if not info:
        return jsonify({"error": "未登录"}), 401
    username, _exp = info
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
    # 附带每个 sidecar 容器实时状态
    for proto, s in shares.items():
        s["container"] = share_container_state(SHARE_CONTAINERS[proto])
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
                return jsonify({"error": f"{proto} 凭据已保存到 settings.json, 但同步到容器失败, 请重建该 sidecar"}), 500
        if "enabled" in p:
            # 启停 sidecar 容器
            want = bool(p["enabled"])
            cur["enabled"] = want
            ok = set_share(proto, want)
            if not ok:
                return jsonify({"error": f"{proto} 容器操作失败(是否已部署 sidecar? 需挂载 docker.sock)"}), 500
    save_shares(shares)
    log(f"[共享] 设置已保存: SMB={shares['samba']['enabled']} WebDAV={shares['webdav']['enabled']}")
    return jsonify({"ok": True, "shares": {k: {**v, "container": share_container_state(SHARE_CONTAINERS[k])} for k, v in shares.items()}})


if __name__ == "__main__":
    log(f"ISO Hub 启动  |  清单: {JSON_FILE}  数据目录: {DATA_DIR}")
    seed_admin()  # 若设置 ISO_HUB_ADMIN_USER/PASS 则播种管理员
    schedule_auto_sync()  # 订阅自动同步调度器(默认每天; ISO_HUB_SYNC_INTERVAL 可改秒数)
    if os.environ.get("ISO_HUB_DEV"):
        app.run(host=HOST, port=PORT, debug=True)
    else:
        from waitress import serve
        print(f"serving on http://{HOST}:{PORT}")
        serve(app, host=HOST, port=PORT, threads=8)
