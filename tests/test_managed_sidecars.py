#!/usr/bin/env python3
"""配套/外部容器(managed 标志)回归测试。

用户需求: 设置面板需要区分「iso-hub 配套的 sidecar 容器」与「用户自行部署的外部容器」。
- 配套容器: iso-hub 能检测并管理它 → 面板可修改用户名/密码。
- 外部容器(非配套): 用户自行部署, iso-hub 面板无法管理其凭据 → 面板应禁用凭据输入并提示。

判断依据:
- SMB/WebDAV: container 状态是否 running/stopped(iso-hub 可检测) vs not_deployed/unknown(检测不到)。
- qBittorrent: url 是否被用户指向外部(非默认内部地址 http://qbittorrent:8080)。

覆盖:
  后端  /api/shares        GET 在容器未部署/未知时 managed=false, running 时 managed=true
  后端  /api/qb/settings   GET 在 url 指向外部时 managed=false, 未自定义时 managed=true
  前端  HTML 契约          存在 managedHint i18n key + applyManagedHint 助手 + 三个提示占位元素
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_has_apply_managed_hint_helper(self):
        self.assertIn("function applyManagedHint", HTML)

    def test_has_hint_placeholder_elements(self):
        for hid in ("managed-hint-samba2", "managed-hint-webdav2", "managed-hint-qb"):
            self.assertIn(f'id="{hid}"', HTML)

    def test_has_qb_save_button_id(self):
        self.assertIn('id="btn-save-qb"', HTML)


if __name__ == "__main__":
    unittest.main()