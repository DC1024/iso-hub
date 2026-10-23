#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4: 保存 qBittorrent 地址时必须校验, 不许"填什么都收"。

背景(线上功能测试 P4): `POST /api/qb/settings {"url":"http://127.0.0.1:99999"}`
返回 200 并原样落盘, 紧接着连接状态就变成 unreachable
(Failed to parse: http://127.0.0.1:99999/...)。错误在保存那一刻就能判定,
却要等用户自己从"连不上"反推。

校验复用 web/url_guard.py —— 它与自定义源/RSS 用的是同一个 SSRF 白名单,
保证"什么算一个 URL"全站只有一处定义(协议 http/https、host 非空、端口 1-65535)。

本文件锁死:
  1. 端口越界 / 非数字端口 / 缺 host / 非 http(s) / 含空白 一律 400;
  2. 400 时**配置不变**(不能"报错了但还是存进去");
  3. 正常地址(带端口、不带端口、https、末尾斜杠)照常保存;
  4. 空串仍表示"不改", 不能因为加了校验就把清空操作变成报错。
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

GOOD_URL = "http://192.168.1.50:18080"


class QbUrlValidationBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.settings = self.data / "settings.json"
        self.settings.write_text(json.dumps(
            {"qb": {"enabled": False, "url": GOOD_URL, "username": "u", "password": "p"}}),
            encoding="utf-8")

        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "SETTINGS_JSON", self.settings),
            patch.object(app, "REQUIRE_LOGIN", False),
            patch.object(app, "AUTH_TOKEN", ""),
            # 保存路径不应触碰 Docker/qB 容器: 一旦碰到说明用例的前提就不对
            patch.object(app, "_docker_request"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = app.app.test_client()

    def post_url(self, url):
        return self.client.post("/api/qb/settings", json={"url": url})

    def saved_qb(self):
        return json.loads(self.settings.read_text(encoding="utf-8")).get("qb", {})


class InvalidUrlTest(QbUrlValidationBase):
    def assert_rejected(self, url):
        resp = self.post_url(url)
        self.assertEqual(resp.status_code, 400,
                         "%r 应被拒绝, 实际 %s %s"
                         % (url, resp.status_code, resp.get_data(as_text=True)[:200]))
        err = (resp.get_json() or {}).get("error") or ""
        self.assertIn("地址", err, "400 必须说明是地址问题")
        self.assertEqual(self.saved_qb().get("url"), GOOD_URL,
                         "%r 被拒绝时不允许改动已保存的地址" % (url,))

    def test_port_out_of_range(self):
        self.assert_rejected("http://127.0.0.1:99999")

    def test_port_not_a_number(self):
        self.assert_rejected("http://127.0.0.1:abc")

    def test_missing_host(self):
        self.assert_rejected("http://")
        self.assert_rejected("http:///path")

    def test_wrong_scheme(self):
        self.assert_rejected("ftp://192.168.1.50:21")
        self.assert_rejected("file:///etc/passwd")

    def test_embedded_whitespace(self):
        self.assert_rejected("http://192.168.1.50 :18080")

    def test_plain_garbage(self):
        self.assert_rejected("not a url")
        self.assert_rejected("192.168.1.50:18080")   # 缺协议头


class ValidUrlTest(QbUrlValidationBase):
    def test_normal_url_saved(self):
        resp = self.post_url("http://10.0.0.9:18080")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:200])
        self.assertEqual(self.saved_qb()["url"], "http://10.0.0.9:18080")

    def test_trailing_slash_stripped(self):
        self.assertEqual(self.post_url("http://10.0.0.9:18080/").status_code, 200)
        self.assertEqual(self.saved_qb()["url"], "http://10.0.0.9:18080")

    def test_default_port_omitted(self):
        self.assertEqual(self.post_url("https://qb.example.com").status_code, 200)
        self.assertEqual(self.saved_qb()["url"], "https://qb.example.com")

    def test_surrounding_whitespace_trimmed(self):
        self.assertEqual(self.post_url("  http://10.0.0.9:8080  ").status_code, 200)
        self.assertEqual(self.saved_qb()["url"], "http://10.0.0.9:8080")

    def test_empty_means_keep_current(self):
        """空串表示"不改", 不能因为加了校验就变成 400。"""
        resp = self.post_url("")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.saved_qb()["url"], GOOD_URL)


if __name__ == "__main__":
    unittest.main()
