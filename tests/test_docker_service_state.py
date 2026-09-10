#!/usr/bin/env python3
"""Docker 服务开关改造(N1/N2/N3/N4)单元测试。

覆盖:
  * service_state() 四态判定(running/stopped/not_deployed/unknown)及边界
  * unknown 不误报 not_deployed(核心验收点)
  * _docker_conn()/DOCKER_HOST 的 TCP/Unix socket 双分支向后兼容
  * compose 文件的 socket-proxy 白名单 / profiles / 主容器摘除裸 sock(人工校验的自动化版本)
"""

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# 把 web 目录加入路径, 使 app.py 可作为模块导入
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402


def _docker_resp(status, text="{}"):
    """构造一个与 _docker_request 返回值同形的假响应对象。"""
    r = MagicMock()
    r.status = status
    r.text = text
    return r


class TestServiceStateFourStates(unittest.TestCase):
    """service_state() 四态判定。"""

    @patch.object(app, "_docker_request")
    def test_404_is_not_deployed(self, mock_req):
        """status=404(容器不存在) -> not_deployed。"""
        mock_req.return_value = _docker_resp(404, '{"message":"No such container"}')
        self.assertEqual(app.service_state("iso-hub-samba"), "not_deployed")

    @patch.object(app, "_docker_request")
    def test_500_is_unknown_not_not_deployed(self, mock_req):
        """status=500(API 出错) -> unknown, 绝不能误报 not_deployed。"""
        mock_req.return_value = _docker_resp(500, '{"message":"boom"}')
        self.assertEqual(app.service_state("iso-hub-samba"), "unknown")

    @patch.object(app, "_docker_request")
    def test_403_is_unknown(self, mock_req):
        """status=403(proxy 拒绝) -> unknown。"""
        mock_req.return_value = _docker_resp(403, "Forbidden")
        self.assertEqual(app.service_state("iso-hub-samba"), "unknown")

    @patch.object(app, "_docker_request")
    def test_exception_is_unknown_not_not_deployed(self, mock_req):
        """_docker_request 抛异常(socket-proxy 挂掉) -> unknown, 绝不能误报 not_deployed。"""
        mock_req.side_effect = ConnectionRefusedError("socket-proxy down")
        self.assertEqual(app.service_state("iso-hub-samba"), "unknown")

    @patch.object(app, "_docker_request")
    def test_running(self, mock_req):
        """State.Status=running -> running。"""
        mock_req.return_value = _docker_resp(200, '{"State":{"Status":"running"}}')
        self.assertEqual(app.service_state("iso-hub-samba"), "running")

    def test_non_running_states_are_stopped(self):
        """created/exited/restarting/paused/dead 全部 -> stopped。"""
        for status in ("created", "exited", "restarting", "paused", "dead"):
            with self.subTest(status=status):
                with patch.object(app, "_docker_request") as mock_req:
                    mock_req.return_value = _docker_resp(200, '{"State":{"Status":"%s"}}' % status)
                    self.assertEqual(app.service_state("iso-hub-samba"), "stopped")

    @patch.object(app, "_docker_request")
    def test_unknown_container_status_maps_stopped(self, mock_req):
        """未知的 State.Status 值保守归为 stopped(不崩、不误报 unknown/not_deployed)。"""
        mock_req.return_value = _docker_resp(200, '{"State":{"Status":"weird-status"}}')
        self.assertEqual(app.service_state("iso-hub-samba"), "stopped")

    @patch.object(app, "_docker_request")
    def test_missing_state_is_unknown(self, mock_req):
        """返回非法结构(无 State.Status) -> unknown。"""
        mock_req.return_value = _docker_resp(200, '{"Id":"abc"}')
        self.assertEqual(app.service_state("iso-hub-samba"), "unknown")

    @patch.object(app, "_docker_request")
    def test_empty_state_is_unknown(self, mock_req):
        """State.Status 为空字符串 -> unknown。"""
        mock_req.return_value = _docker_resp(200, '{"State":{"Status":""}}')
        self.assertEqual(app.service_state("iso-hub-samba"), "unknown")

    @patch.object(app, "_docker_request")
    def test_invalid_json_is_unknown(self, mock_req):
        """返回体不是合法 JSON -> unknown(异常被吞, 不抛出)。"""
        mock_req.return_value = _docker_resp(200, "<html>not json</html>")
        self.assertEqual(app.service_state("iso-hub-samba"), "unknown")

    def test_never_raises(self):
        """service_state 在任何异常下都不抛错(懒加载/不崩溃原则)。"""
        with patch.object(app, "_docker_request", side_effect=RuntimeError("x")):
            try:
                app.service_state("iso-hub-samba")
            except Exception as e:  # noqa: BLE001
                self.fail(f"service_state 不应抛异常, 实际抛出: {e!r}")

    def test_all_return_values_in_enum(self):
        """任何输入下返回值必属于 SERVICE_STATES 集合。"""
        cases = [
            _docker_resp(404), _docker_resp(500), _docker_resp(200, '{"State":{"Status":"running"}}'),
            _docker_resp(200, '{"State":{"Status":"exited"}}'), _docker_resp(200, "{}"),
        ]
        for resp in cases:
            with self.subTest(resp=resp.text):
                with patch.object(app, "_docker_request", return_value=resp):
                    self.assertIn(app.service_state("iso-hub-samba"), app.SERVICE_STATES)


