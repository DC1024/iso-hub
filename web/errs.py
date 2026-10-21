"""统一的可预期异常组合，用于替代散落的 `except Exception`。

约定（v1.6.0 起）：
- 错误类型可枚举的调用点（JSON 读写、Docker API、requests 网络调用、qB API）
  一律使用本模块的具名元组收窄捕获面；
- 真正的边界（请求 500 兜底、后台守护循环、尽力而为的清理、可选模块导入）
  允许保留 `except Exception`，但注释必须写明"边界"理由；
- 全仓库 `except Exception` 的数量与形态由 tests/test_exception_hygiene.py 守门，
  只许减少、不许无理由新增。
"""
from __future__ import annotations

import http.client

import requests

# JSON 文件读取：磁盘错误 + 解码/解析错误。
# json.JSONDecodeError 与 UnicodeDecodeError 均为 ValueError 子类。
JSON_READ = (OSError, ValueError)

# JSON 原子写：磁盘错误 + 序列化错误（不可序列化对象 -> TypeError/ValueError）。
# 同时覆盖"先读后写"场景（OSError/ValueError 与读相同）。
JSON_WRITE = (OSError, TypeError, ValueError)

# Docker Engine API（http.client 走 unix socket）+ 响应体 JSON 解析。
DOCKER_API = (OSError, http.client.HTTPException, ValueError)

# requests 网络调用（HEAD/GET 探测、下载、RSS 抓取）+ 响应解析。
HTTP_REQ = (requests.RequestException, ValueError)

# qBittorrent WebUI：requests 或 urllib 回退（urllib.error.URLError 是 OSError 子类）
# + 响应体 JSON 解析。
QB_API = (OSError, requests.RequestException, ValueError)
