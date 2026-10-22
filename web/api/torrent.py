# -*- coding: utf-8 -*-
"""种子下载：qBittorrent 源、分类、添加/删除、RSS 与链接。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request
from torrent_client import distro_name_from_torrent
from torrent_client import qb_config
import distro_torrents as dtorrents
import torrent_categories as tcats
import uuid

from api._ctx import core

bp = Blueprint("torrent", __name__)


def _server_error(what: str, e: Exception) -> str:
    """把**未预期**异常折成给客户端的通用消息, 原文只进服务端日志。

    M7 修复(审查 2026-10): 异常原文常含容器内绝对路径(/downloads/...)、qB
    主机名与配置片段, 原样回显等于把部署拓扑送给调用方。这里生成一个短关联
    ID: 客户端只拿到 ID, 服务端日志用同一个 ID 能查到原文, 排障不受影响。

    注意边界: **校验类** ValueError 不走这里。那些是作者写给用户看的中文提示
    (如 "RSS 地址必须是 http/https 链接"), 属于正常业务反馈, 必须原样返回。
    """
    eid = uuid.uuid4().hex[:8]
    core.log(f"[种子] [{eid}] {what}: {type(e).__name__}: {e}")
    return f"{what}(错误编号 {eid}), 详情见服务端日志"



@bp.get("/api/torrent/sources")
def api_torrent_sources():
    """扫描 DistroWatch 官方种子源 + 用户自加 RSS/链接, 返回可下载的种子列表。"""
    ok, err = core._ensure_qb_enabled()
    if not ok:
        return err
    if not core.TORRENT_AVAILABLE:
        # M7: 导入失败原文可能带容器路径/模块名, 只进服务端日志, 不回显。
        core.log(f"[种子] 模块导入失败: {core._torrent_import_err or '(无详细信息)'}")
        return jsonify({"error": "种子模块未加载, 详情见服务端日志"}), 500
    try:
        res = dtorrents.scan_sources()
        return jsonify({"ok": True, "source": res.get("source"),
                        "reports": res.get("reports", []),
                        "items": res.get("items", []),
                        "user_rss": dtorrents.get_user_rss(),
                        "user_links": dtorrents.get_user_links()})
    except Exception as e:  # noqa: BLE001 - 边界: 请求处理器兜底, 未预期异常折成 500 返回前端
        return jsonify({"error": _server_error("扫描种子源失败", e)}), 500


@bp.post("/api/torrent/classify")
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
    if len(items) > core.MAX_CLASSIFY_ITEMS:
        return jsonify({"ok": False,
                        "error": "items 过多(上限 %d 条)" % core.MAX_CLASSIFY_ITEMS}), 400

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
    except Exception as e:  # noqa: BLE001 - 边界: 请求处理器兜底, 未预期异常折成 500 返回前端
        core.log(f"[种子] 分类失败: {e!r}")
        return jsonify({"ok": False, "error": f"分类失败: {e}"}), 500

    res["ok"] = True
    if report:      # 配置损坏必须可见, 不能静默当成"没有分类"
        res["warnings"] = report
        core.log(f"[种子] 分类配置告警: {report}")
    return jsonify(res)


@bp.get("/api/torrent/categories")
def api_torrent_categories_get():
    """读取分类配置(设置页回显)。返回合并结果 + 原始用户配置 + 损坏告警。"""
    report = []
    try:
        merged = tcats.load_categories(report=report)
        user = tcats.load_user_doc()
    except Exception as e:  # noqa: BLE001 - 边界: 请求处理器兜底, 未预期异常折成 500 返回前端
        core.log(f"[种子] 读取分类配置失败: {e!r}")
        return jsonify({"ok": False, "error": f"读取分类配置失败: {e}"}), 500
    res = {"ok": True, "categories": merged,
           "user_categories": user["user_categories"],
           "overrides": user["overrides"],
           "show_empty": user["show_empty"]}
    if report:
        res["warnings"] = report
    return jsonify(res)


@bp.post("/api/torrent/categories")
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
    except Exception as e:  # noqa: BLE001 - 边界: 请求处理器兜底, 未预期异常折成 500 返回前端
        return jsonify({"ok": False, "error": _server_error("保存分类配置失败", e)}), 500

    report = []
    merged = tcats.load_categories(report=report)
    res = {"ok": True, "categories": merged, "show_empty": doc["show_empty"],
           "user_categories": tcats.load_user_doc()["user_categories"],
           "overrides": doc["overrides"]}
    if report:
        res["warnings"] = report
    return jsonify(res)


@bp.get("/api/torrent/info")
def api_torrent_info():
    """查询 qBittorrent 连接状态 + 传输信息 + 种子列表(含本地是否已存在)."""
    ok, err = core._ensure_qb_enabled()
    if not ok:
        return err
    if not core.TORRENT_AVAILABLE:
        return jsonify({"error": "种子模块未加载"}), 500
    try:
        qb = core._qb()
        ver = qb.version()
        if not ver:
            cfg = qb_config()
            # P2 修复: 密码绝不回传 —— 旧实现截取前 12 位, 12 位以内的密码(含默认
            # adminadmin)等于全量泄露。与 _redact_password 的约定保持一致。
            return jsonify({"ok": False, "error": f"无法连接 qBittorrent ({cfg['QB_URL']}), 请确认已部署 sidecar 且凭据正确",
                            "config": {k: ("***" if k == "QB_PASS" else cfg[k]) for k in cfg}}), 200
        transfer = qb.transfer_info()
        toks = qb.list_torrents()
        # 标记本地是否已存在同名文件
        inv = core.disk_inventory()
        names = {f["name"] for fs in inv.values() for f in fs}
        for t in toks:
            t["local_has"] = bool(set(t.get("name", "").split()) & names) or any(
                f in names for f in core._torrent_file_names(t))
        return jsonify({"ok": True, "connected": True, "version": ver,
                        "transfer": transfer, "torrents": toks})
    except Exception as e:  # noqa: BLE001 - 边界: 请求处理器兜底, 未预期异常折成 500 返回前端
        core.log(f"[种子] 查询 qBittorrent 状态失败: {e!r}")
        return jsonify({"ok": False, "error": f"查询失败: {e}"}), 500


@bp.post("/api/torrent/add")
def api_torrent_add():
    """把种子(URL/磁力)交给 qBittorrent 下载。
    body: {urls:[...], distro?, type?, category?}

    保存路径随 ISO 存放模式走, 与镜像下载保持一致:
      * classified(默认) — 能推断发行版则 /data/<type>/<发行版>/, 否则 /data/_torrents/
      * flat             — 一律落到统一目录 /data/iso/(与镜像下载同一个目录)
    保存路径经 qBittorrent `torrents/add` 的 `savepath` 参数下发(见 QBClient.add_torrent)。
    """
    ok, err = core._ensure_qb_enabled()
    if not ok:
        return err
    if not core.TORRENT_AVAILABLE:
        return jsonify({"error": "种子模块未加载"}), 500
    body = request.get_json(force=True, silent=True) or {}
    urls = [u for u in (body.get("urls") or []) if u and str(u).strip()]
    if not urls:
        return jsonify({"error": "未提供任何种子 URL/磁力链接"}), 400
    urls = [str(u).strip() for u in urls]
    for u in urls:
        if u.startswith("magnet:"):
            continue
        if not core._is_http_url(u):
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
        target = core._safe_join(typ, distro)
        if target is not None:
            target.mkdir(parents=True, exist_ok=True)
            save_path = str(target)
        else:
            # distro/typ 非法(含 ../ 等): 回退到受控目录, 不信任用户输入
            fallback = core._torrent_fallback_dir()
            fallback.mkdir(parents=True, exist_ok=True)
            save_path = str(fallback)
            core.log(f"[种子] 拒绝非法保存路径 {typ}/{distro}, 回退到 {save_path}")
    else:
        fallback = core._torrent_fallback_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        save_path = str(fallback)
    try:
        qb = core._qb()
        r = qb.add_torrent(urls, save_path=save_path, category=body.get("category") or "iso-hub")
        if not r.get("ok"):
            return jsonify({"error": f"qBittorrent 添加失败: {r.get('error')}"}), 502
        core.log(f"[种子] 添加 {len(urls)} 个下载, 保存到 {save_path}")
        return jsonify({"ok": True, "save_path": save_path, "distro": distro, "type": typ})
    except Exception as e:  # noqa: BLE001 - 边界: 请求处理器兜底, 未预期异常折成 500 返回前端
        return jsonify({"error": _server_error("添加失败", e)}), 500


@bp.post("/api/torrent/delete")
def api_torrent_delete():
    """从 qBittorrent 删除种子。body: {hashes:[...], delete_files?:bool}"""
    ok, err = core._ensure_qb_enabled()
    if not ok:
        return err
    if not core.TORRENT_AVAILABLE:
        return jsonify({"error": "种子模块未加载"}), 500
    body = request.get_json(force=True, silent=True) or {}
    hashes = [str(h) for h in (body.get("hashes") or []) if h]
    if not hashes:
        return jsonify({"error": "未提供种子 hash"}), 400
    delete_files = bool(body.get("delete_files"))
    try:
        qb = core._qb()
        ok = qb.delete_torrents(hashes, delete_files)
        core.log(f"[种子] 删除 {len(hashes)} 个, 同时删文件={delete_files}")
        return jsonify({"ok": ok})
    except Exception as e:  # noqa: BLE001 - 边界: 请求处理器兜底, 未预期异常折成 500 返回前端
        return jsonify({"error": _server_error("删除失败", e)}), 500


@bp.post("/api/torrent/rss/add")
def api_torrent_rss_add():
    ok, err = core._ensure_qb_enabled()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    if not core._is_http_url(url):
        return jsonify({"error": "RSS 地址必须是有效的 http/https 链接"}), 400
    try:
        lst = dtorrents.add_user_rss(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    core.log(f"[种子] 添加 RSS 源: {url}")
    return jsonify({"ok": True, "user_rss": lst})


@bp.post("/api/torrent/rss/remove")
def api_torrent_rss_remove():
    ok, err = core._ensure_qb_enabled()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    lst = dtorrents.remove_user_rss(url)
    core.log(f"[种子] 移除 RSS 源: {url}")
    return jsonify({"ok": True, "user_rss": lst})


@bp.post("/api/torrent/link/add")
def api_torrent_link_add():
    ok, err = core._ensure_qb_enabled()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    if url.startswith("magnet:"):
        pass
    elif not core._is_http_url(url):
        return jsonify({"error": "链接必须是有效的 http/https 磁力链接"}), 400
    try:
        lst = dtorrents.add_user_link(url, (body.get("distro") or ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    core.log(f"[种子] 添加手动链接: {url}")
    return jsonify({"ok": True, "user_links": lst})


@bp.post("/api/torrent/link/remove")
def api_torrent_link_remove():
    ok, err = core._ensure_qb_enabled()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    lst = dtorrents.remove_user_link(url)
    core.log(f"[种子] 移除手动链接: {url}")
    return jsonify({"ok": True, "user_links": lst})


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['api_torrent_sources', 'api_torrent_classify', 'api_torrent_categories_get', 'api_torrent_categories_post', 'api_torrent_info', 'api_torrent_add', 'api_torrent_delete', 'api_torrent_rss_add', 'api_torrent_rss_remove', 'api_torrent_link_add', 'api_torrent_link_remove']:
        setattr(ns, _name, getattr(_mod, _name))
