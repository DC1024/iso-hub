#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A+B: settings.json 落盘加密 (AES-GCM) + chmod 600 的测试。

设计要点(见 web/config_files.py):
  * 加密由 env ISO_HUB_SECRET_KEY / ISO_HUB_SECRET_KEY_FILE 开关; 不设则明文, 向后兼容。
  * 整文件 AES-GCM, 按文件名 settings.json 自动识别(三处写方 + sync_subscriptions 子进程都覆盖)。
  * 解密失败(密钥不对)抛 SettingsDecryptError, 调用方不得静默当空配置 / 当损坏重置。
  * 明文回退: 启用密钥前已存在的明文文件可照常读取, 下次写自动转密文。

注意: chmod 600 的精确断言只在 Linux/CI 有意义(Windows 无 POSIX 权限语义),
本地 Windows 跑会 skip, 由 CI 覆盖。
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402
import config_files  # noqa: E402


class SettingsEncCase(unittest.TestCase):
    def setUp(self):
        # 先抓旧值(可能为 None), 避免泄漏到其他测试模块
        self._old_key = os.environ.get("ISO_HUB_SECRET_KEY")
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name).resolve()
        self.settings = self.data / "settings.json"
        self._patches = [
            patch.object(app, "SETTINGS_JSON", self.settings),
            patch.object(app, "DATA_DIR", self.data),
        ]
        for p in self._patches:
            p.start()
        os.environ["ISO_HUB_SECRET_KEY"] = "test-secret-key-A"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in self._patches:
            p.stop()
        if self._old_key is None:
            os.environ.pop("ISO_HUB_SECRET_KEY", None)
        else:
            os.environ["ISO_HUB_SECRET_KEY"] = self._old_key
        self._tmp.cleanup()

    # 1) 往返 + 磁盘为密文(非合法 JSON)
    def test_roundtrip_stores_ciphertext(self):
        app.save_settings_all({"shared": {"samba": {"password": "hunter2"}}})
        raw = self.settings.read_bytes()
        # 密文是二进制, 用 latin-1 解码(永不抛错)再断言它不是合法 JSON
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw.decode("latin-1"))
        self.assertEqual(
            app.load_settings_all()["shared"]["samba"]["password"], "hunter2")

    # 2) chmod 600(仅 Linux/CI)
    def test_chmod_600_on_settings(self):
        app.save_settings_all({"a": 1})
        if sys.platform == "win32":
            self.skipTest("chmod 600 在 Windows 上无 POSIX 语义, 由 CI(Linux) 覆盖")
        mode = stat.S_IMODE(self.settings.stat().st_mode)
        self.assertEqual(mode, 0o600)

    # 3) .corrupt 备份同样 600
    def test_chmod_600_on_corrupt_backup(self):
        self.settings.write_text('{"x": "garbage', encoding="utf-8")
        app.save_settings_all({"x": 1})
        corrupts = list(self.data.glob("*.corrupt*"))
        self.assertTrue(corrupts, "应生成 .corrupt 备份")
        if sys.platform == "win32":
            self.skipTest("chmod 600 在 Windows 上无 POSIX 语义, 由 CI(Linux) 覆盖")
        for c in corrupts:
            self.assertEqual(stat.S_IMODE(c.stat().st_mode), 0o600)

    # 4) 不设密钥 -> 明文, 行为完全等价于旧版
    def test_plaintext_when_no_key(self):
        os.environ.pop("ISO_HUB_SECRET_KEY", None)
        app.save_settings_all({"a": 1})
        raw = self.settings.read_bytes()
        self.assertEqual(json.loads(raw.decode("utf-8")), {"a": 1})
        self.assertEqual(app.load_settings_all(), {"a": 1})

    # 5) 已存在明文 + 后来设密钥 -> 先当明文读, 下次写转密文
    def test_plaintext_fallback_then_encrypts(self):
        os.environ.pop("ISO_HUB_SECRET_KEY", None)
        self.settings.write_text(json.dumps({"a": 1}), encoding="utf-8")
        os.environ["ISO_HUB_SECRET_KEY"] = "test-secret-key-A"
        self.assertEqual(app.load_settings_all(), {"a": 1})  # 旧明文可读
        app.save_settings_all({"a": 2})                      # 写一次后转密文
        raw = self.settings.read_bytes()
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw.decode("latin-1"))
        self.assertEqual(app.load_settings_all(), {"a": 2})

    # 6) 密钥错 -> 抛 SettingsDecryptError, 绝不覆盖/不静默/不重置
    def test_wrong_key_raises_and_does_not_overwrite(self):
        app.save_settings_all({"secret": "orig"})
        os.environ["ISO_HUB_SECRET_KEY"] = "wrong-key-B"
        with self.assertRaises(config_files.SettingsDecryptError):
            app.load_settings_all()
        with self.assertRaises(config_files.SettingsDecryptError):
            config_files.update_json(self.settings, {"secret": "tampered"})
        # 文件未被覆盖: 原密钥仍能读回
        os.environ["ISO_HUB_SECRET_KEY"] = "test-secret-key-A"
        self.assertEqual(app.load_settings_all(), {"secret": "orig"})
        # 也没生成 .corrupt(说明没被当成损坏重置)
        self.assertEqual(list(self.data.glob("*.corrupt*")), [])

    # 7) 读路径走解密: qb.url 能被正确读回(对应 app.py 那两处 read_text 改造)
    def test_qb_url_saved_reads_via_decrypt(self):
        app.save_settings_all({"qb": {"url": "http://qb:8080", "username": "u"}})
        self.assertEqual(
            app.load_settings_all().get("qb", {}).get("url"), "http://qb:8080")


if __name__ == "__main__":
    unittest.main()
