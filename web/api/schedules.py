# -*- coding: utf-8 -*-
"""用户自建定时任务。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request

from api._ctx import core

bp = Blueprint("schedules", __name__)



@bp.get("/api/schedules")
def api_schedules():
    return jsonify({"schedules": core.load_schedules()})


@bp.post("/api/schedules")
def api_schedules_add():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip() or "定时任务"
    typ = body.get("type") or "daily"
    sched = {
        "id": body.get("id") or core._sched_id(),
        "name": name,
        "type": typ,
        "time": body.get("time") or "00:00",
        "day_of_week": core._as_int(body.get("day_of_week", 0) or 0, 0, 0, 6),
        "day_of_month": core._as_int(body.get("day_of_month", 1) or 1, 1, 1, 31),
        "month_of_year": core._as_int(body.get("month_of_year", 1) or 1, 1, 1, 12),
        "scheduled_at": core._as_int(body.get("scheduled_at") or 0, 0, 0),  # 一次性调度用的时间戳
        "enabled": bool(body.get("enabled", True)),
        "last_run": 0,
    }
    lst = core.load_schedules()
    lst = [x for x in lst if x.get("id") != sched["id"]]  # 更新或新增
    lst.append(sched)
    core.save_schedules(lst)
    return jsonify({"ok": True, "schedules": lst})


@bp.delete("/api/schedules")
def api_schedules_del():
    sid = request.args.get("id")
    if not sid:
        return jsonify({"error": "缺少 id"}), 400
    lst = [x for x in core.load_schedules() if x.get("id") != sid]
    core.save_schedules(lst)
    return jsonify({"ok": True, "schedules": lst})


@bp.post("/api/schedules/run")
def api_schedules_run():
    """手动触发一次订阅同步。"""
    if core.running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    cmd = core._run_sync_cmd()
    if not cmd:
        return jsonify({"error": "无启用的订阅"}), 400
    n = len([s for s in core.load_subscriptions() if s.get("enabled", True)])
    core.start_task("sync", f"手动订阅同步: {n} 个发行版", cmd, [])
    return jsonify({"ok": True})


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['api_schedules', 'api_schedules_add', 'api_schedules_del', 'api_schedules_run']:
        setattr(ns, _name, getattr(_mod, _name))
