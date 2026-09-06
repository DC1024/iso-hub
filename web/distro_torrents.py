#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DistroWatch 官方种子源(RSS)解析 + 用户自加种子源/链接持久化。

来源:
  * 内置官方源: https://distrowatch.com/news/torrents.xml
  * 用户自加 RSS 源: 存 ./data/settings.json 的 torrent_rss 键(URL 数组)
  * 用户自加单个链接: 存 ./data/settings.json 的 torrent_links 键(URL 数组)

只解析条目标题/链接/发布时间, 不下载文件本身; 下载交给 qBittorrent。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, List

import xml.etree.ElementTree as ET

BUILTIN_RSS = "https://distrowatch.com/news/torrents.xml"
TITLE = "DistroWatch Torrents"

SETTINGS_JSON = Path(os.environ.get("ISO_DATA_DIR", "/data")) / "settings.json"


def _load_settings() -> dict:
    if not SETTINGS_JSON.exists():
        return {}
    try:
        return json.loads(SETTINGS_JSON.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save_settings(data: dict) -> None:
    cur = _load_settings()
    cur.update(data)
    SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_JSON.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user_rss() -> List[str]:
    s = _load_settings()
    return list(s.get("torrent_rss") or [])


def get_user_links() -> List[Dict]:
    s = _load_settings()
    arr = s.get("torrent_links") or []
    return [x for x in arr if isinstance(x, dict) and x.get("url")]


def add_user_rss(url: str) -> List[str]:
    lst = get_user_rss()
    url = url.strip()
    if url and url not in lst:
        lst.append(url)
        _save_settings({"torrent_rss": lst})
    return lst


def remove_user_rss(url: str) -> List[str]:
    lst = [u for u in get_user_rss() if u != url]
    _save_settings({"torrent_rss": lst})
    return lst


def add_user_link(url: str, distro: str = "") -> List[Dict]:
    lst = get_user_links()
    url = url.strip()
    if url:
        lst.append({"url": url, "distro": distro.strip(), "ts": int(time.time())})
        _save_settings({"torrent_links": lst})
    return lst


def remove_user_link(url: str) -> List[Dict]:
    lst = [x for x in get_user_links() if x.get("url") != url]
    _save_settings({"torrent_links": lst})
    return lst


def parse_rss(url: str, timeout: float = 20) -> List[Dict]:
    """抓取并解析一个 RSS 种子源, 返回 [{title,url,pubDate}] 列表。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iso-hub)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    items = []
    # 兼容 <rss><channel><item> 与 <feed><entry>
    for it in root.iter():
        if it.tag.split("}")[-1] in ("item", "entry"):
            title = link = pub = ""
            for child in it:
                tag = child.tag.split("}")[-1]
                if tag == "title":
                    title = (child.text or "").strip()
                elif tag == "link":
                    link = child.text.strip() if child.text else ""
                    if not link.startswith("http"):
                        link = child.attrib.get("href", "")
                elif tag == "pubDate":
                    pub = (child.text or "").strip()
                elif tag == "updated":
                    if not pub:
                        pub = (child.text or "").strip()
            if title and link:
                items.append({"title": title, "url": link, "pubDate": pub})
    return items


def scan_sources() -> Dict:
    """扫描内置 DistroWatch 源 + 用户自加源, 合并去重返回。"""
    merged: Dict[str, Dict] = {}
    sources = [{"name": TITLE, "url": BUILTIN_RSS, "builtin": True}]
    for u in get_user_rss():
        sources.append({"name": u, "url": u, "builtin": False})
    reports = []
    for src in sources:
        try:
            items = parse_rss(src["url"])
            for it in items:
                merged.setdefault(it["url"], {"title": it["title"], "url": it["url"],
                                              "pubDate": it["pubDate"], "source": src["name"],
                                              "builtin": src["builtin"]})
            reports.append({"url": src["url"], "ok": True, "count": len(items)})
        except Exception as e:  # noqa: BLE001
            reports.append({"url": src["url"], "ok": False, "error": str(e)[:200]})
    for x in get_user_links():
        merged.setdefault(x["url"], {"title": x["url"], "url": x["url"], "pubDate": "",
                                     "source": "manual", "builtin": False,
                                     "distro": x.get("distro", "")})
    return {"source": TITLE, "builtin": BUILTIN_RSS, "reports": reports,
            "items": list(merged.values())}