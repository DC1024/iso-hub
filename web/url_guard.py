#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ISO Hub - URL 协议白名单(SSRF 防线)的**唯一真源**。

为什么单独成一个模块:
  自定义源(listing_url)与 RSS / 手动链接都会把用户填的字符串直接喂给
  requests / feedparser 去发请求。这条判定如果散落多份, 迟早会漂移 —— 曾经就是
  app.py 与 custom_repo_refresh.py 各写一份, 改了一侧忘另一侧, 结果是"网页上看着
  加进去了、后台刷新时却被拒", 极难排查。现在两边都 import 这里。

收紧点(v1.5.5):
  urlparse 对脏数据相当宽容 —— "https://exa mple\\n" 照样能给出非空 netloc,
  只看 scheme + netloc 就会放行。宽松本身不一定立刻出事, 但它让"什么算一个 URL"
  这条边界变得不可预测: 浏览器、requests、feedparser 三家对同一个字符串的解读
  并不一致, 判定放行却请求失败(或被别的组件按另一种方式解析)就是漏洞温床。
  这里显式拒绝: 前后空白、内嵌空白与控制字符、反斜杠。

刻意不做的事:
  * 不拦内网 IP / localhost —— 本应用的核心用途就是抓局域网镜像站, 拦了等于自废武功。
  * 不解析 DNS、不做重定向跟随, 那是请求层的事。
"""
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")

# 反斜杠: 浏览器按 "/" 处理, 但并非所有 HTTP 客户端都这么干。
# 允许它等于让"同一个字符串在两处含义不同", 直接禁掉。
_FORBIDDEN_CHARS = "\\"


def is_http_url(url) -> bool:
    """仅接受 http/https 且能解析出 host 的字符串。

    非字符串、空串、含空白/控制字符/反斜杠、端口非法、host 为空一律 False。
    任何解析异常都降级为 False(拒绝优于放行)。
    """
    if not isinstance(url, str) or not url:
        return False
    if url != url.strip():
        return False
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    for ch in _FORBIDDEN_CHARS:
        if ch in url:
            return False
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme
        host = parsed.hostname or ""
        # 显式读一次 port: 非数字端口时它抛 ValueError(http://h:notaport)。
        # 惰性解析下不读它就不会报错, 于是这种畸形串会被当成合法 URL 放行,
        # 一路走到 requests 才炸, 且报错与本模块无关 —— 在边界上就拒掉。
        _port = parsed.port
    except ValueError:
        return False
    except Exception:  # noqa: BLE001 - 解析库的行为不该影响判定结果, 一律拒绝
        return False
    return scheme.lower() in ALLOWED_SCHEMES and host != ""
