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
    * v1.5.1 起 never_invoked 只点名**盘上有文件**的条目: 清单里有、硬盘上
      根本没有该 ISO 的条目归 not_downloaded。否则一份 20 条的发行版清单
      会长出一串永远不会自愈的假警报(实测生产 5 条里 4 条是这种情况)。
"""
import json
import os
import time
import urllib.parse
from pathlib import Path

LEDGER_FILENAME = "gpg_ledger.json"
STATES = ("pass", "fail", "skip")

# 与 web/app.py 的 FLAT_ISO_DIRNAME / ALLOWED_TYPES、iso_download/download_linux.py
# 的同名常量保持一致(测试 test_gpg_ledger 会断言三者相等)。
FLAT_DIRNAME = "iso"
ALLOWED_TYPES = {"linux", "bsd", "windows", "macos"}


def has_fingerprint(entry: dict) -> bool:
    """条目是否配置了公钥指纹锚定(gpg_key_fingerprint, 字符串或字符串数组)。

    M6(审查 2026-10): 指纹是**本地信任锚**, 不是来源提供的材料 —— 所以
    "没配指纹"绝不能导致拒绝下载(那是 TOFU, 项目有意保留的降级路径),
    但必须能被观测到, 否则"已锚定且验签通过"与"只是首次信任"在 health 上
    长得一模一样。
    """
    fp = entry.get("gpg_key_fingerprint")
    if isinstance(fp, str):
        return bool(fp.strip())
    if isinstance(fp, (list, tuple)):
        return any(isinstance(x, str) and x.strip() for x in fp)
    return False


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


def _safe_component(c: str) -> bool:
    """单段文件名/目录名: 非空、无分隔符、不是 . 或 ..。"""
    return (bool(c) and c.strip() == c and c not in (".", "..")
            and "/" not in c and "\\" not in c)


def entry_filename(entry: dict) -> str | None:
    """条目对应的 ISO 文件名(只取路径末段), 供磁盘存在性判断。

    与 key_for 的片段规则刻意分开: key_for 保持历史行为(直接切字符串),
    已有账本键不能变; 这里走 urlparse 以剔除 query / fragment。
    """
    fn = entry.get("filename")
    if isinstance(fn, str) and _safe_component(fn.strip()):
        return fn.strip()
    for field in ("download_url", "url"):
        u = entry.get(field)
        if not isinstance(u, str) or not u.lower().startswith(("http://", "https://")):
            continue
        try:
            path = urllib.parse.urlparse(u).path
        except ValueError:
            continue
        frag = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
        if _safe_component(frag):
            return frag
    return None


def local_candidates(entry: dict, download_dir) -> list:
    """条目在两类存放模式下的候选本地路径。

    flat       -> <download_dir>/iso/<file>
    classified -> <download_dir>/<type>/<name>/<file>

    返回空列表 = 无法判定(条目缺文件名或字段非法), 调用方须按保守口径处理。
    """
    fn = entry_filename(entry)
    typ = str(entry.get("type") or "linux")
    name = str(entry.get("distribution") or entry.get("name") or "")
    if not fn or typ not in ALLOWED_TYPES or not _safe_component(name):
        return []
    root = Path(download_dir)
    return [root / FLAT_DIRNAME / fn, root / typ / name / fn]


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):  # 磁盘错误 + JSON 解码/解析错误
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
    except Exception:  # noqa: BLE001 - 边界: 账本绝不影响下载主流程(见 docstring), 任何未预期异常都吞
        return None


def build_summary(json_path=None, path=None, download_dir=None) -> dict:
    """汇总账本: configured(配置了 gpg_verify) vs recorded(账本里有记录)。

    never_invoked 只点名「文件在盘上、验签却从未跑过」的条目 —— 清单里有、
    硬盘上根本没有该 ISO 的条目归 not_downloaded(还没轮到验签, 不是缺陷)。

    磁盘不可判定时退回保守口径并计入 never_invoked: 下载根目录不存在、
    条目缺文件名, 这两种情况下"没扫到盘"绝不能伪装成"一切正常"。

    states 只统计 configured 的键(与 never_invoked 同口径), 账本里残留的
    已下线配置用 ledger_total 体现。任何读取异常都吞掉, health 永远 200。

    关于 stale(v1.5.4): 账本记的是"曾经发生过什么", 删文件不会回头改账本。
    于是会出现"账本说 pass, 盘上却没有这个文件" —— states.pass 就这样虚高
    (实测生产 pass=6 而盘上只有 4 个通过校验的文件)。这类记录单列进 stale,
    并给出 stale_by_state 让监控能算出"当前真正在盘上的通过数":
        states[state] - stale_by_state[state]
    判定 stale 需要**能确定文件确实没了**: 缺文件名(无法拼候选路径)或
    下载根目录不可访问时宁可不算, 不能把"没扫到盘"当成"文件丢了"。

    unpinned(M6, v1.8.0): 配置了 gpg_verify 但没配 gpg_key_fingerprint 的条目。
    它们靠 TOFU(首次信任即信任)工作 —— 项目有意保留的降级路径, **不阻塞下载**,
    但必须可见: 否则"已锚定且验签通过"与"只是首次信任"在 health 上无法区分。
    """
    jp = Path(json_path) if json_path else data_dir() / "distributions.json"
    lp = Path(path) if path else ledger_path()
    root = Path(download_dir) if download_dir else data_dir()

    entries = []
    try:
        cfg = json.loads(jp.read_text(encoding="utf-8")) or {}
        entries = [e for e in cfg.get("distributions", [])
                   if isinstance(e, dict) and e.get("gpg_verify")]
    except (OSError, ValueError):  # 清单读不出按空处理, health 永远 200
        pass

    # 同一 key 可能对应多条 entry(镜像组预设), 按首次出现顺序聚合成一条结论
    by_key = {}
    for e in entries:
        by_key.setdefault(key_for(e), []).append(e)

    # M6(审查 2026-10): 未配置指纹的条目 —— 它们只靠 TOFU(首次信任即信任)。
    # 这是**可见性**输出, 不是判定结果: 这些条目依然正常下载、正常验签,
    # 只是"验签通过"背后的信任根来自首次拉取而非本地锚定。
    # 判据取"整组都没配" —— 镜像组里只要有一条锚定, 该组就有锚可依。
    unpinned = [k for k, group in by_key.items()
                if not any(has_fingerprint(e) for e in group)]

    ledger = _load(lp)
    disk_ok = root.is_dir()
    states = {s: 0 for s in STATES}
    stale_by_state = {s: 0 for s in STATES}
    never_invoked, not_downloaded, stale, on_disk = [], [], [], 0

    for k, group in by_key.items():
        cands = [c for e in group for c in local_candidates(e, root)]
        # decidable = 能拼出候选路径, 只有它为真时"文件不存在"才是结论
        if cands:
            present, decidable = disk_ok and any(c.is_file() for c in cands), True
        else:
            present, decidable = False, False  # 缺文件名 -> 无法判定
        if present:
            on_disk += 1
        st = (ledger.get(k) or {}).get("state")
        if st in states:
            states[st] += 1
            if disk_ok and decidable and not present:
                # 账本有记录, 文件却不在盘上了 -> 陈旧记录, 不该再计入"当前通过"
                stale.append(k)
                stale_by_state[st] += 1
        if k in ledger:
            continue
        if not decidable or not disk_ok or present:
            never_invoked.append(k)
        else:
            not_downloaded.append(k)

    return {
        "configured": len(by_key),
        "on_disk": on_disk,
        "states": states,
        "stale": stale,
        "stale_by_state": stale_by_state,
        "never_invoked": never_invoked,
        "not_downloaded": not_downloaded,
        # M6: 只靠 TOFU、没有本地指纹锚的条目。仅作可见性, 不影响 ok / 不阻塞下载。
        "unpinned": unpinned,
        "unpinned_count": len(unpinned),
        "ledger_total": len(ledger),
        "disk_available": disk_ok,
    }
