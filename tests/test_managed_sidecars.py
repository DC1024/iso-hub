#!/usr/bin/env python3
"""配套/外部容器(managed 标志)回归测试。

用户需求: 设置面板需要区分「iso-hub 配套的 sidecar 容器」与「用户自行部署的外部容器」。
- 配套容器: iso-hub 能检测并管理它 → 面板可修改用户名/密码(同步到 sidecar 并重启)。
- 外部容器(非配套): 用户自行部署。SMB/WebDAV 无配套容器可检测时, 面板禁用凭据输入并提示;
  qBittorrent 指向外部时, 用户名/密码**保持可填**(用于登录外部 QB), 仅提示且只保存不重启。

判断依据:
- SMB/WebDAV: container 状态是否 running/stopped(iso-hub 可检测) vs not_deployed/unknown(检测不到)。
- qBittorrent: url 是否被用户指向外部(非默认内部地址 http://qbittorrent:8080)。

覆盖:
  后端  /api/shares        GET 在容器未部署/未知时 managed=false, running 时 managed=true
  后端  /api/qb/settings   GET 在 url 指向外部时 managed=false, 未自定义时 managed=true
  前端  HTML 契约          存在 managedHint i18n key + applyManagedHint 助手 + 三个提示占位元素
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


class SharesManagedTestBase(unittest.TestCase):
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


class TestSharesManagedFlag(SharesManagedTestBase):
    """/api/shares GET 的 managed 标志: 容器是否由 iso-hub 管理。"""

    def test_not_deployed_is_unmanaged(self):
        """容器未部署(用户自行部署)时 managed=False。"""
        with patch.object(app, "service_state", return_value="not_deployed"):
            r = self.client.get("/api/shares")
            self.assertEqual(r.status_code, 200)
            for proto in ("samba", "webdav"):
                self.assertIs(r.get_json()["shares"][proto]["managed"], False,
                              f"{proto} 未部署时应为非配套(managed=False)")

    def test_unknown_is_unmanaged(self):
        """容器状态未知(socket-proxy 未就绪等)时按非配套处理, 避免面板误导用户可改凭据。"""
        with patch.object(app, "service_state", return_value="unknown"):
            r = self.client.get("/api/shares")
            for proto in ("samba", "webdav"):
                self.assertIs(r.get_json()["shares"][proto]["managed"], False)

    def test_running_is_managed(self):
        """容器 running(iso-hub 配套 sidecar)时 managed=True, 面板可改凭据。"""
        with patch.object(app, "service_state", return_value="running"):
            r = self.client.get("/api/shares")
            for proto in ("samba", "webdav"):
                self.assertIs(r.get_json()["shares"][proto]["managed"], True)

    def test_stopped_is_managed(self):
        """容器 stopped(配套 sidecar 但被停用)时仍 managed=True(iso-hub 仍能管理它)。"""
        with patch.object(app, "service_state", return_value="stopped"):
            r = self.client.get("/api/shares")
            for proto in ("samba", "webdav"):
                self.assertIs(r.get_json()["shares"][proto]["managed"], True)


class TestQbManagedFlag(SharesManagedTestBase):
    """/api/qb/settings GET 的 managed 标志: 是否指向配套 sidecar qb。"""

    def _get_qb(self, settings_obj):
        self.settings.write_text(json.dumps(settings_obj), encoding="utf-8")
        with patch.object(app, "service_state", return_value="running"):
            r = self.client.get("/api/qb/settings")
        self.assertEqual(r.status_code, 200)
        return r.get_json()["qb"]

    def test_url_not_saved_is_managed(self):
        """url 从未被用户保存 → 用默认内部 sidecar 地址 → 配套(managed=True)。"""
        qb = self._get_qb({})  # 空配置, url_saved=False
        self.assertFalse(qb["url_saved"])
        self.assertIs(qb["managed"], True)

    def test_external_url_is_unmanaged(self):
        """url 被用户保存为外部地址 → 自行部署的 qb → 非配套(managed=False)。"""
        qb = self._get_qb({"qb": {"url": "http://192.168.1.50:18080"}})
        self.assertTrue(qb["url_saved"])
        self.assertIs(qb["managed"], False)

    def test_default_internal_url_is_managed(self):
        """url 被保存但仍指向默认内部地址 → 仍是配套 sidecar → managed=True。"""
        qb = self._get_qb({"qb": {"url": app.DEFAULT_QB["url"]}})
        self.assertTrue(qb["url_saved"])
        self.assertIs(qb["managed"], True)

    def test_external_url_with_trailing_slash_is_unmanaged(self):
        """外部 url 末尾带斜杠也要正确判为外部(后端做了 rstrip('/'))。"""
        qb = self._get_qb({"qb": {"url": "http://10.0.0.9:18080/"}})
        self.assertTrue(qb["url_saved"])
        self.assertIs(qb["managed"], False)


class TestManagedFrontendContract(unittest.TestCase):
    """前端 HTML 契约: 提示文案、助手函数、提示占位元素必须存在。"""

    def test_has_managed_hint_i18n(self):
        self.assertIn("managedHint", HTML)
        # 中文与英文文案都应有
        self.assertIn("非配套容器", HTML)
        self.assertIn("non-bundled", HTML)

    def test_has_qb_external_hint_i18n(self):
        """外部 QB 有独立的提示文案(区别于 SMB/WebDAV 的"无法管理用户名/密码")。"""
        self.assertIn("managedHintQb", HTML)
        self.assertIn("用于登录连接", HTML)  # 中文
        self.assertIn("log in to it", HTML)  # 英文

    def test_qb_hint_element_uses_qb_specific_text(self):
        """QB 提示占位元素用 managedHintQb(而不是 SMB/WebDAV 的 managedHint)。"""
        # QB 的 data-i18n 必须是 managedHintQb
        m = re.search(r'id="managed-hint-qb"[^>]*data-i18n="([^"]+)"', HTML)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "managedHintQb")

    def test_has_apply_managed_hint_helper(self):
        self.assertIn("function applyManagedHint", HTML)

    def test_has_hint_placeholder_elements(self):
        for hid in ("managed-hint-samba2", "managed-hint-webdav2", "managed-hint-qb"):
            self.assertIn(f'id="{hid}"', HTML)

    def test_has_qb_save_button_id(self):
        self.assertIn('id="btn-save-qb"', HTML)

    def test_qb_load_keeps_credentials_editable_for_external(self):
        """外部 QB 场景下, 用户名/密码必须保持可填(仅提示, 不传入禁用列表)。

        若 loadQbSettings 把 ['u-qb','p-qb'] 传给 applyManagedHint, 外部 QB 的
        登录凭据会被禁用, 用户就无法连接外部 QB —— 那与需求相悖。
        """
        m = re.search(
            r"applyManagedHint\(qbManaged,'managed-hint-qb',([^;]*)\);",
            HTML
        )
        self.assertIsNotNone(m, "QB 的 applyManagedHint 调用不存在")
        args = m.group(1)
        self.assertNotIn("u-qb", args, "外部 QB 场景下用户名输入框不应被禁用")
        self.assertNotIn("p-qb", args, "外部 QB 场景下密码输入框不应被禁用")
        self.assertNotIn("btn-save-qb", args, "外部 QB 场景下保存按钮不应被禁用")


class TestQbExternalSaveNoRestart(SharesManagedTestBase):
    """外部 QB 保存凭据时, 只落盘配置, 不写 sidecar conf 也不重启容器。"""

    def _post_qb(self, body, settings_obj=None):
        if settings_obj is not None:
            self.settings.write_text(json.dumps(settings_obj), encoding="utf-8")
        r = self.client.post("/api/qb/settings", json=body)
        return r

    def test_external_cred_save_does_not_write_conf_nor_restart(self):
        """外部 QB(url 指向外部) 改凭据 → 只保存, 不调 _set_qb_password、不调 docker restart。"""
        # 预置: 已启用 + 已保存外部 url
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": True, "url": "http://192.168.1.50:18080",
                    "username": "old", "password": "old"}}), encoding="utf-8")
        with patch.object(app, "service_state", return_value="running"), \
             patch.object(app, "_set_qb_password", return_value=True) as m_set, \
             patch.object(app, "_docker_request") as m_docker:
            r = self._post_qb({"username": "newu", "password": "newp"})
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.get_json()["qb"]["managed"], False)
        m_set.assert_not_called()   # 外部 QB 不写 sidecar conf
        m_docker.assert_not_called()  # 外部 QB 不重启容器
        # 但配置确实保存了
        saved = json.loads(self.settings.read_text(encoding="utf-8"))["qb"]
        self.assertEqual(saved["username"], "newu")
        self.assertEqual(saved["password"], "newp")

    def test_bundled_cred_save_still_writes_conf_and_restarts(self):
        """配套 QB(默认内部地址) 改凭据 → 仍写 sidecar conf + 重启(回归护栏)。"""
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": True, "username": "admin", "password": "adminadmin"}}),
            encoding="utf-8")
        # 配套 sidecar 的 conf 必须真实存在(模拟已挂载的 qb-config),
        # 否则后端会因 .exists() 为 False 走"凭据保存失败"分支返回 500。
        conf = self.data / "qb" / "qBittorrent.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("", encoding="utf-8")
        fake_resp = MagicMock()
        fake_resp.status = 200
        with patch.object(app, "service_state", return_value="running"), \
             patch.object(app, "QB_CONF_PATH", conf), \
             patch.object(app, "_set_qb_password", return_value=True) as m_set, \
             patch.object(app, "_docker_request", return_value=fake_resp) as m_docker:
            r = self._post_qb({"username": "newu", "password": "newp"})
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.get_json()["qb"]["managed"], True)
        m_set.assert_called_once()
        m_docker.assert_called_once()


class TestQbExternalEnableNoSidecar(SharesManagedTestBase):
    """外部 QB 切换 enabled 开关时, 不应操作配套 sidecar 容器(没有可启停的容器)。

    回归背景: v1.3.10 只修了"纯凭据变更"路径, enabled 变更路径仍会调 set_qb()
    去 start/stop iso-hub-qbittorrent; 外部 QB 场景下该容器不存在 -> set_qb 失败 ->
    误报"容器状态未知(请检查 socket-proxy 是否运行)"(500)。
    而外部 QB 根本不需要 socket-proxy —— iso-hub 只走 Web API 登录连接它。
    """

    def test_external_enable_does_not_touch_sidecar(self):
        """外部 QB 从禁用切到启用: 不调 set_qb、不碰 Docker, 不报 500, 只落盘 enabled。"""
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": False, "url": "http://192.168.1.50:18080",
                    "username": "u", "password": "p"}}), encoding="utf-8")
        # service_state 返回 unknown(容器不存在/代理不可达) 时, 旧代码会据此报 500
        with patch.object(app, "service_state", return_value="unknown"), \
             patch.object(app, "set_qb", return_value=False) as m_setqb, \
             patch.object(app, "_docker_request") as m_docker:
            r = self.client.post("/api/qb/settings",
                                 json={"enabled": True, "username": "u", "password": "p"})
        self.assertEqual(r.status_code, 200,
                         f"外部 QB 启用不应因容器状态报错: {r.get_data(as_text=True)}")
        m_setqb.assert_not_called()   # 外部 QB 不启停配套容器
        m_docker.assert_not_called()  # 也不该碰 Docker API
        self.assertIs(r.get_json()["qb"]["managed"], False)
        saved = json.loads(self.settings.read_text(encoding="utf-8"))["qb"]
        self.assertIs(saved["enabled"], True)

    def test_external_disable_does_not_touch_sidecar(self):
        """外部 QB 停用同样不碰容器。"""
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": True, "url": "http://192.168.1.50:18080",
                    "username": "u", "password": "p"}}), encoding="utf-8")
        with patch.object(app, "service_state", return_value="unknown"), \
             patch.object(app, "set_qb", return_value=False) as m_setqb:
            r = self.client.post("/api/qb/settings",
                                 json={"enabled": False, "username": "u", "password": "p"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        m_setqb.assert_not_called()
        saved = json.loads(self.settings.read_text(encoding="utf-8"))["qb"]
        self.assertIs(saved["enabled"], False)

    def test_external_enable_with_url_in_same_request(self):
        """同一次请求里既填外部 url 又开启开关(用户最典型的操作) -> 也不碰容器。"""
        self.settings.write_text("{}", encoding="utf-8")
        with patch.object(app, "service_state", return_value="unknown"), \
             patch.object(app, "set_qb", return_value=False) as m_setqb:
            r = self.client.post("/api/qb/settings",
                                 json={"enabled": True, "url": "http://10.0.0.9:18080",
                                       "username": "u", "password": "p"})
        self.assertEqual(r.status_code, 200,
                         f"填地址+开开关是一次性操作, 不应报错: {r.get_data(as_text=True)}")
        m_setqb.assert_not_called()
        self.assertIs(r.get_json()["qb"]["managed"], False)

    def test_bundled_enable_still_calls_set_qb(self):
        """配套 QB(无外部 url) 启用 -> 仍调 set_qb 启停配套容器(回归护栏)。"""
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": False, "username": "u", "password": "p"}}), encoding="utf-8")
        with patch.object(app, "service_state", return_value="running"), \
             patch.object(app, "set_qb", return_value=True) as m_setqb:
            r = self.client.post("/api/qb/settings",
                                 json={"enabled": True, "username": "u", "password": "p"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        m_setqb.assert_called_once()   # 配套 QB 必须照常启停容器
        self.assertIs(r.get_json()["qb"]["managed"], True)

    def test_bundled_enable_failure_still_reports_error(self):
        """配套 QB 启用失败(set_qb 返回 False) -> 仍应报 500 明确提示(回归护栏)。"""
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": False, "username": "u", "password": "p"}}), encoding="utf-8")
        with patch.object(app, "service_state", return_value="unknown"), \
             patch.object(app, "set_qb", return_value=False):
            r = self.client.post("/api/qb/settings",
                                 json={"enabled": True, "username": "u", "password": "p"})
        self.assertEqual(r.status_code, 500)
        self.assertIn("socket-proxy", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()