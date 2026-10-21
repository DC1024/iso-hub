#!/usr/bin/env python3
"""v1.3.21 安全加固回归测试 —— 共享凭据下发方式 + 容器能力收敛。

背景(来自 2026-10-21 代码审查报告的 B 类三项):
  1. 容器以 root 运行        -> compose 增加 cap_drop=ALL + no-new-privileges
  2. Samba 凭据进容器 Cmd    -> 改为"写账号文件 + restart", 不再 create/rm 容器
  3. socket-proxy POST=1     -> 换成显式 allowlist, 只放行 GET /containers/* 与
                                start/stop/restart/update, create/delete 永久 403

本文件覆盖其中可自动化的部分:
  * _apply_samba_creds: 落盘格式/权限/原子替换/拒绝分号换行/容器缺失不算失败
  * 主容器源码不再调用 POST /containers/create 与 DELETE /containers/<id>(这是逃逸链的关键)
  * /api/shares: 未设密码时拒绝启用共享(不再有 iso123 这类默认口令)
  * _ensure_webdav_conf: 无密码时拒绝生成空口令配置
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402

APP_SRC = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")


def _resp(status, text="{}"):
    return type("R", (), {"status": status, "text": text})()


class SambaCredsFileTest(unittest.TestCase):
    """_apply_samba_creds: 凭据只落文件, 容器只 restart。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "samba-users.conf"

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_file_and_restarts_container(self):
        """成功路径: 写 `user;pass`, 只发一次 restart, 不发 create/delete。"""
        with patch.object(app, "SHARE_CONF_DIR", self.dir), \
             patch.object(app, "_docker_request", return_value=_resp(204)) as md:
            self.assertTrue(app._apply_samba_creds("iso", "p@ss-w0rd"))

        md.assert_called_once()
        method, path, _body, _timeout = md.call_args[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/containers/iso-hub-samba/restart")

        content = self.path.read_text(encoding="utf-8")
        self.assertIn("iso;p@ss-w0rd", content)
        self.assertNotIn("create", content)
        # 不留临时文件(原子替换)
        self.assertFalse((self.dir / "samba-users.conf.tmp").exists())

    @unittest.skipIf(os.name == "nt", "Windows 不保留 POSIX 权限位")
    def test_file_mode_is_0600(self):
        """凭据文件必须 0600 —— 同机其他用户不该读到 SMB 密码。"""
        with patch.object(app, "SHARE_CONF_DIR", self.dir), \
             patch.object(app, "_docker_request", return_value=_resp(204)):
            app._apply_samba_creds("iso", "secret")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_rejects_semicolon(self):
        """分号是字段分隔符, 必须拒绝(否则能注入第二个字段/截断密码)。"""
        with patch.object(app, "SHARE_CONF_DIR", self.dir):
            self.assertFalse(app._apply_samba_creds("iso", "a;b"))
        self.assertFalse(self.path.exists(), "校验失败时不应落盘")

    def test_rejects_newline(self):
        with patch.object(app, "SHARE_CONF_DIR", self.dir):
            self.assertFalse(app._apply_samba_creds("iso", "a\nb"))
        self.assertFalse(self.path.exists())

    def test_rejects_empty(self):
        with patch.object(app, "SHARE_CONF_DIR", self.dir):
            self.assertFalse(app._apply_samba_creds("iso", ""))

    def test_missing_container_is_not_a_failure(self):
        """sidecar 未部署(404): 凭据仍应保存, 等 compose 起 sidecar 时生效。"""
        with patch.object(app, "SHARE_CONF_DIR", self.dir), \
             patch.object(app, "_docker_request", return_value=_resp(404)):
            self.assertTrue(app._apply_samba_creds("iso", "pw"))
        self.assertTrue(self.path.exists())

    def test_docker_unreachable_is_not_a_failure(self):
        """Docker 不可达时凭据已经写盘, 不该让设置保存失败。"""
        with patch.object(app, "SHARE_CONF_DIR", self.dir), \
             patch.object(app, "_docker_request", side_effect=ConnectionRefusedError("down")):
            self.assertTrue(app._apply_samba_creds("iso", "pw"))
        self.assertTrue(self.path.exists())

    def test_no_create_or_delete_in_source(self):
        """回归护栏: 应用代码不得再调用 create/delete —— 这是逃逸链的关键一环。"""
        self.assertNotIn('"/containers/create', APP_SRC,
                         "app.py 又出现 /containers/create 调用")
        self.assertNotIn('_docker_request("DELETE"', APP_SRC,
                         "app.py 又出现 DELETE /containers 调用")


class ShareEnableRequiresPasswordTest(unittest.TestCase):
    """没有密码就不允许启用共享(取消内置默认口令)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.settings = self.data / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")
        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "SETTINGS_JSON", self.settings),
            patch.object(app, "REQUIRE_LOGIN", False),
            patch.object(app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()
        self.client = app.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_enable_without_password_rejected(self):
        r = self.client.post("/api/shares", json={"samba": {"enabled": True}})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("密码", r.get_json().get("error", ""))

    def test_enable_with_password_in_same_request_ok(self):
        with patch.object(app, "apply_share_creds", return_value=True), \
             patch.object(app, "set_share", return_value=True), \
             patch.object(app, "service_state", return_value="running"):
            r = self.client.post("/api/shares",
                                 json={"samba": {"enabled": True,
                                                 "username": "iso", "password": "pw"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_default_password_is_empty(self):
        """默认值里不应再有硬编码口令。"""
        for proto in ("samba", "webdav"):
            self.assertEqual(app.DEFAULT_SHARES[proto]["password"], "",
                             f"{proto} 又出现内置默认口令")


class WebdavEmptyPasswordTest(unittest.TestCase):
    """无密码时不得生成"空口令"的 webdav 配置。"""

    def test_refuse_to_generate_without_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "webdav.yml"
            with patch.object(app, "_webdav_conf_path", return_value=target):
                self.assertFalse(app._ensure_webdav_conf("iso", ""))
            self.assertFalse(target.exists())


class SambaEntrypointTest(unittest.TestCase):
    """sidecar 启动脚本的基本契约(容器内执行, 这里只做静态校验)。"""

    def setUp(self):
        self.script = (REPO_ROOT / "docker" / "samba-entrypoint.sh").read_text(encoding="utf-8")

    def test_fails_closed_without_accounts(self):
        """没有有效账号时 exit 1 —— 绝不能退化成一个匿名可读的共享。"""
        self.assertIn("exit 1", self.script)
        self.assertIn("SAMBA_USERS_FILE", self.script)

    def test_does_not_echo_password(self):
        """脚本不回显密码: 日志里只出现用户名列表。"""
        echo_lines = [ln for ln in self.script.splitlines() if ln.strip().startswith("echo")]
        self.assertTrue(echo_lines, "启动脚本应有可诊断的日志输出")
        for ln in echo_lines:
            self.assertNotIn("$_pass", ln)


if __name__ == "__main__":
    unittest.main()
