#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ISO Hub - GPG 验证状态账本(P1-⑤b) —— 应用自证「每条发行版到底验没验过」。

背景:
    外部守卫(spy 断言/日志扫描/变异测试)能证明"代码层接线存在",
    但回答不了"运行层某条发行版是否真的获得过验签机会"。典型盲区:
      * 某发行版 URL 长期 404 -> 每次下载都在校验和之前失败, GPG 从未执行
      * 新增配置字段未被迁移覆盖 -> 该条目 gpg_verify 静默丢失
    这些情况下所有测试仍然全绿。唯一可靠的机制是应用自己记账:
      每次执行 verify_checksum_smart 且条目配置了 gpg_verify,
      就在 DATA_DIR/gpg_ledger.json 落一条 {key: {"state", "ts"}}。
    /api/health 汇总 configured vs recorded, 差集即 never_invoked。

设计约束:
    * 账本是**共享卷上的文件**而非内存 -- iso_runner/sync_subscriptions
      都是子进程, 只有文件能跨进程汇总。
    * record 的任何异常都必须吞掉 -- 账本是观测设施, 绝不影响下载主流程。
    * fail 也算"有过机会"(invoked); 只有从未出现在账本里的才是 never_invoked。
"""
import json
import os
import time
from pathlib import Path

LEDGER_FILENAME = "gpg_ledger.json"
STATES = ("pass", "fail", "skip")


def data_dir() -> Path:
    return Path(os.environ.get("ISO_DATA_DIR", "/data"))


def ledger_path() -> Path:
    return data_dir() / LEDGER_FILENAME


def key_for(entry: dict) -> str:
    """条目唯一键: type/name[/version或URL文件名片段]。

    真实数据里 gpg_verify 条目常无 version 字段(镜像组预设靠 download_url
    区分不同发行版/版本), 若只退化成 type/name 会把多条合并成一个键,
    账本粒度变粗、低估覆盖。故按优先级取区分片段:
      version > download_url 路径文件名 > checksum_url 路径文件名
    """
    typ = str(entry.get("type", "linux"))
    name = str(entry.get("distribution") or entry.get("name") or "?")
    base = f"{typ}/{name}"
    ver = entry.get("version")
    if ver:
        return f"{base}@{ver}"
    for key in ("download_url", "checksum_url"):
        u = entry.get(key)
        if isinstance(u, str) and u.startswith("http"):
            frag = u.rstrip("/").split("/")[-1]
            if frag and frag not in (".", ".."):
                return f"{base}#{frag}"
    return base


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_from_msg(entry: dict, ok: bool, msg: str, path=None) -> str | None:
    """按一次校验结果给 entry 记账, 返回落账状态(未配置 gpg_verify 返回 None)。

    分类规则(与 download_linux 日志三态一致):
      ok=False                -> fail(验签执行了且拒绝)
      ok=True 且 msg 含"跳过"  -> skip(降级放行)
      其他 ok=True            -> pass
    任何异常都吞掉: 账本绝不影响下载主流程。
    """
    try:
        if not entry or not entry.get("gpg_verify"):
            return None
        state = "pass" if ok else "fail"
        if ok and "跳过" in (msg or ""):
            state = "skip"
        p = Path(path) if path else ledger_path()
        data = _load(p)
        data[key_for(entry)] = {"state": state, "ts": int(time.time())}
        _save(p, data)
        return state
    except Exception:  # noqa: BLE001
        return None


def build_summary(json_path=None, path=None) -> dict:
    """汇总账本: configured(配置了 gpg_verify) vs recorded(账本里有记录)。

    never_invoked = 配置了验签但账本里从未出现过的条目键列表。
    任何读取异常都吞掉, 保证 /api/health 永远 200。
    """
    jp = Path(json_path) if json_path else data_dir() / "distributions.json"
    lp = Path(path) if path else ledger_path()
    configured, states, recorded = [], {s: 0 for s in STATES}, set()
    try:
        cfg = json.loads(jp.read_text(encoding="utf-8")) or {}
        configured = [key_for(e) for e in cfg.get("distributions", [])
                      if e.get("gpg_verify")]
    except Exception:  # noqa: BLE001
        pass
    for k, rec in _load(lp).items():
        recorded.add(k)
        st = (rec or {}).get("state")
        if st in states:
            states[st] += 1
    return {
        "configured": len(configured),
        "states": states,
        "never_invoked": [k for k in configured if k not in recorded],
    }
