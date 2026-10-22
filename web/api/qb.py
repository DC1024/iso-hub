# -*- coding: utf-8 -*-
"""qBittorrent sidecar 设置。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request
import errs

from api._ctx import core

bp = Blueprint("qb", __name__)



@bp.get("/api/qb/settings")
def api_qb_settings_get():
    """获取 qBittorrent 设置及容器实时四态状态。"""
    qb = core.load_qb_settings()
    # 标记 url 是否被用户显式保存过(区别于默认的内部 sidecar 地址 http://qbittorrent:8080)。
    # 前端据此决定输入框是否回显 url —— 未自定义时留空, 由用户按占位提示自行填写。
    try:
        _raw = core.load_settings_all() if core.SETTINGS_JSON.exists() else {}
        qb["url_saved"] = bool((_raw.get("qb") or {}).get("url"))
    except errs.JSON_READ:
        qb["url_saved"] = False
    # 区分「配套容器」与「外部容器」: 只要 url 未被用户指向外部(仍是默认内部 sidecar 地址,
    # 或从未自定义), iso-hub 就能管理该 qb 容器, 可改凭据并同步到 sidecar; 若用户填了外部
    # qBittorrent 地址, 那是自行部署的容器, iso-hub 无法改其用户名/密码 → managed=False。
    qb["managed"] = not core._qb_is_external(qb)
    # 容器状态只在**配套** sidecar 场景才有意义。外部 QB 是用户自行部署的容器, iso-hub 没有
    # 它可查(查的是同名配套 sidecar, 必然 not_deployed/unknown): 既会污染日志(未接 Docker 的
    # 部署每次访问都抛 FileNotFoundError), 又会让界面误报"请检查 socket-proxy"。
    # 故外部 QB 不查询, 直接标记未部署; 界面看 conn 判断"能否连上并登录"。
    qb["container"] = core.service_state(core.QB_CONTAINER) if qb["managed"] else "not_deployed"
    # 外部 QB 真正要看的是"能否连上并登录这个外部实例" —— 直接探测一次并回传, 面板据此显示
    # 「已连接 / 凭据错误 / 无法连接」, 而不是误导性的"请检查 socket-proxy"。
    qb["conn"] = core._probe_qb_connection(qb) if not qb["managed"] else None
    return jsonify({"ok": True, "qb": core._redact_password(qb)})


@bp.post("/api/qb/settings")
def api_qb_settings_post():
    """保存 qBittorrent 设置并启停/重启容器。body: {enabled?:bool, username?:str, password?:str}"""
    body = request.get_json(force=True, silent=True) or {}
    qb = core.load_qb_settings()
    changed = False
    cred_changed = False
    if "username" in body:
        qb["username"] = str(body["username"]).strip()
        changed = True
        cred_changed = True
    if "password" in body:
        _pw = str(body["password"]).strip()
        if _pw:  # 空 = 不改: 前端密码框留空即沿用已保存的那份
            qb["password"] = _pw
            changed = True
            cred_changed = True
    if "url" in body:
        # 允许用户指向外部 qBittorrent(如自行部署的实例), 而非默认的 sidecar 内部地址
        qb["url"] = str(body["url"]).strip().rstrip("/") or qb.get("url", "")
        changed = True

    # 启用或保持启用时，必须提供非空凭据
    will_be_enabled = bool(body["enabled"]) if "enabled" in body else qb.get("enabled", False)
    if will_be_enabled and (not qb.get("username") or not qb.get("password")):
        return jsonify({"error": "启用 qBittorrent 必须提供非空用户名和密码"}), 400

    # 计算是否指向外部 QB(用户自行部署, 非配套 sidecar)。url_saved 需从原始 settings 判断,
    # 与 GET 保持一致: 用户显式保存过 url 才视为已自定义; 若本次请求带 url 字段同样视为已保存。
    # 必须在 enabled 分支**之前**算好: 外部 QB 启停时不应去操作配套 sidecar 容器。
    try:
        _raw = core.load_settings_all() if core.SETTINGS_JSON.exists() else {}
        qb["url_saved"] = bool((_raw.get("qb") or {}).get("url")) or ("url" in body)
    except errs.JSON_READ:
        qb["url_saved"] = bool("url" in body)
    _is_ext = core._qb_is_external(qb)

    enabled_changed = False
    if "enabled" in body:
        want = bool(body["enabled"])
        if want != qb.get("enabled", False):
            # 外部 QB(用户自行部署): 根本没有 iso-hub 配套的 sidecar 容器可启停。
            # 这里的 enabled 只表示"是否用这个外部 QB 做种子下载", 只落盘开关状态即可,
            # 绝不能去 start/stop QB_CONTAINER —— 那个容器不存在(或不属于 iso-hub),
            # 否则必定失败并误报"容器状态未知(请检查 socket-proxy 是否运行)"。
            # 这与用户预期完全不符: 外部 QB 不需要 socket-proxy, iso-hub 只走 Web API 登录连接。
            if _is_ext:
                core.log(f"[qB] 外部 qBittorrent({qb.get('url')}) 开关 -> enabled={want}, 不操作配套 sidecar 容器")
            else:
                ok = core.set_qb(want, qb["username"], qb["password"])
                if not ok:
                    st = core.service_state(core.QB_CONTAINER)
                    if st == "not_deployed":
                        return jsonify({"error": "qBittorrent 未部署: 请用 docker compose --profile bt up -d 先创建该容器"}), 500
                    if st == "unknown":
                        return jsonify({"error": "qBittorrent 容器状态未知(请检查 socket-proxy 是否运行)"}), 500
                    return jsonify({"error": "qBittorrent 容器操作失败，请检查是否已部署 sidecar"}), 500
            qb["enabled"] = want
            changed = True
            enabled_changed = True

    # 仅当"凭据"变更且当前是启用状态、且是配套 sidecar 时, 才同步密码到 sidecar 容器并重启。
    # 外部 QB(用户自行部署): 改凭据只是为了登录连接, 只保存到配置即可, 不写 conf 不重启
    # (根本没有 iso-hub 配套容器可重启)。
    if cred_changed and qb.get("enabled", False) and not _is_ext:
        if not enabled_changed:
            # 仅修改凭据：直接写 conf 并重启
            if core.QB_CONF_PATH.exists() and core._set_qb_password(qb["username"], qb["password"]):
                rr = core._docker_request("POST", f"/containers/{core.QB_CONTAINER}/restart", None, 30)
                if rr.status not in (200, 204, 304):
                    return jsonify({"error": "qBittorrent 凭据已保存，但重启容器失败"}), 500
            else:
                return jsonify({"error": "qBittorrent 凭据保存失败，请检查 qb-config 是否正确挂载"}), 500
        core.save_qb_settings(qb)
    elif changed:
        core.save_qb_settings(qb)

    # 与 GET 一致: 外部 QB 不查配套 sidecar 容器(查了必然 not_deployed/unknown, 只会污染
    # 日志并让界面误报"请检查 socket-proxy")。复用上面已算好的 _is_ext, 避免重复判断。
    qb["managed"] = not _is_ext
    qb["container"] = core.service_state(core.QB_CONTAINER) if not _is_ext else "not_deployed"
    return jsonify({"ok": True, "qb": core._redact_password(qb)})


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['api_qb_settings_get', 'api_qb_settings_post']:
        setattr(ns, _name, getattr(_mod, _name))
