# -*- coding: utf-8 -*-
"""路由层与内核（app.py）之间的唯一通道。

为什么要这个文件
----------------
v1.7.0 把 54 条路由从 4245 行的 app.py 里按域搬进 web/api/。搬移不难，难的是
**不能破坏两种既有引用方式**：

1. 测试里有 340 处 `patch.object(app, "log")` / `patch.object(app, "DATA_DIR")`
   这类打桩。打桩生效的前提是：函数体里那次查找**读的必须是打桩的那个模块对象**。
   若路由模块写成 `from app import log`，日志函数在 import 那一刻就被固化进本模块
   全局，之后 `patch.object(app, "log")` 改的是 app 的全局，这里的副本纹丝不动
   —— 测试照样绿，但桩没打上，等于拆了守卫还看不出来。
2. app.py 里其余代码仍在用裸名字互相引用，app 的 `__main__` 分支也在用它们。

所以约定：**内核符号一律现查，不缓存、不 import 符号**。

为什么不能"直接找到 app 模块再 getattr"
---------------------------------------
同一个 app.py 在进程里可能有**两个模块对象**：

  * 生产：`CMD ["python", "app.py"]` —— app.py 以 `__main__` 身份运行，
    `sys.modules` 里没有 `app` 这个键。子模块若 `import app` 会再执行一遍，
    造出第二个模块对象，`task` / `_log_lines` / `_dl_tickets` / `_sessions`
    全部分叉（症状：任务状态永远查不到、日志不增长、下载完拿不到文件）。
  * 测试：一部分用例 `sys.path.insert(0, "web"); import app`，另一部分
    `import web.app as web_app` —— 这两个是不同的模块对象，各有各的 Flask 实例
    和全局变量。测试打桩打在其中之一上。

因此"猜哪个是内核"必然有一半场景猜错（本文件第一版就是这么翻车的：
`import web.app` 的用例打了 `web.app.JSON_FILE`，core 却解析到 `app`）。
正确做法是**让每个 Flask 实例自己携带自己的内核**：安装路由时把内核模块塞进
`app.extensions[KERNEL_KEY]`，请求期用 `current_app` 取回来 —— 请求由哪个
Flask 实例处理，就用哪个内核，不存在歧义。

用法::

    from api._ctx import core
    core.log(...)                    # 现查内核的 log —— patch.object(app,"log") 生效
    return jsonify(core.build_distros())

**禁止**把 `core.x` 的结果存成模块级变量（`LOG = core.log`），那样就成快照了，
打桩立刻失效。只能在使用点现取。

为什么用属性访问 `core.x` 而不是函数调用 `C("x")`
------------------------------------------------
`C("x")` 是早期写法，但它要在 f-string 里嵌套，而 f-string 的引号与 C() 的引号
很容易撞（`f"{C("X")}"` 在 3.12 之前直接语法错误，3.12 之后合法但没人想读）。
属性代理把引号问题整个消掉：`core.x` 一行不含任何引号。
"""
import sys

from flask import current_app, has_app_context

#: Flask 实例上存放内核模块的 extension 键。
KERNEL_KEY = "isohub_kernel"


def _discover():
    """无请求上下文时的兜底：按模块名找 app.py 对应的模块对象。

    只在安装期/脚本直调等场景走到这里；请求期一律走 current_app.extensions，
    因为那时才会出现"两个 app 模块对象"的歧义。
    """
    mod = sys.modules.get("__main__")
    f = getattr(mod, "__file__", "") or ""
    if f.endswith("app.py"):
        return mod
    mod = sys.modules.get("app")
    if mod is None:
        import app as mod  # noqa: PLC0415
    return mod


class _Kernel:
    """访问内核命名空间的活代理。

    每次属性读取都重新解析模块对象并取值 —— 不缓存模块、不缓存属性，因此
    运行期对内核 `X` 的任何重新绑定（测试打桩、热改配置）都立即可见。
    """

    __slots__ = ()

    def __getattr__(self, name):
        if has_app_context():
            mod = current_app.extensions.get(KERNEL_KEY)
            if mod is not None:
                return getattr(mod, name)
        return getattr(_discover(), name)

    def __repr__(self):
        return "<api._ctx.core -> 内核命名空间>"


core = _Kernel()
