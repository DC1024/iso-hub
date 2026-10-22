# -*- coding: utf-8 -*-
"""页面与主流程（下载、清理、保护名单、订阅、可写自检等）。

v1.7.0 从 app.py 按域迁出，只做搬移不改行为。两条铁律：

1. 内核符号一律 `core.名字` 现查，**不要** `from app import 名字`，
   也**不要**写模块级快照 `X = core.X` —— 那样 `patch.object(app, 'X')`
   会静默失效（测试还绿，但桩没打上）。原因见 api/_ctx.py 顶部注释。
2. 路径与鉴权都在内核里，这里不碰：`require_auth` / `no_cache_api`
   仍是 app.py 的 before_request / after_request 钩子，对蓝图路由一视同仁。
"""
from flask import Blueprint, jsonify, request, send_from_directory
import json
import gpg_ledger

from api._ctx import core

bp = Blueprint("misc", __name__)



@bp.get("/")
def index():
    # 禁用缓存, 防止前端更新后浏览器仍加载旧 index.html(导致输入框缺失/交互失效)
    resp = send_from_directory(core.app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@bp.get("/api/health")
def api_health():
    # P1-⑤b: 暴露 GPG 状态账本 —— never_invoked 非空 = 有发行版从未获得验签机会
    # v1.5.1: 传 download_dir, 让摘要能区分"盘上有文件却没验签"与"根本没下载"。
    # v1.5.0: 暴露各挂载点的可写性 —— 非 root 运行后, "属主配错"的典型症状就是
    # writable.data=false。字段是诊断用, 不影响 ok(写不了盘时服务本身确实是活的,
    # 让容器被判 unhealthy 后重启并不能解决属主问题, 只会掩盖日志)。
    # v1.5.2: writable 是布尔视图, writable_states 给出 ok/missing/denied/not_configured,
    # 用于区分"卷没挂"与"属主不对" —— 两者的处置方式完全不同。
    return jsonify({"ok": True,
                    "gpg_ledger": gpg_ledger.build_summary(core.JSON_FILE, download_dir=core.DATA_DIR),
                    "writable": core.writable_snapshot(),
                    "writable_states": core.writable_states()})


@bp.get("/api/distros")
def api_distros():
    return jsonify(core.build_distros())


@bp.get("/api/state")
def api_state():
    t = core.running_task()
    return jsonify({"running": t is not None, "task": t})


@bp.get("/api/logs")
def api_logs():
    # L1 修复: 非数字 after 参数兜底为 0, 不再抛 500
    try:
        after = int(request.args.get("after", 0))
    except (ValueError, TypeError):
        after = 0
    with core._lock:
        lines = [x for x in core._log_lines if x["i"] > after]
        final_seq = core._log_seq
    return jsonify({"after": final_seq, "lines": lines})


@bp.post("/api/download")
def api_download():
    body = request.get_json(force=True, silent=True) or {}
    entries = body.get("entries") or []
    if not entries:
        return jsonify({"error": "没有选择任何条目"}), 400
    # 重新从当前清单校验，防止提交伪造数据
    # 前端每条目传 {distribution, download_url}; download_url 是用户选定的源URL
    # (可为主源 download_url, 也可为 download_urls 里的某个镜像), 据此匹配对应条目
    cur = core.load_json()
    wanted = {(e.get("distribution"), e.get("download_url")) for e in entries}
    cand_index = {}   # (distribution, 任意候选url) -> entry
    for e in cur.get("distributions", []):
        cand_index[(e["distribution"], e["download_url"])] = e
        for c in e.get("download_urls", []):
            cand_index[(e["distribution"], c)] = e
    matched = [cand_index[w] for w in wanted if w in cand_index]
    if not matched:
        return jsonify({"error": "所选条目不在当前发行版清单中，请先刷新列表"}), 400

    # 用户对某个文件手动指定的源URL(不存在则空 = 跟随全局策略自动选)
    chosen = {e.get("distribution"): e.get("download_url", "") for e in entries}
    download_payload = []
    seen = set()
    for e in matched:
        fname = e["download_url"].rstrip("/").rsplit("/", 1)[-1]
        # 路径穿越防护: 通过白名单校验确保目标目录在 DATA_DIR 内
        target = core._safe_join(e.get("type", "linux"), e.get("distribution", ""))
        if target is None:
            return jsonify({"error": f"非法的发行版类型/名称: {e.get('type')}/{e.get('distribution')}"}), 400
        # 下载期间字节写在 <最终名>.part 上(iso_runner 的 .part 原子落盘协议),
        # 且 #TARGET 哨兵上报的也是 .part 路径。这里必须同样用 .part 路径,
        # 才能让 running_task() 的 stat(size) 与 targets 的 key 对得上,
        # 否则进度条永远 0%(size=0 且 total 取不到)。
        path = str(target / (fname + core.PART_SUFFIX))
        if (e["distribution"], fname) not in seen:
            seen.add((e["distribution"], fname))
            download_payload.append({"filename": fname, "path": path})

    select_json = json.dumps(
        [{"distribution": e["distribution"], "download_url": e["download_url"],
          "pin": chosen.get(e["distribution"]) or ""} for e in matched],
        ensure_ascii=False,
    )
    cmd = [
        core.PY, str(core.BASE_DIR / "iso_runner.py"),
        "--json-file", str(core.JSON_FILE),
        "--download-dir", str(core.DATA_DIR),
        "--select", select_json,
        "--strategy", core.load_source_strategy(),
        "--storage-mode", core.load_storage_mode(),
    ]
    names = sorted({e["distribution"] for e in matched})
    ok = core.start_task("download", f"下载: {'、'.join(names)}（{len(matched)} 个文件）", cmd, download_payload)
    if not ok:
        return jsonify({"error": "已有任务在运行"}), 409
    return jsonify({"ok": True, "files": len(matched)})


@bp.post("/api/update-meta")
def api_update_meta():
    cmd = [
        core.PY, str(core.REPO_DIR / "update_distributions.py"),
        "--config", str(core.REPO_DIR / "sources_config.json"),
        "--output", str(core.JSON_FILE),
        "--pretty",
    ]
    ok = core.start_task("meta", "抓取镜像站，刷新发行版清单元数据", cmd)
    if not ok:
        return jsonify({"error": "已有任务在运行"}), 409
    # 自定义源独立存储，不随 update-meta 覆盖；UI 读取时再合并
    return jsonify({"ok": True})


@bp.get("/api/source-strategy")
def api_source_strategy_get():
    return jsonify({"strategy": core.load_source_strategy()})


@bp.post("/api/source-strategy")
def api_source_strategy_set():
    body = request.get_json(force=True, silent=True) or {}
    s = (body.get("strategy") or "A").strip().upper()
    if s not in ("A", "B"):
        return jsonify({"error": "strategy 只能是 A 或 B"}), 400
    core.save_source_strategy(s)
    core.log(f"[设置] 选源策略改为 {s}")
    return jsonify({"ok": True, "strategy": s})


@bp.get("/api/storage-mode")
def api_storage_mode_get():
    """读取 ISO 存放模式。flat 模式下附带统一目录的绝对路径, 供 UI 提示用户去哪里找文件。"""
    mode = core.load_storage_mode()
    return jsonify({"mode": mode,
                    "dir": str(core._flat_iso_dir()) if mode == "flat" else ""})


@bp.post("/api/storage-mode")
def api_storage_mode_set():
    """切换 ISO 存放模式。只影响**之后**的新下载, 已有文件不会迁移。"""
    body = request.get_json(force=True, silent=True) or {}
    mode = str(body.get("mode") or "").strip().lower()
    if mode not in core.STORAGE_MODES:
        return jsonify({"error": f"mode 只能是 {' 或 '.join(core.STORAGE_MODES)}"}), 400
    core.save_storage_mode(mode)
    core.log(f"[设置] ISO 存放模式改为 {mode}"
        + ("(统一目录 %s, 仅对新下载生效)" % core._flat_iso_dir() if mode == "flat" else
           "(按类型/发行版分类, 仅对新下载生效)"))
    return jsonify({"ok": True, "mode": mode})


@bp.get("/api/subscriptions")
def api_subscriptions():
    return jsonify({"subscriptions": core.load_subscriptions()})


@bp.post("/api/subscriptions")
def api_subscriptions_save():
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("subscriptions") or []
    core.save_subscriptions(items)
    core.log(f"[订阅] 保存 {len(items)} 条订阅配置")
    return jsonify({"ok": True})


@bp.post("/api/sync-subscriptions")
def api_sync_subscriptions():
    """对所有已启用订阅执行: 刷新清单 -> 下载最新 N -> 删旧版。"""
    if core.running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    subs = [s for s in core.load_subscriptions() if s.get("enabled", True)]
    if not subs:
        return jsonify({"error": "没有已启用的订阅"}), 400
    sync_cmd = core._run_sync_cmd()
    ok = core.start_task(
        "sync",
        f"订阅同步: {len(subs)} 个发行版(自动拉最新+删旧)",
        sync_cmd,
        [],
    )
    if not ok:
        return jsonify({"error": "已有任务在运行"}), 409
    return jsonify({"ok": True, "count": len(subs)})


@bp.post("/api/prune")
def api_prune():
    body = request.get_json(force=True, silent=True) or {}
    name, typ = body.get("distribution"), body.get("type")
    if not name or not typ:
        return jsonify({"error": "缺少 distribution/type"}), 400
    # H1 修复: 路径穿越防护, target 必须安全地落在 DATA_DIR 内
    target = core._safe_join(typ, name)
    if target is None:
        return jsonify({"error": "非法的 distribution/type 参数"}), 400
    if core.running_task():
        return jsonify({"error": "已有任务在运行"}), 409
    cur = core.load_json()
    # B7 修复: 记录当前清单文件名到历史(累积), 供清理时判断哪些文件"曾经属于清单"
    core._record_manifest_history(cur.get("distributions", []))
    expected = {
        e["download_url"].rstrip("/").rsplit("/", 1)[-1]
        for e in cur.get("distributions", [])
        if e.get("distribution") == name and e.get("type") == typ
    }
    removed, skipped = [], []
    protected = set(core.load_protected())
    if target.exists():
        for f in target.iterdir():
            if f.is_file() and f.name not in expected and f.suffix.lower() in core.ISO_SUFFIXES:
                rel = f"{typ}/{name}/{f.name}"
                if rel in protected or f.name in protected:
                    skipped.append(f"{f.name}: 受保护, 跳过")
                    continue
                # B7 修复: 只删除"曾出现在历史清单中"的文件, 保护用户手动放入/种子下载的 ISO
                if not core._is_known_file(typ, name, f.name):
                    skipped.append(f"{f.name}: 非清单文件, 保留")
                    continue
                try:
                    f.unlink()
                    removed.append(f.name)
                except OSError as e:
                    skipped.append(f"{f.name}: {e}")
    core.log(f"[清理] {typ}/{name}: 删除 {len(removed)} 个过期文件，跳过 {len(skipped)}")
    return jsonify({"ok": True, "removed": removed, "skipped": skipped})


@bp.post("/api/delete-files")
def api_delete_files():
    """删除清单内已下载的 ISO 文件。

    请求体: {"items": [{"type": "linux", "distribution": "Ubuntu", "filename": "xxx.iso"}, ...]}

    安全约束(逐条对应):
      1. 路径穿越: (type, distribution) 经 _safe_join 校验, 必须落在 DATA_DIR 内
      2. 文件名: 拒绝空/含分隔符/为 . 或 ..; 且最终路径必须仍在目标目录内
      3. 只删清单内文件: 文件名必须属于该发行版**当前清单**(expected), 否则拒绝
         —— 防止误删用户手动放入的 ISO; 若要删除非清单文件请用「清理过期」接口
      4. **受保护(🔒锁定)文件一律拒绝删除**: 无任何绕过参数。锁定优先级高于手动删除,
         用户必须先点解锁按钮才可删除。命中时以 locked 列表单独返回, 供前端给出明确指引。
      5. 任务互斥: 有下载任务在跑时拒绝, 避免边下边删造成状态错乱
    """
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items") or []
    if not items:
        return jsonify({"error": "没有选择任何文件"}), 400
    if core.running_task():
        return jsonify({"error": "已有任务在运行, 请稍后再试"}), 409

    cur = core.load_json()
    # 按 (type, distribution) 归集当前清单里的合法文件名
    expected: dict[tuple[str, str], set[str]] = {}
    for e in cur.get("distributions", []):
        url = e.get("download_url", "") or ""
        fname = url.rstrip("/").rsplit("/", 1)[-1]
        if not fname:
            continue
        expected.setdefault((e.get("type", "linux"), e.get("distribution", "")), set()).add(fname)

    protected = set(core.load_protected())
    removed, skipped, locked = [], [], []
    for it in items:
        typ = str(it.get("type") or "").strip()
        name = str(it.get("distribution") or "").strip()
        fname = str(it.get("filename") or "").strip()
        label = f"{fname or '(空文件名)'}"
        if not fname or "/" in fname or "\\" in fname or fname in (".", ".."):
            skipped.append(f"{label}: 非法文件名")
            continue
        target = core._safe_join(typ, name)
        if target is None:
            skipped.append(f"{label}: 非法的 type/distribution")
            continue
        # 只允许删除当前清单内声明的文件(防误删用户自有 ISO)
        if fname not in expected.get((typ, name), set()):
            skipped.append(f"{label}: 不在当前清单内, 已拒绝(如需清理请用「清理过期」)")
            continue
        rel = f"{typ}/{name}/{fname}"
        # 锁定文件: 硬拒绝, 无 force 后门
        if rel in protected or fname in protected:
            locked.append({"type": typ, "distribution": name, "filename": fname})
            continue
        # 前端传的是"目标文件名"(如 xxx.iso), 但下载中的半成品实际叫
        # xxx.iso.part。删除必须连带处理半成品: 否则"下载停止"的文件点删除会
        # 报"文件不存在"被跳过(用户明明看得到它占着空间)。
        fp = (target / fname)
        part_fp = target / (fname + core.PART_SUFFIX)
        # 二次确认最终路径仍在目标目录内(防 symlink / 拼接绕过)
        for _p in (fp, part_fp):
            try:
                _p.resolve().relative_to(target.resolve())
            except ValueError:
                skipped.append(f"{label}: 路径越界, 已拒绝")
                break
        else:
            victims = [p for p in (fp, part_fp) if p.is_file()]
            if not victims:
                skipped.append(f"{label}: 文件不存在")
                continue
            try:
                for _p in victims:
                    _p.unlink()
                # 半成品被删掉后, 残留的"下载停止"失败记录也要清掉,
                # 否则 UI 仍会显示「下载停止」而文件已经没了。
                if part_fp in victims:
                    core.clear_failure(f"{typ}/{name}/{fname}")
                removed.append(fname + (core.PART_SUFFIX if fp not in victims else ""))
            except OSError as e:
                skipped.append(f"{label}: {e}")

    ok = not locked
    for lk in locked:
        skipped.append(f"{lk['filename']}: 文件已被锁定, 已跳过")
    resp = {"ok": ok, "removed": removed, "skipped": skipped, "locked": locked}
    if locked:
        resp["error"] = f"{len(locked)} 个文件已被锁定"
    return jsonify(resp)


@bp.get("/api/protected")
def api_protected():
    lst = core.load_protected()
    return jsonify({"protected": lst})


@bp.post("/api/protected")
def api_protected_toggle():
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path")  # 相对路径 type/name/文件名 或 文件名
    if not path:
        return jsonify({"error": "缺少 path"}), 400
    lst = core.load_protected()
    if path in lst:
        lst = [p for p in lst if p != path]
        removed = True
    else:
        lst.append(path)
        removed = False
    core.save_protected(lst)
    return jsonify({"ok": True, "protected": lst, "removed": removed})


@bp.post("/api/stop")
def api_stop():
    return jsonify({"ok": core.stop_task()})


@bp.post("/api/verify")
def api_verify():
    """对已落定的文件补一次校验(不重新下载), v1.4.0。

    背景: 校验源不可达但长度达标时文件会被落定(避免每周重下整包), 并在
    download_unverified.json 留一条记录 → UI 显示「未验签」。旧版清除该标记的
    **唯一**路径是整文件重下, 对 2~6GB 的 ISO 代价过高, 用户实际不会点。
    这里复用 iso_runner 的 --verify-only 分支: 后台跑一次 verify_checksum_smart,
    通过则清记录, 不符则记 state=mismatch, 仍不可达则更新原因。

    为什么走 start_task 而不是同步请求: 校验要顺序读完整个文件, 机械盘上可能
    几十秒到两分钟; 且与下载互斥能避免"读盘时文件正被 .part 改写"与 gpg keyring
    并发写。复用同一把"单任务"锁, 语义与下载/元数据刷新一致。
    """
    body = request.get_json(force=True, silent=True) or {}
    rel = str(body.get("rel") or "").strip()
    parts = rel.split("/")
    if len(parts) != 3 or any(
        (not p) or p != p.strip() or p in (".", "..") or "/" in p or "\\" in p
        for p in parts
    ):
        return jsonify({"error": "非法的文件标识"}), 400
    typ, name, fname = parts
    if fname.lower().endswith(core.PART_SUFFIX):
        return jsonify({"error": "半成品(.part)不在校验范围内, 请先完成下载"}), 409

    entry = None
    for e in core.load_json().get("distributions", []):
        if e.get("type", "linux") != typ or e.get("distribution") != name:
            continue
        url = e.get("download_url") or ""
        if url.rstrip("/").rsplit("/", 1)[-1] == fname:
            entry = e
            break
    if entry is None:
        return jsonify({"error": "发行版清单里找不到该文件, 请先刷新列表"}), 404
    # 前置守卫: verify_checksum_smart 在 checksum_url 与 checksum 都为空时返回
    # (True, "跳过校验和验证（无可用信息）") —— 那是下载路径"没信息就放行"的历史语义。
    # 作为"重新校验"的结果会把"没配校验源"冒充成"校验通过", 必须在这里挡成 400。
    if not (entry.get("checksum_url") or entry.get("checksum")):
        return jsonify({
            "error": "该发行版没有配置校验源(checksum_url / checksum), 无法校验; "
                     "请先在自定义源里补充后再试"
        }), 400

    target = core._safe_join(typ, name)
    if target is None:
        return jsonify({"error": f"非法的发行版类型/名称: {typ}/{name}"}), 400
    if not (target / fname).is_file():
        return jsonify({"error": "文件不存在或不完整, 无法校验"}), 409

    cmd = [
        core.PY, str(core.BASE_DIR / "iso_runner.py"),
        "--json-file", str(core.JSON_FILE),
        "--download-dir", str(core.DATA_DIR),
        "--storage-mode", core.load_storage_mode(),
        "--verify-only",
        "--rel", rel,
    ]
    ok = core.start_task("verify", f"重新校验: {fname}", cmd)
    if not ok:
        return jsonify({"error": "已有任务在运行, 请稍后再试"}), 409
    core.log(f"[校验] 开始重新校验: {rel}")
    return jsonify({"ok": True, "rel": rel})


def install(flask_app, ns):
    """注册本域蓝图，并把视图回填到内核模块命名空间（`app.api_xxx` 写法继续可用）。

    flask_app 是 Flask 实例，ns 是 app 模块 —— 分开传，别混。
    """
    flask_app.register_blueprint(bp)
    _mod = __import__(__name__, fromlist=["_"])
    for _name in ['index', 'api_health', 'api_distros', 'api_state', 'api_logs', 'api_download', 'api_update_meta', 'api_source_strategy_get', 'api_source_strategy_set', 'api_storage_mode_get', 'api_storage_mode_set', 'api_subscriptions', 'api_subscriptions_save', 'api_sync_subscriptions', 'api_prune', 'api_delete_files', 'api_protected', 'api_protected_toggle', 'api_stop', 'api_verify']:
        setattr(ns, _name, getattr(_mod, _name))
