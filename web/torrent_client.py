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
import re
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None  # type: ignore


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

    # ---- 底层请求 ----
    def _request(self, method: str, path: str, data: Optional[Dict] = None,
                 timeout: float = 15) -> Tuple[int, Any]:
        uri = f"{self.url}{path}"
        headers = {}
        if self._sid:
            headers["Cookie"] = f"SID={self._sid}"
        try:
            if requests is not None:
                resp = requests.request(method, uri, data=data, headers=headers,
                                        timeout=timeout, verify=False)
                try:
                    j = resp.json()
                except Exception:  # noqa: BLE001
                    j = resp.text
                return resp.status_code, j
            body = urllib.parse.urlencode(data or {}).encode() if data else None
            req = urllib.request.Request(uri, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ct = r.headers.get("Content-Type", "")
                raw = r.read()
                try:
                    return r.status, json.loads(raw) if "json" in ct else raw.decode(errors="replace")
                except Exception:  # noqa: BLE001
                    return r.status, raw.decode(errors="replace")
        except Exception as e:  # noqa: BLE001
            return 0, f"连接失败: {e}"

    def login(self) -> bool:
        """登录拿 SID cookie。失败返回 False。"""
        code, text = self._request("POST", "/api/v2/auth/login",
                                   {"username": self.user, "password": self.password})
        if code == 200 and isinstance(text, str) and text.strip() == "Ok.":
            self._sid = self._sid or self._session_cookie()
            self._login_failures = 0
            return True
        self._login_failures += 1
        return False

    def _session_cookie(self) -> Optional[str]:
        # requests 会记录 cookie; 用 urllib 时从响应头拿(简单场景直接再试一次)
        if requests is not None:
            try:
                s = requests.Session()
                s.post(f"{self.url}/api/v2/auth/login",
                       data={"username": self.user, "password": self.password}, timeout=15)
                return s.cookies.get("SID")
            except Exception:  # noqa: BLE001
                return None
        return None

    def _ensure_login(self) -> bool:
        return self.login() if self._sid is None else True

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
        if code == 200 and (t in ("Ok.", "") or str(t) in ("Ok.", "")):
            return {"ok": True}
        return {"ok": False, "error": str(t)[:300]}

    def list_torrents(self, tag: str = "") -> List[Dict]:
        """列出种子信息。可按自定义标签过滤。"""
        self._ensure_login()
        code, t = self._request("GET", "/api/v2/torrents/info?filter=all")
        arr = t if code == 200 and isinstance(t, list) else []
        if tag:
            arr = [x for x in arr if tag in (x.get("tags") or "").split(",")]
        return arr

    def set_category(self, hashes: List[str], category: str) -> bool:
        self._ensure_login()
        code, _ = self._request("POST", "/api/v2/torrents/setCategory",
                                {"hashes": "|".join(hashes), "category": category})
        return code == 200

    def set_tags(self, hashes: List[str], tags: str) -> bool:
        self._ensure_login()
        code, _ = self._request("POST", "/api/v2/torrents/addTags",
                                {"hashes": "|".join(hashes), "tags": tags})
        return code == 200

    def delete_torrents(self, hashes: List[str], delete_files: bool = False) -> bool:
        """删除种子; delete_files=True 同时删已下载文件。"""
        self._ensure_login()
        code, _ = self._request("POST", "/api/v2/torrents/delete",
                                {"hashes": "|".join(hashes),
                                 "deleteFiles": "true" if delete_files else "false"})
        return code == 200


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