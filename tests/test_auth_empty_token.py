#!/usr/bin/env python3
"""P0 回归: 空字符串 X-Auth-Token 不得击穿登录门禁。

审查发现(2026-10): `request.headers.get("X-Auth-Token") == AUTH_TOKEN` 在
ISO_HUB_TOKEN 未设置(文档推荐的默认部署)时退化为 `"" == ""` —— 攻击者发送
一个**值为空**的请求头即可在会话校验之前整体放行, REQUIRE_LOGIN=1 形同虚设。

修复: 比较前先判 AUTH_TOKEN 非空, 且用 secrets.compare_digest 恒定时间比较。

断言:
  * REQUIRE_LOGIN=1 + AUTH_TOKEN 未设: 空头/无头/错误头一律 401, 有效会话 200
  * REQUIRE_LOGIN=0 + AUTH_TOKEN 已设: 空头 401(不得命中 "" 比较), 正确 token 200
"""

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402


class EmptyTokenBypassTest(unittest.TestCase):
    """强制登录(默认部署)下, 空 X-Auth-Token 头必须被拒绝。"""

    def setUp(self):
        self._patches = [
            patch.object(app, "REQUIRE_LOGIN", True),
            patch.object(app, "AUTH_TOKEN", ""),   # ISO_HUB_TOKEN 未设置的默认形态
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        app._sessions.clear()
        self.addCleanup(app._sessions.clear)
        self.client = app.app.test_client()

    def test_empty_header_is_rejected_under_require_login(self):
        """核心回归: 携带空 X-Auth-Token 头不得命中 "" == "" 而放行。"""
        r = self.client.get("/api/distros", headers={"X-Auth-Token": ""})
        self.assertEqual(r.status_code, 401,
                         "空 token 头在 AUTH_TOKEN 未设置时曾绕过强制登录(P0)")

    def test_missing_header_is_rejected(self):
        self.assertEqual(self.client.get("/api/distros").status_code, 401)

    def test_wrong_header_is_rejected(self):
        r = self.client.get("/api/distros", headers={"X-Auth-Token": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_valid_session_still_passes(self):
        """修复不能误伤正常登录会话。"""
        app._sessions["real-session-token"] = ("admin", int(time.time()) + 3600)
        r = self.client.get("/api/distros",
                            headers={"X-Auth-Token": "real-session-token"})
        self.assertEqual(r.status_code, 200)


class EmptyTokenWithTokenSetTest(unittest.TestCase):
    """ISO_HUB_REQUIRE_LOGIN=0 + 设置了 ISO_HUB_TOKEN 的旧语义分支同样不得被空串击穿。"""

    def setUp(self):
        self._patches = [
            patch.object(app, "REQUIRE_LOGIN", False),
            patch.object(app, "AUTH_TOKEN", "sekrit-token"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        app._sessions.clear()
        self.addCleanup(app._sessions.clear)
        self.client = app.app.test_client()

    def test_empty_header_is_rejected_when_token_set(self):
        r = self.client.get("/api/distros", headers={"X-Auth-Token": ""})
        self.assertEqual(r.status_code, 401, "空 token 不得匹配任何已设置的密钥")

    def test_correct_token_passes(self):
        r = self.client.get("/api/distros", headers={"X-Auth-Token": "sekrit-token"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
