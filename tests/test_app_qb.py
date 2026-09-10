#!/usr/bin/env python3
"""qBittorrent 默认禁用改造的业务逻辑单元测试。"""

import inspect
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

    @patch.object(app, "_docker_request")
    @patch.object(app, "QB_CONF_PATH")
    def test_enable_returns_false_when_restart_update_fails(self, mock_conf_path, mock_docker):
        """回归: start 成功但 update restart 失败 -> 必须返回 False。

        旧实现不检查 update 的返回值, 于是"启用成功"实际留下 running + 旧策略,
        宿主重启后 qB 不会被拉起, 而面板仍显示绿灯。

        注意: 必须把 conf/密码环节一并 mock 成成功, 否则函数会(无论 update 成败)
        因"写密码失败"而返回 False, 测试就抓不到 update 校验的缺失 —— 这个盲区
        是变异测试发现的。
        """
        mock_docker.side_effect = [
            self._fake_docker_resp(204),  # start OK
            self._fake_docker_resp(500),  # update restart FAILED
            self._fake_docker_resp(204),  # restart(仅在校验缺失时才会走到, 且会成功)
        ]
        mock_conf_path.exists.return_value = True
        with patch.object(app, "_set_qb_password", return_value=True):
            result = app.set_qb(True, "admin", "adminadmin")
        self.assertFalse(result, "update restart 失败时启用不得报成功")

    @patch.object(app, "_docker_request")
    def test_disable_returns_false_when_restart_update_fails(self, mock_docker):
        """回归: 停用时 update restart=no 失败 -> 也必须返回 False。"""
        mock_docker.side_effect = [
            self._fake_docker_resp(500),  # update restart FAILED
            self._fake_docker_resp(204),  # stop OK
        ]
        result = app.set_qb(False, "admin", "adminadmin")
        self.assertFalse(result, "update restart 失败时停用不得报成功")


class TestSetQbFailureMessaging(unittest.TestCase):
    """回归: set_qb 失败时必须区分"完全失败"与"部分成功", 把"当前能不能用"说清楚。"""

    def _fake(self, status):
        r = MagicMock()
        r.status = status
        r.text = "{}"
        return r

    @staticmethod
    def _logmsg(mock_log):
        return " ".join(str(c.args[0]) for c in mock_log.call_args_list)

    @patch.object(app, "QB_CONF_PATH")
    @patch.object(app, "_docker_request")
    @patch.object(app, "_set_qb_password")
    @patch.object(app, "log")
    def test_enable_partial_success_message(self, mock_log, mock_pwd, mock_docker, mock_conf):
        """start 成功 + update 重启策略失败 -> 部分成功(当前可用, 重启后失效)。"""
        mock_conf.exists.return_value = True
        mock_pwd.return_value = True
        mock_docker.side_effect = [self._fake(204), self._fake(500), self._fake(204)]
        self.assertFalse(app.set_qb(True, "admin", "adminadmin"))
        msg = self._logmsg(mock_log)
        self.assertIn("部分成功", msg)
        self.assertIn("当前可用", msg)
        self.assertIn("重启后不会自动恢复", msg)
        self.assertIn("步骤2", msg)

    @patch.object(app, "QB_CONF_PATH")
    @patch.object(app, "_docker_request")
    @patch.object(app, "_set_qb_password")
    @patch.object(app, "log")
    def test_enable_complete_failure_message(self, mock_log, mock_pwd, mock_docker, mock_conf):
        """start 失败 -> 完全失败(配置未变更, 当前不可用)。"""
        mock_conf.exists.return_value = True
        mock_pwd.return_value = True
        mock_docker.side_effect = [self._fake(500)]
        self.assertFalse(app.set_qb(True, "admin", "adminadmin"))
        msg = self._logmsg(mock_log)
        self.assertIn("启用失败", msg)
        self.assertIn("当前不可用", msg)
        self.assertIn("步骤1", msg)

    @patch.object(app, "_docker_request")
    @patch.object(app, "log")
    def test_disable_partial_success_message(self, mock_log, mock_docker):
        """停用: update no 失败但 stop 成功 -> 部分成功(当前已停用, 重启后可能被拉起)。"""
        mock_docker.side_effect = [self._fake(500), self._fake(204)]
        self.assertFalse(app.set_qb(False, "admin", "adminadmin"))
        msg = self._logmsg(mock_log)
        self.assertIn("部分成功", msg)
        self.assertIn("当前已停用", msg)
        self.assertIn("可能被自动拉起", msg)

    @patch.object(app, "_docker_request")
    @patch.object(app, "log")
    def test_disable_complete_failure_message(self, mock_log, mock_docker):
        """停用: stop 失败 -> 完全失败(配置未变更)。"""
        mock_docker.side_effect = [self._fake(200), self._fake(500)]
        self.assertFalse(app.set_qb(False, "admin", "adminadmin"))
        msg = self._logmsg(mock_log)
        self.assertIn("停用失败", msg)
        self.assertIn("配置未变更", msg)


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


class TestLockReentrant(unittest.TestCase):
    """回归: 锁必须是可重入 RLock, 否则 stop_task 在持锁内调 log() 会死锁,
    占满 waitress 8 线程, 队列飙升、应用日志一条打不出来。"""

    def test_stop_task_log_inside_lock_does_not_deadlock(self):
        """_lock 为 RLock 时, 持锁线程内调 log() 可重入, 不永久阻塞。"""
        self.assertIsInstance(app._lock, type(app.threading.RLock()))
        with app._lock:
            # 持锁内调 log()——旧实现普通 Lock 在此永久死锁
            app.log("[测试] 持锁内写日志(可重入)")
        # 若死锁此处永不返回, 测试超时失败
        self.assertTrue(True)

    def test_running_task_stat_outside_lock(self):
        """running_task 的磁盘 IO 不得持 _lock 进行(防止慢盘阻塞日志/停止)。"""
        import ast
        src = inspect.getsource(app.running_task)
        # 用 AST 定位 with _lock 节点, 只取该节点的 body 源码(不含函数级注释), 断言无可执行磁盘 IO
        tree = ast.parse(src)
        fn = tree.body[0]
        with_node = next(n for n in ast.walk(fn) if isinstance(n, ast.With))
        body_src = ast.get_source_segment(src, with_node) or ""
        for kw in ("stat", "exists", "for d in dl", "p.iterdir", "os.scandir"):
            self.assertNotIn(kw, body_src, f"锁内不应出现 {kw}")


if __name__ == "__main__":
    unittest.main()
