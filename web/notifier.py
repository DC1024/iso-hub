#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载完成后的邮件通知(纯模块: 不读盘、不写盘、不碰 settings.json)。

设计边界(重要):
  * **本模块不写任何配置文件**。settings.json 的写入永远走 `web/config_files.update_json`
    (由 app.py 的 `save_settings_all()` 统一转调) —— 详见 `tests/test_v1316_settings_guard.py`,
    那个守卫会扫所有 NEWSettings 写方, 多一个裸 open() 就让 CI 变红。
  * **本模块也不 import app**。app.py 单向 import 本模块并**把配置作为参数传进来**,
    避免循环 import, 也让本模块可以被测试直接喂 dict。
  * 依赖只有标准库(smtplib + email.*), web/requirements.txt 不需要加任何东西。

为什么挂点是"下载任务"而不是"种子任务": 种子下载由 qBittorrent 负责, 它自带邮件通知,
再发一遍是重复。这里的入口只认 `kind == "download"`。

端口选择不是玄学(2026-09-12 在两台实例上实测):
  * 腾讯云 Lighthouse 宿主机 **和容器里**, `smtp.qq.com:25` 都不通(Errno 101 / 超时) ——
    国内云厂商默认封禁 25 出站。
  * 465(SMTPS) 与 587(STARTTLS) 两台都通。
  所以默认给 465 + SSL, 且**所有 socket 操作必须带 timeout** —— 否则一个黑洞 25 端口
  能把整个任务线程挂死。