class TestDockerConnCompat(unittest.TestCase):
    """N1: _docker_conn 的 TCP/Unix socket 双分支与向后兼容。"""

    def test_tcp_branch_uses_http_connection(self):
        """DOCKER_HOST=tcp://host:port -> 走 HTTPConnection(host:port), 不建 Unix socket。"""
        with patch.object(app, "DOCKER_HOST", "tcp://socket-proxy:2375"), \
             patch("http.client.HTTPConnection") as mock_http, \
             patch("socket.socket") as mock_sock:
            conn = app._docker_conn(7)
            mock_http.assert_called_once_with("socket-proxy:2375", timeout=7)
            self.assertIs(conn, mock_http.return_value)
            mock_sock.assert_not_called()  # 绝不回退 Unix socket

    def test_unix_socket_branch_when_host_unset(self):
        """DOCKER_HOST 为空 -> 回退 Unix socket(DOCKER_SOCK), 向后兼容。

        注意: Windows 上 Python 的 socket 模块没有 AF_UNIX, 该分支为 Linux 生产路径,
        测试通过补丁注入 AF_UNIX 常量以在任意平台验证分支逻辑。
        """
        af_unix = getattr(socket, "AF_UNIX", 1)  # 1 为 Linux 上 AF_UNIX 的数值
        with patch.object(app, "DOCKER_HOST", ""), \
             patch.object(app, "DOCKER_SOCK", "/var/run/docker.sock"), \
             patch("http.client.HTTPConnection") as mock_http, \
             patch("socket.socket") as mock_sock, \
             patch.object(socket, "AF_UNIX", af_unix, create=True):
            conn = app._docker_conn(11)
            mock_sock.assert_called_once_with(af_unix, socket.SOCK_STREAM)
            mock_sock.return_value.connect.assert_called_once_with("/var/run/docker.sock")
            self.assertIs(conn, mock_http.return_value)  # 复用 HTTPConnection 对象挂 sock
            mock_http.assert_called_once_with("localhost", timeout=11)

    def test_unix_socket_branch_when_host_has_other_scheme(self):
        """DOCKER_HOST 非 tcp:// 前缀(如 unix://) -> 仍回退 Unix socket。"""
        af_unix = getattr(socket, "AF_UNIX", 1)
        with patch.object(app, "DOCKER_HOST", "unix:///var/run/docker.sock"), \
             patch.object(app, "DOCKER_SOCK", "/var/run/docker.sock"), \
             patch("http.client.HTTPConnection"), \
             patch("socket.socket") as mock_sock, \
             patch.object(socket, "AF_UNIX", af_unix, create=True):
            app._docker_conn(5)
            mock_sock.assert_called_once_with(af_unix, socket.SOCK_STREAM)

    def test_docker_request_closes_conn(self):
        """_docker_request 必须在 finally 中关闭连接(不泄漏 fd)。"""
        fake_conn = MagicMock()
        with patch.object(app, "_docker_conn", return_value=fake_conn):
            r = app._docker_request("GET", "/containers/x/json", None, 5)
        fake_conn.request.assert_called_once()
        fake_conn.close.assert_called_once()
        self.assertEqual(r.status, fake_conn.getresponse.return_value.status)

    def test_docker_request_closes_conn_on_error(self):
        """请求过程抛异常时连接仍被关闭。"""
        fake_conn = MagicMock()
        fake_conn.request.side_effect = RuntimeError("network error")
        with patch.object(app, "_docker_conn", return_value=fake_conn):
            with self.assertRaises(RuntimeError):
                app._docker_request("GET", "/containers/x/json", None, 5)
        fake_conn.close.assert_called_once()

    def test_docker_request_encodes_body(self):
        """body 非空时以 JSON 编码并带上 Content-Type。"""
        fake_conn = MagicMock()
        with patch.object(app, "_docker_conn", return_value=fake_conn):
            app._docker_request("POST", "/containers/x/update", {"RestartPolicy": {"Name": "no"}}, 5)
        _, kwargs = fake_conn.request.call_args
        self.assertEqual(kwargs["body"], b'{"RestartPolicy": {"Name": "no"}}')
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")


