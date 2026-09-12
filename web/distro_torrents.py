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

import logging
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

# settings.json 的共享读写通道(加锁 + 原子写 + 损坏隔离)。
# app.py 也在写同一个文件 —— 两个模块必须共用同一把锁, 否则锁只保护了一半写方。
import config_files  # noqa: E402

_log = logging.getLogger(__name__)

try:  # 优先使用 defusedxml 防御 XML 炸弹/XXE
    from defusedxml import ElementTree as ET
except Exception:  # noqa: BLE001
    # 生产环境已依赖 defusedxml; 此 fallback 仅用于无该库的开发环境,
    # 且下层 parse_rss 已对 URL 做 http/https 校验并限制响应大小。
    import xml.etree.ElementTree as ET  # type: ignore  # nosec B405

BUILTIN_RSS = "https://distrowatch.com/news/torrents.xml"
TITLE = "DistroWatch Torrents"

SETTINGS_JSON = Path(os.environ.get("ISO_DATA_DIR", "/data")) / "settings.json"


def _is_http_url(url: str) -> bool:
    """仅允许 http/https 协议, 阻断 file://、ftp:// 等 SSRF 向量。"""
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.netloc != ""
    except Exception:  # noqa: BLE001
        return False


def _load_settings() -> dict:
    """读取整个 settings.json, 不存在或损坏返回 {}。

    v1.3.16: 走 config_files 的共享通道。损坏不再静默 —— 会写一条 warning。
    读取**不**挪动文件(并发读会打架), 原件会保留到下一次写入时备份为 .corrupt。
    """
    try:
        return config_files.read_json_raw(SETTINGS_JSON)
    except config_files.CorruptJsonFile:
        _log.warning("%s 不是合法 JSON 对象, 已按空配置处理;"
                     " 下一次保存会把原件备份为 %s.corrupt",
                     SETTINGS_JSON, SETTINGS_JSON.name)
        return {}
    except Exception as e:  # noqa: BLE001  读盘/权限/解码异常: 保持不外抛
        _log.warning("读取 %s 失败: %s", SETTINGS_JSON, e)
        return {}


def _on_settings_quarantined(backup) -> None:
    """settings.json 损坏并被隔离后的告警。

    由 config_files.update_json 在**释放 json 锁之后**回调 —— 回调里可以安全地
    去做别的事, 但在锁内做会产生环路死锁(见 config_files 模块文档第 3 条)。
    """
    _log.warning("%s 不是合法 JSON 对象, 原文件已备份到 %s; 本次以空配置写回。"
                 "请检查其中的凭据/用户/会话/种子源是否需要恢复。", SETTINGS_JSON, backup)


def _save_settings(data: dict) -> None:
    """写回 settings.json(保留其它顶层键)。

    v1.3.16: 这里过去是与 app.py 完全独立的一套 read+write_text。两个模块各自读写
    同一个文件却互不知情, 恰好是"JSON 文件写竞争"最难发现的一面 —— 在 app.py 里
    加锁也只能保护一半的写方。现在两边都锁在同一把 `config_files.file_lock` 上。
    """
    config_files.update_json(SETTINGS_JSON, data,
                             on_quarantine=_on_settings_quarantined)


def get_user_rss() -> List[str]:
    s = _load_settings()
    return list(s.get("torrent_rss") or [])


def get_user_links() -> List[Dict]:
    s = _load_settings()
    arr = s.get("torrent_links") or []
    return [x for x in arr if isinstance(x, dict) and x.get("url")]


def add_user_rss(url: str) -> List[str]:
    url = url.strip()
    if not _is_http_url(url):
        raise ValueError("RSS 地址必须是 http/https 链接")
    lst = get_user_rss()
    if url and url not in lst:
        lst.append(url)
        _save_settings({"torrent_rss": lst})
    return lst


def remove_user_rss(url: str) -> List[str]:
    lst = [u for u in get_user_rss() if u != url]
    _save_settings({"torrent_rss": lst})
    return lst


def add_user_link(url: str, distro: str = "") -> List[Dict]:
    url = url.strip()
    if not url.startswith(("http://", "https://", "magnet:")):
        raise ValueError("链接必须是 http/https/magnet 开头")
    lst = get_user_links()
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
    if not _is_http_url(url):
        raise ValueError("仅允许 http/https 协议的 RSS 源")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iso-hub)"})
    # 禁止 urllib 自动跟随重定向到非 http(s) 方案(防御 SSRF)
    req.add_unredirected_header("Accept", "application/rss+xml, application/xml, text/xml, */*")
    # 调用前已通过 _is_http_url 校验 URL 方案为 http/https,
    # 并在响应后再次校验最终 URL, 禁止重定向到 file:// 等非预期方案。
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
        # 校验最终 URL 仍合法(某些服务可能 30x 到 file://)
        final_url = r.geturl()
        if not _is_http_url(final_url):
            raise ValueError(f"RSS 请求被重定向到非法地址: {final_url}")
        raw = r.read()
        # 限制 RSS 实体大小, 防御 XML 炸弹/内存耗尽
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("RSS 响应超过 5MB, 已拒绝")
    # 优先使用 defusedxml; fallback 仅用于无该库环境, 且已做 URL/大小限制。
    root = ET.fromstring(raw)  # nosec B314
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
    sources = [{"name": TITLE, "url": BUILTIN_RSS, "builtin": True}]
    for u in get_user_rss():
        sources.append({"name": u, "url": u, "builtin": False})

    # L13 修复: 并行抓取 RSS, 单个源失效不再 N×20s 串行阻塞接口
    def _fetch(src):
        try:
            items = parse_rss(src["url"])
            return (src, items, None)
        except Exception as e:  # noqa: BLE001
            return (src, [], str(e)[:200])

    reports = []
    merged: Dict[str, Dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(sources) or 1)) as ex:
        for src, items, err in ex.map(_fetch, sources):
            if err is not None:
                reports.append({"url": src["url"], "ok": False, "error": err})
                continue
            for it in items:
                merged.setdefault(it["url"], {"title": it["title"], "url": it["url"],
                                              "pubDate": it["pubDate"], "source": src["name"],
                                              "builtin": src["builtin"]})
            reports.append({"url": src["url"], "ok": True, "count": len(items)})

    for x in get_user_links():
        merged.setdefault(x["url"], {"title": x["url"], "url": x["url"], "pubDate": "",
                                     "source": "manual", "builtin": False,
                                     "distro": x.get("distro", "")})
    return {"source": TITLE, "builtin": BUILTIN_RSS, "reports": reports,
            "items": list(merged.values())}