"""
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
import smtplib
import ssl

# 安全模式 -> 默认端口。none = 明文(仅限内网中继, 不推荐)。
SECURITY_MODES = ("ssl", "starttls", "none")
DEFAULT_PORTS = {"ssl": 465, "starttls": 587, "none": 25}
DEFAULT_TIMEOUT = 15          # 秒; 单次连接/读写的兜底超时
MAX_TIMEOUT = 60

DEFAULTS = {
    "enabled": False,
    "smtp_host": "",
    "smtp_port": 0,      # 0 = 跟随 security 的默认端口
    "security": "ssl",
    "username": "",
    "password": "",
    "mail_from": "",     # 留空则用 username
    "mail_to": "",
    "on_success": True,  # 全部成功时是否发信
    "on_failure": True,  # 有失败时是否发信
}

SECRET_FIELDS = ("password",)


# --------------------------------------------------------------------------- 配置归一化
def normalize(raw=None) -> dict:
    """把任意来源的配置(dict/None/脏数据)收敛成合法 dict。

    不抛异常、不丢键: 未知原样返回的键一律按默认值处理, 端口必须是 1~65535,
    security 非法值收敛成 ssl(可用性最高的那个)。
    """
    cfg = dict(DEFAULTS)
    src = raw if isinstance(raw, dict) else {}
    for k in cfg:
        if k in src and src[k] is not None:
            cfg[k] = src[k]

    cfg["enabled"] = bool(cfg["enabled"])
    cfg["on_success"] = bool(cfg["on_success"])
    cfg["on_failure"] = bool(cfg["on_failure"])
    for k in ("smtp_host", "username", "password", "mail_from", "mail_to"):
        cfg[k] = str(cfg[k]).strip()

    sec = str(cfg["security"]).strip().lower()
    cfg["security"] = sec if sec in SECURITY_MODES else "ssl"

    port = cfg["smtp_port"]
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    if port <= 0 or port > 65535:
        port = DEFAULT_PORTS[cfg["security"]]
    cfg["smtp_port"] = port
    return cfg


def merge(base: dict, patch: dict) -> dict:
    """在既有配置上叠加一次局部修改, 产出可直接存回的完整配置。"""
    merged = dict(base) if isinstance(base, dict) else {}
    merged.update(patch if isinstance(patch, dict) else {})
    return normalize(merged)


def missing_fields(cfg: dict) -> list:
    """缺少哪些必填项。SMTP 服务器/收件人是硬门槛; 用户名留空视为匿名中继。"""
    need = []
    if not str(cfg.get("smtp_host") or "").strip():
        need.append("smtp_host")
    if not str(cfg.get("mail_to") or "").strip():
        need.append("mail_to")
    if str(cfg.get("username") or "").strip() and not str(cfg.get("password") or ""):
        need.append("password")
    return need


def is_ready(cfg: dict) -> bool:
    """开关打开且必填齐全 -> 可以发信。"""
    return bool(cfg.get("enabled")) and not missing_fields(cfg)


def redact(cfg: dict) -> dict:
    """把配置脱敏后交给前端: 密码永不回传, 只告诉 UI『有没有配过』。"""
    safe = dict(cfg or {})
    for k in SECRET_FIELDS:
        safe.pop(k, None)
        safe["%s_set" % k] = bool(str(cfg.get(k) or ""))
    return safe


# --------------------------------------------------------------------------- 报文
def _fmt_size(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "?"
    if n < 0:
        return "?"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return "%.0f %s" % (f, units[i]) if i else "%d B" % n


def _fmt_duration(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "?"
    if sec < 0:
        sec = 0
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d 小时 %d 分 %d 秒" % (h, m, s)
    if m:
        return "%d 分 %d 秒" % (m, s)
    return "%d 秒" % s


def summarize(entries: list) -> tuple:
    """统计明细 -> (总数, 成功数, 失败数)。"""
    total = len(entries or [])
    failed = sum(1 for e in (entries or []) if e.get("failed"))
    return total, total - failed, failed


def should_notify(cfg: dict, entries: list) -> bool:
    """按成功/失败开关决定是否要发 —— 全成功走 on_success, 有失败走 on_failure。"""
    if not is_ready(cfg):
        return False
    _, _, failed = summarize(entries)
    return bool(cfg.get("on_failure")) if failed else bool(cfg.get("on_success"))


def build_report(title: str, entries: list, exit_code=None, duration=None) -> tuple:
    """拼一封纯文本邮件的 (subject, body)。

    entries: [{"filename": str, "size": int, "failed": bool, "reason": str}]
    失败原因单独一节列出, 免得用户在一长串成功列表里找针。
    """
    entries = entries or []
    total, ok_n, fail_n = summarize(entries)

    if fail_n:
        subject = "[iso-hub] 下载完成（%d/%d 成功，%d 个失败）" % (ok_n, total, fail_n)
    else:
        subject = "[iso-hub] 下载完成：%d 个文件" % ok_n

    lines = [title or "下载任务", ""]
    if duration is not None:
        lines.append("耗时: %s" % _fmt_duration(duration))
    if exit_code is not None:
        lines.append("退出码: %s" % exit_code)
    lines.append("结果: 成功 %d / 失败 %d / 合计 %d" % (ok_n, fail_n, total))
    lines.append("")

    if entries:
        lines.append("文件明细:")
        for e in entries:
            name = str(e.get("filename") or "?")
            if e.get("failed"):
                why = str(e.get("reason") or "失败").strip()
                lines.append("  × %s  —— %s" % (name, why))
            else:
                lines.append("  √ %s  %s" % (name, _fmt_size(e.get("size"))))
    else:
        lines.append("（没有文件明细）")

    if fail_n:
        lines.append("")
        lines.append("失败明细:")
        for e in entries:
            if e.get("failed"):
                lines.append("  %s: %s" % (str(e.get("filename") or "?"),
                                           str(e.get("reason") or "失败").strip()))

    lines.append("")
    lines.append("—— 本邮件由 iso-hub 自动发送")
    return subject, "\n".join(lines)


def build_test_mail(host_hint: str = "") -> tuple:
    """「发送测试邮件」用的报文。"""
    subject = "[iso-hub] 这是一封测试邮件"
    body = "\n".join([
        "如果你收到这封邮件, 说明 iso-hub 的邮件通知已经配通。",
        "",
        "SMTP 服务器: %s" % (host_hint or "(未配置)"),
        "之后每次「镜像列表下载」任务结束都会按你设置的开关发信。",
        "",
        "—— 本邮件由 iso-hub 自动发送",
    ])
    return subject, body


# --------------------------------------------------------------------------- 发送
def send(cfg: dict, subject: str, body: str, timeout=DEFAULT_TIMEOUT) -> tuple:
    """发一封邮件, 返回 (ok, detail)。**不抛异常** —— 调用方在任务线程里, 炸了会丢任务状态。

    所有网络操作都在 timeout 内, 且连接必关(finally)。
    """
    cfg = normalize(cfg)
    miss = missing_fields(cfg)
    if miss:
        return False, "缺少必填配置: %s" % "、".join(miss)

    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    host = cfg["smtp_host"]
    port = cfg["smtp_port"]
    user = cfg["username"]
    pwd = cfg["password"]
    sender = cfg["mail_from"] or user or "iso-hub@localhost"
    to_list = [x.strip() for x in str(cfg["mail_to"]).replace(";", ",").split(",")
               if x.strip()]
    if not to_list:
        return False, "收件人为空"

    msg = MIMEText(body or "", "plain", "utf-8")
    msg["Subject"] = Header(str(subject or ""), "utf-8")
    msg["From"] = formataddr(("iso-hub", sender)) if "@" in sender else sender
    msg["To"] = ", ".join(to_list)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid("isohub")

    server = None
    try:
        if cfg["security"] == "ssl":
            ctx = ssl.create_default_context()
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.ehlo()
            if cfg["security"] == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
        if user:
            server.login(user, pwd)
        refused = server.sendmail(sender, to_list, msg.as_string())
        if refused:
            return False, "服务器拒收: %s" % ", ".join(str(v) for v in refused)
        return True, "已发送到 %s" % ", ".join(to_list)
    except Exception as e:  # noqa: BLE001 —— 邮件失败绝不能影响下载任务本身
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 —— quit 失败不影响投递结果
                pass