class TestComposeConfig(unittest.TestCase):
    """N2/N3: compose 文件结构校验(本机无 docker, 用 YAML 解析替代 docker compose config)。"""

    COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dockerhub.yml", "docker-compose.acr.yml")
    WHITELIST_ON = {"CONTAINERS": "1", "POST": "1"}
    WHITELIST_OFF = {"IMAGES": "0", "VOLUMES": "0", "NETWORKS": "0", "EXEC": "0", "SYSTEM": "0"}

    @classmethod
    def setUpClass(cls):
        try:
            import yaml  # noqa: F401
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("pyyaml 不可用, 跳过 compose 校验")
        cls._docs = {}
        for name in cls.COMPOSE_FILES:
            p = REPO_ROOT / name
            if p.exists():
                cls._docs[name] = yaml.safe_load(p.read_text(encoding="utf-8"))

    @staticmethod
    def _env_dict(service):
        env = service.get("environment", [])
        if isinstance(env, dict):
            return {str(k): str(v) for k, v in env.items()}
        out = {}
        for item in env:
            k, _, v = str(item).partition("=")
            out[k] = v
        return out

    def test_all_compose_files_exist_and_parse(self):
        """三个 compose 文件均存在且可被 YAML 解析。"""
        self.assertEqual(set(self._docs.keys()), set(self.COMPOSE_FILES), "缺少 compose 文件或解析失败")

    def test_socket_proxy_present_without_profile(self):
        """socket-proxy 存在且不设 profile(核心依赖, 必须随核心一并启动)。"""
        for name, doc in self._docs.items():
            with self.subTest(compose=name):
                svc = doc["services"]
                self.assertIn("socket-proxy", svc)
                self.assertNotIn("profiles", svc["socket-proxy"])

    def test_socket_proxy_image_digest_pinned(self):
        """socket-proxy 镜像必须以 digest 锁定, 不得使用 :latest(上游 ACL 变更会改变放行范围)。"""
        for name, doc in self._docs.items():
            with self.subTest(compose=name):
                image = str(doc["services"]["socket-proxy"].get("image", ""))
                self.assertIn("@sha256:", image, f"{name}: socket-proxy 镜像未锁定 digest")
                self.assertNotIn(":latest", image, f"{name}: socket-proxy 镜像不得使用 :latest")
                digest = image.split("@sha256:", 1)[1]
                self.assertEqual(len(digest), 64, f"{name}: digest 长度应为 64 位十六进制")

    def test_socket_proxy_whitelist(self):
        """socket-proxy 白名单: CONTAINERS/POST=1, 镜像/卷/网络/exec/系统=0。"""
        for name, doc in self._docs.items():
            with self.subTest(compose=name):
                env = self._env_dict(doc["services"]["socket-proxy"])
                for k, v in self.WHITELIST_ON.items():
                    self.assertEqual(env.get(k), v, f"{name}: socket-proxy {k} 应为 {v}")
                for k, v in self.WHITELIST_OFF.items():
                    self.assertEqual(env.get(k), v, f"{name}: socket-proxy {k} 应为 {v}")

    def test_socket_proxy_sock_mounted_readonly(self):
        """socket-proxy 以只读(:ro)挂载裸 sock。"""
        for name, doc in self._docs.items():
            with self.subTest(compose=name):
                vols = doc["services"]["socket-proxy"].get("volumes", [])
                sock_vols = [v for v in vols if "docker.sock" in str(v)]
                self.assertEqual(len(sock_vols), 1, f"{name}: socket-proxy 应恰好挂 1 个 docker.sock")
                self.assertTrue(str(sock_vols[0]).endswith(":ro"), f"{name}: docker.sock 须只读挂载")

    def test_main_container_uses_proxy_and_drops_sock(self):
        """主容器 DOCKER_HOST 指向 proxy, 且不再挂载裸 sock。"""
        for name, doc in self._docs.items():
            with self.subTest(compose=name):
                ih = doc["services"]["iso-hub"]
                env = self._env_dict(ih)
                self.assertEqual(env.get("DOCKER_HOST"), "tcp://socket-proxy:2375")
                for v in ih.get("volumes", []):
                    self.assertNotIn("docker.sock", str(v), f"{name}: 主容器仍挂载裸 docker.sock!")

    def test_main_container_depends_on_socket_proxy(self):
        """主容器 depends_on socket-proxy。"""
        for name, doc in self._docs.items():
            with self.subTest(compose=name):
                dep = doc["services"]["iso-hub"].get("depends_on", [])
                deps = list(dep.keys()) if isinstance(dep, dict) else list(dep)
                self.assertIn("socket-proxy", deps)

    def test_sidecar_profiles(self):
        """samba/webdav -> share, qbittorrent -> bt。"""
        expect = {"samba": "share", "webdav": "share", "qbittorrent": "bt"}
        for name, doc in self._docs.items():
            for svc_name, profile in expect.items():
                with self.subTest(compose=name, service=svc_name):
                    svc = doc["services"][svc_name]
                    self.assertIn(profile, svc.get("profiles", []))


