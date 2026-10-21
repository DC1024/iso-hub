#!/usr/bin/env python3
"""自定义源展开 runner 测试 —— 此前全项目唯一零测试覆盖的模块(136 行)。

/app.py 的请求线程只读取 custom_repo_cache.json, 展开动作全靠这个子进程完成。
它一旦出错的表现是"自定义源凭空消失"或"列表停在旧结果", 而 Web 侧没有任何
报错, 所以行为必须由测试钉住:

  1. cache_key 与 app.py 的 _custom_repo_cache_key 必须逐字节一致 ——
     两边各写了一份实现, 一旦漂移, runner 写进去的键 app 永远读不命中,
     现象是刷新成功但列表不变, 极难排查。
  2. listing_url 协议白名单是 SSRF 防线之一, 必须挡住 file:// 等。
  3. 刷新失败(网络异常 / 展开为空 / 协议非法)一律保留旧缓存 ——
     宁可展示旧结果, 也不能把用户已看到的列表清空。
  4. 缓存写入必须原子, 且部分失败要以退出码 1 告知调用方。
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import custom_repo_refresh as crr  # noqa: E402

SRC = {
    "strategy": "flat_listing",
    "distribution": "MyDistro",
    "type": "linux",
    "listing_url": "https://example.com/iso/",
    "timeout": 30,
}
ENTRY_A = {"download_url": "https://example.com/iso/a.iso", "filename": "a.iso"}


class TestCacheKey(unittest.TestCase):
    """runner 与 app.py 各写了一份 cache_key, 必须永远算出同一个字符串。"""

    def test_timeout_is_excluded(self):
        """timeout 只影响抓取耗时, 不影响结果, 改它不该导致缓存失效。"""
        self.assertEqual(crr.cache_key(SRC), crr.cache_key({**SRC, "timeout": 120}))

    def test_other_fields_matter(self):
        self.assertNotEqual(crr.cache_key(SRC), crr.cache_key({**SRC, "type": "bsd"}))

    def test_key_order_irrelevant(self):
        a = {"strategy": "static", "distribution": "D", "timeout": 5}
        b = {"timeout": 5, "distribution": "D", "strategy": "static"}
        self.assertEqual(crr.cache_key(a), crr.cache_key(b), "sort_keys 必须生效")

    def test_matches_app_implementation(self):
        """核心回归: 两份实现漂移 = 刷新成功但列表永远不变。"""
        import web.app as web_app
        samples = [
            SRC,
            {**SRC, "timeout": 1},
            {"strategy": "static", "entries": [{"download_url": "https://x/y.iso"}]},
            {"strategy": "dated_directory", "distribution": "Ubuntu", "type": "linux",
             "listing_url": "https://m/ubuntu/", "timeout": 30},
        ]
        for item in samples:
            self.assertEqual(crr.cache_key(item), web_app._custom_repo_cache_key(item),
                             "cache_key 与 app.py 不一致会导致缓存永不命中")

    def test_default_str_for_unserializable(self):
        """ensure_ascii=False + default=str: 中文与非常规类型都不能抛异常。"""
        out = crr.cache_key({"strategy": "static", "distribution": "深度", "n": 1})
        self.assertIn("深度", out)


class TestUrlSchemeWhitelist(unittest.TestCase):
    """listing_url 协议白名单 —— app.py 之外的第二道 SSRF 防线。"""

    def test_http_and_https_allowed(self):
        self.assertTrue(crr._is_http_url("https://example.com/iso/"))
        self.assertTrue(crr._is_http_url("http://192.168.1.10:8080/"))

    def test_file_scheme_rejected(self):
        self.assertFalse(crr._is_http_url("file:///etc/passwd"),
                         "file:// 必须挡住, 否则展开会去读宿主本地文件")

    def test_other_schemes_rejected(self):
        for u in ("ftp://h/x", "smb://h/x", "//host/share", "example.com/x", ""):
            self.assertFalse(crr._is_http_url(u), f"{u!r} 不该被当作合法 listing_url")

    def test_empty_netloc_rejected(self):
        self.assertFalse(crr._is_http_url("https:///no-host"))

    def test_malformed_input_does_not_raise(self):
        """非法 URL 必须降级为 bool, 绝不能抛异常炸掉整个刷新。

        注意这里是"不抛"而非"判 False": urlparse 对含空格/换行的输入相当宽松,
        netloc 非空就放行。与 app.py 的同名实现保持同一宽松度 —— 收紧其中一侧
        反而会造成两侧判定不一致。真正的网络层校验交给 requests。
        """
        self.assertIsInstance(crr._is_http_url("https://exa mple\n"), bool)
        self.assertIsInstance(crr._is_http_url("::::"), bool)


class _RunnerCase(unittest.TestCase):
    """把 main() 当黑盒跑: 塞 custom_sources.json + 桩掉 expand_source。"""

    def _invoke(self, d, sources, expand=None, existing=None, custom_raw=None):
        custom_path = Path(d) / "custom_sources.json"
        if custom_raw is not None:
            custom_path.write_text(custom_raw, encoding="utf-8")
        else:
            custom_path.write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
        cache_path = Path(d) / "custom_repo_cache.json"
        if existing is not None:
            cache_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
        argv = ["custom_repo_refresh.py", "--custom-json", str(custom_path),
                "--cache-json", str(cache_path),
                "--repo-dir", str(REPO_ROOT / "iso_download")]
        out, err = io.StringIO(), io.StringIO()
        exc = None
        with patch.object(sys, "argv", argv), \
             patch.object(crr, "expand_source", expand or _default_expand), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                crr.main()
            except SystemExit as e:          # main 用 sys.exit(1) 报告部分失败
                exc = e
        return cache_path, out.getvalue(), err.getvalue(), exc

    def _cache(self, cache_path) -> dict:
        if not cache_path.exists():
            return {}
        return json.loads(cache_path.read_text(encoding="utf-8"))


def _default_expand(item):
    return [dict(ENTRY_A)]


class TestRefreshBehaviour(_RunnerCase):

    def test_successful_expand_writes_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache_path, out, _err, exc = self._invoke(d, [SRC])
            self.assertIsNone(exc, "全部成功时不得非零退出")
            cache = self._cache(cache_path)
            key = crr.cache_key(SRC)
            self.assertIn(key, cache)
            self.assertEqual(cache[key]["entries"], [ENTRY_A])
            self.assertIsInstance(cache[key]["updated_at"], int)
            self.assertIn("1/1", out)

    def test_no_strategy_sources_writes_nothing(self):
        """无 strategy 源时直接返回, 连缓存文件都不该被创建。"""
        with tempfile.TemporaryDirectory() as d:
            cache_path, out, _err, exc = self._invoke(d, [{"distribution": "X"}])
            self.assertIsNone(exc)
            self.assertFalse(cache_path.exists(), "没有可展开的源就不该动缓存文件")
            self.assertIn("无需展开", out)

    def test_corrupt_custom_json_degrades(self):
        with tempfile.TemporaryDirectory() as d:
            cache_path, _out, err, exc = self._invoke(d, [], custom_raw="{corrupt!")
            self.assertIsNone(exc, "配置损坏不得让 runner 崩掉")
            self.assertIn("WARN", err)
            self.assertFalse(cache_path.exists())

    def test_unknown_strategy_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            cache_path, _out, _err, _exc = self._invoke(d, [{**SRC, "strategy": "nope"}])
            self.assertEqual(self._cache(cache_path), {})

    def test_non_http_listing_preserves_old_cache(self):
        """协议非法: 跳过该源, 但旧结果必须留在缓存里。"""
        with tempfile.TemporaryDirectory() as d:
            bad = {**SRC, "listing_url": "file:///etc"}
            key = crr.cache_key(bad)
            cache_path, _out, err, exc = self._invoke(
                d, [bad], existing={key: {"entries": [ENTRY_A], "updated_at": 1}})
            self.assertIsNotNone(exc, "源被跳过 = 该源没刷成, 仍须以退出码 1 告知调用方")
            self.assertEqual(exc.code, 1)
            self.assertIn("拒绝非 http/https", err)
            self.assertEqual(self._cache(cache_path)[key]["entries"], [ENTRY_A],
                             "刷新失败时旧缓存必须保留, 否则列表会被清空")

    def test_expand_failure_preserves_old_cache(self):
        def boom(_item):
            raise RuntimeError("connection reset")

        with tempfile.TemporaryDirectory() as d:
            key = crr.cache_key(SRC)
            cache_path, _out, err, exc = self._invoke(
                d, [SRC], expand=boom,
                existing={key: {"entries": [ENTRY_A], "updated_at": 1}})
            self.assertIsNotNone(exc, "有源失败必须退出码 1, 让调用方知道没刷全")
            self.assertEqual(exc.code, 1)
            self.assertIn("展开失败", err)
            self.assertEqual(self._cache(cache_path)[key]["entries"], [ENTRY_A])

    def test_empty_expand_preserves_old_cache(self):
        """展开为空(镜像改版/目录暂时空)不该把已有列表抹掉。"""
        with tempfile.TemporaryDirectory() as d:
            key = crr.cache_key(SRC)
            cache_path, _out, err, exc = self._invoke(
                d, [SRC], expand=lambda _i: [],
                existing={key: {"entries": [ENTRY_A], "updated_at": 1}})
            self.assertIsNotNone(exc)
            self.assertIn("展开为空", err)
            self.assertEqual(self._cache(cache_path)[key]["entries"], [ENTRY_A])

    def test_partial_failure_exits_nonzero(self):
        """2 个源中 1 个失败 -> 成功的那个要写入, 同时以退出码 1 报警。"""
        calls = []

        def flaky(item):
            calls.append(item)
            if len(calls) == 1:
                return [dict(ENTRY_A)]
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as d:
            src2 = {**SRC, "distribution": "Other"}
            cache_path, out, _err, exc = self._invoke(d, [SRC, src2], expand=flaky)
            self.assertIsNotNone(exc)
            self.assertEqual(exc.code, 1)
            self.assertIn("1/2", out)
            cache = self._cache(cache_path)
            self.assertIn(crr.cache_key(SRC), cache, "成功的源仍须写入")

    def test_atomic_write_leaves_no_tmp(self):
        """原子写的证据: 运行结束后不得残留 .tmp 文件。"""
        with tempfile.TemporaryDirectory() as d:
            cache_path, _out, _err, _exc = self._invoke(d, [SRC])
            leftovers = list(cache_path.parent.glob("*.tmp"))
            self.assertEqual(leftovers, [], "残留 .tmp 说明又退回裸 write_text 了")

    def test_corrupt_existing_cache_is_ignored(self):
        """旧缓存文件损坏: 忽略重来, 不能让一次刷新永久失败。"""
        with tempfile.TemporaryDirectory() as d:
            self._invoke(d, [SRC])                       # 先产生一份正常缓存
            cache_path = Path(d) / "custom_repo_cache.json"
            cache_path.write_text("not json", encoding="utf-8")
            cache_path, _out, err, exc = self._invoke(d, [SRC])
            self.assertIsNone(exc, "旧缓存损坏不得让刷新失败")
            self.assertIn("读取缓存文件失败", err)
            self.assertIn(crr.cache_key(SRC), self._cache(cache_path))


if __name__ == "__main__":
    unittest.main()
