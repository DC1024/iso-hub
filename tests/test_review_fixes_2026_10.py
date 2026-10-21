#!/usr/bin/env python3
"""2026-10 代码审查修复的回归测试。

覆盖审查报告里 D 盘修复之外、本次补齐的项:
  * P3-9  PBKDF2 迭代数 12 万 → 60 万, 且**存量哈希仍可验证** + 登录后透明升级
  * P3-10 改密码吊销该用户其他会话(保留当前这一把)
  * P3-8  调度参数垃圾输入不再 500(安全转 int + 夹区间)
  * P3-11 基础安全响应头(CSP / nosniff / frame-ancestors)
  * P3-2  登录失败限速(连续 5 次 → 429)
  * P2-2  未验签落定的文件在 /api/distros 里带 unverified 标记
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


class _Base(unittest.TestCase):
    """临时 DATA_DIR + 关掉鉴权, 直接打 Flask test_client。"""

    require_login = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        (self.data / "linux" / "Ubuntu").mkdir(parents=True)
        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.data / "distributions.json"),
            patch.object(app, "SETTINGS_JSON", self.data / "settings.json"),
            patch.object(app, "FAILURES_JSON", self.data / "download_failures.json"),
            patch.object(app, "UNVERIFIED_JSON", self.data / "download_unverified.json"),
            patch.object(app, "REQUIRE_LOGIN", self.require_login),
            patch.object(app, "AUTH_TOKEN", ""),
            patch.object(app, "AUTO_SYNC_LAST", {"t": 0}),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        app._sessions.clear()
        app._login_fails.clear()
        self.addCleanup(app._sessions.clear)
        self.addCleanup(app._login_fails.clear)
        self.client = app.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def write_users(self, users: dict):
        (self.data / "settings.json").write_text(
            json.dumps({"users": users}), encoding="utf-8")

    def read_users(self) -> dict:
        p = self.data / "settings.json"
        if not p.exists():
            return {}
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("users", {})


class PasswordIterationsTest(_Base):
    """P3-9: 新哈希 60 万次, 旧哈希(12 万, 无 iterations 字段)必须仍可登录。"""

    def test_legacy_hash_without_iterations_still_logs_in(self):
        salt = "abcd1234"
        legacy = app._hash_pw("old-pw", salt, app.PBKDF2_ITERATIONS_LEGACY)
        self.write_users({"alice": {"salt": salt, "password_hash": legacy, "created_at": 0}})
        self.assertEqual(app._verify_login("alice", "old-pw"), "ok",
                         "存量用户(旧迭代数)不能被升级动作锁在门外")

    def test_legacy_hash_is_upgraded_transparently_after_login(self):
        salt = "abcd1234"
        legacy = app._hash_pw("old-pw", salt, app.PBKDF2_ITERATIONS_LEGACY)
        self.write_users({"alice": {"salt": salt, "password_hash": legacy, "created_at": 0}})
        self.assertEqual(app._verify_login("alice", "old-pw"), "ok")
        u = self.read_users()["alice"]
        self.assertEqual(u.get("iterations"), app.PBKDF2_ITERATIONS,
                         "登录成功应把旧记录透明升级到新迭代数")
        self.assertEqual(app._verify_login("alice", "old-pw"), "ok",
                         "升级后的哈希必须仍能验证同一个密码")

    def test_wrong_password_on_legacy_hash_still_fails(self):
        salt = "abcd1234"
        legacy = app._hash_pw("old-pw", salt, app.PBKDF2_ITERATIONS_LEGACY)
        self.write_users({"alice": {"salt": salt, "password_hash": legacy, "created_at": 0}})
        self.assertEqual(app._verify_login("alice", "nope"), "bad_pass")


class RevokeSessionsOnPasswordChangeTest(_Base):
    """P3-10: 改密码后其他会话失效, 当前会话保留。"""

    def test_change_password_revokes_others(self):
        salt = "s1"
        self.write_users({"admin": {"salt": salt,
                                    "password_hash": app._hash_pw("old-pw", salt,
                                                                  app.PBKDF2_ITERATIONS_LEGACY),
                                    "created_at": 0}})
        exp = int(time.time()) + 3600
        app._sessions.update({
            "cur-token": ("admin", exp),
            "other-1": ("admin", exp),
            "other-2": ("admin", exp),
            "someone-else": ("bob", exp),
        })
        r = self.client.post("/api/user/password",
                             json={"old_password": "old-pw", "new_password": "new-pw"},
                             headers={"X-Auth-Token": "cur-token"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["revoked_sessions"], 2)
        self.assertIn("cur-token", app._sessions, "发起改密的会话不能被踢")
        self.assertNotIn("other-1", app._sessions)
        self.assertNotIn("other-2", app._sessions)
        self.assertIn("someone-else", app._sessions, "不能误伤其他用户")
        self.assertEqual(app._verify_login("admin", "new-pw"), "ok")


class ScheduleInputTest(_Base):
    """P3-8: 垃圾输入回退默认值而不是 500。"""

    def test_bad_day_of_week_does_not_500(self):
        r = self.client.post("/api/schedules", json={
            "type": "weekly", "day_of_week": "abc", "day_of_month": [],
            "month_of_year": {"x": 1},
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        lst = r.get_json().get("schedules") or app.load_schedules()
        row = [s for s in lst if s.get("type") == "weekly"][0]
        self.assertEqual(row["day_of_week"], 0)
        self.assertEqual(row["day_of_month"], 1)
        self.assertEqual(row["month_of_year"], 1)

    def test_out_of_range_values_are_clamped(self):
        r = self.client.post("/api/schedules", json={
            "type": "weekly", "day_of_week": 99, "day_of_month": -5, "month_of_year": 42,
        })
        self.assertEqual(r.status_code, 200)
        lst = r.get_json().get("schedules") or app.load_schedules()
        row = [s for s in lst if s.get("type") == "weekly"][0]
        self.assertEqual(row["day_of_week"], 6)
        self.assertEqual(row["day_of_month"], 1)
        self.assertEqual(row["month_of_year"], 12)


class SecurityHeadersTest(_Base):
    """P3-11: 基础安全响应头。"""

    def test_headers_present_on_api_and_page(self):
        for path in ("/api/health", "/"):
            r = self.client.get(path)
            h = r.headers
            self.assertEqual(h.get("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(h.get("X-Frame-Options"), "DENY", path)
            self.assertIn("frame-ancestors 'none'", h.get("Content-Security-Policy", ""), path)
            self.assertIn("object-src 'none'", h.get("Content-Security-Policy", ""), path)


class LoginRateLimitTest(_Base):
    """P3-2: 连续失败达到阈值后锁定该 IP。"""

    def test_locked_after_five_failures(self):
        salt = "s1"
        self.write_users({"admin": {"salt": salt,
                                    "password_hash": app._hash_pw("right", salt,
                                                                  app.PBKDF2_ITERATIONS_LEGACY),
                                    "created_at": 0}})
        for _ in range(app._LOGIN_MAX_FAILS):
            r = self.client.post("/api/user/login",
                                 json={"username": "admin", "password": "wrong"})
            self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/user/login", json={"username": "admin", "password": "right"})
        self.assertEqual(r.status_code, 429, "锁定期间即使密码正确也应拒绝")
        self.assertEqual(r.get_json()["code"], "rate_limited")

    def test_success_clears_counter(self):
        salt = "s1"
        self.write_users({"admin": {"salt": salt,
                                    "password_hash": app._hash_pw("right", salt,
                                                                  app.PBKDF2_ITERATIONS_LEGACY),
                                    "created_at": 0}})
        for _ in range(app._LOGIN_MAX_FAILS - 1):
            self.client.post("/api/user/login", json={"username": "admin", "password": "wrong"})
        r = self.client.post("/api/user/login", json={"username": "admin", "password": "right"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(app._login_fails.get("127.0.0.1"), None,
                         "登录成功应清零该 IP 的失败计数")


class UnverifiedSurfacingTest(_Base):
    """P2-2: 未验签落定的文件必须能被前端区分出来。"""

    def _write_distro(self, fname: str, size: int = 16):
        (self.data / "linux" / "Ubuntu" / fname).write_bytes(b"x" * size)
        (self.data / "distributions.json").write_text(json.dumps({
            "distributions": [{
                "distribution": "Ubuntu", "type": "linux",
                "download_url": f"https://example.invalid/{fname}",
            }]
        }), encoding="utf-8")

    def test_unverified_flag_when_record_exists(self):
        self._write_distro("ubuntu-26.10.iso")
        (self.data / "download_unverified.json").write_text(json.dumps({
            "linux/Ubuntu/ubuntu-26.10.iso": {"at": int(time.time()),
                                              "reason": "checksum 404"}
        }), encoding="utf-8")
        entries = [e for g in app.build_distros()["groups"]
                   for e in g["entries"] if e["filename"] == "ubuntu-26.10.iso"]
        self.assertTrue(entries, "条目应出现在 build_distros 结果里")
        e = entries[0]
        self.assertEqual(e["status"], "downloaded")
        self.assertTrue(e["unverified"], "未验签记录存在时条目必须带标记")
        self.assertIn("checksum 404", e["unverified_reason"])

    def test_no_flag_when_no_record(self):
        self._write_distro("ubuntu-26.10.iso")
        entries = [e for g in app.build_distros()["groups"]
                   for e in g["entries"] if e["filename"] == "ubuntu-26.10.iso"]
        self.assertFalse(entries[0]["unverified"])


if __name__ == "__main__":
    unittest.main()