class TestSetShareStrictResult(unittest.TestCase):
    """回归: set_share 必须严格校验两步(start/stop + RestartPolicy update)。

    旧实现用 `and` 判断 —— 只有两步**都**失败才返回 False。于是 start 成功但
    update 失败时会误报成功, 静默留下 running + restart=no 的不一致: 面板显示
    绿灯, 但宿主重启后容器不会被拉起。现在任一步失败都必须返回 False。
    """

    @patch.object(app, "_docker_request")
    def test_enable_returns_false_when_update_fails(self, mock_docker):
        """start 成功(204) 但 update 失败(500) -> 必须返回 False。"""
        mock_docker.side_effect = [_docker_resp(204), _docker_resp(500)]
        self.assertFalse(app.set_share("samba", True))

    @patch.object(app, "_docker_request")
    def test_enable_returns_false_when_start_fails(self, mock_docker):
        """start 失败(500) 但 update 成功 -> 同样必须返回 False。"""
        mock_docker.side_effect = [_docker_resp(500), _docker_resp(200)]
        self.assertFalse(app.set_share("samba", True))

    @patch.object(app, "_docker_request")
    def test_enable_returns_true_when_both_ok(self, mock_docker):
        """两步都成功 -> True(含 start 对已运行容器返回 304 的情形)。"""
        mock_docker.side_effect = [_docker_resp(304), _docker_resp(200)]
        self.assertTrue(app.set_share("samba", True))

    @patch.object(app, "_docker_request")
    def test_disable_returns_false_when_update_fails(self, mock_docker):
        """停用: update 失败 但 stop 成功 -> 也必须返回 False。"""
        mock_docker.side_effect = [_docker_resp(500), _docker_resp(204)]
        self.assertFalse(app.set_share("samba", False))

    @patch.object(app, "_docker_request")
    def test_disable_returns_true_when_both_ok(self, mock_docker):
        mock_docker.side_effect = [_docker_resp(200), _docker_resp(304)]
        self.assertTrue(app.set_share("samba", False))


