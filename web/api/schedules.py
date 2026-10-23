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

import re

from api._ctx import core

bp = Blueprint("schedules", __name__)

# P3 修复: 这两个白名单是 app._sched_matches() 真正认得的值。
# 此前接口把 type/time 原样落盘, `type=every_second` 或 `time=99:99` 都能存进去 ——
# 前端下拉限制只是"看起来有效", 直接打接口就能造出一条永远不触发的任务,
# 而界面上看不出任何异常(调度器只是永远不命中)。这类脏数据只能靠人翻 settings.json 才找得到。
SCHEDULE_TYPES = ("daily", "weekly", "monthly", "yearly", "once")
# 只接受 24 小时制的 HH:MM。允许 "2:00"? 不允许 —— type=time 的输入框只会给出补零值,
# 而 2:5 / 02:5 这类半补零写法在 split(":") 后虽能 int(), 却会让用户对"到底存了什么"产生分歧。
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@bp.get("/api/schedules")
def api_schedules():
    return jsonify({"schedules": core.load_schedules()})


@bp.post("/api/schedules")
def api_schedules_add():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip() or "定时任务"
    typ = body.get("type") or "daily"
    if typ not in SCHEDULE_TYPES:
        return jsonify({"error": "无效的任务类型 %r, 可选: %s"
                                 % (typ, ", ".join(SCHEDULE_TYPES))}), 400
    # 缺省时间沿用旧行为(00:00); 但给了就必须合法 —— 空串也按缺省处理,
    # 别把 "" 存进配置(调度器会 split 失败然后静默按 00:00 算, 与用户理解不符)。
    raw_time = body.get("time")
    time_str = "00:00" if raw_time in (None, "") else str(raw_time).strip()
    if not _TIME_RE.match(time_str):
        return jsonify({"error": "时间必须为 HH:MM(00:00-23:59), 收到 %r" % (raw_time,)}), 400
    scheduled_at = core._as_int(body.get("scheduled_at") or 0, 0, 0)
    if typ == "once" and scheduled_at <= 0:
        # 一次性任务没有执行时刻 = 永远不会触发(调度器判 scheduled_at 为真才动手)
        return jsonify({"error": "一次性任务必须指定有效的执行时间(scheduled_at)"}), 400
    sched = {
        "id": body.get("id") or core._sched_id(),
        "name": name,
        "type": typ,
        "time": time_str,
        "day_of_week": core._as_int(body.get("day_of_week", 0) or 0, 0, 0, 6),
        "day_of_month": core._as_int(body.get("day_of_month", 1) or 1, 1, 1, 31),
        "month_of_year": core._as_int(body.get("month_of_year", 1) or 1, 1, 1, 12),
        "scheduled_at": scheduled_at,  # 一次性调度用的时间戳
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
