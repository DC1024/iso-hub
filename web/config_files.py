#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settings.json 这一类**共享 JSON 配置文件**的唯一读写通道: 加锁 + 原子写 + 损坏隔离。

为什么要单独一个模块
--------------------
settings.json 一个文件里塞了: 共享凭据 / 用户 / 会话 / 受保护清单 / 调度 /
清单历史 / 选源策略 / 种子 RSS / 种子链接。写方是 waitress 的 8 个工作线程,
外加 1 个后台调度线程; 而且**两个模块各自实现了"读全量 → 合并 → 写全量"**:
  * app.py:            save_settings_all / save_shares / save_qb_settings
  * distro_torrents.py: _save_settings(add/remove 用户 RSS 与种子链接)

两条写线程交错时后写的覆盖先写的:

    A: read  -> {"users":..., "shares": v1}
    B:          read  -> {"users":..., "shares": v1}
    A: write {"users":..., "shares": v1, "protected": X}
    B:          write {"users":..., "shares": v1, "torrent_rss": Y}   <- X 没了

丢掉的是"改共享密码""加保护项"这类用户刚刚点过保存的东西, 而且**没有任何报错**。

所以要把锁放在两个模块都能 import 的位置 —— 放进 app.py 是不行的:
app.py 会 import distro_torrents, 反向引用会形成循环 import。

三条硬约束(都是本项目踩过的坑)
------------------------------
1. **必须是一把独立的锁, 绝不能复用 app._lock。** _lock 保护的是**内存状态**,
   历史上因为"锁内做磁盘 IO / 锁顺序"死锁过一次(app.py 的记载: waitress 线程
   耗尽、队列飙升、日志一条打不出来)。容器操作(start→rm→create→start 最长约 50 秒)
   一旦被包进锁里, 整个设置页会被串行化。
2. **必须是可重入锁(RLock)**: save_* 内部会再调 load_*。
3. **持锁期间不许再去拿别的锁** —— 尤其是 app.log(), 它内部要 app._lock。
   否则: 线程甲持 _lock 后再进 save_*(要 json 锁), 线程乙持 json 锁后又调 log()
   (要 _lock) —— 环路死锁。因此本模块**只做磁盘 IO**, 所有日志/告警一律由调用方
   在**释放锁之后**补(见 update_json 的 on_quarantine 回调)。

不在本模块处理的两件事
----------------------
* **读取路径不做备份。** 读取可能发生在任何请求线程里, 挪文件这种写副作用不可预测,
  并发读还会抢着备份。备份只在 update_json 真正要覆盖它之前做一次。
* **download_failures.json 的跨进程竞争。** 它的另一个写方是 iso_runner **子进程**
  (直接落盘), 和本进程不共享任何线程锁 —— 线程锁治不了。要修得引入 filelock 依赖
  (改 requirements + 镜像); 不修的最坏情况只是一次"下载失败/停止"标记错位,
  下次下载成功时 clear_failure 会把它清掉。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

__all__ = [
    "CorruptJsonFile",
    "file_lock",
    "read_json_raw",
    "write_json_atomic",
    "update_json",
]


class CorruptJsonFile(Exception):
    """文件内容不是合法 JSON 对象。

    属性:
      path   出问题的文件
      backup 已就地备份到的路径(读取路径不备份, 恒为 None)
    """

    def __init__(self, path, backup=None):
        super().__init__("%s 不是合法 JSON 对象" % path)
        self.path = Path(path)
        self.backup = backup


# key -> RLock。用 dict 而不是单把全局锁, 是为了将来别的 JSON 文件接入时
# 不会被 settings.json 的写入白白串行化(当前实际只有一条路径)。
_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def file_lock(path) -> threading.RLock:
    """返回 path 对应的可重入锁(同一路径字符串共用一把)。

    刻意用 str(path) 而不是 os.path.abspath(): 后者要读 cwd, 而容器里 DATA_DIR
    本来就是绝对路径; 测试把 SETTINGS_JSON 换成临时目录时也能自动得到独立分组。
    """
    key = str(path)
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _locks[key] = lk
        return lk