class TestSetShareFailureMessaging(unittest.TestCase):
    """回归: set_share 失败时必须区分"完全失败"(当下不可用) 与"部分成功"(当下可用但重启失效)。

    用户最怕的是状态不明 —— 不知道现在到底能不能用。因此日志要把
    "当前能不能用" 与 "重启后会不会失效" 分开说清楚, 恰好对应需求里的
    "完全失败" / "部分成功" 两种文案。
    """

    @staticmethod
    def _logmsg(mock_log):
        return " ".join(str(c.args[0]) for c in mock_log.call_args_list)

    @patch.object(app, "_docker_request")
    @patch.object(app, "log")
    def test_enable_partial_success_message(self, mock_log, mock_docker):
        """start 成功 + update 失败 -> 部分成功, 明确"当前可用, 重启后不会自动恢复"。"""
        mock_docker.side_effect = [_docker_resp(204), _docker_resp(500)]
        self.assertFalse(app.set_share("samba", True))
        msg = self._logmsg(mock_log)
        self.assertIn("部分成功", msg)
        self.assertIn("当前可用", msg)
        self.assertIn("重启后不会自动恢复", msg)
        self.assertIn("步骤2", msg)

    @patch.object(app, "_docker_request")
    @patch.object(app, "log")
    def test_enable_complete_failure_message(self, mock_log, mock_docker):
        """start 失败 -> 完全失败, 明确"配置未变更, 当前不可用"。"""
        mock_docker.side_effect = [_docker_resp(500), _docker_resp(200)]
        self.assertFalse(app.set_share("samba", True))
        msg = self._logmsg(mock_log)
        self.assertIn("启用失败", msg)
        self.assertIn("当前不可用", msg)
        self.assertIn("步骤1", msg)

    @patch.object(app, "_docker_request")
    @patch.object(app, "log")
    def test_disable_partial_success_message(self, mock_log, mock_docker):
        """停用: update no 失败但 stop 成功 -> 部分成功(当前已停用, 重启后可能被拉起)。"""
        mock_docker.side_effect = [_docker_resp(500), _docker_resp(204)]
        self.assertFalse(app.set_share("samba", False))
        msg = self._logmsg(mock_log)
        self.assertIn("部分成功", msg)
        self.assertIn("当前已停用", msg)
        self.assertIn("可能被自动拉起", msg)

    @patch.object(app, "_docker_request")
    @patch.object(app, "log")
    def test_disable_complete_failure_message(self, mock_log, mock_docker):
        """停用: stop 失败 -> 完全失败(配置未变更)。"""
        mock_docker.side_effect = [_docker_resp(200), _docker_resp(500)]
        self.assertFalse(app.set_share("samba", False))
        msg = self._logmsg(mock_log)
        self.assertIn("停用失败", msg)
        self.assertIn("配置未变更", msg)


class TestHealEnabledSidecarPolicyNone(unittest.TestCase):
    """回归: _heal_enabled_sidecar 在重启策略查询失败时(policy=None)应留待重试, 不可误判为已一致。

    修复前 `policy is None` 走 `return False`, 会把"查不到"当成"已一致"跳过, 漏掉
    真正需要补自愈策略的容器。修复后改为 `return True`(pending) + 告警日志。
    """

    def test_policy_none_pends_retry_and_does_not_act(self):
        """running + 策略查询返回 None -> 返回 True(下一轮重试), 且不得调用 update/启停。"""
        with patch.object(app, "share_container_state", return_value="running"), \
             patch.object(app, "container_restart_policy", return_value=None), \
             patch.object(app, "_docker_request") as mock_docker, \
             patch.object(app, "log") as mock_log:
            rc = app._heal_enabled_sidecar("samba-test", lambda on: None, set())
        self.assertTrue(rc, "policy=None 必须留待下一轮重试, 不能视为已一致")
        mock_docker.assert_not_called()  # 不误动作
        self.assertIn("留待下一轮重试", " ".join(str(c.args[0]) for c in mock_log.call_args_list))

    def test_policy_unless_stopped_is_consistent(self):
        """running + unless-stopped -> 已一致, 返回 False, 不动作。"""
        with patch.object(app, "share_container_state", return_value="running"), \
             patch.object(app, "container_restart_policy", return_value="unless-stopped"), \
             patch.object(app, "_docker_request") as mock_docker:
            rc = app._heal_enabled_sidecar("samba-test", lambda on: None, set())
        self.assertFalse(rc)
        mock_docker.assert_not_called()


