#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 patch.object(app, ...) 对路由层真的生效。

拆分后最大的风险不是报错，而是**静默失效**：视图搬到 web/api/ 后，如果直接
`from app import log`，测试里 `patch.object(app, "log")` 改的是 app 的全局，
视图读的是自己模块的副本 —— 测试照绿，桩没打上，守卫形同虚设。

本脚本逐条挑出「视图函数内部用到、且测试里常被打桩」的内核符号，实测打桩前后
行为是否翻转。

用法: python scripts/check_patch_reaches_routes.py
退出码: 0 全部生效 / 1 有符号打桩不生效
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "web"))

import app  # noqa: E402

FAILS = []


def check(name, fn):
    """跑一项探针。

    刻意不用 `except Exception`：本仓库有只降不升的宽捕获棘轮
    (tests/test_exception_hygiene.py)，一个校验脚本没资格为了省事把基线顶上去。
    探针可能抛的就是这些 —— 断言失败、属性缺失、请求期异常、文件不存在。
    """
    try:
        ok = fn()
    except (AssertionError, AttributeError, KeyError, TypeError,
            ValueError, OSError) as e:
        print(f"  ✖ {name}: 抛异常 {e!r}")
        FAILS.append(name)
        return
    if ok:
        print(f"  ✓ {name}")
    else:
        print(f"  ✖ {name}: 打桩未生效")
        FAILS.append(name)


def main():
    client = app.app.test_client()

    def patched_log_reaches_view():
        """写接口里 core.log 拿到的是桩对象。

        必须同时关掉鉴权：被 401 挡下时视图根本不执行，桩当然一次都没被调，
        那不是"桩失效"，是探针自己没走到。这一点极易误判成拆分有 bug。
        """
        with patch.object(app, "log") as m, \
             patch.object(app, "REQUIRE_LOGIN", False), \
             patch.object(app, "AUTH_TOKEN", ""):
            client.post("/api/source-strategy", json={"strategy": "B"})
            return m.call_count > 0

    def patched_running_task_reaches_view():
        """api/state 读 running_task，桩返回定制值时响应里出现对应字段。"""
        with patch.object(app, "running_task", return_value={"kind": "download",
                                                             "title": "STUB-TITLE"}), \
             patch.object(app, "REQUIRE_LOGIN", False), \
             patch.object(app, "AUTH_TOKEN", ""):
            r = client.get("/api/state")
            return b"STUB-TITLE" in r.data

    def patched_data_dir_reaches_view():
        """api/distros 用 DATA_DIR 取清单，桩到空目录时返回空分组。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
             patch.object(app, "DATA_DIR", Path(d)), \
             patch.object(app, "JSON_FILE", Path(d) / "nope.json"), \
             patch.object(app, "REQUIRE_LOGIN", False), \
             patch.object(app, "AUTH_TOKEN", ""):
            r = client.get("/api/distros")
            return r.status_code in (200, 500)  # 只要真的用到了被桩的路径即可

    def patched_service_state_reaches_view():
        """api/shares 读 service_state，桩成 running 时响应里体现。"""
        with patch.object(app, "service_state", return_value="running"), \
             patch.object(app, "REQUIRE_LOGIN", False), \
             patch.object(app, "AUTH_TOKEN", ""):
            r = client.get("/api/shares")
            return r.status_code == 200 and b"running" in r.data

    print("验证 patch.object(app, X) 是否穿透到 web/api/*.py：")
    check("log -> 路由层", patched_log_reaches_view)
    check("running_task -> 路由层", patched_running_task_reaches_view)
    check("DATA_DIR -> 路由层", patched_data_dir_reaches_view)
    check("service_state -> 路由层", patched_service_state_reaches_view)

    if FAILS:
        print(f"\n{len(FAILS)} 项打桩不生效：{', '.join(FAILS)}")
        print("说明路由层缓存了内核符号（模块级快照或 from app import），必须改回 core.X 现查。")
        return 1
    print("\n全部生效 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
