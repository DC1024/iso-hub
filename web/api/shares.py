# -*- coding: utf-8 -*-
"""网络共享（SMB / WebDAV）设置。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request

from api._ctx import core

bp = Blueprint("shares", __name__)



@bp.get("/api/shares")
def api_shares():
    shares = core.load_shares()
    # 附带每个 sidecar 容器实时四态(running/stopped/not_deployed/unknown)
    for proto, s in shares.items():
        st = core.service_state(core.SHARE_CONTAINERS[proto])
        s["container"] = st
        # managed = 是否 iso-hub 配套部署的容器: 能检测到(非 not_deployed/unknown)即为配套,
        # 凭据可管理; 用户自行部署的外部容器 iso-hub 检测不到, 无法改凭据。
        s["managed"] = st not in ("not_deployed", "unknown")
    return jsonify({"shares": {k: core._redact_password(v) for k, v in shares.items()}})


@bp.post("/api/shares")
def api_shares_save():
    body = request.get_json(force=True, silent=True) or {}
    shares = core.load_shares()
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
            ok = core.apply_share_creds(proto, cur["username"], cur["password"])
            if not ok:
                # L4 修复: 此处尚未 save_shares, 凭据并未落盘 —— 修正与实际不符的错误文案
                return jsonify({"error": f"{proto} 凭据同步到容器失败, 设置未保存, 请检查 sidecar 是否运行"}), 500
        if "enabled" in p:
            # 启停 sidecar 容器; 未部署时给出命令提示(应用读不到宿主 compose 文件, 无法自动创建)
            want = bool(p["enabled"])
            # v1.3.21: 没有密码不允许启用 —— 旧版内置 iso123, 用户常直接开共享忘了改,
            # 等于把整库 ISO 匿名暴露给局域网。这里改成硬性前置条件。
            if want and not str(cur.get("password") or ""):
                return jsonify({"error": f"{proto} 尚未设置密码: 请先在上方填写密码并保存凭据, 再启用共享"}), 400
            cur["enabled"] = want
            ok = core.set_share(proto, want)
            if not ok:
                st = core.service_state(core.SHARE_CONTAINERS[proto])
                if st == "not_deployed":
                    return jsonify({"error": f"{proto} 未部署: 请用 docker compose --profile share up -d 先创建该容器"}), 500
                if st == "unknown":
                    return jsonify({"error": f"{proto} 容器状态未知(请检查 socket-proxy 是否运行)"}), 500
                return jsonify({"error": f"{proto} 容器操作失败(是否已部署 sidecar?)"}), 500
    core.save_shares(shares)
    core.log(f"[共享] 设置已保存: SMB={shares['samba']['enabled']} WebDAV={shares['webdav']['enabled']}")
    return jsonify({"ok": True, "shares": {
        k: {**core._redact_password(v), "container": (_st := core.service_state(core.SHARE_CONTAINERS[k])),
            "managed": _st not in ("not_deployed", "unknown")}
        for k, v in shares.items()}})


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['api_shares', 'api_shares_save']:
        setattr(ns, _name, getattr(_mod, _name))