class TestEnabledSidecarConvergence(unittest.TestCase):
    """回归: 启动收敛的**反向**分支(配置为启用的 sidecar 也要收敛)。

    补齐原先"只管禁用"的半边缺口。两种漂移:
      1) 容器在运行, 但 RestartPolicy 被 recreate 打回 no(自愈能力丢失)
      2) 容器存在但未运行(宿主重启后没被拉起)却配置为启用

    底线: 无论哪种, 启用的容器**绝不能被停止**。
    """

    def _patches(self, shares, qb, state, policy):
        return [
            patch.object(app, "load_shares", return_value=shares),
            patch.object(app, "load_qb_settings", return_value=qb),
            patch.object(app, "share_container_state", return_value=state),
            patch.object(app, "container_restart_policy", return_value=policy),
        ]

    def test_running_but_policy_no_is_repaired(self):
        """running + restart=no -> 补 update 为 unless-stopped, 且不停止容器。"""
        shares = {"samba": {"enabled": True}, "webdav": {"enabled": True}}
        qb = {"enabled": True, "username": "u", "password": "p"}
        p = self._patches(shares, qb, "running", "no")
        with p[0], p[1], p[2], p[3], \
             patch.object(app, "_docker_request") as mock_docker, \
             patch.object(app, "set_share") as mock_set_share, \
             patch.object(app, "set_qb") as mock_set_qb, \
             patch.object(app.time, "sleep"):
            app._converge_disabled_sidecars(attempts=2, interval=0)

        # 三个 sidecar 各补一次 update, 且补的必须是 unless-stopped
        self.assertGreaterEqual(mock_docker.call_count, 1, "应发出补策略请求")
        for c in mock_docker.call_args_list:
            self.assertEqual(c[0][0], "POST")
            self.assertIn("/update", c[0][1])
            self.assertEqual(c[0][2], {"RestartPolicy": {"Name": "unless-stopped"}})
        # 底线: 启用的容器绝不能被停止
        mock_set_share.assert_not_called()
        mock_set_qb.assert_not_called()

    def test_stopped_but_enabled_is_started(self):
        """配置启用但容器未运行 -> 必须拉起, 且以 enabled=True 调用。"""
        shares = {"samba": {"enabled": True}, "webdav": {"enabled": True}}
        qb = {"enabled": True, "username": "u", "password": "p"}
        p = self._patches(shares, qb, "exited", "no")
        with p[0], p[1], p[2], p[3], \
             patch.object(app, "_docker_request"), \
             patch.object(app, "set_share", return_value=True) as mock_set_share, \
             patch.object(app, "set_qb", return_value=True) as mock_set_qb, \
             patch.object(app.time, "sleep"):
            app._converge_disabled_sidecars(attempts=3, interval=0)

        self.assertEqual(mock_set_share.call_count, 2, "samba/webdav 各应被启动一次")
        for c in mock_set_share.call_args_list:
            self.assertEqual(c[0][1], True, "必须以 enabled=True 启动, 绝不能是停用")
        mock_set_qb.assert_called_once_with(True, "u", "p")

    def test_policy_unknown_pends_retry_conservatively(self):
        """重启策略查不到(None) -> 留待下一轮重试, 但仍保守(本轮不据此启停任何容器)。

        修复前这里会 `return False` 把"查不到"误判成"已一致", 从而漏掉真正需要补
        自愈策略的容器; 修复后改为 `return True`(pending), 等代理/API 恢复后再判定。
        """
        shares = {"samba": {"enabled": True}, "webdav": {"enabled": True}}
        qb = {"enabled": True, "username": "u", "password": "p"}
        p = self._patches(shares, qb, "running", None)
        with p[0], p[1], p[2], p[3], \
             patch.object(app, "_docker_request") as mock_docker, \
             patch.object(app, "set_share") as mock_set_share, \
             patch.object(app, "set_qb") as mock_set_qb, \
             patch.object(app.time, "sleep"):
            app._converge_disabled_sidecars(attempts=2, interval=0)

        # 仍不得误动作(不调用 docker / 不启停容器), 但会标记为 pending 继续重试
        mock_docker.assert_not_called()
        mock_set_share.assert_not_called()
        mock_set_qb.assert_not_called()

    def test_disabled_sidecar_still_stops(self):
        """反向分支不得削弱原有能力: 禁用的容器仍要被停止。"""
        shares = {"samba": {"enabled": False}, "webdav": {"enabled": True}}
        qb = {"enabled": False, "username": "u", "password": "p"}
        p = self._patches(shares, qb, "running", "unless-stopped")
        with p[0], p[1], p[2], p[3], \
             patch.object(app, "_docker_request"), \
             patch.object(app, "set_share", return_value=True) as mock_set_share, \
             patch.object(app, "set_qb", return_value=True) as mock_set_qb, \
             patch.object(app.time, "sleep"):
            app._converge_disabled_sidecars(attempts=2, interval=0)

        mock_set_share.assert_called_once_with("samba", False)
        mock_set_qb.assert_called_once_with(False, "u", "p")


