# -*- coding: utf-8 -*-
"""邮件通知配置与测试发送。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request
import notifier

from api._ctx import core

bp = Blueprint("notify", __name__)



@bp.get("/api/notify")
def api_notify_get():
    """读取邮件通知配置。**密码不回传**, 只用 password_set 告诉 UI 有没有配过。"""
    cfg = core.load_notify_config()
    return jsonify({"config": notifier.redact(cfg),
                    "ready": notifier.is_ready(cfg),
                    "missing": notifier.missing_fields(cfg)})


@bp.post("/api/notify")
def api_notify_set():
    """保存邮件通知配置。局部提交即可(只传要改的字段), 密码留空表示不改。"""
    body = request.get_json(force=True, silent=True) or {}
    cfg = core.save_notify_config(core.notify_cfg_from_body(body))
    core.log(f"[设置] 邮件通知已更新(启用={cfg['enabled']}, "
        f"服务器={cfg['smtp_host']}:{cfg['smtp_port']}/{cfg['security']})")
    return jsonify({"ok": True, "config": notifier.redact(cfg),
                    "ready": notifier.is_ready(cfg),
                    "missing": notifier.missing_fields(cfg)})


@bp.post("/api/notify/test")
def api_notify_test():
    """发一封测试邮件(**不落盘**)——先验证凭据能不能通, 再决定要不要保存。

    正文用表单里当前的值; 密码留空时沿用已保存的那份。
    """
    body = request.get_json(force=True, silent=True) or {}
    submitted = body.get("config") if isinstance(body.get("config"), dict) else body
    cfg = core.notify_cfg_from_body(submitted)
    subject, text = notifier.build_test_mail(
        "%s:%s (%s)" % (cfg["smtp_host"], cfg["smtp_port"], cfg["security"]))
    ok, detail = notifier.send(cfg, subject, text)
    core.log("[邮件] 测试邮件已发送" if ok else "[邮件] 测试邮件发送失败: " + detail)
    return (jsonify({"ok": ok, "detail": detail}), 200 if ok else 400)


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['api_notify_get', 'api_notify_set', 'api_notify_test']:
        setattr(ns, _name, getattr(_mod, _name))
