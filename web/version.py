# -*- coding: utf-8 -*-
"""ISO Hub 的**唯一版本源**。

为什么单独一个文件:
  版本号此前只存在于 web/static/index.html 的 `APP_VERSION` 常量里 —— 一个
  纯前端字符串, 没人跟 git tag 对齐。结果 v1.8.2 的镜像里, 页面徽标还写着
  1.7.0, 用户根本无法从界面上确认自己跑的是哪个版本(线上功能测试 P9)。

  现在后端的 `__version__` 是唯一真源:
    * GET /api/health 返回 {"version": ...}
    * GET / 的响应头带 X-Version
    * 前端徽标启动后以 /api/health 的值为准(静态常量只做首屏兜底)
    * tests/test_version_consistency.py 锁死三者一致

发版流程: 改这里 → 同步 index.html 的兜底常量(测试会盯着)→ 打 git tag。
tag 与 __version__ 必须一致, 否则镜像里的版本和 tag 对不上。
"""

__version__ = "1.8.3"
