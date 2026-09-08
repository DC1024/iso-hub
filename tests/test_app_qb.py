#!/usr/bin/env python3
"""qBittorrent 默认禁用改造的业务逻辑单元测试。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# 把 web 目录加入路径, 使 app.py 可作为模块导入
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402


class TestDefaultQbDisabled(unittest.TestCase):
    """验证 qBittorrent 默认禁用及启停控制逻辑。"""

    def test_default_qb_enabled_is_false(self):
        """DEFAULT_QB['enabled'] 必须为 False。"""
        self.assertIn("enabled", app.DEFAULT_QB)
        self.assertIs(app.DEFAULT_QB["enabled"], False)

    def test_load_qb_settings_defaults_to_disabled(self):
        """settings.json 不存在时, load_qb_settings 返回默认禁用配置。"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(app, "DATA_DIR", data_dir), \
                 patch.object(app, "SETTINGS_JSON", data_dir / "settings.json"):
                cfg = app.load_qb_settings()
                self.assertFalse(cfg["enabled"])
                self.assertEqual(cfg["username"], app.DEFAULT_QB["username"])
                self.assertEqual(cfg["password"], app.DEFAULT_QB["password"])

    def test_load_qb_settings_preserves_enabled_true(self):
        """settings.json 中 qb.enabled=true 时能被正确读取。"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = data_dir / "settings.json"
            settings.write_text(json.dumps({"qb": {"enabled": True, "username": "u", "password": "p"}}), encoding="utf-8")
            with patch.object(app, "DATA_DIR", data_dir), \
                 patch.object(app, "SETTINGS_JSON", settings):
                cfg = app.load_qb_settings()
                self.assertTrue(cfg["enabled"])
                self.assertEqual(cfg["username"], "u")
                self.assertEqual(cfg["password"], "p")


class TestEnsureQbEnabled(unittest.TestCase):
    """验证 /api/torrent/* 前置检查 _ensure_qb_enabled。"""

    def test_returns_403_when_disabled(self):
        """未启用时返回 (False, 403 响应元组)。"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = data_dir / "settings.json"
            settings.write_text(json.dumps({"qb": {"enabled": False}}), encoding="utf-8")
            with patch.object(app, "DATA_DIR", data_dir), \
                 patch.object(app, "SETTINGS_JSON", settings), \
                 app.app.app_context():
                ok, err = app._ensure_qb_enabled()
                self.assertFalse(ok)
                self.assertIsNotNone(err)
                self.assertEqual(err[1], 403)
                self.assertIn("未启用", err[0].json["error"])

    def test_returns_true_when_enabled(self):
        """启用时返回 (True, None)。"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = data_dir / "settings.json"
            settings.write_text(json.dumps({"qb": {"enabled": True}}), encoding="utf-8")
            with patch.object(app, "DATA_DIR", data_dir), \
                 patch.object(app, "SETTINGS_JSON", settings):
                ok, err = app._ensure_qb_enabled()
                self.assertTrue(ok)
                self.assertIsNone(err)


class TestSetQb(unittest.TestCase):
    """验证 set_qb 对容器启停及重启策略的调用。"""

    def _fake_docker_resp(self, status):
        r = MagicMock()
        r.status = status
        r.text = "{}"
        return r

    @patch.object(app, "_docker_request")
    @patch.object(app, "QB_CONF_PATH")
    def test_enable_starts_updates_restart_and_restarts(self, mock_conf_path, mock_docker):
        """启用: start -> update restart=unless-stopped -> 写密码 -> restart。"""
        mock_docker.side_effect = [
            self._fake_docker_resp(204),  # start
            self._fake_docker_resp(200),  # update restart
            self._fake_docker_resp(204),  # restart
        ]
        mock_conf_path.exists.return_value = True
        with patch.object(app, "_set_qb_password", return_value=True):
            result = app.set_qb(True, "admin", "adminadmin")
        self.assertTrue(result)
        calls = mock_docker.call_args_list
        self.assertEqual(calls[0][0][0], "POST")
        self.assertIn(f"/containers/{app.QB_CONTAINER}/start", calls[0][0][1])
        self.assertEqual(calls[1][0][0], "POST")
        self.assertEqual(calls[1][0][2], {"RestartPolicy": {"Name": "unless-stopped"}})
        self.assertEqual(calls[2][0][0], "POST")
        self.assertIn(f"/containers/{app.QB_CONTAINER}/restart", calls[2][0][1])

    @patch.object(app, "_docker_request")
    def test_disable_stops_and_removes_restart(self, mock_docker):
        """禁用: update restart=no -> stop。"""
        mock_docker.side_effect = [
            self._fake_docker_resp(200),  # update restart
            self._fake_docker_resp(204),  # stop
        ]
        result = app.set_qb(False, "admin", "adminadmin")
        self.assertTrue(result)
        calls = mock_docker.call_args_list
        self.assertEqual(calls[0][0][2], {"RestartPolicy": {"Name": "no"}})
        self.assertEqual(calls[1][0][0], "POST")
        self.assertIn(f"/containers/{app.QB_CONTAINER}/stop", calls[1][0][1])


class TestSyncDisabledQb(unittest.TestCase):
    """验证启动同步 _sync_disabled_qb。"""

    @patch.object(app, "share_container_state", return_value="running")
    @patch.object(app, "set_qb")
    @patch.object(app, "load_qb_settings")
    def test_stops_running_container_when_disabled(self, mock_load, mock_set_qb, mock_state):
        """配置为禁用且容器在运行时, 应调用 set_qb(False, ...) 停止。"""
        mock_load.return_value = {"enabled": False, "username": "u", "password": "p"}
        app._sync_disabled_qb()
        mock_set_qb.assert_called_once_with(False, "u", "p")

    @patch.object(app, "share_container_state", return_value="exited")
    @patch.object(app, "set_qb")
    @patch.object(app, "load_qb_settings")
    def test_does_nothing_when_not_running(self, mock_load, mock_set_qb, mock_state):
        """容器已停止时, 不调用 set_qb。"""
        mock_load.return_value = {"enabled": False, "username": "u", "password": "p"}
        app._sync_disabled_qb()
        mock_set_qb.assert_not_called()

    @patch.object(app, "set_qb")
    @patch.object(app, "load_qb_settings")
    def test_does_nothing_when_enabled(self, mock_load, mock_set_qb):
        """配置为启用时, 不停止容器。"""
        mock_load.return_value = {"enabled": True, "username": "u", "password": "p"}
        app._sync_disabled_qb()
        mock_set_qb.assert_not_called()


class TestQbPasswordHash(unittest.TestCase):
    """验证 PBKDF2 密码写入格式。"""

    def test_pbkdf2_format(self):
        """_make_qb_pbkdf2 返回 @ByteArray(base64salt:base64key) 格式。"""
        pw_hash = app._make_qb_pbkdf2("secret")
        self.assertTrue(pw_hash.startswith("@ByteArray("))
        self.assertTrue(pw_hash.endswith(")"))
        inner = pw_hash[len("@ByteArray("):-1]
        salt_b64, key_b64 = inner.split(":")
        self.assertGreater(len(salt_b64), 0)
        self.assertGreater(len(key_b64), 0)


if __name__ == "__main__":
    unittest.main()
