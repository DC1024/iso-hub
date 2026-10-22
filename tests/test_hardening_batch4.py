#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 4 批加固的验收测试 —— 对应审查报告 M8 / M10 / H1。

这批三项都跨"代码 + 部署面", 本地没有 Docker 可跑, 所以测试分两层:

  * **纯函数层**: 直接调 app.py 里的判定函数(IP 归属、备份目录解析、口令文件读取),
    这类断言不依赖容器;
  * **配置契约层**: 把 docker-compose*.yml 当成被测对象, 断言它们表达的隔离关系
    —— M8 的"备份卷不能被共享服务挂到"是**结构性**约束, 只在代码里改是拦不住的,
    谁往 samba 的 volumes 里加一行 `./backups:/srv/bk:ro` 就全废了。

为什么 M8 要盯着三个地方解析同一套规则:
  backup.py(写归档)、restore.py(写 pre-restore 快照)、app.py(/api/health 的可写
  探测与启动告警)各自独立解析输出目录。三份实现一旦漂移, 症状是"备份写到 A、
  恢复去 B 找、健康检查报 C" —— 单看任何一个文件都正常。所以这里做**一致性**断言。
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402

COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dockerhub.yml",
                 "docker-compose.acr.yml")


def _load_script(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        mod_name, str(REPO_ROOT / "scripts" / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bk = _load_script("batch4_backup", "backup.py")
rs = _load_script("batch4_restore", "restore.py")


def _env_dict(service) -> dict:
    """compose 的 environment 既可能是 list 也可能是 dict。"""
    env = service.get("environment", [])
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out = {}
    for item in env:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


def _host_paths(service) -> list:
    """volumes 里的宿主机侧路径。"""
    return [str(v).split(":", 1)[0] for v in service.get("volumes", [])]


# --------------------------------------------------------------- M8: 备份位置
class BackupDirResolutionTest(unittest.TestCase):
    """backup.py / restore.py / app.py 必须用同一套规则解析输出目录。"""

    DATA_DIRS = (Path("/data"), Path("/srv/iso-hub/data"), Path("/tmp/x"))

    def test_three_implementations_agree_without_env(self):
        env = {k: v for k, v in os.environ.items() if k != "ISO_HUB_BACKUP_DIR"}
        with patch.dict(os.environ, env, clear=True):
            for d in self.DATA_DIRS:
                with self.subTest(data_dir=str(d)):
                    want = bk._default_backup_dir(d)
                    self.assertEqual(rs._default_backup_dir(d), want)
                    self.assertEqual(app._default_backup_dir(d), want)

    def test_three_implementations_agree_with_env(self):
        with patch.dict(os.environ, {"ISO_HUB_BACKUP_DIR": "/mnt/nas/bk"}):
            for d in self.DATA_DIRS:
                with self.subTest(data_dir=str(d)):
                    for mod in (bk, rs, app):
                        self.assertEqual(mod._default_backup_dir(d),
                                         Path("/mnt/nas/bk"))

    def test_default_is_outside_the_data_dir(self):
        """M8 的根: 默认目录不能落在 data 里 —— 那是 SMB/WebDAV 的共享树。"""
        for d in self.DATA_DIRS:
            with self.subTest(data_dir=str(d)):
                out = app._default_backup_dir(d)
                self.assertNotEqual(out, d / "backups")
                with self.assertRaises(ValueError):
                    out.relative_to(d)

    def test_empty_env_is_treated_as_unset(self):
        """空字符串必须回落到默认值, 否则 .env 里留空的 `ISO_HUB_BACKUP_DIR=` 会把
        输出目录变成当前工作目录。"""
        with patch.dict(os.environ, {"ISO_HUB_BACKUP_DIR": "   "}):
            self.assertEqual(app._default_backup_dir(Path("/data")), Path("/backups"))


class ComposeBackupIsolationTest(unittest.TestCase):
    """M8 的部署面: 备份卷只挂给主容器, 共享服务一份都碰不到。"""

    @classmethod
    def setUpClass(cls):
        cls.docs = {n: yaml.safe_load((REPO_ROOT / n).read_text(encoding="utf-8"))
                    for n in COMPOSE_FILES}

    def test_main_container_mounts_dedicated_volume(self):
        for name, doc in self.docs.items():
            with self.subTest(compose=name):
                self.assertIn("./backups:/backups",
                              doc["services"]["iso-hub"]["volumes"])

    def test_main_container_env_points_at_that_mount(self):
        for name, doc in self.docs.items():
            with self.subTest(compose=name):
                env = _env_dict(doc["services"]["iso-hub"])
                raw = env.get("ISO_HUB_BACKUP_DIR", "")
                # 允许两种写法: 写死 /backups, 或 ${ISO_HUB_BACKUP_DIR:-/backups}
                # (后者才是可覆盖的, 也是我们要的默认值)
                self.assertIn("/backups", raw)

    def test_sidecars_never_mount_the_backup_volume(self):
        """这一条是整项 M8 的命门: 加一行挂载就把归档重新交出去了。"""
        for name, doc in self.docs.items():
            for svc in ("samba", "webdav", "qbittorrent"):
                for host in _host_paths(doc["services"][svc]):
                    with self.subTest(compose=name, service=svc, volume=host):
                        self.assertNotIn("backups", host)

    def test_sidecars_still_share_the_iso_tree(self):
        """反向兜底: 上面的断言不能靠"把共享挂载删掉"来满足。"""
        for name, doc in self.docs.items():
            with self.subTest(compose=name):
                self.assertIn("./data", _host_paths(doc["services"]["samba"]))
                self.assertIn("./data", _host_paths(doc["services"]["webdav"]))

    def test_backup_volume_is_outside_every_shared_host_path(self):
        """把"备份在哪"和"共享了什么"接起来断言 —— 这才是 M8 真正要保证的关系。"""
        backup_host = Path("./backups")
        for name, doc in self.docs.items():
            for svc in ("samba", "webdav"):
                for host in _host_paths(doc["services"][svc]):
                    with self.subTest(compose=name, service=svc, shared=host):
                        shared = Path(host)
                        self.assertNotEqual(backup_host, shared)
                        self.assertNotIn(shared, backup_host.parents,
                                         "备份目录落在共享路径之下, 归档会经共享泄露")


class BackupWritableTargetTest(unittest.TestCase):
    def test_backups_is_probed_but_optional(self):
        rows = {r[0]: r for r in app._writable_targets()}
        self.assertIn("backups", rows)
        _key, _label, path, required = rows["backups"]
        self.assertEqual(path, app.BACKUP_DIR)
        self.assertFalse(required, "备份卷是可选挂载, 写不了只该告警, 不该拦启动")

    def test_legacy_dir_warning_fires(self):
        """升级后旧归档不会自己搬家 —— 启动时必须点名, 否则会以为已经安全了。"""
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "backups").mkdir(parents=True)
            (data / "backups" / "isohub-backup-20260101-000000.tar.gz").write_bytes(b"x")
            msgs = []
            with patch.object(app, "DATA_DIR", data), \
                    patch.object(app, "BACKUP_DIR", data.parent / "backups"), \
                    patch.object(app, "log", msgs.append):
                self.assertTrue(app._warn_if_legacy_backup_dir())
            self.assertTrue(any("旧备份目录" in m for m in msgs), msgs)
            self.assertTrue(any("chown" not in m and "mv " in m for m in msgs), msgs)

    def test_legacy_warning_is_quiet_when_clean(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "backups").mkdir(parents=True)      # 目录在但没归档
            msgs = []
            with patch.object(app, "DATA_DIR", data), \
                    patch.object(app, "BACKUP_DIR", data.parent / "backups"), \
                    patch.object(app, "log", msgs.append):
                self.assertFalse(app._warn_if_legacy_backup_dir())
            self.assertEqual(msgs, [])

    def test_legacy_warning_skipped_when_backup_dir_is_legacy_dir(self):
        """用户显式把 ISO_HUB_BACKUP_DIR 指回 data/backups 时不该报"需要迁移"。"""
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "backups").mkdir(parents=True)
            (data / "backups" / "isohub-backup-20260101-000000.tar.gz").write_bytes(b"x")
            with patch.object(app, "DATA_DIR", data), \
                    patch.object(app, "BACKUP_DIR", data / "backups"), \
                    patch.object(app, "log", lambda _m: None):
                self.assertFalse(app._warn_if_legacy_backup_dir())


