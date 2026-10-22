# -*- coding: utf-8 -*-
"""用户登录 / 会话 token / 改密。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request
import os
import secrets
import time

from api._ctx import core

bp = Blueprint("user", __name__)



@bp.get("/api/user/me")
def api_user_me():
    """返回当前登录状态(不强制要求已登录)。附带 require_login/auth_configured 标志供前端判定是否弹登录遮罩。"""
    username = core._valid_session()
    return jsonify({
        "authenticated": bool(username),
        "username": username,
        # B5 修复: 只有强制登录模式下前端才应弹遮罩; 未设 token 时 API 本就敞开, 不应强凑 UX
        "require_login": core.REQUIRE_LOGIN,
        "auth_configured": bool(core.AUTH_TOKEN) or bool(core.REQUIRE_LOGIN),
    })


@bp.post("/api/user/login")
def api_user_login():
    body = request.get_json(force=True, silent=True) or {}
    u = (body.get("username") or "").strip()
    p = body.get("password") or ""
    # P3 修复: 登录失败限速 —— 此前 /api/user/login 完全没有节流, 公网实例可被
    # 无限次撞库。策略刻意温和(连续 5 次失败锁 5 分钟, 成功即清零), 只挡暴力枚举,
    # 不惩罚手误; 计数只在内存, 容器重启归零。
    ip = core._client_ip()
    wait = core._login_lock_remaining(ip)
    if wait > 0:
        return jsonify({"error": f"登录尝试过于频繁，请 {wait} 秒后再试",
                        "code": "rate_limited", "retry_after": wait}), 429
    # 首次登录且尚未播种: 允许用 ISO_HUB_ADMIN_USER/PASS 播种
    if not core.load_users() and not (os.environ.get("ISO_HUB_ADMIN_USER", "").strip() or ""):
        # 无可播种账号, 允许首个登录者创建管理员(设置页用相同用户名/密码建账号)
        # H1: 判定收敛到内核的 _bootstrap_denied_reason() —— 由它统一处理
        # "本机/局域网 vs 公网" 与 "反代是否被信任" 两件事。此前这里是裸的
        # `remote_addr not in ("127.0.0.1", "::1", "localhost")`, 既挡掉了整个
        # 局域网, 又在反代部署下把公网客户端误判成本机。
        reason = core._bootstrap_denied_reason()
        if reason:
            return jsonify({"error": reason, "code": "bootstrap_denied"}), 403
        if not u or not p:
            return jsonify({"error": "缺少用户名或密码"}), 400
        salt = secrets.token_hex(16)
        users = core.load_users()
        users[u] = {"password_hash": core._hash_pw(p, salt), "salt": salt,
                    "iterations": core.PBKDF2_ITERATIONS,
                    "created_at": int(time.time())}
        core.save_users(users)
        core.log(f"[用户] 首次创建管理员账号: {u}")
        core._login_note_ok(ip)
        return jsonify({"ok": True, "token": core._issue_token(u), "username": u})
    v = core._verify_login(u, p)
    if v != "ok":
        core._login_note_fail(ip)
        # 精确原因只进日志: 回给客户端的是同一句话, 否则登录接口就是一台账号枚举机
        core.log(f"[用户] 登录失败({v}): {u} @ {ip}")
        return jsonify({"error": "用户名或密码错误", "code": "bad_credentials"}), 401
    core._login_note_ok(ip)
    return jsonify({"ok": True, "token": core._issue_token(u), "username": u})


@bp.post("/api/user/logout")
def api_user_logout():
    tok = request.headers.get("X-Auth-Token")
    if tok and tok in core._sessions:
        core._sessions.pop(tok, None)
        core._sessions_persist()
    return jsonify({"ok": True})


@bp.post("/api/user/password")
def api_user_password():
    """修改当前登录用户密码(需会话 token)。"""
    # H3 修复: 用 _valid_session 统一校验(含过期检查), 而非裸 _sessions.get(过期也放行)
    username = core._valid_session()
    if not username:
        return jsonify({"error": "未登录"}), 401
    body = request.get_json(force=True, silent=True) or {}
    old = body.get("old_password") or ""
    nw = body.get("new_password") or ""
    if not nw:
        return jsonify({"error": "新密码不能为空"}), 400
    if not core._check_login(username, old):
        return jsonify({"error": "当前密码错误"}), 401
    salt = secrets.token_hex(16)
    users = core.load_users()
    users[username] = {"password_hash": core._hash_pw(nw, salt), "salt": salt,
                       "iterations": core.PBKDF2_ITERATIONS,
                       "created_at": users.get(username, {}).get("created_at", int(time.time()))}
    core.save_users(users)
    # P3 修复: 改密码 = 把其他设备/已泄露的 token 踢下线; 当前会话保留,
    # 否则用户改完密码会立刻被自己的请求踢出去(前端只看到一次莫名的 401)。
    keep = (request.headers.get("X-Auth-Token") or "").strip()
    revoked = core._revoke_other_sessions(username, keep=keep)
    core.log(f"[用户] {username} 修改了密码, 已吊销其他会话 {revoked} 个")
    return jsonify({"ok": True, "revoked_sessions": revoked})


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['api_user_me', 'api_user_login', 'api_user_logout', 'api_user_password']:
        setattr(ns, _name, getattr(_mod, _name))
