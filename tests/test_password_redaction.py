#!/usr/bin/env python3
"""SMB / WebDAV / qBittorrent 密码脱敏(beta 2.3)回归测试。

需求: 与「邮件通知」保持一致 —— 密码/凭据一旦保存就不再回传给前端(输入框永远留空,
带上「留空表示不修改」的占位提示), 只用一个布尔 `password_set` 告诉 UI「有没有配过」;
前端提交空密码 = 沿用已保存的那份, 避免「只想改用户名却把密码清了」的事故。

覆盖:
  纯函数  _redact_password           密码 -> password_set, 其余键原样保留
  后端    /api/shares        GET     不回传 password, 带 password_set
  后端    /api/shares        POST    空密码保留、非空密码替换、响应也脱敏
  后端    /api/qb/settings   GET     不回传 password, 带 password_set
  后端    /api/qb/settings   POST    空密码保留
  前端    HTML 契约                  三个密码框 type=password + 占位提示; 加载不回填;
                                     保存时非空才提交 password; qb 启用校验用 password_set
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


class RedactBase(unittest.TestCase):
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

    def saved(self, key):
        return json.loads(self.settings.read_text(encoding="utf-8")).get(key, {})


class TestRedactHelper(unittest.TestCase):
    """纯函数 _redact_password。"""

    def test_password_replaced_by_flag(self):
        out = app._redact_password({"username": "u", "password": "secret"})
        self.assertNotIn("password", out)
        self.assertTrue(out["password_set"])

    def test_empty_password_flag_false(self):
        out = app._redact_password({"username": "u", "password": ""})
        self.assertNotIn("password", out)
        self.assertFalse(out["password_set"])

    def test_other_keys_preserved(self):
        out = app._redact_password({"username": "u", "password": "p", "port": "1445"})
        self.assertEqual(out["username"], "u")
        self.assertEqual(out["port"], "1445")

    def test_none_and_empty_are_safe(self):
        self.assertFalse(app._redact_password(None)["password_set"])
        self.assertFalse(app._redact_password({})["password_set"])


class TestSharesRedaction(RedactBase):
    """GET/POST /api/shares 的密码脱敏。"""

    def _seed(self):
        self.settings.write_text(json.dumps({
            "samba": {"enabled": True, "username": "u", "password": "sekret"},
            "webdav": {"enabled": False, "username": "u", "password": ""},
        }), encoding="utf-8")

    def test_get_hides_password(self):
        self._seed()
        with patch.object(app, "service_state", return_value="running"):
            r = self.client.get("/api/shares")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("sekret", r.get_data(as_text=True))
        shares = r.get_json()["shares"]
        for proto in ("samba", "webdav"):
            self.assertNotIn("password", shares[proto], proto)
            self.assertIn("password_set", shares[proto], proto)
        self.assertTrue(shares["samba"]["password_set"])
        self.assertFalse(shares["webdav"]["password_set"])

    def test_post_empty_password_keeps_existing(self):
        self._seed()
        with patch.object(app, "apply_share_creds", return_value=True), \
             patch.object(app, "service_state", return_value="running"):
            r = self.client.post("/api/shares", json={"samba": {"username": "u2", "password": ""}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saved("samba")["password"], "sekret")
        self.assertEqual(self.saved("samba")["username"], "u2")

    def test_post_new_password_replaces(self):
        self._seed()
        with patch.object(app, "apply_share_creds", return_value=True), \
             patch.object(app, "service_state", return_value="running"):
            r = self.client.post("/api/shares", json={"samba": {"username": "u", "password": "newp"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saved("samba")["password"], "newp")

    def test_post_response_redacted(self):
        self._seed()
        with patch.object(app, "apply_share_creds", return_value=True), \
             patch.object(app, "set_share", return_value=True), \
             patch.object(app, "service_state", return_value="running"):
            r = self.client.post("/api/shares", json={"samba": {"enabled": True}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertNotIn("sekret", r.get_data(as_text=True))
        self.assertTrue(r.get_json()["shares"]["samba"]["password_set"])


class TestQbRedaction(RedactBase):
    """GET/POST /api/qb/settings 的密码脱敏。"""

    def test_get_hides_password(self):
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": True, "username": "u", "password": "qbsecret"}}), encoding="utf-8")
        with patch.object(app, "service_state", return_value="running"):
            r = self.client.get("/api/qb/settings")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("qbsecret", r.get_data(as_text=True))
        qb = r.get_json()["qb"]
        self.assertNotIn("password", qb)
        self.assertTrue(qb["password_set"])

    def test_get_flag_false_when_empty(self):
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": False, "username": "u", "password": ""}}), encoding="utf-8")
        with patch.object(app, "service_state", return_value="running"):
            r = self.client.get("/api/qb/settings")
        self.assertFalse(r.get_json()["qb"]["password_set"])

    def test_post_empty_password_keeps_existing(self):
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": True, "username": "u", "password": "keepqb"}}), encoding="utf-8")
        conf = self.data / "qb" / "qBittorrent.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("", encoding="utf-8")
        fake = MagicMock()
        fake.status = 200
        with patch.object(app, "service_state", return_value="running"), \
             patch.object(app, "QB_CONF_PATH", conf), \
             patch.object(app, "_set_qb_password", return_value=True), \
             patch.object(app, "_docker_request", return_value=fake):
            r = self.client.post("/api/qb/settings", json={"username": "u2", "password": ""})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saved("qb")["password"], "keepqb")
        self.assertEqual(self.saved("qb")["username"], "u2")
        self.assertNotIn("keepqb", r.get_data(as_text=True))

    def test_post_new_password_replaces(self):
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": True, "username": "u", "password": "oldqb"}}), encoding="utf-8")
        conf = self.data / "qb" / "qBittorrent.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("", encoding="utf-8")
        fake = MagicMock()
        fake.status = 200
        with patch.object(app, "service_state", return_value="running"), \
             patch.object(app, "QB_CONF_PATH", conf), \
             patch.object(app, "_set_qb_password", return_value=True) as m_set, \
             patch.object(app, "_docker_request", return_value=fake):
            r = self.client.post("/api/qb/settings", json={"password": "freshqb"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.saved("qb")["password"], "freshqb")
        m_set.assert_called_once()


class TestPasswordFrontendContract(unittest.TestCase):
    """前端契约: 三个密码框必须脱敏, 且"留空=不修改"的语义在三处保存里都成立。"""

    def test_inputs_are_password_type_with_keep_placeholder(self):
        for i in ("p-samba2", "p-webdav2", "p-qb"):
            with self.subTest(input=i):
                m = re.search(r'<input[^>]*id="%s"[^>]*>' % re.escape(i), HTML)
                self.assertIsNotNone(m, i)
                tag = m.group(0)
                self.assertIn('type="password"', tag, i)
                self.assertIn("留空表示不修改", tag, i)

    def test_load_functions_do_not_backfill_password(self):
        self.assertNotIn("value=cfg.password", HTML)
        self.assertNotIn("pin.value=cfg.password", HTML)

    def test_save_functions_omit_empty_password(self):
        # 共享保存、qb 保存、qb 开关: 均为「非空才提交 password」
        self.assertIn("if(pw)p.password=pw;", HTML)
        self.assertEqual(HTML.count("if(password)body.password=password;"), 2)
        self.assertNotIn("r.j.qb.password", HTML)

    def test_qb_enable_uses_password_set(self):
        # 启用校验必须把「已配过但没重新输入」视为有效, 否则用户没法只切开关
        self.assertIn("QB_PW_SET", HTML)
        self.assertIn("!QB_PW_SET", HTML)
        self.assertIn("password_set", HTML)


if __name__ == "__main__":
    unittest.main()