# --------------------------------------------------------------- M10: 机密走文件
class SecretFileTest(unittest.TestCase):
    def test_file_beats_env(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pw"
            p.write_text("from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"ISO_HUB_ADMIN_PASS": "from-env",
                                         "ISO_HUB_ADMIN_PASS_FILE": str(p)}):
                self.assertEqual(app._secret_env("ISO_HUB_ADMIN_PASS", "口令"), "from-file")

    def test_trailing_newline_is_stripped(self):
        """`echo pw > f` 会带换行; 不 strip 的话播种出来的口令永远对不上。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pw"
            p.write_text("  s3cret \r\n", encoding="utf-8")
            with patch.dict(os.environ, {"ISO_HUB_ADMIN_PASS_FILE": str(p)}):
                self.assertEqual(app._secret_env("ISO_HUB_ADMIN_PASS", "口令"), "s3cret")

    def test_missing_file_does_not_fall_back_to_env(self):
        """设了文件却读不到 = 配置错了。此时回退到环境变量会让人以为改生效了。"""
        msgs = []
        with patch.dict(os.environ, {"ISO_HUB_ADMIN_PASS": "from-env",
                                     "ISO_HUB_ADMIN_PASS_FILE": "/nope/not-there"}):
            with patch.object(app, "log", msgs.append):
                self.assertEqual(app._secret_env("ISO_HUB_ADMIN_PASS", "口令"), "")
        self.assertTrue(any("无法读取" in m for m in msgs), msgs)
        self.assertFalse(any("from-env" in m for m in msgs), "告警里不能带出值")

    def test_no_file_falls_back_to_env(self):
        with patch.dict(os.environ, {"ISO_HUB_ADMIN_PASS": " from-env ",
                                     "ISO_HUB_ADMIN_PASS_FILE": ""}):
            self.assertEqual(app._secret_env("ISO_HUB_ADMIN_PASS", "口令"), "from-env")


class SeedAdminFromFileTest(unittest.TestCase):
    def test_seeds_with_password_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            pw = Path(td) / "admin_pass"
            pw.write_text("S3cret-from-file\n", encoding="utf-8")
            msgs = []
            env = {"ISO_HUB_ADMIN_USER": "admin",
                   "ISO_HUB_ADMIN_PASS": "env-pass",
                   "ISO_HUB_ADMIN_PASS_FILE": str(pw)}
            with patch.dict(os.environ, env), \
                    patch.object(app, "SETTINGS_JSON", settings), \
                    patch.object(app, "log", msgs.append):
                app.seed_admin()
                self.assertIn("admin", app.load_users())
                self.assertTrue(app._check_login("admin", "S3cret-from-file"),
                                "文件里的口令应该生效")
                self.assertFalse(app._check_login("admin", "env-pass"),
                                 "设了 PASS_FILE 就不该再用 ADMIN_PASS")
            self.assertFalse(any("S3cret-from-file" in m for m in msgs),
                             "口令不得出现在日志里: %r" % msgs)

    def test_unreadable_file_seeds_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            settings = Path(td) / "settings.json"
            env = {"ISO_HUB_ADMIN_USER": "admin",
                   "ISO_HUB_ADMIN_PASS": "env-pass",
                   "ISO_HUB_ADMIN_PASS_FILE": str(Path(td) / "missing")}
            with patch.dict(os.environ, env), \
                    patch.object(app, "SETTINGS_JSON", settings), \
                    patch.object(app, "log", lambda _m: None):
                app.seed_admin()
                self.assertEqual(app.load_users(), {},
                                 "读不到口令文件时不该拿环境变量里的旧口令凑数")


class ComposeSecretPassthroughTest(unittest.TestCase):
    """compose 少透传一个变量, 用户在 .env 里配了也白配 —— 静默失效最难查。"""

    @classmethod
    def setUpClass(cls):
        cls.docs = {n: yaml.safe_load((REPO_ROOT / n).read_text(encoding="utf-8"))
                    for n in COMPOSE_FILES}

    def test_secret_file_vars_are_passed_through(self):
        for name, doc in self.docs.items():
            env = _env_dict(doc["services"]["iso-hub"])
            for var in ("ISO_HUB_ADMIN_PASS_FILE", "ISO_HUB_TOKEN_FILE",
                        "ISO_HUB_SECRET_KEY_FILE"):
                with self.subTest(compose=name, var=var):
                    self.assertIn(var, env)

    def test_admin_seed_vars_are_passed_through(self):
        """dockerhub 变体曾整个漏掉 ADMIN_USER/PASS —— 那条路径根本无法播种管理员。"""
        for name, doc in self.docs.items():
            env = _env_dict(doc["services"]["iso-hub"])
            for var in ("ISO_HUB_ADMIN_USER", "ISO_HUB_ADMIN_PASS"):
                with self.subTest(compose=name, var=var):
                    self.assertIn(var, env)


# ------------------------------------------------------------------- H1: 首次建号
class IpClassifyTest(unittest.TestCase):
    LAN = ("127.0.0.1", "::1", "192.168.1.5", "192.168.43.18", "10.0.0.1",
           "172.16.0.1", "172.31.255.254", "169.254.1.1", "fc00::1",
           "fd12:3456::1", "fe80::1", "::ffff:192.168.1.5")
    WAN = ("8.8.8.8", "1.1.1.1", "223.5.5.5", "172.32.0.1", "172.15.255.255",
           "2001:4860:4860::8888", "", "not-an-ip", "999.1.1.1")

    def test_private_and_loopback_are_lan(self):
        for ip in self.LAN:
            with self.subTest(ip=ip):
                self.assertTrue(app._ip_is_lan(ip), f"{ip} 应判为内网")

    def test_public_is_not_lan(self):
        for ip in self.WAN:
            with self.subTest(ip=ip):
                self.assertFalse(app._ip_is_lan(ip), f"{ip} 不该判为内网")

    def test_ipv4_mapped_is_unwrapped(self):
        """::ffff:192.168.1.5 若不还原成 v4, 会落到 ::ffff:0:0/96 上判反。"""
        self.assertTrue(app._ip_is_lan("::ffff:192.168.1.5"))
        self.assertFalse(app._ip_is_lan("::ffff:8.8.8.8"))


class BootstrapPolicyTest(unittest.TestCase):
    """_bootstrap_denied_reason(): 空串=放行, 非空=拒绝(内容直接回给客户端)。"""

    @staticmethod
    def _reason(ip, xff=None, trust_proxy=False):
        headers = {"X-Forwarded-For": xff} if xff else {}
        with patch.object(app, "TRUST_PROXY", trust_proxy), \
                app.app.test_request_context("/api/user/login",
                                             environ_base={"REMOTE_ADDR": ip},
                                             headers=headers):
            return app._bootstrap_denied_reason()

    def test_lan_without_proxy_is_allowed(self):
        for ip in ("127.0.0.1", "::1", "192.168.43.18", "10.8.0.2"):
            with self.subTest(ip=ip):
                self.assertEqual(self._reason(ip), "")

    def test_public_without_proxy_is_denied(self):
        self.assertNotEqual(self._reason("8.8.8.8"), "")

    def test_forwarded_header_without_trust_is_denied(self):
        """没设 TRUST_PROXY 却收到 XFF: 说明前面有看不穿的代理, 拒绝并点名该设的变量。"""
        reason = self._reason("192.168.43.18", xff="8.8.8.8")
        self.assertIn("ISO_HUB_TRUST_PROXY", reason)

    def test_forwarded_header_with_trust_still_checks_real_ip(self):
        """开了信任代理也只是"改看真实 IP", 公网照样拒绝。"""
        self.assertNotEqual(self._reason("8.8.8.8", xff="8.8.8.8", trust_proxy=True), "")
        self.assertEqual(self._reason("192.168.43.18", xff="192.168.43.18",
                                      trust_proxy=True), "")


class LoginBootstrapEndToEndTest(unittest.TestCase):
    """走完整 HTTP 栈: 局域网来源能建号, 公网来源 403 且不建号。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self._tmp.name) / "settings.json"
        self._env = patch.dict(os.environ, {
            k: v for k, v in os.environ.items()
            if k not in ("ISO_HUB_ADMIN_USER", "ISO_HUB_ADMIN_PASS",
                         "ISO_HUB_ADMIN_PASS_FILE", "ISO_HUB_TOKEN",
                         "ISO_HUB_TOKEN_FILE")}, clear=True)
        self._env.start()
        self._settings_patch = patch.object(app, "SETTINGS_JSON", self.settings)
        self._settings_patch.start()
        self._sessions_backup = dict(app._sessions)
        app._sessions.clear()

    def tearDown(self):
        app._sessions.clear()
        app._sessions.update(self._sessions_backup)
        self._settings_patch.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _login(self, ip, user="admin", pw="pw-123456"):
        return app.app.test_client().post(
            "/api/user/login", json={"username": user, "password": pw},
            environ_base={"REMOTE_ADDR": ip})

    def test_lan_client_can_bootstrap(self):
        r = self._login("192.168.43.50")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json().get("token"))
        self.assertIn("admin", app.load_users())

    def test_public_client_is_refused_and_creates_nothing(self):
        r = self._login("8.8.8.8")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(app.load_users(), {}, "公网来源不能建号")

    def test_forwarded_header_blocks_bootstrap_when_proxy_untrusted(self):
        r = app.app.test_client().post(
            "/api/user/login", json={"username": "admin", "password": "pw-123456"},
            environ_base={"REMOTE_ADDR": "192.168.43.50"},
            headers={"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(app.load_users(), {})

    def test_second_login_goes_through_normal_auth(self):
        """建号之后同一路径必须回到正常校验 —— 不能因为"用户表非空"就放行任意口令。"""
        self.assertEqual(self._login("192.168.43.50").status_code, 200)
        bad = self._login("192.168.43.50", pw="wrong")
        self.assertEqual(bad.status_code, 401)


class ProxyFixWiringTest(unittest.TestCase):
    """ProxyFix 只在显式开启时包上去。

    必须在**子进程**里验证: 这个决定发生在 import 期, 在当前进程里 reload 会换掉
    sys.modules['app'], 后面所有测试都会拿着一个孤儿模块跑。
    """

    SNIPPET = (
        "import sys; sys.path.insert(0, {web!r}); import app; "
        "from werkzeug.middleware.proxy_fix import ProxyFix; "
        "print('WRAPPED' if isinstance(app.app.wsgi_app, ProxyFix) else 'PLAIN')"
    )

    def _run(self, trust_proxy):
        env = {k: v for k, v in os.environ.items() if k != "ISO_HUB_TRUST_PROXY"}
        if trust_proxy:
            env["ISO_HUB_TRUST_PROXY"] = "1"
        out = subprocess.run(
            [sys.executable, "-c", self.SNIPPET.format(web=str(REPO_ROOT / "web"))],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return out.stdout.strip().splitlines()[-1]

    def test_default_is_plain(self):
        self.assertEqual(self._run(False), "PLAIN")

    def test_flag_wraps_wsgi_app(self):
        self.assertEqual(self._run(True), "WRAPPED")


class UserRouteUsesCorePolicyTest(unittest.TestCase):
    def test_user_route_has_no_inline_localhost_check(self):
        """防止有人把裸的 remote_addr 判定加回去 —— 那正是 H1 要消灭的写法。"""
        src = (REPO_ROOT / "web" / "api" / "user.py").read_text(encoding="utf-8")
        self.assertNotIn('request.remote_addr not in', src)
        self.assertIn("_bootstrap_denied_reason", src)


class ComposeTrustProxyTest(unittest.TestCase):
    def test_trust_proxy_is_passed_through(self):
        """compose 不透传的话, 用户在 .env 里配了 ISO_HUB_TRUST_PROXY 也不生效。"""
        for name in COMPOSE_FILES:
            doc = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
            with self.subTest(compose=name):
                self.assertIn("ISO_HUB_TRUST_PROXY",
                              _env_dict(doc["services"]["iso-hub"]))


if __name__ == "__main__":
    unittest.main()
