# -*- coding: utf-8 -*-
"""下载到本机（短时票据鉴权）。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request, send_file

from api._ctx import core

bp = Blueprint("files", __name__)



@bp.post("/api/files/ticket")
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
    if len(items) > core.DL_TICKET_MAX:
        return jsonify({"error": f"一次最多下载 {core.DL_TICKET_MAX} 个文件"}), 400

    tickets, skipped = [], []
    for it in items:
        # 形态 1: 种子下载(按 hash 枚举已完成文件, 后端走 qBittorrent)
        h = it.get("hash")
        if h:
            entries, hskip = core._torrent_completed_files([h])
            skipped.extend(hskip)
            h = str(h).strip().lower()
            for kind, val in entries:
                # DATA_DIR 内的走 _rel; 目录外(外部 qB 挂载形态)的走 _abs,
                # _abs 票据在下载时还会再对照 qB 报告复核一次, 防伪路径
                tk, sk = (core._issue_ticket_for("_abs", h, val) if kind == "abs"
                          else core._issue_ticket_for("_rel", "", val))
                if tk is not None:
                    tickets.append(tk)
                else:
                    skipped.append(sk)
            continue
        # 形态 2: 相对路径(种子/手动放入的任意文件, 不依赖 catalog)
        rel = it.get("rel")
        if rel:
            tk, sk = core._issue_ticket_for("_rel", "", str(rel))
            if tk is not None:
                tickets.append(tk)
            else:
                skipped.append(sk)
            continue
        # 形态 3: catalog 三元组(历史形态, 保持兼容)
        tk, sk = core._issue_ticket_for(
            str(it.get("type") or "").strip(),
            str(it.get("distribution") or "").strip(),
            str(it.get("filename") or "").strip())
        if tk is not None:
            tickets.append(tk)
        else:
            skipped.append(sk)

    if tickets:
        user = core._valid_session() or ("token" if core.AUTH_TOKEN else "匿名")
        core.log(f"[下载] {user} 请求下载 {len(tickets)} 个文件到本机")
    return jsonify({"ok": bool(tickets), "tickets": tickets, "skipped": skipped})


@bp.get("/api/files/get")
def api_files_get():
    """凭票据下发文件(支持 Range, 因此浏览器可暂停/续传)。

    票据**可重复使用直到过期** —— 断点续传/连接重试会让浏览器对同一 URL 发多次
    请求(带 Range 头), 一次性票据会直接掐断续传。代价是票据在 TTL 内可重放,
    所以 TTL 默认只有 10 分钟。
    """
    tok = request.args.get("t", "")
    core._dl_tickets_purge()
    hit = core._dl_tickets.get(tok) if tok else None
    if not hit:
        return jsonify({"error": "下载链接无效或已过期, 请在面板上重新点击下载"}), 403
    typ, name, fname, _exp = hit
    # 二次校验: 票据里存的是 (type, distro, 文件名) 分量而不是路径, 必须重新过一遍约束
    fp = core._resolve_dl_target(typ, name, fname)
    if fp is None:
        return jsonify({"error": f"文件不存在: {fname}"}), 404
    # P3 修复: download_name 必须是**文件名**, 不能直接用票据里的 fname ——
    # _abs 寻址时 fname 是容器内绝对路径(/downloads/xxx.iso), 直接塞进
    # Content-Disposition 会变成 "/downloads/xxx.iso" 这种带路径的怪名字,
    # 部分浏览器/下载器还会拒绝。用解析后真实文件的 .name。
    resp = send_file(fp, as_attachment=True, download_name=fp.name, conditional=True)
    # 大文件必须允许浏览器缓存分片, 否则续传会从头开始(no-store 是续传杀手)。
    # 该路径同时被 after_request 的 no-cache 规则排除, 见 no_cache_api。
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['api_files_ticket', 'api_files_get']:
        setattr(ns, _name, getattr(_mod, _name))
