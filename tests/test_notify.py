#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载完成邮件通知(beta 2.2)回归测试。

需求: 「镜像列表」下载任务结束后发一封邮件告知结果。**种子下载不做** ——
qBittorrent 自带邮件通知, 再发一遍是重复打扰。

本文件锁死的契约:
  1. `web/notifier.py` 是纯模块: 不写 settings.json、不 import app、只依赖标准库;
  2. 配置归一化 / 脱敏(password 永不回传) / 必填校验;
  3. 报文: 成功与失败分别成文, 失败要有单独一节;
  4. 发送: SSL 走 SMTP_SSL、STARTTLS 走 SMTP+starttls、任何异常都转成 (False, detail)
     而**不外抛** —— 邮件失败绝不能把下载任务的收尾流程炸掉;
  5. 触发面只有 `kind == "download"`; 种子同步/kind=sync/meta/取消任务都不发;
  6. app 侧 `#TARGET` 之外的失败账本(download_failures.json)才是"这个文件到底成没成"
     的权威依据, 不能拿进程退出码猜;
  7. 前端契约: DOM id / i18n key / 接口路径 / 接线。

端口选择有实测依据(2026-09-12): 腾讯云 Lighthouse 的宿主与容器内 25 端口出站都不通,
465/587 通 —— 所以默认端口与安全模式必须落在 SSL/465。
"""

import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402
import notifier  # noqa: E402

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

SMTP_IDS = ["#mail-host", "#mail-port", "#mail-sec", "#mail-user", "#mail-pass",
            "#mail-to", "#sw-mail", "#mail-hint"]
I18N_KEYS = ["mailNotify", "mailNotifyChip", "mailNotifyDesc", "mailHost", "mailPort",
             "mailSecurity", "mailSecSsl", "mailSecStarttls", "mailSecNone",
             "mailPassword", "mailTo", "mailSendTest", "mailSaved", "mailTesting",
             "mailNeedServer", "mailHintMissing", "mailHintReady", "mailHintOff"]


def cfg_ok(**kw):
    base = {"enabled": True, "smtp_host": "smtp.example.org", "mail_to": "a@b.c",
            "username": "u", "password": "p"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- A. 纯模块边界
class TestNotifierIsPure(unittest.TestCase):

    def test_settings_json_is_not_a_literal_in_the_module(self):
        """文档里可以提到它, 但**代码里**不能出现这个字符串 —— 出现就说明它在自己拼路径。

        用 AST 取所有字符串常量(而不是全文 grep), 否则写在 docstring 里的设计说明
        (本项目刻意记了"为什么要绕开它")会被误判成违规; 模块自己的 docstring 同样不计。
        """
        import ast
        tree = ast.parse((REPO_ROOT / "web" / "notifier.py").read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree, clean=False)
        lits = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value is not doc]
        self.assertNotIn("settings.json", lits,
                         "notifier 不该知道配置文件叫什么 —— 配置由 app 传进来")

    def test_does_not_import_app(self):
        """反向 import 会造成循环依赖(app 单向依赖 notifier)。"""
        import ast
        tree = ast.parse((REPO_ROOT / "web" / "notifier.py").read_text(encoding="utf-8"))
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module.split(".")[0])
        self.assertNotIn("app", names, "notifier 不能反向依赖 app")

    def test_only_stdlib_imports(self):
        src = (REPO_ROOT / "web" / "notifier.py").read_text(encoding="utf-8")
        mods = {m.split(".")[0]
                for m in re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", src, re.M)}
        allowed = {"email", "smtplib", "ssl"}
        self.assertTrue(mods <= allowed, "混进了非标准库依赖: %s" % (mods - allowed))

    def test_default_port_is_465_not_25(self):
        """25 在云主机上是被封的出站端口, 默认值必须落在能通的那个上。"""
        self.assertEqual(notifier.normalize({})["security"], "ssl")
        self.assertEqual(notifier.normalize({})["smtp_port"], 465)
        self.assertEqual(notifier.DEFAULT_PORTS["starttls"], 587)


# --------------------------------------------------------------------------- B. 配置
class TestNormalize(unittest.TestCase):

    def test_none_and_garbage_converge(self):
        for raw in (None, {}, [], "x", 42, {"enabled": "yes", "smtp_port": "abc"}):
            self.assertEqual(notifier.normalize(raw)["smtp_port"], 465)

    def test_illegal_security_falls_back_to_ssl(self):
        self.assertEqual(notifier.normalize({"security": "PLAIN"})["security"], "ssl")
        self.assertEqual(notifier.normalize({"security": "starttls"})["security"],
                         "starttls")

    def test_explicit_port_is_kept_and_clamped(self):
        self.assertEqual(notifier.normalize({"smtp_port": 2525})["smtp_port"], 2525)
        self.assertEqual(notifier.normalize({"smtp_port": 99999})["smtp_port"], 465)
        self.assertEqual(notifier.normalize({"smtp_port": -1})["smtp_port"], 465)

    def test_explicit_port_follows_security_default(self):
        self.assertEqual(notifier.normalize({"security": "starttls"})["smtp_port"], 587)

    def test_booleans_and_strings_are_coerced(self):
        n = notifier.normalize({"enabled": 1, "mail_to": "  x@y.z  ", "username": None})
        self.assertIs(n["enabled"], True)
        self.assertEqual(n["mail_to"], "x@y.z")
        self.assertEqual(n["username"], "")

    def test_unknown_keys_are_dropped(self):
        n = notifier.normalize({"hack": 1})
        self.assertNotIn("hack", n)
        for k in notifier.DEFAULTS:
            self.assertIn(k, n, "丢掉了默认键 %s" % k)


class TestMissingAndReady(unittest.TestCase):

    def test_host_and_recipient_are_mandatory(self):
        self.assertEqual(notifier.missing_fields(notifier.normalize({})),
                         ["smtp_host", "mail_to"])

    def test_password_only_required_when_username_given(self):
        n = notifier.normalize({"smtp_host": "h", "mail_to": "a@b.c", "username": "u"})
        self.assertEqual(notifier.missing_fields(n), ["password"])

    def test_anonymous_relay_needs_no_credentials(self):
        n = notifier.normalize({"smtp_host": "h", "mail_to": "a@b.c"})
        self.assertEqual(notifier.missing_fields(n), [])

    def test_disabled_is_never_ready(self):
        self.assertFalse(notifier.is_ready(notifier.normalize(cfg_ok(enabled=False))))
        self.assertTrue(notifier.is_ready(notifier.normalize(cfg_ok())))


class TestRedact(unittest.TestCase):

    def test_password_never_comes_back(self):
        safe = notifier.redact(notifier.normalize(cfg_ok()))
        self.assertNotIn("p", [safe[k] for k in safe if isinstance(safe[k], str)])
        self.assertNotIn("password", safe)
        self.assertTrue(safe["password_set"])

    def test_empty_password_reported_as_unset(self):
        self.assertFalse(notifier.redact(notifier.normalize({}))["password_set"])

    def test_other_fields_survive(self):
        safe = notifier.redact(notifier.normalize(cfg_ok(smtp_host="h")))
        self.assertEqual(safe["smtp_host"], "h")


class TestMerge(unittest.TestCase):

    def test_patch_applies_on_top_of_base(self):
        merged = notifier.merge(notifier.normalize(cfg_ok()), {"mail_to": "z@z.z"})
        self.assertEqual(merged["mail_to"], "z@z.z")
        self.assertEqual(merged["smtp_host"], "smtp.example.org")

    def test_blank_password_is_not_a_wipe(self):
        """「密码留空 = 不修改」这条由调用方把 password 摘掉实现, merge 本身必须原样保留。"""
        merged = notifier.merge(notifier.normalize(cfg_ok()), {"mail_to": "z@z.z"})
        self.assertEqual(merged["password"], "p")


# --------------------------------------------------------------------------- C. 报文
class TestBuildReport(unittest.TestCase):

    ENTRIES = [
        {"filename": "ubuntu.iso", "size": 2 * 1024 ** 3, "failed": False, "reason": ""},
        {"filename": "debian.iso", "size": 0, "failed": True, "reason": "下载失败"},
    ]

    def test_subject_marks_partial_failure(self):
        subject, _ = notifier.build_report("下载: X", self.ENTRIES)
        self.assertIn("1/2", subject)
        self.assertIn("失败", subject)

    def test_subject_is_clean_when_all_ok(self):
        subject, body = notifier.build_report("T", self.ENTRIES[:1])
        self.assertNotIn("失败", subject)
        self.assertIn("1 个文件", subject)

    def test_body_lists_every_file(self):
        _, body = notifier.build_report("T", self.ENTRIES)
        self.assertIn("ubuntu.iso", body)
        self.assertIn("debian.iso", body)
        self.assertIn("2 GiB", body)

    def test_failures_get_their_own_section(self):
        _, body = notifier.build_report("T", self.ENTRIES)
        self.assertIn("失败明细", body)
        self.assertIn("下载失败", body)

    def test_no_failure_no_section(self):
        _, body = notifier.build_report("T", self.ENTRIES[:1])
        self.assertNotIn("失败明细", body)

    def test_empty_entries_still_renders(self):
        subject, body = notifier.build_report("T", [], exit_code=1, duration=3)
        self.assertTrue(subject)
        self.assertIn("没有文件明细", body)
        self.assertIn("退出码: 1", body)

    def test_duration_is_human_readable(self):
        self.assertEqual(notifier._fmt_duration(45), "45 秒")
        self.assertEqual(notifier._fmt_duration(65), "1 分 5 秒")
        self.assertEqual(notifier._fmt_duration(3725), "1 小时 2 分 5 秒")
        self.assertEqual(notifier._fmt_duration("junk"), "?")

    def test_size_units(self):
        self.assertEqual(notifier._fmt_size(512), "512 B")
        self.assertEqual(notifier._fmt_size(1024), "1 KiB")
        self.assertEqual(notifier._fmt_size(-5), "?")

    def test_test_mail_mentions_host(self):
        subject, body = notifier.build_test_mail("smtp.x:465")
        self.assertIn("测试", subject)
        self.assertIn("smtp.x:465", body)


class TestShouldNotify(unittest.TestCase):

    def test_disabled_or_incomplete_never_sends(self):
        self.assertFalse(notifier.should_notify(notifier.normalize({}),
                                                [{"failed": False}]))
        self.assertFalse(notifier.should_notify(
            notifier.normalize({"smtp_host": "h"}), [{"failed": False}]))

    def test_success_switch_governs_clean_runs(self):
        self.assertTrue(notifier.should_notify(notifier.normalize(cfg_ok()),
                                               [{"failed": False}]))
        self.assertFalse(notifier.should_notify(
            notifier.normalize(cfg_ok(on_success=False)), [{"failed": False}]))

    def test_failure_switch_governs_failed_runs(self):
        self.assertTrue(notifier.should_notify(notifier.normalize(cfg_ok()),
                                               [{"failed": True}]))
        self.assertFalse(notifier.should_notify(
            notifier.normalize(cfg_ok(on_failure=False)), [{"failed": True}]))


# --------------------------------------------------------------------------- D. 发送
class _FakeSMTP:
    """记录调用序列的 SMTP 桩: 关键是**哪条路径**被走到了。"""

    def __init__(self, raise_on=None):
        self.calls = []
        self.sent = None
        self.closed = False
        self._raise_on = raise_on or set()

    def _maybe_raise(self, what):
        if what in self._raise_on:
            raise OSError("%s 炸了" % what)

    def ehlo(self):
        self.calls.append("ehlo")
        self._maybe_raise("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        self._maybe_raise("starttls")

    def login(self, u, p):
        self.calls.append("login")
        self._maybe_raise("login")

    def sendmail(self, frm, to, msg):
        self.calls.append("sendmail")
        self.sent = (frm, to, msg)
        self._maybe_raise("sendmail")
        return {}

    def quit(self):
        self.calls.append("quit")
        self.closed = True


class TestSendBranches(unittest.TestCase):

    def _run(self, cfg, fake, enhance=None):
        with patch.object(notifier.smtplib, "SMTP_SSL", return_value=fake) as ssl_m, \
             patch.object(notifier.smtplib, "SMTP", return_value=fake) as smtp_m:
            ok, detail = notifier.send(cfg, "s", "b")
        return ok, detail, ssl_m, smtp_m

    def test_ssl_uses_smtp_ssl_on_465(self):
        fake = _FakeSMTP()
        ok, detail, ssl_m, smtp_m = self._run(notifier.normalize(cfg_ok()), fake)
        self.assertTrue(ok, detail)
        ssl_m.assert_called_once()
        smtp_m.assert_not_called()
        self.assertEqual(ssl_m.call_args[0][:2], ("smtp.example.org", 465))
        self.assertIn("login", fake.calls)
        self.assertIn("sendmail", fake.calls)
        self.assertTrue(fake.closed, "连接必须关闭")

    def test_starttls_uses_plain_smtp_then_upgrades(self):
        fake = _FakeSMTP()
        ok, detail, ssl_m, smtp_m = self._run(
            notifier.normalize(cfg_ok(security="starttls")), fake)
        self.assertTrue(ok, detail)
        ssl_m.assert_not_called()
        self.assertEqual(smtp_m.call_args[0][:2], ("smtp.example.org", 587))
        self.assertLess(fake.calls.index("starttls"), fake.calls.index("login"),
                        "STARTTLS 必须在登录之前完成, 否则密码是明文出去的")

    def test_none_mode_does_not_upgrade(self):
        fake = _FakeSMTP()
        ok, _, _, _ = self._run(notifier.normalize(cfg_ok(security="none")), fake)
        self.assertTrue(ok)
        self.assertNotIn("starttls", fake.calls)

    def test_anonymous_relay_skips_login(self):
        fake = _FakeSMTP()
        notifier.send(notifier.normalize({"enabled": True, "smtp_host": "h",
                                          "mail_to": "a@b.c", "security": "none"}),
                      "s", "b")
        self.assertNotIn("login", fake.calls)

    def test_timeout_is_always_bounded(self):
        fake = _FakeSMTP()
        with patch.object(notifier.smtplib, "SMTP_SSL", return_value=fake) as ssl_m:
            notifier.send(notifier.normalize(cfg_ok()), "s", "b", timeout="junk")
        kw = ssl_m.call_args[1]
        self.assertIn("timeout", kw)
        self.assertGreaterEqual(kw["timeout"], 1)
        self.assertLessEqual(kw["timeout"], notifier.MAX_TIMEOUT)

    def test_connection_closed_even_when_it_blows_up(self):
        fake = _FakeSMTP(raise_on={"sendmail"})
        ok, detail, _, _ = self._run(notifier.normalize(cfg_ok()), fake)
        self.assertFalse(ok)
        self.assertIn("OSError", detail, "异常类型要带出来, 否则排障只能猜")
        self.assertTrue(fake.closed, "异常路径也必须关连接")

    def test_bad_config_short_circuits_without_socket(self):
        with patch.object(notifier.smtplib, "SMTP_SSL", side_effect=AssertionError("不该连网")):
            ok, detail = notifier.send(notifier.normalize({}), "s", "b")
        self.assertFalse(ok)
        self.assertIn("缺少必填配置", detail)

    def test_multiple_recipients_are_split(self):
        fake = _FakeSMTP()
        with patch.object(notifier.smtplib, "SMTP_SSL", return_value=fake):
            notifier.send(notifier.normalize(cfg_ok(mail_to="a@b.c; d@e.f")), "s", "b")
        self.assertEqual(fake.sent[1], ["a@b.c", "d@e.f"])

    def test_refused_recipients_are_reported(self):
        class Refuser(_FakeSMTP):
            def sendmail(self, frm, to, msg):
                return {"d@e.f": (550, "no")}
        fake = Refuser()
        with patch.object(notifier.smtplib, "SMTP_SSL", return_value=fake):
            ok, detail = notifier.send(notifier.normalize(cfg_ok()), "s", "b")
        self.assertFalse(ok)
        self.assertIn("拒收", detail)

    def test_non_ascii_subject_is_encoded(self):
        fake = _FakeSMTP()
        with patch.object(notifier.smtplib, "SMTP_SSL", return_value=fake):
            notifier.send(notifier.normalize(cfg_ok()), "下载完成 ✓", "正文")
        self.assertIn("utf-8", fake.sent[2].lower())


# --------------------------------------------------------------------------- E. app 集成
class NotifyAppBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        # 基线: 一份**已启用**的完整配置。不发信的用例各自把开关/补丁改掉来对照。
        (self.data / "settings.json").write_text(
            json.dumps({"notify": cfg_ok()}, ensure_ascii=False), encoding="utf-8")
        (self.data / "distributions.json").write_text(
            json.dumps({"updated_at": 0, "distributions": []}), encoding="utf-8")
        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.data / "distributions.json"),
            patch.object(app, "SETTINGS_JSON", self.data / "settings.json"),
            patch.object(app, "FAILURES_JSON", self.data / "download_failures.json"),
            patch.object(app, "REQUIRE_LOGIN", False),
            patch.object(app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = app.app.test_client()
        self._log_len = len(app._log_lines)

    def tearDown(self):
        self._tmp.cleanup()

    def new_logs(self):
        return [d["l"] for d in list(app._log_lines)[self._log_len:]]

    def saved(self):
        return json.loads((self.data / "settings.json").read_text(encoding="utf-8"))

    def write_failures(self, obj):
        (self.data / "download_failures.json").write_text(
            json.dumps(obj, ensure_ascii=False), encoding="utf-8")


class TestNotifyApi(NotifyAppBase):

    def test_get_returns_redacted_defaults(self):
        (self.data / "settings.json").write_text("{}", encoding="utf-8")
        r = self.client.get("/api/notify")
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertNotIn("password", j["config"])
        self.assertFalse(j["ready"])
        self.assertEqual(j["missing"], ["smtp_host", "mail_to"])
        self.assertEqual(j["config"]["smtp_port"], 465, "UI 要能直接显示推荐的默认端口")

    def test_post_saves_and_reads_back(self):
        r = self.client.post("/api/notify", json=cfg_ok())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ready"])
        self.assertEqual(self.saved()["notify"]["smtp_host"], "smtp.example.org")
        self.assertEqual(self.saved()["notify"]["password"], "p")

    def test_password_is_never_echoed_back(self):
        self.client.post("/api/notify", json=cfg_ok(password="topsecret"))
        body = self.client.get("/api/notify").get_data(as_text=True)
        self.assertNotIn("topsecret", body)
        self.assertIn("password_set", body)

    def test_blank_password_keeps_the_saved_one(self):
        self.client.post("/api/notify", json=cfg_ok())
        self.client.post("/api/notify",
                         json={"mail_to": "new@x.y", "password": ""})
        self.assertEqual(self.saved()["notify"]["password"], "p")
        self.assertEqual(self.saved()["notify"]["mail_to"], "new@x.y")

    def test_partial_post_does_not_wipe_other_fields(self):
        self.client.post("/api/notify", json=cfg_ok())
        self.client.post("/api/notify", json={"username": "u2"})
        n = self.saved()["notify"]
        self.assertEqual(n["username"], "u2")
        self.assertEqual(n["smtp_host"], "smtp.example.org")

    def test_illegal_values_are_normalized_before_saving(self):
        self.client.post("/api/notify", json=cfg_ok(security="junk", smtp_port=0))
        n = self.saved()["notify"]
        self.assertEqual(n["security"], "ssl")
        self.assertEqual(n["smtp_port"], 465)

    def test_save_leaves_no_temp_file(self):
        self.client.post("/api/notify", json=cfg_ok())
        self.assertEqual([p.name for p in self.data.iterdir() if ".tmp" in p.name], [])

    def test_save_goes_through_the_single_writer(self):
        seen = []
        real = app.config_files.update_json

        def spy(path, patch_, **kw):
            seen.append(Path(path))
            return real(path, patch_, **kw)

        with patch.object(app.config_files, "update_json", side_effect=spy):
            self.client.post("/api/notify", json=cfg_ok())
        self.assertEqual(seen, [self.data / "settings.json"],
                         "邮件配置也必須走 config_files 这条唯一写通道")

    def test_test_endpoint_does_not_persist(self):
        """「先试后存」的关键: 试发不能改任何已落盘的值(基类 setUp 里已有一份配置)。"""
        before = self.saved()
        with patch.object(notifier, "send", return_value=(True, "ok")) as send:
            r = self.client.post("/api/notify/test",
                                 json={"config": cfg_ok(mail_to="probe@x.y")})
        self.assertEqual(r.status_code, 200)
        send.assert_called_once()
        self.assertEqual(self.saved(), before, "测试邮件不该顺手把配置存了")

    def test_test_endpoint_reuses_saved_password(self):
        self.client.post("/api/notify", json=cfg_ok())
        with patch.object(notifier, "send", return_value=(True, "ok")) as send:
            self.client.post("/api/notify/test", json={"config": {"mail_to": "z@z.z"}})
        self.assertEqual(send.call_args[0][0]["password"], "p",
                         "表单没填密码时应沿用已保存的")

    def test_test_failure_is_400_with_detail(self):
        with patch.object(notifier, "send", return_value=(False, "boom")):
            r = self.client.post("/api/notify/test", json={"config": cfg_ok()})
        self.assertEqual(r.status_code, 400)
        self.assertIn("boom", r.get_json()["detail"])

    def test_test_endpoint_logs_the_outcome(self):
        with patch.object(notifier, "send", return_value=(False, "nope")):
            self.client.post("/api/notify/test", json={"config": cfg_ok()})
        self.assertTrue(any("邮件" in x for x in self.new_logs()),
                        "测试邮件成功/失败都要在日志里留痕")


class TestNotifyEntries(NotifyAppBase):
    """失败判定以 download_failures.json 为准, 不拿退出码猜。"""

    def setUp(self):
        super().setUp()
        self.dl = self.data / "linux" / "Ubuntu"
        self.dl.mkdir(parents=True)
        (self.dl / "ubuntu.iso").write_bytes(b"x" * 2048)
        self.snap = {"downloads": [
            {"filename": "ubuntu.iso", "path": str(self.dl / "ubuntu.iso.part")}]}

    def test_clean_file_is_success_with_size(self):
        entries = app._notify_entries(self.snap)
        self.assertEqual(entries, [{"filename": "ubuntu.iso", "size": 2048,
                                    "failed": False, "reason": ""}])

    def test_failure_ledger_marks_the_file(self):
        self.write_failures({str(self.dl / "ubuntu.iso"): {"at": 0, "kind": "hard"}})
        entries = app._notify_entries(self.snap)
        self.assertTrue(entries[0]["failed"])
        self.assertIn("下载失败", entries[0]["reason"])

    def test_part_path_lookup(self):
        """.part 路径要去后缀后再查账本 —— 账本记的是最终名。"""
        self.write_failures({str(self.dl / "ubuntu.iso"): {"at": 0, "kind": "stopped"}})
        e = app._notify_entries(self.snap)[0]
        self.assertTrue(e["failed"])
        self.assertIn("可续传", e["reason"])

    def test_filename_fallthrough(self):
        """runner 写账本用的是相对路径, 与 task 里的绝对路径不同形 -> 按文件名兜一层。"""
        self.write_failures({"linux/Ubuntu/ubuntu.iso": {"at": 0, "kind": "hard"}})
        self.assertTrue(app._notify_entries(self.snap)[0]["failed"])

    def test_missing_file_is_not_a_crash(self):
        snap = {"downloads": [{"filename": "ghost.iso", "path": "/nope/ghost.iso"}]}
        self.assertEqual(app._notify_entries(snap)[0]["size"], 0)

    def test_empty_task_has_no_entries(self):
        self.assertEqual(app._notify_entries({}), [])


class TestNotifyTrigger(NotifyAppBase):
    """什么情况下真的会发出去 —— 边界在 kind 与 cancelled。"""

    def _call(self, **kw):
        snap = {"kind": kw.get("kind", "download"), "title": "T", "downloads": [],
                "started": time.time() - 5, "finished": time.time(),
                "exit_code": kw.get("exit_code", 0),
                "cancelled": kw.get("cancelled", False)}
        with patch.object(notifier, "send", return_value=(True, "sent")) as send:
            app.notify_download_finished(snap)
        return send

    def test_download_sends(self):
        self.assertTrue(self._call().called)

    def test_other_kinds_stay_silent(self):
        for kind in ("sync", "meta", "custom-refresh", "torrent"):
            self.assertFalse(self._call(kind=kind).called, "kind=%s 不该发信" % kind)

    def test_cancelled_task_stays_silent(self):
        self.assertFalse(self._call(cancelled=True).called)

    def test_disabled_config_stays_silent(self):
        """基类 setUp 给的是**已启用**的配置, 所以这里必须显式关掉开关才有对照意义。"""
        with patch.object(app, "load_notify_config",
                          return_value=notifier.normalize(cfg_ok(enabled=False))):
            with patch.object(notifier, "send") as send:
                app.notify_download_finished({"kind": "download", "downloads": []})
        self.assertFalse(send.called)

    def test_send_exception_is_swallowed_and_logged(self):
        """协作-service 挂了不能把任务收尾拖死 —— 这条是整份功能的底线。"""
        with patch.object(notifier, "send", side_effect=RuntimeError("smtp down")):
            app.notify_download_finished({"kind": "download", "downloads": [],
                                          "enabled": True})
        self.assertTrue(any("邮件" in x for x in self.new_logs()))

    def test_config_read_failure_is_swallowed(self):
        with patch.object(app, "load_notify_config", side_effect=OSError("disk")):
            app.notify_download_finished({"kind": "download", "downloads": []})
        self.assertTrue(any("邮件" in x for x in self.new_logs()))

    def test_report_carries_duration_and_exit_code(self):
        self.client.post("/api/notify", json=cfg_ok())
        seen = {}

        def spy(cfg, subject, body, **kw):
            seen["subject"] = subject
            seen["body"] = body
            return True, "ok"

        snap = {"kind": "download", "title": "下载: Ubuntu", "downloads": [],
                "started": time.time() - 65, "finished": time.time(),
                "exit_code": 0, "cancelled": False}
        with patch.object(notifier, "send", side_effect=spy):
            app.notify_download_finished(snap)
        self.assertIn("1 分 5 秒", seen["body"])
        self.assertIn("退出码: 0", seen["body"])

    def test_worker_spawns_the_notifier_for_downloads(self):
        """端到端接线: 真跑一个子进程任务, 任务收尾必须把邮件发出去。

        REPO_DIR 必须 patch 成存在的目录 —— 它是 Popen 的 cwd, 本机上没有 /app/iso_download,
        不 patch 的话 Popen 直接 FileNotFoundError 走启动失败分支, 那这条测试会假绿。
        """
        self.client.post("/api/notify", json=cfg_ok())
        done = []

        def spy(cfg, subject, body, **kw):
            done.append(subject)
            return True, "ok"

        with patch.object(app, "REPO_DIR", self.data), \
             patch.object(notifier, "send", side_effect=spy):
            ok = app.start_task("download", "下载: X",
                                [sys.executable, "-c", "print('hi')"], [])
            self.assertTrue(ok)
            for _ in range(400):              # 最多等 4 秒
                if done:
                    break
                time.sleep(0.01)
        self.assertEqual(len(done), 1, "任务结束后邮件没发出去 —— 接线断了")
        self.assertIn("[iso-hub]", done[0])

    def test_worker_stays_silent_for_meta(self):
        self.client.post("/api/notify", json=cfg_ok())
        with patch.object(app, "REPO_DIR", self.data), \
             patch.object(notifier, "send") as send:
            app.start_task("meta", "刷新清单", [sys.executable, "-c", "print('x')"], [])
            for _ in range(200):
                if send.called:
                    break
                time.sleep(0.01)
        self.assertFalse(send.called, "kind=meta 也发了邮件")


# --------------------------------------------------------------------------- F. 前端契约
class TestFrontendContract(unittest.TestCase):

    def test_dom_ids_exist(self):
        for i in SMTP_IDS:
            self.assertRegex(HTML, r'id="%s"' % i[1:], "缺少输入框 %s" % i)

    def test_i18n_keys_defined_and_used(self):
        for k in I18N_KEYS:
            with self.subTest(key=k):
                self.assertRegex(HTML, r"'%s':\{zh:'[^']*',en:'[^']*'\}" % k,
                                 "%s 没有中英文对照" % k)
                used = re.findall(r"t\('%s'\)" % k, HTML) or \
                    re.findall(r'data-i18n="%s"' % k, HTML)
                self.assertTrue(used, "%s 定义了但没被使用" % k)

    def test_api_paths_are_wired(self):
        self.assertRegex(HTML, r"jget\('/api/notify'\)")
        self.assertRegex(HTML, r"jpost\('/api/notify'")
        self.assertRegex(HTML, r"jpost\('/api/notify/test'")

    def test_functions_exist_and_are_loaded(self):
        for fn in ("mailVal", "mailPayload", "paintMailNotify", "loadMailNotify",
                   "saveMailNotify", "testMailNotify"):
            self.assertRegex(HTML, r"function %s\(" % fn)
        self.assertRegex(HTML, r"loadMailNotify\(\);",
                         "loadSettings 里没有调用 loadMailNotify")

    def test_password_input_is_masked(self):
        m = re.search(r'<input[^>]*id="mail-pass"[^>]*>', HTML)
        self.assertTrue(m, "找不到密码输入框")
        self.assertIn('type="password"', m.group(0))

    def test_password_is_never_read_back_into_the_page(self):
        """GET 只回 password_set, 所以 JS 里也不该出现把密码塞回输入框的写法。"""
        self.assertNotIn("c.password", HTML)

    def test_blank_password_means_keep(self):
        """前端不能把空密码提交上去覆盖后端已存的那份。"""
        self.assertRegex(HTML, r"if\(pw\)p\.password=pw")


if __name__ == "__main__":
    unittest.main()