class TestStartupConvergence(unittest.TestCase):
    """回归: 启动时序竞态修复。

    背景(Bug): `docker compose up -d` 并发创建容器, 主容器启动执行一次性同步检查时
    sidecar 往往尚未创建(查询返回 None), 检查被跳过; 随后 sidecar 被拉起, 于是长期
    停留在"配置为禁用但容器在运行"的不一致状态。

    修复: _converge_disabled_sidecars 反复重试, 直到禁用 sidecar 全部停止或次数耗尽。
    """

    def _base_patches(self, shares, qb):
        return [
            patch.object(app, "load_shares", return_value=shares),
            patch.object(app, "load_qb_settings", return_value=qb),
        ]

    def test_retries_until_sidecar_appears(self):
        """sidecar 首轮不存在(None), 次轮变为 running -> 必须被发现并停止。

        这是竞态的核心场景: 一次性快照会漏掉, 收敛重试必须兜住。
        用一个 sleep 钩子推进"轮次", 精确模拟 compose 稍后才把 sidecar 拉起。
        """
        shares = {"samba": {"enabled": False}, "webdav": {"enabled": False}}
        qb = {"enabled": False, "username": "u", "password": "p"}
        round_no = {"n": 0}  # 0 = 第一轮

        def fake_state(_name):
            # 第 0 轮: 容器尚未创建; 第 1 轮起: 已被 compose 拉起
            return None if round_no["n"] == 0 else "running"

        def on_sleep(*_a, **_kw):
            round_no["n"] += 1  # 每次进入下一轮前, 时钟前进一格

        with self._base_patches(shares, qb)[0], self._base_patches(shares, qb)[1], \
             patch.object(app, "share_container_state", side_effect=fake_state), \
             patch.object(app, "set_share", return_value=True) as mock_set_share, \
             patch.object(app, "set_qb", return_value=True) as mock_set_qb, \
             patch.object(app.time, "sleep", side_effect=on_sleep):
            app._converge_disabled_sidecars(attempts=4, interval=0)

        self.assertEqual(mock_set_share.call_count, 2, "samba/webdav 应各被停止一次")
        mock_set_qb.assert_called_once_with(False, "u", "p")

    def test_no_repeated_stop_for_same_container(self):
        """停止请求已发出但容器仍 running(如生效有延迟) -> 不得反复重复 stop。"""
        shares = {"samba": {"enabled": False}, "webdav": {"enabled": False}}
        qb = {"enabled": False, "username": "u", "password": "p"}
        with self._base_patches(shares, qb)[0], self._base_patches(shares, qb)[1], \
             patch.object(app, "share_container_state", return_value="running"), \
             patch.object(app, "set_share", return_value=True) as mock_set_share, \
             patch.object(app, "set_qb", return_value=True) as mock_set_qb, \
             patch.object(app.time, "sleep"):
            app._converge_disabled_sidecars(attempts=5, interval=0)
        # 每个容器只应被请求停止一次, 而不是 5 轮各停一次
        self.assertEqual(mock_set_share.call_count, 2, "samba/webdav 各只 stop 一次")
        mock_set_qb.assert_called_once()

    def test_stop_failure_is_retried(self):
        """set_share 失败(返回 False) -> 视为未处理, 下一轮应再次尝试。"""
        shares = {"samba": {"enabled": False}, "webdav": {"enabled": True}}
        qb = {"enabled": True, "username": "u", "password": "p"}
        # samba 一直 running; set_share 前两次失败, 第三次成功
        with self._base_patches(shares, qb)[0], self._base_patches(shares, qb)[1], \
             patch.object(app, "share_container_state", return_value="running"), \
             patch.object(app, "set_share", side_effect=[False, False, True]) as mock_set_share, \
             patch.object(app, "set_qb", return_value=True), \
             patch.object(app.time, "sleep"):
            app._converge_disabled_sidecars(attempts=5, interval=0)
        self.assertEqual(mock_set_share.call_count, 3, "失败后应重试直到成功")

    def test_stops_converging_when_all_consistent(self):
        """所有禁用 sidecar 都已停止 -> 一轮即结束, 不做无谓重试。"""
        shares = {"samba": {"enabled": False}, "webdav": {"enabled": False}}
        qb = {"enabled": False, "username": "u", "password": "p"}
        with self._base_patches(shares, qb)[0], self._base_patches(shares, qb)[1], \
             patch.object(app, "share_container_state", return_value="exited"), \
             patch.object(app, "set_share") as mock_set_share, \
             patch.object(app, "set_qb") as mock_set_qb, \
             patch.object(app.time, "sleep") as mock_sleep:
            app._converge_disabled_sidecars(attempts=5, interval=0)
        mock_set_share.assert_not_called()
        mock_set_qb.assert_not_called()
        mock_sleep.assert_not_called()

    def test_enabled_sidecars_never_stopped(self):
        """用户已启用的 sidecar 绝不能被停止。

        语义演进: 早期收敛只管"禁用", 对启用的容器完全不动。现在补了反向收敛
        (补自愈能力/拉起停摆容器), 因此不再断言"完全不触碰", 而是断言**永远
        不会被停用** —— 这是该测试真正要守的底线。
        """
        shares = {"samba": {"enabled": True}, "webdav": {"enabled": True}}
        qb = {"enabled": True, "username": "u", "password": "p"}
        with self._base_patches(shares, qb)[0], self._base_patches(shares, qb)[1], \
             patch.object(app, "share_container_state", return_value="running"), \
             patch.object(app, "container_restart_policy", return_value="unless-stopped"), \
             patch.object(app, "set_share") as mock_set_share, \
             patch.object(app, "set_qb") as mock_set_qb, \
             patch.object(app.time, "sleep"):
            app._converge_disabled_sidecars(attempts=3, interval=0)
        mock_set_share.assert_not_called()
        mock_set_qb.assert_not_called()

    def test_gives_up_after_attempts_without_raising(self):
        """sidecar 始终未就绪 -> 耗尽次数后正常返回, 不抛异常。"""
        shares = {"samba": {"enabled": False}, "webdav": {"enabled": False}}
        qb = {"enabled": False, "username": "u", "password": "p"}
        with self._base_patches(shares, qb)[0], self._base_patches(shares, qb)[1], \
             patch.object(app, "share_container_state", return_value=None), \
             patch.object(app, "set_share"), patch.object(app, "set_qb"), \
             patch.object(app.time, "sleep"):
            try:
                app._converge_disabled_sidecars(attempts=3, interval=0)
            except Exception as e:  # noqa: BLE001
                self.fail(f"收敛逻辑不应抛异常, 实际: {e!r}")

    def test_convergence_runs_in_daemon_thread(self):
        """start_sidecar_convergence 必须在线程中启动且为 daemon(不阻塞/不阻止退出)。"""
        with patch.object(app, "_converge_disabled_sidecars") as mock_conv:
            t = app.start_sidecar_convergence()
        self.assertTrue(t.daemon, "收敛线程必须是 daemon")
        t.join(timeout=5)  # 等线程跑完, 避免残留
        mock_conv.assert_called_once()

    def test_main_entrypoint_uses_convergence(self):
        """回归: __main__ 启动路径必须走收敛函数, 不得退回一次性快照。"""
        import inspect
        src = inspect.getsource(app)
        main_src = src[src.index('if __name__ == "__main__":'):]
        self.assertIn("start_sidecar_convergence()", main_src,
                      "启动路径应调用 start_sidecar_convergence 以覆盖时序竞态")

    def test_main_entrypoint_bootstraps_webdav_before_convergence(self):
        """回归: 启动路径必须先 _bootstrap_webdav_conf 再 start_sidecar_convergence。

        顺序反了会怎样: 收敛线程可能在 webdav.yml 尚不存在时就去 docker start webdav,
        而 webdav 容器的启动命令挂载 `-c /config/webdav.yml`, 文件缺失即崩溃重启 ——
        "配置为启用但未运行" 的死循环。先兜底生成配置再收敛, 才是唯一正确的顺序。
        """
        import inspect
        src = inspect.getsource(app)
        main_src = src[src.index('if __name__ == "__main__":'):]
        i_boot = main_src.index("_bootstrap_webdav_conf()")
        i_conv = main_src.index("start_sidecar_convergence()")
        self.assertLess(i_boot, i_conv,
                        "_bootstrap_webdav_conf() 必须先于 start_sidecar_convergence() 执行")


if __name__ == "__main__":
    unittest.main()
