# -*- coding: utf-8 -*-
"""自定义源与自动刷新。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request
import re

from api._ctx import core

bp = Blueprint("custom_sources", __name__)



@bp.get("/api/custom-sources")
def api_custom_sources():
    return jsonify({"sources": core.load_custom_sources()})


@bp.post("/api/custom-sources")
def api_custom_sources_add():
    body = request.get_json(force=True, silent=True) or {}
    distribution = (body.get("distribution") or "").strip()
    typ = (body.get("type") or "linux").strip()
    if not distribution:
        return jsonify({"error": "需要 distribution(发行版名)"}), 400
    if typ not in core.ALLOWED_TYPES:
        return jsonify({"error": f"type 必须是 {', '.join(sorted(core.ALLOWED_TYPES))} 之一"}), 400
    items = core.load_custom_sources()
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
        if not core._is_http_url(listing_url):
            return jsonify({"error": "listing_url 必须是 http/https 链接"}), 400
        if download_template and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", download_template):
            if not core._is_http_url(download_template):
                return jsonify({"error": "download_template 若含绝对 URL 协议, 必须是 http/https"}), 400
        checksum_template = (body.get("checksum_template") or "").strip()
        if checksum_template and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", checksum_template):
            if not core._is_http_url(checksum_template):
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
        core.save_custom_sources(items)
        # 不再在请求线程内实时抓取校验: 落盘后由后台任务展开(若当前有任务运行则稍后手动刷新)
        core.refresh_custom_repo_cache()
        core.log(f"[自定义源] 添加发行版源 {distribution}({strategy}) <- {listing_url} (待后台展开)")
        return jsonify({"ok": True, "sources": items, "expanded": 0, "pending_refresh": True})
    # 普通直链
    url = (body.get("download_url") or "").strip()
    if not url:
        return jsonify({"error": "需要 download_url 或 strategy"}), 400
    if not core._is_http_url(url):
        return jsonify({"error": "download_url 必须是 http/https 链接"}), 400
    if any(c.get("download_url") == url for c in items):
        return jsonify({"error": "该地址已存在"}), 400
    checksum_url = (body.get("checksum_url") or "").strip()
    if checksum_url and not core._is_http_url(checksum_url):
        return jsonify({"error": "checksum_url 必须是 http/https 链接"}), 400
    items.append({
        "distribution": distribution,
        "type": typ,
        "download_url": url,
        "checksum_url": checksum_url,
        "checksum": (body.get("checksum") or "").strip(),
    })
    core.save_custom_sources(items)
    core.log(f"[自定义源] 添加 {distribution} <- {url}")
    return jsonify({"ok": True, "sources": items})


@bp.delete("/api/custom-sources")
def api_custom_sources_del():
    url = request.args.get("url", "")
    distribution = request.args.get("distribution", "")
    strategy = request.args.get("strategy", "")
    if url:
        items = [c for c in core.load_custom_sources() if c.get("download_url") != url]
    elif distribution and strategy:
        items = [c for c in core.load_custom_sources()
                 if not (c.get("distribution") == distribution and c.get("strategy") == strategy)]
    else:
        return jsonify({"error": "需要 url 或 distribution+strategy"}), 400
    core.save_custom_sources(items)
    core.log(f"[自定义源] 删除 {url or f'{distribution}({strategy})'}")
    return jsonify({"ok": True, "sources": items})


@bp.post("/api/custom-sources/refresh")
def api_custom_sources_refresh():
    """手动触发自定义源展开(后台子进程, 非阻塞)。复用 start_task 的单一任务锁。"""
    if core.running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    sources = core._custom_source_list()
    if not sources:
        return jsonify({"error": "没有 strategy 类型的自定义源"}), 400
    ok = core.refresh_custom_repo_cache()
    if not ok:
        return jsonify({"error": "已有任务在运行"}), 409
    return jsonify({"ok": True, "count": len(sources)})


@bp.get("/api/custom-sources/auto-refresh")
def api_custom_auto_refresh_get():
    """读取自定义源自动刷新设置(独立开关, 默认关闭)。"""
    return jsonify({"custom_source_auto_refresh": core.load_custom_auto_refresh()})


@bp.post("/api/custom-sources/auto-refresh")
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
    cfg = core.save_custom_auto_refresh(enabled, interval)
    core.log(f"[自定义源] 自动刷新设置已保存: enabled={cfg['enabled']}, interval={cfg['interval']}s")
    return jsonify({"ok": True, "custom_source_auto_refresh": cfg})


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['api_custom_sources', 'api_custom_sources_add', 'api_custom_sources_del', 'api_custom_sources_refresh', 'api_custom_auto_refresh_get', 'api_custom_auto_refresh_set']:
        setattr(ns, _name, getattr(_mod, _name))