def read_json_raw(path) -> dict:
    """读取 JSON 对象: 文件不存在返回 {}; 内容非法抛 CorruptJsonFile(不挪动文件)。"""
    p = Path(path)
    if not p.exists():
        return {}
    raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError as e:            # JSONDecodeError 是它的子类
        raise CorruptJsonFile(p, None) from e
    if data is None:                   # 空内容 -> 当成空配置, 而不是损坏
        return {}
    if not isinstance(data, dict):
        raise CorruptJsonFile(p, None)
    return data


def write_json_atomic(path, data: dict) -> None:
    """原子写 JSON: 先落同目录临时文件, 再 os.replace 替换。

    读者永远只能看到"旧内容"或"完整的新内容", 不会读到被写了一半的 JSON
    (进程在 write_text 中途被 kill / 磁盘满, 都会留下半截文件 —— 那正是过去
    不得不靠 try/except 兜底的原因)。临时文件以 . 开头, 且在异常时清理,
    正常情况下不会残留在数据目录里。

    注意: 调用方应当已经持有 file_lock(path)。

    os.replace(重命名)在 Windows 上若目标文件正被别的句柄打开(并发读者 / 杀毒 /
    备份代理)会抛 WinError 5(拒绝访问); 这是瞬时的, 让出一点时间重试即可,
    不要让一次保存因为"刚好有人在读"就失败。Linux 上 rename 原子且不会触发此路径。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 名字里带 pid + 线程号: 即便将来有两个进程写同一文件, 也不会互相踩临时文件
    tmp = p.parent / (".%s.%d.%d.tmp" % (p.name, os.getpid(), threading.get_ident()))
    blob = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())     # 落盘后再替换, 避免掉电时拿到新内容的空壳
        # 重试吸收 Windows 上"目标被占用"的瞬时失败; 最终仍失败才上抛
        last_err = None
        for _attempt in range(5):
            try:
                os.replace(tmp, p)
                last_err = None
                break
            except OSError as e:
                last_err = e
                if _attempt < 4:
                    time.sleep(0.02)
        if last_err is not None:
            raise last_err
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _quarantine(path):
    """把损坏文件挪到 <name>.corrupt(已存在则加时间戳)。返回备份路径, 失败返回 None。"""
    p = Path(path)
    if not p.exists():
        return None
    primary = p.parent / (p.name + ".corrupt")
    target = primary if not primary.exists() else p.parent / (
        "%s.corrupt.%s" % (p.name, time.strftime("%Y%m%d%H%M%S")))
    try:
        p.replace(target)
        return target
    except OSError:
        return None


def update_json(path, patch, *, on_quarantine=None) -> dict:
    """在锁内完成 **读 → 合并 → 原子写**, 返回写入后的完整内容。

    patch 可以是 dict(走 dict.update), 也可以是 callable(dict) -> None
    (需要按旧值计算新值时用)。

    文件损坏时先备份到 <name>.corrupt 再以空配置继续, 随后**在释放 json 锁之后**
    回调 on_quarantine(backup_path) 让调用方去打日志 —— 回调里允许去拿别的锁,
    但正因为如此它也必须等到锁外才安全(见模块 docstring 第 3 条)。

    返回写入后的完整 dict; backup 路径只通过回调告知(保持签名简单)。
    """
    p = Path(path)
    with file_lock(p):
        try:
            cur = read_json_raw(p)
            backup = None
        except CorruptJsonFile:
            backup = _quarantine(p)
            cur = {}
        if callable(patch):
            patch(cur)
        else:
            cur.update(patch)
        write_json_atomic(p, cur)
    if backup is not None and on_quarantine is not None:
        on_quarantine(backup)
    return cur
