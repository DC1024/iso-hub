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

    def test_enabled_sidecars_untouched(self):
        """用户已启用的 sidecar 即便 running 也不得被停止。"""
        shares = {"samba": {"enabled": True}, "webdav": {"enabled": True}}
        qb = {"enabled": True, "username": "u", "password": "p"}
        with self._base_patches(shares, qb)[0], self._base_patches(shares, qb)[1], \
             patch.object(app, "share_container_state", return_value="running"), \
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


if __name__ == "__main__":
    unittest.main()
