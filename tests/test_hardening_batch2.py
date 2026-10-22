#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 2 批加固的验收测试 —— 对应审查报告 M2 / H3。

M2 的两层含义, 必须分开验:
  ① **原子性**: 进程在写的中途死掉, 盘上要么是旧内容, 要么是完整新内容,
     绝不能是半截 JSON(半截会被 load 解析失败吞成 [], 用户数据静默全丢)。
  ② **不丢更新**: 两个并发请求各自 load→改→save, 后写的会把先写的覆盖掉。
     只把 save 换成原子写**治不了这一层** —— 锁的范围必须覆盖整个读-改-写。

H3: 清空订阅是合法操作, 但必须是**显式**的 {"subscriptions": []}。
空 body / 字段拼错 / 空对象一律 400, 不能静默清空。
"""

import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

if "tqdm" not in sys.modules:
    try:
        import tqdm  # noqa: F401
    except ImportError:
        _stub = types.ModuleType("tqdm")
        _stub.tqdm = lambda *a, **k: MagicMock()
        sys.modules["tqdm"] = _stub

import app  # noqa: E402
import config_files  # noqa: E402


class _Base(unittest.TestCase):
    """临时 DATA_DIR + 关掉登录门禁。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.custom = self.data / "custom_sources.json"
        self.subs = self.data / "subscriptions.json"
        self.settings = self.data / "settings.json"
        self.custom.write_text("[]", encoding="utf-8")
        self.subs.write_text("[]", encoding="utf-8")
        self.settings.write_text("{}", encoding="utf-8")

        for p in (patch.object(app, "DATA_DIR", self.data),
                  patch.object(app, "CUSTOM_JSON", self.custom),
                  patch.object(app, "SUBS_JSON", self.subs),
                  patch.object(app, "SETTINGS_JSON", self.settings),
                  patch.object(app, "REQUIRE_LOGIN", False),
                  patch.object(app, "AUTH_TOKEN", ""),
                  # 添加自定义源会真的起一个后台子进程去展开, 测试里必须挡住
                  patch.object(app, "refresh_custom_repo_cache", lambda: False)):
            p.start()
            self.addCleanup(p.stop)

    def _read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- M2

class TestM2AtomicWrite(_Base):
    """M2-①: 写入必须是原子的。"""

    def test_roundtrip(self):
        app.save_custom_sources([{"distribution": "A", "download_url": "https://a"}])
        self.assertEqual(self._read(self.custom)[0]["distribution"], "A")
        app.save_subscriptions([{"distribution": "B", "type": "linux"}])
        self.assertEqual(self._read(self.subs)[0]["distribution"], "B")

    def test_list_shape_is_preserved(self):
        """两个文件顶层都是数组 —— 不能被写成 {"items": [...]} 之类。"""
        app.save_custom_sources([{"distribution": "A"}])
        self.assertIsInstance(self._read(self.custom), list)

    def test_failed_replace_keeps_old_content(self):
        """替换失败时旧内容必须完好(不能留下半截或空文件)。"""
        app.save_custom_sources([{"distribution": "OLD"}])
        with patch.object(config_files.os, "replace",
                          side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                app.save_custom_sources([{"distribution": "NEW"}])
        self.assertEqual(self._read(self.custom), [{"distribution": "OLD"}])

    def test_no_temp_file_left_behind(self):
        """失败后不应在数据目录留下 .tmp 垃圾。"""
        with patch.object(config_files.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                app.save_custom_sources([{"distribution": "X"}])
        leftovers = [p.name for p in self.data.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestM2NoLostUpdate(_Base):
    """M2-②: 并发读-改-写不得互相覆盖。"""

    def test_concurrent_adds_all_persist(self):
        """20 个线程并发"添加自定义源" -> 一条都不能丢。

        没有整段加锁时, 这个用例会稳定失败: 线程们读到同一份旧列表,
        各自 append 后写回, 最终条目数远小于 20。
        """
        n = 20
        errors = []
        barrier = threading.Barrier(n)

        def worker(i):
            try:
                client = app.app.test_client()
                barrier.wait(timeout=10)  # 尽量让 20 个请求同时进入
                resp = client.post("/api/custom-sources", json={
                    "distribution": f"Dist{i}",
                    "type": "linux",
                    "download_url": f"https://example.com/{i}.iso",
                })
                if resp.status_code != 200:
                    errors.append((i, resp.status_code, resp.get_json()))
            except Exception as e:  # noqa: BLE001 - 线程内异常要带回主线程断言
                errors.append((i, repr(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], "并发请求不应报错")
        saved = self._read(self.custom)
        self.assertEqual(len(saved), n,
                         f"应保留 {n} 条, 实际 {len(saved)} 条 —— 发生了丢失更新")
        self.assertEqual({c["distribution"] for c in saved}, {f"Dist{i}" for i in range(n)})


# --------------------------------------------------------------------------- H3

class TestH3EmptyBodyMustNotClear(_Base):
    """H3: 只有显式空数组才清空。"""

    def setUp(self):
        super().setUp()
        app.save_subscriptions([{"distribution": "Keep", "type": "linux",
                                 "keep": 2, "enabled": True}])
        self.client = app.app.test_client()

    def _subs(self):
        return self._read(self.subs)

    def test_empty_body_rejected_and_data_kept(self):
        resp = self.client.post("/api/subscriptions", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(self._subs()), 1, "空对象不得清空订阅")

    def test_missing_field_rejected(self):
        resp = self.client.post("/api/subscriptions", json={"subs": []})
        self.assertEqual(resp.status_code, 400, "字段名拼错不得被当成清空")
        self.assertEqual(len(self._subs()), 1)

    def test_wrong_type_rejected(self):
        for bad in ("abc", 123, {"a": 1}, None):
            resp = self.client.post("/api/subscriptions", json={"subscriptions": bad})
            self.assertEqual(resp.status_code, 400, f"{bad!r} 应被拒绝")
        self.assertEqual(len(self._subs()), 1)

    def test_explicit_empty_array_still_clears(self):
        """清空是合法能力, 必须保留 —— 只是要显式。"""
        resp = self.client.post("/api/subscriptions", json={"subscriptions": []})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._subs(), [])

    def test_normal_save_works(self):
        resp = self.client.post("/api/subscriptions", json={"subscriptions": [
            {"distribution": "A", "type": "linux", "keep": 3, "enabled": True},
            {"distribution": "B", "type": "bsd", "keep": 1, "enabled": False},
        ]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([s["distribution"] for s in self._subs()], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
