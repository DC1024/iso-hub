#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qBittorrent Web API v2 客户端封装。

让 iso-hub 主容器把种子(torrent/磁力)交给外部的 qBittorrent sidecar 下载,
并查询进度、删除任务、移动已下载文件到 ./data 共享目录以被主容器统一识别。

配置来源(优先级 settings.json > 环境变量):
  QB_URL    qBittorrent WebUI 地址(默认 http://qbittorrent:8080)
  QB_USER   账号(默认 admin)
  QB_PASS   密码(默认 adminadmin)
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None  # type: ignore

try:
    import http.cookiejar as _cj
except Exception:  # noqa: BLE001
    _cj = None  # type: ignore


def qb_config() -> Dict[str, str]:
    """从 settings.json + 环境变量读取 qBittorrent 连接配置。"""
    cfg = {}
    base = Path(os.environ.get("ISO_DATA_DIR", "/data"))
    sj = base / "settings.json"
    try:
        if sj.exists():
            data = json.loads(sj.read_text(encoding="utf-8")) or {}
            t = data.get("qbittorrent") or {}
            if isinstance(t, dict):
                cfg.update({k: str(v) for k, v in t.items() if v})
    except Exception:  # noqa: BLE001
        pass
    env_defaults = {
        "QB_URL": "http://qbittorrent:8080",
        "QB_USER": "admin",
        # nosec B105: 这是 qBittorrent 官方默认密码, 仅作为兜底; 部署时必须通过 QB_PASS 环境变量修改。
        "QB_PASS": "adminadmin",
    }
    merged = {k: cfg.get(k.lower()) or os.environ.get(k) or env_defaults[k] for k in env_defaults}
    return merged


class QBClient:
    """极简 qBittorrent Web API v2 客户端(requests 优先, 否则 urllib)。"""

    def __init__(self, url: Optional[str] = None, user: Optional[str] = None,
                 password: Optional[str] = None) -> None:
        cfg = qb_config()
        self.url = (url or cfg["QB_URL"]).rstrip("/")
        self.user = user or cfg["QB_USER"]
        self.password = password or cfg["QB_PASS"]
        self._sid: Optional[str] = None
        self._login_failures = 0
        self._logged_in = False
        # 会话保持: requests 用 Session, urllib 用 cookie jar
        # (qBittorrent 的 session cookie 名是 QBT_SID_<端口>, 手工拼 Cookie 头不可靠)
        self._sess = requests.Session() if requests is not None else None
        self._jar = _cj.CookieJar() if _cj is not None else None
        # 默认开启 TLS 校验(避免 MITM); 仅当显式设置 QB_INSECURE=1 时才关闭。
        # 内网 http 无 TLS, 不受影响。
        self._verify = not (os.environ.get("QB_INSECURE", "").strip().lower() in ("1", "true", "yes", "on"))
        self._opener = (urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)) if self._jar is not None else None)

    # ---- 底层请求 ----
    def _request(self, method: str, path: str, data: Optional[Dict] = None,
                 timeout: float = 15) -> Tuple[int, Any]:
        uri = f"{self.url}{path}"
        headers = {"Referer": self.url + "/", "Origin": self.url}
        try:
            if self._sess is not None:
                resp = self._sess.request(method, uri, data=data, headers=headers,
                                          timeout=timeout, verify=self._verify)
                try:
                    j = resp.json()
                except Exception:  # noqa: BLE001
                    j = resp.text
                return resp.status_code, j
            body = urllib.parse.urlencode(data or {}).encode() if data else None
            req = urllib.request.Request(uri, data=body, headers=headers, method=method)
            opener = self._opener or urllib.request.build_opener()
            with opener.open(req, timeout=timeout) as r:
                ct = r.headers.get("Content-Type", "")
                raw = r.read()
                try:
                    return r.status, json.loads(raw) if "json" in ct else raw.decode(errors="replace")
                except Exception:  # noqa: BLE001
                    return r.status, raw.decode(errors="replace")
        except urllib.error.HTTPError as e:  # 4xx/5xx 也带 body, 尽量读出
            try:
                raw = e.read().decode(errors="replace")
            except Exception:  # noqa: BLE001
                raw = ""
            return e.code, raw or f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            return 0, f"连接失败: {e}"

    def login(self) -> bool:
        """登录拿会话 cookie。

        注意: qBittorrent 4.6+/5.x 登录成功返回 **204 No Content**(旧版返回 200 "Ok."),
        两种都要接受; 会话靠 cookie jar / Session 自动维持, 不要手工拼 Cookie 头。
        """
        code, text = self._request("POST", "/api/v2/auth/login",
                                   {"username": self.user, "password": self.password})
        # B1 修复: 登录成功的判定必须是 "状态码 OK 且 响应不含失败标记"。
        # 旧版密码错误返回 200 "Fails."; 旧逻辑 `or "Forbidden" not in text` 会把 "Fails." 误判为成功。
        # 正确做法: 状态码 OK, 且响应要么是空/Ok., 要么明确拒绝(Forbidden/Fails)则判失败。
        code_ok = code in (200, 204)
        if not code_ok:
            ok = False
        else:
            t = text.strip() if isinstance(text, str) else ""
            low = t.lower() if t else ""
            # 明确失败标记优先判定失败
            if any(bad in low for bad in ("fails.", "forbidden", "unauthorized", "invalid")):
                ok = False
            elif t in ("", "Ok."):
                ok = True
            else:
                # 其它非空响应(非明确失败)按成功接受, 保持对新版 202/JSON 兼容
                ok = True
        self._logged_in = bool(ok)
        if ok:
            self._sid = "ok"  # 仅作为"已登录"标记, 真正的凭证在 cookie 里
            self._login_failures = 0
        else:
            self._login_failures += 1
            self._sid = None
        return bool(ok)

    def _ensure_login(self) -> bool:
        return True if self._logged_in else self.login()

    def version(self) -> Optional[str]:
        self._ensure_login()
        code, t = self._request("GET", "/api/v2/app/version")
        return str(t) if code == 200 and t else None

    def transfer_info(self) -> Dict:
        """全局传输信息(速率/空间)。"""
        self._ensure_login()
        code, t = self._request("GET", "/api/v2/transfer/info")
        return t if code == 200 and isinstance(t, dict) else {}

    def add_torrent(self, urls: List[str], save_path: Optional[str] = None,
                    category: str = "") -> Dict:
        """添加种子(urls 可为 http(s).torrent 链接或磁力链)。返回 {ok, error}。"""
        if not self._ensure_login():
            return {"ok": False, "error": "qBittorrent 登录失败"}
        data = {"urls": "\n".join(urls)}
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category
        data["autoTMM"] = "false"
        code, t = self._request("POST", "/api/v2/torrents/add", data)
        # 旧版 200 "Ok."; qBittorrent 5.x 异步接受返回 **202** + JSON
        if code not in (200, 202):
            return {"ok": False, "error": f"HTTP {code}: {str(t)[:300]}"}
        # 5.x 返回: {"added_torrent_ids": [...], "failure_count": 0,
        #           "pending_count": 1, "success_count": 0}
        # pending_count>0 表示正在拉取种子/元数据, 属正常接受
        if isinstance(t, dict):
            if int(t.get("failure_count") or 0) == 0:
                return {"ok": True, "detail": t}
            return {"ok": False, "error": str(t)[:300]}
        if str(t).strip() in ("Ok.", ""):
            return {"ok": True}
        return {"ok": False, "error": str(t)[:300]}

    def list_torrents(self, tag: str = "") -> List[Dict]:
        """列出种子信息。可按自定义标签过滤。"""
        self._ensure_login()
        code, t = self._request("GET", "/api/v2/torrents/info?filter=all")
        arr = t if code == 200 and isinstance(t, list) else []
        if tag:
            # L12 修复: qB 返回的 tags 是 ", " 分隔(含空格), 用逗号切分后去空白再匹配
            arr = [x for x in arr if tag in [s.strip() for s in (x.get("tags") or "").split(",")]]
        return arr

    def torrent_files(self, hash: str) -> List[Dict]:
        """列出某种子的文件(含 progress / is_seed / size / name)。

        name 是相对 save_path 的路径(单文件种子即文件名本身)。用于「下载到本机」
        时筛选已完成文件。
        """
        self._ensure_login()
        code, t = self._request("GET", "/api/v2/torrents/files?hash=" + (hash or ""))
        return t if code == 200 and isinstance(t, list) else []

    @staticmethod
    def _ok(code: int) -> bool:
        """qBittorrent 写操作成功码: 旧版 200, 5.x 可能 202(异步)/204。"""
        return code in (200, 202, 204)

    def set_category(self, hashes: List[str], category: str) -> bool:
        self._ensure_login()
        code, _ = self._request("POST", "/api/v2/torrents/setCategory",
                                {"hashes": "|".join(hashes), "category": category})
        return self._ok(code)

    def set_tags(self, hashes: List[str], tags: str) -> bool:
        self._ensure_login()
        code, _ = self._request("POST", "/api/v2/torrents/addTags",
                                {"hashes": "|".join(hashes), "tags": tags})
        return self._ok(code)

    def delete_torrents(self, hashes: List[str], delete_files: bool = False) -> bool:
        """删除种子; delete_files=True 同时删已下载文件。"""
        self._ensure_login()
        code, _ = self._request("POST", "/api/v2/torrents/delete",
                                {"hashes": "|".join(hashes),
                                 "deleteFiles": "true" if delete_files else "false"})
        return self._ok(code)


