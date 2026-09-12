#!/usr/bin/env python3
"""Docker 查询失败日志降噪 + 外部 QB 跳过容器查询 的回归测试。

背景(实测): 未挂载 docker.sock 且未设置 DOCKER_HOST 的部署里, 每一次容器状态查询
都会抛 FileNotFoundError。启动收敛线程会对 3 个 sidecar 重试 12 轮(间隔 5 秒), 设置页
的 /api/shares 与 /api/qb/settings 每次访问也各查一轮 —— 面板日志被同一条错误刷满,
真正有用的信息反而被淹没。

本测试锁定的行为:
  * 同类失败在 TTL 内只输出首条日志, 其余静默并累计(不再刷屏)
  * 首条日志附带可操作提示(区分"未接 Docker"与"socket-proxy 不可达")
  * 返回值语义完全不变(仍是 unknown / None), 既有四态判定不受影响
  * 外部 QB 不查询配套 sidecar 容器(container 直接 not_deployed), 配套 QB 仍查询
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402


class _DedupBase(unittest.TestCase):
    """每个用例前清空降噪状态, 避免用例间互相污染。"""

    def setUp(self):
        app._DOCKER_ERR_SEEN.clear()
        self.addCleanup(app._DOCKER_ERR_SEEN.clear)


class TestLogDockerOnce(_DedupBase):
    """_log_docker_once: 同 key 在 TTL 内只打首条, 其余静默并计数。"""

    def test_first_call_is_logged(self):
        with patch.object(app, "log") as mock_log:
            app._log_docker_once("k1", "boom")
        mock_log.assert_called_once_with("boom")

    def test_repeat_within_ttl_is_silent(self):
        """TTL 内重复 5 次 -> 只打 1 条, 不刷屏。"""
        with patch.object(app, "log") as mock_log:
            for _ in range(5):
                app._log_docker_once("k1", "boom")
        self.assertEqual(mock_log.call_count, 1, "TTL 内同类失败必须只输出首条")

    def test_repeat_accumulates_count(self):
        """静默期间仍要计数(为后续可能的统计留痕), 不能直接丢弃。"""
        with patch.object(app, "log"):
            for _ in range(5):
                app._log_docker_once("k1", "boom")
        _ts, count = app._DOCKER_ERR_SEEN["k1"]
        self.assertEqual(count, 5)

    def test_logs_again_after_ttl(self):
        """超过 TTL 后应再次输出(不能永久静默, 否则问题被彻底掩盖)。"""
        with patch.object(app, "log"):
            app._log_docker_once("k1", "boom")
        app._DOCKER_ERR_SEEN["k1"] = (time.time() - app._DOCKER_ERR_TTL - 1, 1)
        with patch.object(app, "log") as mock_log:
            app._log_docker_once("k1", "boom")
        mock_log.assert_called_once_with("boom")

    def test_distinct_keys_are_independent(self):
        """不同 key(不同容器 / 不同异常类型)互不影响, 各自打首条。"""
        with patch.object(app, "log") as mock_log:
            app._log_docker_once("a", "1")
            app._log_docker_once("b", "2")
            app._log_docker_once("a", "1")
        self.assertEqual(mock_log.call_count, 2)


class TestFailureHints(_DedupBase):
    """异常类型 -> 可操作提示。让用户知道这是"未接 Docker"而非"功能坏了"。"""

    def test_file_not_found_hints_sock_or_docker_host(self):
        hint = app._docker_failure_hint(FileNotFoundError(2, "No such file or directory"))
        self.assertIn("/var/run/docker.sock", hint)
        self.assertIn("DOCKER_HOST", hint)

    def test_refused_hints_socket_proxy(self):
        for exc in (ConnectionRefusedError("down"), ConnectionResetError("reset"),
                    TimeoutError("timeout")):
            with self.subTest(exc=type(exc).__name__):
                self.assertIn("socket-proxy", app._docker_failure_hint(exc))

    def test_other_exceptions_have_no_hint(self):
        self.assertEqual(app._docker_failure_hint(ValueError("x")), "")


class TestQueryFuncsDedup(_DedupBase):
    """三个只读查询函数: 失败时返回值语义不变, 但日志被降噪。"""

    def _fnf(self):
        return patch.object(app, "_docker_request",
                            side_effect=FileNotFoundError(2, "No such file or directory"))

    def test_service_state_still_unknown_and_logs_once(self):
        with self._fnf(), patch.object(app, "log") as mock_log:
            results = [app.service_state("iso-hub-samba") for _ in range(5)]
        self.assertEqual(results, ["unknown"] * 5, "返回值语义不得改变")
        self.assertEqual(mock_log.call_count, 1, "5 次失败只应产生 1 条日志")

    def test_service_state_first_log_carries_hint(self):
        """首条日志必须告诉用户该怎么办(挂 socket 或设 DOCKER_HOST)。"""
        with self._fnf(), patch.object(app, "log") as mock_log:
            app.service_state("iso-hub-samba")
        msg = str(mock_log.call_args[0][0])
        self.assertIn("iso-hub-samba", msg)
        self.assertIn("FileNotFoundError", msg)
        self.assertIn("/var/run/docker.sock", msg)

    def test_share_container_state_none_and_logs_once(self):
        with self._fnf(), patch.object(app, "log") as mock_log:
            results = [app.share_container_state("iso-hub-webdav") for _ in range(3)]
        self.assertEqual(results, [None] * 3, "返回值语义不得改变")
        self.assertEqual(mock_log.call_count, 1)

    def test_restart_policy_none_and_logs_once(self):
        with self._fnf(), patch.object(app, "log") as mock_log:
            results = [app.container_restart_policy("iso-hub-qbittorrent") for _ in range(3)]
        self.assertEqual(results, [None] * 3, "返回值语义不得改变")
        self.assertEqual(mock_log.call_count, 1)

    def test_each_container_logs_its_own_first_line(self):
        """三个 sidecar 各失败一次 -> 恰好 3 条(按容器名隔离), 而不是互相吞掉。"""
        with self._fnf(), patch.object(app, "log") as mock_log:
            for name in ("iso-hub-samba", "iso-hub-webdav", "iso-hub-qbittorrent"):
                app.service_state(name)
        self.assertEqual(mock_log.call_count, 3)

    def test_same_container_different_exception_logs_again(self):
        """同一容器换了异常类型(如 socket 没挂 -> 代理挂了)应重新提示。"""
        with patch.object(app, "_docker_request",
                          side_effect=FileNotFoundError(2, "nf")), \
             patch.object(app, "log") as mock_log:
            app.service_state("iso-hub-samba")
        with patch.object(app, "_docker_request",
                          side_effect=ConnectionRefusedError("down")), \
             patch.object(app, "log") as mock_log2:
            app.service_state("iso-hub-samba")
        self.assertEqual(mock_log.call_count, 1)
        self.assertEqual(mock_log2.call_count, 1)
        self.assertIn("socket-proxy", str(mock_log2.call_args[0][0]))


class TestExternalQbSkipsContainerQuery(_DedupBase):
    """外部 QB: 配套 sidecar 的容器状态对它毫无意义, 不得再发起查询。"""

    QB_EXT = {"qb": {"enabled": True, "url": "http://192.168.1.50:18080",
                     "username": "u", "password": "p"}}
    QB_OWN = {"qb": {"enabled": True, "username": "u", "password": "p"}}

    def _get(self, cfg, state="running"):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            sj = data / "settings.json"
            sj.write_text(json.dumps(cfg), encoding="utf-8")
            with patch.object(app, "DATA_DIR", data), \
                 patch.object(app, "SETTINGS_JSON", sj), \
                 patch.object(app, "REQUIRE_LOGIN", False), \
                 patch.object(app, "AUTH_TOKEN", ""), \
                 patch.object(app, "service_state", return_value=state) as m_st, \
                 patch.object(app, "_probe_qb_connection",
                              return_value={"state": "connected", "detail": "ok"}):
                r = app.app.test_client().get("/api/qb/settings")
        return r, m_st

    def test_external_qb_does_not_query_container(self):
        r, m_st = self._get(self.QB_EXT)
        self.assertEqual(r.status_code, 200)
        qb = r.get_json()["qb"]
        self.assertIs(qb["managed"], False)
        m_st.assert_not_called()
        self.assertEqual(qb["container"], "not_deployed")

    def test_managed_qb_still_queries_container(self):
        """配套 sidecar 场景必须保持原行为: 仍查容器, 并用查询结果填充 container。"""
        r, m_st = self._get(self.QB_OWN, state="running")
        qb = r.get_json()["qb"]
        self.assertIs(qb["managed"], True)
        m_st.assert_called_once()
        self.assertEqual(qb["container"], "running")

    def test_post_external_qb_does_not_query_container(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            sj = data / "settings.json"
            sj.write_text(json.dumps(self.QB_EXT), encoding="utf-8")
            with patch.object(app, "DATA_DIR", data), \
                 patch.object(app, "SETTINGS_JSON", sj), \
                 patch.object(app, "REQUIRE_LOGIN", False), \
                 patch.object(app, "AUTH_TOKEN", ""), \
                 patch.object(app, "service_state", return_value="running") as m_st:
                r = app.app.test_client().post(
                    "/api/qb/settings", json={"url": "http://192.168.1.50:18080"})
        self.assertEqual(r.status_code, 200)
        qb = r.get_json()["qb"]
        self.assertIs(qb["managed"], False)
        m_st.assert_not_called()
        self.assertEqual(qb["container"], "not_deployed")


class TestSharesStillQueried(_DedupBase):
    """反向保护: samba/webdav 的 not_deployed 是界面需要的有效信息, 不得被短路掉。"""

    def test_api_shares_queries_both_sidecars(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            sj = data / "settings.json"
            sj.write_text(json.dumps({}), encoding="utf-8")
            with patch.object(app, "DATA_DIR", data), \
                 patch.object(app, "SETTINGS_JSON", sj), \
                 patch.object(app, "REQUIRE_LOGIN", False), \
                 patch.object(app, "AUTH_TOKEN", ""), \
                 patch.object(app, "service_state", return_value="not_deployed") as m_st:
                r = app.app.test_client().get("/api/shares")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(m_st.call_count, 2, "samba/webdav 各应查询一次")
        shares = r.get_json()["shares"]
        for proto in ("samba", "webdav"):
            self.assertEqual(shares[proto]["container"], "not_deployed")
            self.assertIs(shares[proto]["managed"], False)


if __name__ == "__main__":
    unittest.main()
