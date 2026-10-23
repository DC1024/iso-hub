#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P9: 版本号的单一真源 —— 三处必须永远一致。

背景(线上功能测试 P9): v1.8.2 的镜像里, 页面标题旁的版本徽标写着 **1.7.0**。
前端把版本号硬编码成 `const APP_VERSION='1.7.0'`, 发版时没人记得改它 ——
用户无法从界面上确认自己跑的到底是哪个版本, 只能去翻镜像 tag。

本文件锁死:
  1. web/version.py 的 __version__ 是唯一真源, 形如 X.Y.Z;
  2. index.html 的兜底常量与它一致(兜底只用于首屏, 但不许漂);
  3. GET /api/health 返回 version;
  4. GET / 的响应头带 X-Version。

为什么要有 2: 首屏徽标在 fetch 回来之前就得有内容, 否则会闪一下空白。
允许兜底, 但不允许它长期停在旧值 —— 这正是这次 bug 的形态。
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import version  # noqa: E402

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")


class VersionFormatTest(unittest.TestCase):
    def test_is_semver_like(self):
        self.assertRegex(version.__version__, r"^\d+\.\d+\.\d+$",
                         "__version__ 必须是 X.Y.Z(与 git tag 去掉前缀 v 后一致)")

    def test_frontend_fallback_matches_backend(self):
        """index.html 里的兜底常量必须与 web/version.py 同步。

        改版本只改一边 = 徽标又骗人一次(只是程度轻些: fetch 回来会被纠正)。
        """
        m = re.search(r"let APP_VERSION='([^']+)'", HTML)
        self.assertIsNotNone(m, "index.html 里找不到 APP_VERSION 常量")
        self.assertEqual(
            m.group(1), version.__version__,
            "index.html 的 APP_VERSION 与 web/version.py 不一致 —— 发版要两处一起改")

    def test_frontend_reads_version_from_health(self):
        """前端必须真的去读 /api/health 覆盖兜底值, 否则常量一漂就没人救了。"""
        self.assertIn("/api/health", HTML)
        self.assertRegex(HTML, r"APP_VERSION=r\.version",
                         "缺少「以后端 version 覆盖前端常量」的赋值")


class VersionExposureTest(unittest.TestCase):
    """/api/health 与首页响应头都要带版本(运维不用进容器也能确认)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _client(self):
        import web.app as web_app
        with patch.dict(os.environ, dict(os.environ, ISO_DATA_DIR=self._tmp.name)):
            return web_app.app.test_client()

    def test_health_exposes_version(self):
        body = self._client().get("/api/health").get_json()
        self.assertEqual(body.get("version"), version.__version__)

    def test_index_sends_x_version_header(self):
        resp = self._client().get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Version"), version.__version__)

    def test_index_cache_headers_still_no_store(self):
        """加版本头不该顺手改掉原有的禁缓存策略(升级后必须拿到新前端)。"""
        resp = self._client().get("/")
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))


if __name__ == "__main__":
    unittest.main()