def distro_name_from_torrent(name: str) -> Tuple[str, str]:
    """从种子标题(文件名)尽量推断发行版名与类型。

    例: ubuntu-26.04.1-desktop-amd64.iso.torrent -> ("ubuntu","linux")
    distrowatch.com/dwres/torrents/haiku-r1beta6-x86_64.torrent -> ("haiku","linux")
    返回 (发行版名, 类型), 无法识别时 ("", "")。
    """
    base = name.replace(".torrent", "")
    # 去掉可能的路径/查询
    base = base.rsplit("/", 1)[-1].split("?")[0]
    low = base.lower()
    # 常见官方 Ubuntu 系: kubuntu/xubuntu/lubuntu/... -> 前段就是发行版
    ubuntu_flavors = [
        "kubuntu", "xubuntu", "lubuntu", "ubuntustudio", "ubuntukylin",
        "ubuntucinnamon", "ubuntu-budgie", "edubuntu", "ubuntu",
    ]
    for f in sorted(ubuntu_flavors, key=len, reverse=True):
        if low.startswith(f):
            return f.replace("-", ""), "linux"
    known = {
        "netbsd": "bsd", "freebsd": "bsd", "openbsd": "bsd", "haiku": "linux",
        "manjaro": "linux", "endeavouros": "linux", "garuda": "linux",
        "sparkylinux": "linux", "biglinux": "linux", "arch": "linux",
        "debian": "linux", "fedora": "linux", "opensuse": "linux",
        "proxmox": "linux", "deepin": "linux", "butterbian": "linux",
        "pop_os": "linux", "elementary": "linux", "linuxmint": "linux",
    }
    for k, typ in known.items():
        if low.startswith(k):
            return (k.replace("_", "-") if k == "pop_os" else k), typ
    return "", ""


def distro_dir(data_root: Path, name: str, typ: str = "linux") -> Optional[Path]:
    """给定发行版名, 在 ./data 下找匹配目录(忽略大小写)。找不到返回 None。"""
    if not data_root.exists():
        return None
    if not name:
        return None
    low = name.lower()
    # 先精确, 再忽略大小写/短横
    for tdir in data_root.iterdir():
        if not tdir.is_dir() or tdir.name.startswith("_"):
            continue
        for ddir in tdir.iterdir():
            if not ddir.is_dir():
                continue
            dlow = ddir.name.lower()
            if dlow == low or dlow.replace("-", "") == low.replace("-", "") or \
               dlow.startswith(low[:6]) or low.startswith(dlow[:6]):
                return ddir
    return None