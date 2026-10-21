#!/usr/bin/env python3
"""URL 协议白名单测试 —— 单一真源 + 收紧后的边界。

背景: app.py 与 custom_repo_refresh.py 曾经各写一份 _is_http_url。同一个 URL
在网页上被接受、在后台刷新时被拒(或反过来)的现象极难排查, 所以这里除了测行为,
还要钉住"只有一份实现"这件事 —— 直接检查两个调用方里不再出现 urlparse。

收紧的动机: urlparse 太宽容, "https://exa mple\\n" 照样给出非空 netloc,
只看 scheme + netloc 就放行了。宽松本身不一定立刻出事, 但它让"什么算一个 URL"
这条边界不可预测 —— 浏览器 / requests / feedparser 对同一字符串的解读并不一致。
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import url_guard  # noqa: E402

APP_PY = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
CRR_PY = (REPO_ROOT / "web" / "custom_repo_refresh.py").read_text(encoding="utf-8")
DT_PY = (REPO_ROOT / "web" / "distro_torrents.py").read_text(encoding="utf-8")
# 曾经这里出现过三份各自复制的实现(app.py / custom_repo_refresh.py /
# distro_torrents.py)。新增一处 URL 判定前先想清楚: 能不能直接 import url_guard。
CALLERS = {"app.py": APP_PY, "custom_repo_refresh.py": CRR_PY,
           "distro_torrents.py": DT_PY}


class TestAccepts(unittest.TestCase):
    """合法输入必须放行 —— 收紧不能误伤正常用法。"""

    def test_plain_https(self):
        self.assertTrue(url_guard.is_http_url("https://example.com/iso/"))

    def test_plain_http(self):
        self.assertTrue(url_guard.is_http_url("http://example.com/iso/"))

    def test_lan_ip_with_port(self):
        """抓局域网镜像站是核心用途, 不能拦。"""
        self.assertTrue(url_guard.is_http_url("http://192.168.43.102:18083/"))

    def test_uppercase_scheme(self):
        self.assertTrue(url_guard.is_http_url("HTTPS://example.com/"))

    def test_query_and_fragment(self):
        self.assertTrue(url_guard.is_http_url("https://example.com/a?b=c#d"))

    def test_userinfo(self):
        self.assertTrue(url_guard.is_http_url("https://user:pw@example.com/"))

    def test_ipv6_host(self):
        self.assertTrue(url_guard.is_http_url("http://[::1]:8080/"))

    def test_trailing_slash_only_host(self):
        self.assertTrue(url_guard.is_http_url("https://example.com"))


class TestRejects(unittest.TestCase):
    """必须挡住的输入。"""

    def test_file_scheme(self):
        self.assertFalse(url_guard.is_http_url("file:///etc/passwd"))

    def test_ftp_scheme(self):
        self.assertFalse(url_guard.is_http_url("ftp://example.com/x"))

    def test_no_scheme(self):
        self.assertFalse(url_guard.is_http_url("example.com/iso/"))

    def test_empty_host(self):
        self.assertFalse(url_guard.is_http_url("https:///no-host"))

    def test_empty_string(self):
        self.assertFalse(url_guard.is_http_url(""))

    def test_non_string(self):
        for bad in (None, 123, ["https://x"], {"u": "https://x"}):
            self.assertFalse(url_guard.is_http_url(bad), repr(bad))

    def test_leading_or_trailing_whitespace(self):
        for bad in (" https://example.com", "https://example.com ",
                    "\thttps://example.com", "https://example.com\n"):
            self.assertFalse(url_guard.is_http_url(bad), repr(bad))

    def test_embedded_whitespace(self):
        """曾经的宽松点: urlparse 对它照样给出非空 netloc。"""
        for bad in ("https://exa mple", "https://exa\tmple",
                    "https://exa\nmple", "https://example.com/a b"):
            self.assertFalse(url_guard.is_http_url(bad), repr(bad))

    def test_control_characters(self):
        self.assertFalse(url_guard.is_http_url("https://example.com/\x00"))
        self.assertFalse(url_guard.is_http_url("https://example.com/\x1b[31m"))
        self.assertFalse(url_guard.is_http_url("https://example.com/\x7f"))

    def test_backslash(self):
        """浏览器当 '/', 别的客户端未必 —— 判定与请求必须一致, 直接禁掉。"""
        self.assertFalse(url_guard.is_http_url("https://example.com\\@evil.com/"))
        self.assertFalse(url_guard.is_http_url("https:\\\\example.com"))

    def test_bad_port(self):
        """urlparse(...).hostname 对非法端口会抛 ValueError, 必须降级为拒绝。"""
        self.assertFalse(url_guard.is_http_url("http://example.com:notaport/"))

    def test_garbage_does_not_raise(self):
        for bad in ("::::", "http://[::1", "%%%", "https://[", "\x00\x01"):
            self.assertIsInstance(url_guard.is_http_url(bad), bool, repr(bad))


class TestSingleSourceOfTruth(unittest.TestCase):
    """防线散落多份 = 迟早漂移。钉住只有一份实现。"""

    def test_url_guard_exists(self):
        self.assertTrue((REPO_ROOT / "web" / "url_guard.py").is_file())

    def test_no_caller_reimplements(self):
        """三个调用方都不许自己解析 URL —— 散落多份迟早漂移。"""
        for name, src in CALLERS.items():
            body = src.split("def _is_http_url")[1][:400]
            self.assertNotIn("urlparse", body, f"{name} 又在自己解析 URL 了")
            self.assertNotIn("netloc", body, f"{name} 又在自己解析 URL 了")
            self.assertIn("url_guard.is_http_url", body, f"{name} 没有转发到 url_guard")

    def test_callers_agree_with_guard(self):
        """行为层面再兜一道: 所有调用方对所有输入的判定必须与真源一致。"""
        import custom_repo_refresh as crr  # noqa: PLC0415
        import distro_torrents as dt  # noqa: PLC0415

        try:
            import app  # noqa: PLC0415
        except Exception:  # 环境缺依赖时跳过, 上面的源码断言仍生效
            app = None

        samples = ["https://example.com/", "http://192.168.1.1:8080/",
                   "file:///etc/passwd", "ftp://x/y", "example.com",
                   "https:///no-host", "", "https://exa mple",
                   "https://example.com\\x", "http://h:notaport", "::::"]
        impls = [crr._is_http_url, dt._is_http_url] + ([app._is_http_url]
                                                        if app else [])
        for s in samples:
            want = url_guard.is_http_url(s)
            for fn in impls:
                self.assertEqual(fn(s), want, f"{fn.__module__} 对 {s!r} 判定不一致")


if __name__ == "__main__":
    unittest.main()
