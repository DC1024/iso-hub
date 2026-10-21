#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ISO Hub - 选择部分发行版/版本下载的辅助 runner。

复用上游 download_linux.py 的 LinuxDistributionDownloader，但只下载
命令行传入的选定条目，并禁用其"清理整组目录"的行为（清理由 UI 上
单独的"清理过期"动作触发），避免误删用户未选择的同组 ISO。
"""
import argparse
import json
import os
import shutil
import sys
import time
import types
from pathlib import Path

import requests

ALLOWED_TYPES = {"linux", "bsd", "windows", "macos"}
PART_SUFFIX = ".part"
# ISO 存放模式: classified=<type>/<发行版>/ 分类存放; flat=统一单目录。
# 必须与 web/app.py 的 STORAGE_MODES / FLAT_ISO_DIRNAME 保持一致。
STORAGE_MODES = ("classified", "flat")
FLAT_DIRNAME = "iso"

# v1.5.2: 下载前剩余空间预检。
# 过去没有任何空间检查: 20GB 的 ISO 下到 95% 才 ENOSPC, 用户只得到一个坏掉的
# .part 和一句"下载失败"。现在在写第一个字节前按 Content-Length(或 Content-Range/
# HEAD 兜底)比一次可用空间, 不足直接拒单, 不产生半成品、不浪费带宽。
# 预留 256 MiB: 校验和文件、日志、以及"刚好写满"导致的其他写盘失败都要留余地。
DISK_SAFETY_MARGIN = 256 * 1024 * 1024


class DiskSpaceShortage(Exception):
    """目标盘剩余空间不足。区别于网络/校验失败: 换源重试毫无意义, 必须整个文件放弃。"""


def _human_bytes(n: int) -> str:
    """人类可读的字节数。低空间时 GiB 两位小数会显示成 0.00, 必须往下退一级。"""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GiB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MiB"
    return f"{n / 1024:.0f} KiB"


def _check_disk_space(directory, need_bytes: int, margin: int = DISK_SAFETY_MARGIN) -> tuple:
    """返回 (是否够用, 说明)。need_bytes<=0 表示大小未知 —— 无从判断, 放行。

    刻意用 shutil.disk_usage 而不是 os.statvfs: 前者跨平台, 且拿的是**该目录所在
    文件系统**的空闲量(目标目录与 .part 必定同盘, 这才是真正要保护的空间)。
    """
    if need_bytes <= 0:
        return True, "文件大小未知, 跳过剩余空间预检"
    try:
        free = shutil.disk_usage(str(directory)).free
    except OSError as e:
        # 拿不到就不拦: 预检是"锦上添花", 不能因为某些不支持 stat 的文件系统
        # (部分 FUSE/网络盘)把正常下载全挡死。
        return True, f"无法获取剩余空间({e}), 跳过预检"
    if free >= need_bytes + margin:
        return True, ""
    return False, (
        f"磁盘空间不足: 还需 {_human_bytes(need_bytes)}, "
        f"另需预留 {_human_bytes(margin)}, 当前可用 {_human_bytes(free)} "
        f"({directory})"
    )

# P1-⑤b: GPG 验证状态账本(同目录模块; 每次验签后落账, 供 /api/health 汇总)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpg_ledger  # noqa: E402
import errs  # noqa: E402  # 可预期异常组合(JSON 读写/网络), 替代宽捕获


class TruncatedTransfer(Exception):
    """响应流提前结束: 有预期长度但实际写入不足。

    语义: 传输被截断, 已写入的字节仍是**有效前缀**, 应保留 .part 供下次续传,
    不能当作"文件损坏"丢弃 —— 否则一次网络抖动就丢掉全部已下进度。
    """


class CorruptPayload(Exception):
    """传输完成(或长度未知)但内容校验不通过。

    语义: 落盘数据不可信, 应丢弃 .part 重下, 避免下次拿着坏数据续传。
    """



def _safe_dist_dir(download_dir: Path, typ: str, name: str,
                   flat: bool = False) -> Path | None:
    """把 (type, name) 安全解析为该发行版 ISO 的**存放目录**, 拒绝路径穿越/非法字符。

    校验顺序刻意放在模式判断之前: 非法 (type, name) 在任何存放模式下都返回 None,
    避免"切到 flat 就让穿越输入蒙混过关"。返回目录随 `flat` 变化:
      * flat=False(默认, classified) -> download_dir/<type>/<name>/
      * flat=True                    -> download_dir/iso/  (全发行版平铺同一目录)

    `flat` 由 `main()` 经 `--storage-mode` 传入(本文件**不读** settings.json,
    保持 iso_runner 不直接触碰共享配置文件的约束)。
    """
    if not typ or not name or typ not in ALLOWED_TYPES:
        return None
    for comp in (typ, name):
        comp = str(comp)
        if comp != comp.strip() or comp in (".", ".."):
            return None
        if "/" in comp or "\\" in comp:
            return None
    target = (download_dir / FLAT_DIRNAME) if flat else (download_dir / typ / name)
    target = target.resolve()
    try:
        target.relative_to(download_dir.resolve())
    except ValueError:
        return None
    return target


# 在上游 download_linux 被 import 之前 mock 掉 tqdm。
# 上游用 tqdm 的 \r(回车) 覆盖式进度条输出(无换行), 会阻塞后端按行读取子进程 stdout,
# 导致日志/状态不实时刷新(进度条卡 0%)、停止任务后才一次性 flush。这里用无输出的 stub 替换,
# 前端进度条靠后端跑文件 stat(size/total) 实现, 不依赖 tqdm 的细粒度进度。
_tqdm_stub = types.ModuleType("tqdm")


class _NoopTqdm:
    def __init__(self, *a, **k):
        pass

    def update(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a, **k):
        pass

    def close(self):
        pass


_tqdm_stub.tqdm = lambda *a, **k: _NoopTqdm()
sys.modules["tqdm"] = _tqdm_stub


# v1.3.17: 网络抖动/镜像限流的统一重试参数。
#   背景(生产实测): ubuntu-26.04(.1)、archlinux-2026.08.01 三条下载在同一分钟内
#   全部 hard 失败, 事后探测 URL 均 200 —— 典型的出口瞬时抖动。旧代码一次失败即
#   记 failure, 且 failure 无 TTL、无重试 → 一分钟的抖动在 UI 上留永久的失败疤。
#   现在: 连接/读超时与 429/5xx 各退避重试 3 次(1.5s → 3s → 6s)。
#   与 iso_download/download_linux.py 保持同参(该模块有同名实现, 因 runner 需
#   单文件可运行而独立复制 —— 与 ALLOWED_TYPES/PART_SUFFIX 的既有约定一致)。
RETRY_TOTAL = 3
RETRY_BACKOFF = 1.5
RETRY_STATUSES = (429, 500, 502, 503, 504)


def _http_get_with_retry(url: str, *, headers=None, stream: bool = False,
                         timeout=None, retries: int = RETRY_TOTAL):
    """带指数退避的 GET。保持调用模块级 ``requests.get``(测试与生产都按此打桩)。

    **不重试 416** —— 416 表示 .part 已写满(断点越界), 是"完成"信号而非故障,
    由调用方当作完整候选送进校验(见 _download_file_with_failover)。
    """
    last_exc = None
    for attempt in range(max(1, retries)):
        try:
            resp = requests.get(url, headers=headers, stream=stream, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt + 1 < max(1, retries):
                _delay = RETRY_BACKOFF * (2 ** attempt)
                print(f"  网络异常({e.__class__.__name__}), {_delay:.1f}s 后重试"
                      f" ({attempt + 1}/{retries})")
                time.sleep(_delay)
                continue
            raise
        code = getattr(resp, "status_code", 0) or 0
        if code in RETRY_STATUSES and attempt + 1 < max(1, retries):
            _delay = RETRY_BACKOFF * (2 ** attempt)
            try:
                resp.close()
            except OSError:  # 关闭失败不影响重试
                pass
            print(f"  服务器返回 {code}, {_delay:.1f}s 后重试 ({attempt + 1}/{retries})")
            time.sleep(_delay)
            continue
        return resp
    if last_exc is not None:
        raise last_exc
    raise requests.exceptions.RequestException("重试耗尽且无响应")


def _head_target_size(url: str, headers: dict) -> int:
    """对下载 URL 发 HEAD 请求获取目标文件字节数(总大小), 用于 UI 进度条。失败返回 0。"""
    try:
        r = requests.head(url, headers=headers, timeout=15, allow_redirects=True)
        if r.status_code < 400 and r.headers.get("content-length"):
            return int(r.headers["content-length"])
    except errs.HTTP_REQ:  # 探测失败按 0(大小未知)处理, 不拦下载
        pass
    return 0


def _pick_candidates(strategy: str, entry: dict, headers: dict):
    """根据选源策略, 返回有序的 [(url, checksum_url), ...] 候选列表。

    策略 A（固定优先级）: 保持 sources_config.json 里 mirrors 的配置顺序
      （清华在前, 官方源兜底）, 不额外请求。
    策略 B（实测选优）: 对每个候选源发 HEAD 探测, 按响应耗时排序,
      最快可达源排最前; 若全部探测失败, 回退到配置顺序。

    候选列表来源: entry 的 download_urls/checksum_urls（由 update_distributions.py
    按各镜像模板生成）。无多源时退化为单一 download_url。
    """
    urls = entry.get("download_urls") or [entry["download_url"]]
    cs_urls = entry.get("checksum_urls") or []
    cs = cs_urls + [None] * (len(urls) - len(cs_urls))  # 校验和不足的镜像补 None

    # 用户手动指定的源URL(entry.pin): 存在且是候选之一 → 强制移到最前
    pin = (entry.get("pin") or "").strip()
    if pin and pin in urls:
        i = urls.index(pin)
        urls = [urls[i]] + urls[:i] + urls[i + 1:]
        cs = [cs[i]] + cs[:i] + cs[i + 1:]

    if strategy != "B":
        # 策略 A: 固定优先级(配置顺序, pin 已置顶)
        return list(zip(urls, cs))

    # 策略 B: HEAD 实测各候选源, 选最快可达源
    # 若用户手动指定了源(pin 已置顶), 只要它可达就优先使用, 不参与速度排序
    pin = (entry.get("pin") or "").strip()
    scored = []
    pinned_ok = None
    for u, c in zip(urls, cs):
        try:
            t0 = time.monotonic()
            r = requests.head(u, headers=headers, timeout=8, allow_redirects=True)
            dt = time.monotonic() - t0
            if r.status_code < 400:
                if u == pin:
                    pinned_ok = (u, c)   # 用户指定的源可达, 直接置顶
                else:
                    scored.append((dt, u, c))
                print(f"  [策略B] 可达 {dt*1000:.0f}ms  {u}")
            else:
                print(f"  [策略B] HTTP {r.status_code} 跳过  {u}")
        except errs.HTTP_REQ:
            print(f"  [策略B] 不可达 跳过  {u}")
    if pinned_ok:
        rest = [x for x in sorted(scored, key=lambda x: x[0])]
        return [pinned_ok] + [(u, c) for _, u, c in rest]
    if scored:
        scored.sort(key=lambda x: x[0])
        return [(u, c) for _, u, c in scored]
    return list(zip(urls, cs))


def _record_failure(data_dir, typ: str, name: str, fname: str, kind: str,
                    err: str = "", url: str = "") -> None:
    """把文件级失败写进 download_failures.json(供前端显示「下载失败/下载停止」)。

    kind="hard"    半成品已清理, 下次只能从头下 → 下载失败
    kind="stopped" 半成品保留, 下次可续传     → 下载停止

    v1.3.17 增强:
      * 新增 ``err``/``url`` 字段 —— 旧记录只有 ``{at, kind}``, 事后完全看不出
        到底是 404、超时还是校验失败, 排查只能靠翻(内存里、重启即失的)日志;
      * 写入时顺带**按 TTL 清理过期记录** —— failure 过去没有过期机制, 一次
        瞬时网络抖动留下的"疤"会永久显示在 UI 上(生产实测: 10-05 三条 hard
        记录, URL 早已恢复 200, 疤却挂了 10 天)。
    """
    try:
        jf = Path(data_dir) / "download_failures.json"
        data = {}
        if jf.exists():
            try:
                data = json.loads(jf.read_text(encoding="utf-8")) or {}
            except errs.JSON_READ:
                data = {}
        data = _drop_expired_failures(data)
        data[f"{typ}/{name}/{fname}"] = {
            "at": int(time.time()),
            "kind": kind,
            # 只在有值时写, 避免把 JSON 撑满空字段
            **({"err": err[:300]} if err else {}),
            **({"url": url[:500]} if url else {}),
        }
        tmp = jf.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(jf)
    except errs.JSON_WRITE:  # 失败记录写不进不影响下载结果本身
        pass


def _drop_expired_failures(data: dict) -> dict:
    """丢弃超过 TTL 的失败记录。

    TTL 默认 7 天(可用环境变量 ISO_HUB_FAILURE_TTL_DAYS 覆盖, 0 表示不清理)。
    语义: 失败记录只代表"最近一次同步的状态"; 超过 TTL 仍未重下, 说明该文件
    已不在订阅/清单里, 继续显示失败只会误导(且用户无从手动清除)。
    """
    try:
        ttl_days = float(os.environ.get("ISO_HUB_FAILURE_TTL_DAYS", "7"))
    except (TypeError, ValueError):
        ttl_days = 7.0
    if ttl_days <= 0:
        return data
    cutoff = time.time() - ttl_days * 86400
    return {
        k: v for k, v in data.items()
        if not (isinstance(v, dict) and isinstance(v.get("at"), (int, float))
                and v["at"] < cutoff)
    }


def _clear_failure(data_dir, typ: str, name: str, fname: str) -> None:
    """下载成功后清除该文件的失败记录。"""
    try:
        jf = Path(data_dir) / "download_failures.json"
        if not jf.exists():
            return
        data = json.loads(jf.read_text(encoding="utf-8")) or {}
        rel = f"{typ}/{name}/{fname}"
        if rel in data:
            data.pop(rel, None)
            tmp = jf.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(jf)
    except errs.JSON_WRITE:  # 清除失败记录失败不影响下载结果本身
        pass


def _record_unverified(data_dir, typ: str, name: str, fname: str, reason: str,
                       state: str = "unverified") -> None:
    """记录"未验签落定"的文件(校验源不可达但长度达标, 文件被接受)。

    P2 修复(审查 2026-10): 这类文件与"校验通过"在 UI 上无法区分, 会让用户误以为
    SHA256 校验覆盖了它。写入独立记录文件, 由 build_distros() 下发给前端显示
    「未验签」。**这不是失败** —— 文件可用, 只是没有密码学证据。

    state(v1.4.0):
      * ``unverified`` —— 没有证据(下载时校验源不可达 / 本次重校验仍不可达)
      * ``mismatch``   —— 有证据且**对不上**(重校验明确返回 False)。比 unverified 更严重:
        文件内容与官方清单不一致, UI 用红色区分, 但同样**不自动删除**(6GB 的东西
        交给用户决定)。旧记录没有该字段, 读取端一律按 unverified 处理。
    """
    try:
        jf = Path(data_dir) / "download_unverified.json"
        data = {}
        if jf.exists():
            try:
                data = json.loads(jf.read_text(encoding="utf-8")) or {}
            except errs.JSON_READ:
                data = {}
        data[f"{typ}/{name}/{fname}"] = {
            "at": int(time.time()),
            "reason": (reason or "")[:300],
            "state": state if state in ("unverified", "mismatch") else "unverified",
        }
        tmp = jf.with_name(jf.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(jf)
    except errs.JSON_WRITE:  # 未验签记录写不进不影响落定结果本身
        pass


def _clear_unverified(data_dir, typ: str, name: str, fname: str) -> None:
    """真正校验通过后清除「未验签」标记。"""
    try:
        jf = Path(data_dir) / "download_unverified.json"
        if not jf.exists():
            return
        data = json.loads(jf.read_text(encoding="utf-8")) or {}
        rel = f"{typ}/{name}/{fname}"
        if rel in data:
            data.pop(rel, None)
            tmp = jf.with_name(jf.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(jf)
    except errs.JSON_WRITE:  # 清除未验签标记失败不影响校验结果本身
        pass


# --------------------------------------------------------------------------- 只校验模式
# v1.4.0: 「未验签」条目此前**只能靠整文件重下**来清标记 —— 对 2~6GB 的 ISO 代价过高,
# 用户实际不会点,v1.3.21 加的 ⚠ 徽标于是变成永久警告。新增 --verify-only: 对**已经在盘上**
# 的最终文件补一次校验(拉清单 + 算 SHA256 + 验 GPG 签名), 完全不下载。
# 退出码供 app 侧与测试判定三态, 不要改成 0/1 二值:
VERIFY_EXIT_OK = 0            # 校验通过(已清除未验签记录)
VERIFY_EXIT_BADARG = 2        # 参数/条目/文件不合法 —— 调用方本应先挡掉
VERIFY_EXIT_MISMATCH = 3      # 明确不符(文件损坏或上游重制), 已记 state=mismatch
VERIFY_EXIT_UNDECIDABLE = 4   # 仍无法判定(源不可达/无校验源/校验期间文件被改)


def _parse_rel(rel: str):
    """把 ``<type>/<name>/<file>`` 拆成三段并拒绝一切非法形态。

    与 app.py 的 rel 拼法、_safe_dist_dir 的校验规则保持一致: 只接受恰好三段、
    每段非空且不含路径分隔符、文件名不得是 . / .. / 半成品。任何不合规返回 None。
    """
    parts = (rel or "").strip().split("/")
    if len(parts) != 3:
        return None
    typ, name, fname = parts
    for comp in parts:
        if not comp or comp != comp.strip() or comp in (".", ".."):
            return None
        if "/" in comp or "\\" in comp:
            return None
    if fname.lower().endswith(PART_SUFFIX):
        return None
    return typ, name, fname


def _find_entry(entries, typ: str, name: str, fname: str):
    """按 (type, distribution, URL 里的文件名) 定位清单条目。"""
    for e in entries:
        if e.get("type", "linux") != typ or e.get("distribution") != name:
            continue
        url = e.get("download_url") or ""
        if url.rstrip("/").rsplit("/", 1)[-1] == fname:
            return e
    return None


def _run_verify_only(downloader, args) -> int:
    """对已落定的文件补一次校验(不下载), 返回进程退出码。

    三态落库与下载路径完全一致, 只是入口不同: 通过 → 清记录; 不符 → state=mismatch;
    仍不可判定 → 更新原因与时间。**绝不删除文件** —— 判定不符时删掉 6GB 数据属于
    不可逆操作, 是否重下由用户在 UI 上决定。
    """
    parsed = _parse_rel(args.rel)
    if not parsed:
        print(f"错误: 非法的 rel 参数: {args.rel!r}", file=sys.stderr)
        return VERIFY_EXIT_BADARG
    typ, name, fname = parsed
    print(f">>> 只校验模式: {typ}/{name}/{fname}")

    dist_dir = _safe_dist_dir(Path(downloader.download_dir), typ, name,
                              flat=args.storage_mode == "flat")
    if dist_dir is None:
        print(f"错误: 非法的发行版类型/名称: {typ}/{name}", file=sys.stderr)
        return VERIFY_EXIT_BADARG
    filepath = dist_dir / fname
    if not filepath.is_file():
        print(f"错误: 文件不存在: {filepath}", file=sys.stderr)
        return VERIFY_EXIT_BADARG

    entry = _find_entry(downloader.distributions.get("distributions", []), typ, name, fname)
    if entry is None:
        print(f"错误: 清单里找不到 {typ}/{name}/{fname}", file=sys.stderr)
        return VERIFY_EXIT_BADARG

    # 关键守卫: 两个校验字段都为空时 verify_checksum_smart 会返回
    # (True, "跳过校验和验证（无可用信息）")。那是下载路径的历史语义(没信息就放行),
    # 但作为"重新校验"的结果, 会把"这个发行版压根没配校验源"冒充成"校验通过" ——
    # 必须在这里拦成"无法判定", 并在记录里写明原因。
    if not (entry.get("checksum_url") or entry.get("checksum")):
        msg = "该发行版未配置任何校验源(checksum_url / checksum 均为空), 无从校验"
        print(f"✗ {msg}", file=sys.stderr)
        _record_unverified(args.download_dir, typ, name, fname, msg, "unverified")
        return VERIFY_EXIT_UNDECIDABLE

    def _sig() -> tuple[int, int]:
        st = filepath.stat()
        return st.st_size, st.st_mtime_ns

    before = _sig()   # 校验要读完整文件(数十秒), 期间文件若被并发下载改写, 结果不可信
    ok, msg = downloader.verify_checksum_smart(
        filepath, entry.get("checksum_url"), entry.get("checksum"), dist=entry
    )
    if ok is not None:
        # ok=None 表示"没验成", 记账会把它归到 fail; 那是"源不可达"而非"验签拒绝",
        # 不该污染 /api/health 的账本, 故只在有明确结论时落账。
        gpg_ledger.record_from_msg(entry, ok, msg)
    if ok is True and "跳过校验和验证" in str(msg):
        ok, msg = None, "该发行版未配置可用的校验源, 无从校验"
    if _sig() != before:
        print("✗ 校验期间文件大小/修改时间发生变化, 本次结果作废(可能有并发下载)", file=sys.stderr)
        return VERIFY_EXIT_UNDECIDABLE

    if ok is True:
        print(f"✓ {msg}")
        _clear_unverified(args.download_dir, typ, name, fname)
        _clear_failure(args.download_dir, typ, name, fname)
        print(">>> 校验通过, 已撤掉「未验签」标记")
        return VERIFY_EXIT_OK
    if ok is False:
        print(f"✗ {msg}")
        _record_unverified(args.download_dir, typ, name, fname, msg, "mismatch")
        print(">>> 校验不符: 已标记为 mismatch(不自动删除, 请在界面上决定是否重下)")
        return VERIFY_EXIT_MISMATCH

    print(f"⚠ {msg}")
    _record_unverified(args.download_dir, typ, name, fname, msg, "unverified")
    print(">>> 仍无法判定: 保留「未验签」标记, 已更新原因与时间")
    return VERIFY_EXIT_UNDECIDABLE


def _drop_stale_part(part_path) -> None:
    """删掉与"已校验通过的完整文件"同名的残留半成品(.part)。

    真实场景(线上 bug): 上一轮任务被「停止任务」kill, 留下 ``xxx.iso.part``;
    而完整文件 ``xxx.iso`` 其实早已落定并通过校验。此时本轮走到"文件已存在 +
    校验通过"分支, 直接 continue —— .part 永远不会被清理。

    后果: 后端 build_distros() 在同名的完整文件与 .part 之间取 **mtime 较新者**
    作为条目代表, 残留的 .part 通常更新 → 条目被判成 partial, UI 一直显示
    「下载停止(已下载部分)」, 即便文件本身完整且校验和与官方清单一致。

    所以这里必须显式清理: 完整文件已校验通过, .part 只是冗余字节。
    """
    try:
        if part_path.exists():
            part_path.unlink()
            print(f"  清理冗余半成品: {part_path}")
    except OSError as e:  # noqa: BLE001
        # 清理失败不影响下载结果(文件本身已校验通过), 只留日志
        print(f"  清理冗余半成品失败(忽略): {e}")


def _resolve_total(resp, have: int, head_total: int) -> int:
    """确定这次传输的**完整文件应有字节数**, 三级兜底, 拿不到返回 0。

    优先级(从最可靠到最兜底):
      1. Content-Range: bytes a-b/TOTAL —— 服务器明确告知全长, 最可信;
      2. Content-Length + have —— 续传时是"本次剩余量", 加上已下部分即全长;
      3. head_total —— 调用方事先 HEAD 探测的大小, 服务器不给长度时的兜底。

    v1.2.8 及以前只看 1/2, 两者都缺失时 total=0, 导致后续完整性检查被
    `if total and ...` 短路跳过 —— 这正是"没下载完就去算校验和"的根因。
    引入第 3 级 + 显式返回 0 的语义(0 = 确实无从判断), 让调用方能区分
    "确定不完整"与"无法判断"。
    """
    # 1) Content-Range 全长
    cr = resp.headers.get("content-range") or ""
    if "/" in cr:
        try:
            n = int(cr.rsplit("/", 1)[-1])
            if n > 0:
                return n
        except ValueError:
            pass
    # 2) Content-Length(+ 已下的 have)
    try:
        cl = int(resp.headers.get("content-length") or 0)
    except ValueError:
        cl = 0
    if cl > 0:
        return have + cl
    # 3) HEAD 预取大小兜底
    if head_total > 0:
        return head_total
    return 0


def _download_file_with_failover(downloader, target_dist: dict, candidates, filename: str,
                                 dist_dir, filepath, head_total: int = 0,
                                 err_out: list | None = None,
                                 data_dir=None) -> tuple:
    """逐个候选源下载同一文件, 失败/校验失败自动切换下一候选源。

    返回 (成功与否, 实际使用的下载URL)。全部候选失败返回 (False, None)。

    err_out: 可选的 list, 由调用方传入; 函数把最后一次失败原因 append 进去,
             供 download_failures.json 记录 err 字段(保持返回值为 2 元组不破坏调用方)。

    落盘协议(关键): 下载期间一律写 ``<最终名>.part``, 只有"大小校验 + 校验和"
    全部通过后才 ``os.replace`` 原子改名为最终文件名。这样:

      * 进程被「停止任务」SIGTERM/SIGKILL 杀掉时, 磁盘上留下的是 ``xxx.iso.part``,
        后端 disk_inventory() 能识别为半成品 → 显示「下载停止」而不是「已下载」;
      * 半成品绝不会以最终文件名出现在磁盘上, 杜绝"残缺文件被当成完整 ISO"。

    完整性判据(v1.2.9 修复):
      * 期望长度由 ``_resolve_total()`` 三级兜底解析, 不再是"拿不到就跳过检查";
      * 实际不足 → TruncatedTransfer: **保留 .part 供续传**, 不计为损坏;
      * 长度达标但校验和不符 → CorruptPayload: 丢弃 .part 重下;
      * 长度完全未知且校验和不符 → 无法区分截断与损坏 → **保守保留 .part**,
        只切下一个候选源(宁可多占点磁盘, 也不误删用户已下的进度)。

    head_total: 调用方 HEAD 预取的大小, 长度信息缺失时用于兜底判据。
    """
    part = Path(str(filepath) + PART_SUFFIX)
    last_err = None
    for idx, (url, checksum_url) in enumerate(candidates):
        print(f"  候选源 {idx + 1}/{len(candidates)}: {url}")
        # 断点续传: 已存在的 .part 就是本源的断点位置。
        # 注意每个候选源都要重新评估(不同镜像上的文件可能不同, 故续传只在本源
        # 首次失败后保留, 换源时从该源的断点重新开始)。
        try:
            have = part.stat().st_size if part.exists() else 0
            headers = dict(downloader.headers or {})
            if have:
                headers["Range"] = f"bytes={have}-"
            # v1.3.17: 带指数退避的重试(瞬时抖动/429限流/5xx), 不重试 416。
            # 与 download_linux.http_get_with_retry 同策略; 这里独立实现是因为
            # iso_runner 必须保持单文件可运行(仅在运行期才 import download_linux),
            # 且全部测试都围绕模块级 requests.get 打桩。
            resp = _http_get_with_retry(url, headers=headers, stream=True, timeout=60)
            if have and resp.status_code == 416:
                # v1.3.17 核心修复: 416 = Range 起点 >= 文件全长, 也就是说 .part
                # **已经是完整文件**。旧代码在这里 `raise Exception("将重下")` →
                # 切下一候选源从头下 → 下完又变全量 → 下一轮再 416 → 死循环。
                # 生产实测: Proxmox 9.1/9.2 的 .part 与正式 ISO 字节完全相同(SHA 已核对),
                # 却因为这个分支每天重下一次, UI 长期显示「下载停止」。
                # 现在: 不再传输任何字节, 直接把 .part 当完整候选送进校验。
                print(f"  断点已满({have/1024/1024:.1f} MiB): 服务器返回 416, "
                      f"{part.name} 视为完整候选, 直接进入校验")
                try:
                    resp.close()
                except OSError:  # 关闭失败不影响后续校验流程
                    pass
                # 416 保证了 have >= 远端全长, 因此以 have 为准最可靠(此时
                # head_total 可能是上一轮的陈旧值, 反而会误判成"未下完")
                total = have
                already_complete = True
            elif have and resp.status_code == 206:
                # 服务器支持 Range: 追加写入
                total = _resolve_total(resp, have, head_total)
                if total:
                    print(f"  续传: 从 {have/1024/1024:.1f} MiB 继续 "
                          f"(共 {total/1024/1024:.1f} MiB)")
                else:
                    print(f"  续传: 从 {have/1024/1024:.1f} MiB 继续 "
                          f"(服务器未提供总大小, 将无法核对完整性)")
                mode = "ab"
                already_complete = False
            elif have and resp.status_code == 200:
                # 服务器不支持 Range(返回 200 全量) → 只能从头下, 截断重写
                total = _resolve_total(resp, 0, head_total)
                print(f"  服务器不支持断点续传, 从头下载 (已丢弃 {have/1024/1024:.1f} MiB)")
                have = 0
                mode = "wb"
                already_complete = False
            else:
                total = _resolve_total(resp, 0, head_total)
                mode = "wb"
                have = 0
                already_complete = False
            if not already_complete:
                # 416 是"已完成"信号而非错误, 上面已 close, 不能再 raise_for_status
                resp.raise_for_status()

            if not already_complete:
                # v1.5.2: 写第一个字节前确认盘放得下。total 是本次应达到的**全长**
                # (Content-Range / Content-Length+have / HEAD 三级兜底), 已有 .part
                # 的字节已经占着盘, 所以只需按剩余量判断。
                _ok_space, _why_space = _check_disk_space(dist_dir, max(0, total - have))
                if not _ok_space:
                    try:
                        resp.close()
                    except OSError:  # 关闭失败不影响空间不足的处理
                        pass
                    # 半成品保留(不删): 用户清出空间后还能续传
                    raise DiskSpaceShortage(_why_space)
                if _why_space:
                    print(f"  ⚠ {_why_space}")

            if not already_complete:
                # 下载到 .part; 下载途中被 kill 也会留下 .part 供识别
                with open(part, mode) as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            written = part.stat().st_size

            # ---- 完整性判据(v1.2.9) ----
            # 期望长度已知却不足 → 传输被截断。注意这里**不再跳过检查**:
            # 只有 total 确实为 0(三级兜底全都拿不到)时才无从判断。
            if total and written < total:
                raise TruncatedTransfer(
                    f"传输未完成: 期望 {total}B, 实际 {written}B "
                    f"(缺 {total - written}B, 可续传)")
            if total and written > total:
                # 超出预期长度: 落盘内容不可信, 丢弃重下
                raise CorruptPayload(f"大小超出预期: 期望 {total}B, 实际 {written}B")
            if not total:
                print("  ⚠ 服务器未提供文件总大小, 无法核对完整性, 直接交由校验和判定")

            # 校验和优先跟随当前候选源自身; 无则回退 entry 存储值
            # 必须传 dist=target_dist: 否则 GPG 验签会被静默跳过(漏传 dist 的回归)
            success, msg = downloader.verify_checksum_smart(
                part, checksum_url, target_dist.get("checksum"), dist=target_dist
            )
            gpg_ledger.record_from_msg(target_dist, success, msg)
            _typ = target_dist.get("type", "linux")
            _name = target_dist.get("distribution", "")
            if success:
                print(f"  ✓ {msg}")
                # 全部校验通过 → 原子改名为最终文件名(此刻才"看起来"下载完成)
                if filepath.exists():
                    filepath.unlink()
                os.replace(part, filepath)
                # P2: 曾经"未验签落定"的文件这次真验过了 —— 撤掉警告标记
                if data_dir is not None:
                    _clear_unverified(data_dir, _typ, _name, filename)
                return True, url

            if success is None:
                # v1.3.17: 校验源不可达/清单里没这个文件名 —— 不是"验不过", 是"没验成"。
                # 旧代码在这里与 False 同路 → 尺寸达标就**删掉 .part 重下**, 于是
                # "checksum 模板配错"这种纯配置问题变成每周一次的整文件重下, 且
                # 永远修不好(重下后依然取不到摘要)。现在:
                #   * 尺寸已达标 → 尽最大努力验证后落定(留告警, 提示检查 checksum 源)
                #   * 尺寸未知   → 保留 .part, 不删不重下(避免坏数据判别与无谓流量)
                print(f"  ⚠ 完整性无法判定: {msg}")
                if total and written == total:
                    print("    长度已与服务器声明一致, 按无法校验处理并落定"
                          "(请检查该发行版的 checksum 源配置)")
                    if filepath.exists():
                        filepath.unlink()
                    os.replace(part, filepath)
                    # P2 修复: 记下"这个文件一次都没验过" —— UI 需要区别于校验通过的条目
                    if data_dir is not None:
                        _record_unverified(data_dir, _typ, _name, filename, msg)
                    return True, url
                print("    长度未知, 保留半成品供下次续传(不删除、不重下)")
                raise TruncatedTransfer("校验源不可用且长度未知, 保留半成品")

            # 校验和不符: 区分"尺寸已达标 → 内容损坏"与"尺寸未知 → 无法判定"
            print(f"  ✗ 校验和验证失败: {msg}")
            if total:
                # 尺寸与预期一致却校验不过 → 内容确实损坏, 丢弃避免坏数据被续传
                try:
                    if part.exists():
                        part.unlink()
                except OSError:
                    pass
                print("  半成品尺寸正确但内容校验不符, 已丢弃(避免坏数据被续传)")
            else:
                # 尺寸未知: 可能是截断(可续传)也可能是损坏, 无法区分 → 保守保留
                print("  无法确认是否为传输截断, 保守保留半成品供下次续传")
                raise TruncatedTransfer("校验失败且长度未知, 保守保留半成品")
        except DiskSpaceShortage as e:
            # 空间不足与源无关: 换下一个镜像照样写不下, 只会把同一份错误重试 N 遍。
            # 直接跳出候选源循环, 让失败原因落到 download_failures.json 供 UI 展示。
            last_err = e
            print(f"  ✗ 磁盘空间不足, 放弃该文件(不换源): {e}")
            break
        except TruncatedTransfer as e:
            # 传输截断: 半成品是有效前缀, **保留**供下次续传, 不当作损坏
            last_err = e
            print(f"  ✗ 传输被截断, 保留半成品待续传: {e}")
        except CorruptPayload as e:
            last_err = e
            print(f"  ✗ 响应内容不可信, 丢弃半成品: {e}")
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001 - 边界: 换源重试兜底, 单源的未预期异常应 failover 到下一候选
            last_err = e
            print(f"  ✗ 候选源失败, 尝试下一源: {e}")
    if last_err is not None:
        print(f"  ✗ 该文件所有候选源均失败: {last_err}")
        if err_out is not None:
            err_out.append(last_err)
    return False, None


def main() -> None:
    parser = argparse.ArgumentParser(description="ISO Hub selective download runner")
    parser.add_argument("--json-file", required=True, help="distributions.json path")
    parser.add_argument("--download-dir", required=True, help="ISO download dir")
    parser.add_argument(
        "--select", default="", help='JSON array of {"distribution","download_url"}'
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="只校验已落定文件(不下载), 需配合 --rel; 退出码 0/2/3/4 = 通过/参数错/不符/无法判定",
    )
    parser.add_argument(
        "--rel", default="",
        help="verify-only 的目标, 形如 linux/ubuntu/ubuntu-24.04.iso",
    )
    parser.add_argument(
        "--strategy", default="A", choices=["A", "B"],
        help="多源选源策略: A=固定优先级(配置顺序, 默认), B=实测选最快可达源",
    )
    parser.add_argument(
        "--storage-mode", default="classified", choices=list(STORAGE_MODES),
        help="ISO 存放模式: classified=按 <类型>/<发行版>/ 分类(默认), flat=统一单目录",
    )
    args = parser.parse_args()

    repo_dir = Path(__file__).resolve().parent.parent / "iso_download"
    sys.path.insert(0, str(repo_dir))

    from download_linux import LinuxDistributionDownloader  # noqa: E402

    downloader = LinuxDistributionDownloader(args.json_file, args.download_dir)
    # 把存放模式透传给上游下载器(其内部 _safe_dist_dir 也按同一模式落盘)
    downloader.storage_mode = args.storage_mode
    # 禁用整组清理：只做增量校验/下载，旧文件保留等用户手动清理
    downloader.cleanup_distribution_dir = lambda *a, **k: None

    # v1.4.0: 只校验模式 —— 不进入下载流程, 打完一次校验就退出
    if args.verify_only:
        sys.exit(_run_verify_only(downloader, args))

    if not args.select:
        print("错误: 缺少 --select(下载模式)或 --verify-only(只校验模式)", file=sys.stderr)
        sys.exit(VERIFY_EXIT_BADARG)
    selected = json.loads(args.select)

    all_entries = downloader.distributions.get("distributions", [])
    pinmap = {e.get("distribution"): e.get("pin") or "" for e in selected}
    wanted = {(e.get("distribution"), e.get("download_url")) for e in selected}
    subset = [
        {**e, "pin": pinmap.get(e.get("distribution")) or ""}
        for e in all_entries if (e.get("distribution"), e.get("download_url")) in wanted
    ]

    if not subset:
        print("错误: 没有匹配到任何选定的发行版条目", file=sys.stderr)
        sys.exit(1)

    # 按发行版名分组，逐组调用下载逻辑（组内一次下载多个版本文件）
    names = []
    for entry in subset:
        if entry["distribution"] not in names:
            names.append(entry["distribution"])

    failed = False
    for name in names:
        group = [e for e in subset if e["distribution"] == name]
        downloader.distributions = {"distributions": group}
        print(f"\n{'='*60}\n>>> 任务组: {name}（选源策略: {'A 固定优先级' if args.strategy == 'A' else 'B 实测选优'}）")

        for entry in group:
            fname = entry["download_url"].rstrip("/").rsplit("/", 1)[-1]
            dist_dir = _safe_dist_dir(Path(downloader.download_dir), entry.get("type", "linux"), entry.get("distribution", ""), flat=args.storage_mode == "flat")
            if dist_dir is None:
                print(f"错误: 发行版 {entry.get('distribution')} 的 type/distribution 不合法, 跳过", file=sys.stderr)
                failed = True
                continue
            dist_dir.mkdir(parents=True, exist_ok=True)
            filepath = dist_dir / fname

            # 依据策略选出有序候选源列表
            candidates = _pick_candidates(args.strategy, entry, downloader.headers)
            primary = candidates[0][0] if candidates else entry["download_url"]

            # 预先 HEAD 探测默认源的目标文件大小, 供 UI 显示下载进度条。
            # 注意上报的是 .part 路径: 下载期间字节都写在 .part 上(完成后才改名为
            # filepath), 后端 running_task() 对该路径 stat() 才能得到真实进度。
            # HEAD 失败时回退到候选源逐个探测(v1.2.9): 只探测第一个源的话, 该源
            # 恰好故障就会让 total=0, UI 进度条失去百分比基准(与"卡 0%"同类症状)。
            total = _head_target_size(primary, downloader.headers)
            if not total:
                for _u, _c in candidates[1:]:
                    total = _head_target_size(_u, downloader.headers)
                    if total:
                        print(f"  默认源未返回大小, 改用候选源探测: {total/1024/1024:.1f} MiB")
                        break
            part_path = Path(str(filepath) + PART_SUFFIX)
            # 标记行由后端拦截收集, 不写入任务日志
            print(f"#TARGET {part_path} {total}")
            if total:
                print(f"  目标大小: {total/1024/1024:.1f} MiB")
            else:
                print("  ⚠ 所有候选源均未返回文件大小, 进度条将无法显示百分比")

            if filepath.exists():
                print(f"文件已存在: {filepath}")
                # 必须传 dist=entry: 否则 GPG 验签被静默跳过(漏传 dist 的回归)
                ok, msg = downloader.verify_checksum_smart(
                    filepath, entry.get("checksum_url"), entry.get("checksum"), dist=entry
                )
                gpg_ledger.record_from_msg(entry, ok, msg)
                if ok is None:
                    # v1.3.17: 校验源不可达/清单无此文件名 → 无法判定, 但文件已在磁盘上。
                    # 旧代码与 False 同路 → "校验和验证失败, 将重新下载" → 每周期把
                    # 完整 ISO 重下一遍(Proxmox 因此循环 10 天)。现在: 保留不动。
                    print(f"⚠ {msg}")
                    print("  完整性无法判定(校验源不可用), 保留现有文件, 不重新下载")
                    _clear_failure(args.download_dir, entry.get("type", "linux"), name, fname)
                    # P2: 现有文件没验过 —— UI 必须能看出这一点, 否则用户以为它校验通过
                    _record_unverified(args.download_dir, entry.get("type", "linux"), name, fname, msg)
                    _drop_stale_part(part_path)
                    continue
                if ok:
                    print(f"✓ {msg}")
                    _clear_failure(args.download_dir, entry.get("type", "linux"), name, fname)
                    _clear_unverified(args.download_dir, entry.get("type", "linux"), name, fname)
                    # 完整文件已校验通过 -> 同名的 .part 是上一轮中断留下的冗余字节。
                    # 不清理会让 UI 长期误报「下载停止(已下载部分)」, 见 _drop_stale_part。
                    _drop_stale_part(part_path)
                    continue
                print(f"✗ {msg}")
                print("校验和验证失败, 将重新下载")

            # err_out: 收集最后一次失败原因, 供 _record_failure 落到 download_failures.json
            # (v1.3.17: 旧记录只有 {at, kind}, 排查时完全看不出是 404 还是超时)
            err_out: list = []
            ok, used_url = _download_file_with_failover(
                downloader, entry, candidates, fname, dist_dir, filepath,
                head_total=total, err_out=err_out, data_dir=args.download_dir,
            )
            if not ok:
                failed = True
                # 半成品(.part)保留 → 下次可续传, 记 stopped; 连 .part 都没有记 hard
                kind = "stopped" if part_path.exists() else "hard"
                _record_failure(args.download_dir, entry.get("type", "linux"), name, fname,
                                kind,
                                err=str(err_out[-1])[:300] if err_out else "",
                                url=used_url or primary)
            else:
                _clear_failure(args.download_dir, entry.get("type", "linux"), name, fname)

    if failed:
        sys.exit(1)
    print("\n>>> 所有选定条目处理完成")


if __name__ == "__main__":
    main()
