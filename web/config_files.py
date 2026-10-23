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
* **读取路径不加跨进程锁。** 写入走 tmp+replace, 读者要么看到旧内容要么看到完整新内容,
  加锁只会让高频读路径无谓排队。需要串行化的是"读-改-写", 见 cross_process_lock。

download_failures.json 的跨进程竞争(M3, 已修)
---------------------------------------------
它的另一个写方是 iso_runner **子进程**(直接落盘), 和本进程不共享任何线程锁 ——
threading.RLock 治不了。原先的结论是"要修得引入 filelock 依赖", 后来发现标准库
就够: POSIX 的 fcntl.flock / Windows 的 msvcrt.locking 包一层即可, 不必加依赖。
实现见 cross_process_lock(); 加锁失败一律降级为"只有进程内锁"(= 修复前的行为)。
"""
from __future__ import annotations

import base64  # noqa: F401  (保留: 将来若改 base64 封装便于迁移)
import contextlib
import hashlib
import json
import os
import threading
import time
from pathlib import Path

# 跨进程文件锁: POSIX 用 flock, Windows 用 msvcrt.locking。
# 只在对应平台导入, 另一个名字不会存在 —— 由 _os_lock_acquire 按 os.name 分支调用。
if os.name == "nt":                       # pragma: no cover - 平台分支
    import msvcrt
else:                                     # pragma: no cover - 平台分支
    import fcntl

__all__ = [
    "CorruptJsonFile",
    "SettingsDecryptError",
    "file_lock",
    "read_json_raw",
    "write_json_atomic",
    "update_json",
]


# --------------------------------------------------------------------------- 加密落盘 (A+B)
# settings.json 里含共享凭据(samba/webdav/qb 明文) + 用户/会话/调度/保护清单等敏感数据。
# 设了 ISO_HUB_SECRET_KEY(原始密钥) 或 ISO_HUB_SECRET_KEY_FILE(密钥文件路径) 时, 对
# settings.json 整文件做 AES-GCM 加密后再落盘; 未设则保持明文(向后兼容, 现有部署零变化)。
# 密钥只用于"磁盘加解密", 与登录认证(PBKDF2 哈希) 完全无关, 终端用户永不接触。
_ENC_MAGIC = b"ISOHUB-SETTINGS-ENC-v1\n"


def _resolve_secret_key():
    raw = os.environ.get("ISO_HUB_SECRET_KEY")
    if raw:
        return raw.encode("utf-8")
    fp = os.environ.get("ISO_HUB_SECRET_KEY_FILE")
    if fp and os.path.isfile(fp):
        try:
            return open(fp, "rb").read().strip()   # 密钥文件可能是二进制
        except OSError:
            return None
    return None


def _is_settings(path):
    return os.path.basename(str(path)) == "settings.json"


def _derive_aes_key(secret: bytes) -> bytes:
    # 静态密钥 -> 32 字节 AES-256 密钥(对静态 at-rest 密钥足够; 非每次会话派生)
    return hashlib.sha256(secret).digest()


class SettingsDecryptError(Exception):
    """解密失败(密钥不匹配 / 文件被篡改)。

    调用方**绝不能**把它当"损坏文件 -> 重置为空配置"处理, 否则会覆盖一份其实是
    有效密文、只是密钥不对的文件, 造成不可逆的数据丢失。
    """


def _aesgcm(secret):
    # 懒导入: 没装 cryptography 且未设密钥的环境(如未装依赖的测试)不会因 import 而崩
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(_derive_aes_key(secret))


def _encrypt_settings(plaintext: bytes) -> bytes:
    secret = _resolve_secret_key()
    if secret is None:
        return plaintext
    nonce = os.urandom(12)
    ct = _aesgcm(secret).encrypt(nonce, plaintext, None)
    return _ENC_MAGIC + nonce + ct


def _decrypt_settings(raw: bytes) -> bytes:
    secret = _resolve_secret_key()
    if secret is None:
        return raw
    if not raw.startswith(_ENC_MAGIC):
        # 明文回退: 文件早于加密存在, 或刚启用加密但还没重写过 -> 当明文读
        return raw
    body = raw[len(_ENC_MAGIC):]
    if len(body) <= 12:
        raise SettingsDecryptError("settings.json 密文过短")
    nonce, ct = body[:12], body[12:]
    # 懒导入同 _aesgcm: 仅在已配置密钥(必然已装 cryptography)时才会走到这里
    from cryptography.exceptions import InvalidTag  # noqa: PLC0415
    try:
        return _aesgcm(secret).decrypt(nonce, ct, None)
    except (InvalidTag, OverflowError, ValueError) as e:  # cryptography 对错误密钥/篡改抛 InvalidTag
        raise SettingsDecryptError(
            "settings.json 解密失败(密钥不匹配或文件被篡改)") from e


def _chmod_600(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass   # 权限不足(如某些只读挂载)不致命, 加密本身已提供主要保护


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

    **这把锁只管本进程内的线程**。要挡住另一个进程(下载 runner 子进程、
    sync_subscriptions 脚本、独立 CLI)必须用 cross_process_lock()。
    """
    key = str(path)
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _locks[key] = lk
        return lk


# --------------------------------------------------------------------------- 跨进程锁
# 为什么还需要另一把锁(M3)
# --------------------------
# download_failures.json / download_unverified.json 是**多个进程共写**的账本:
#   * web 进程(waitress 线程)         —— 下载结束后补记录 / 清理记录
#   * iso_runner.py 子进程(可多个并行) —— 每个文件下载完立刻写一条
#   * sync_subscriptions.py 子进程      —— 同步订阅时写失败记录
# 三者的写法都是 "读全量 → 改一条 → tmp+replace 写全量"。原子写只保证"没有半截
# 文件", **不保证不丢更新**: A 读到 {x}, B 读到 {x}, A 写 {x,a}, B 写 {x,b} —— a 没了。
# threading.RLock 对此完全无效(它连另一个进程的边都碰不到)。
#
# 代价与降级
# ----------
# 临界区就是读一个小 JSON + 写一个小 JSON(毫秒级), 加一次 flock 的开销可以忽略。
# 但**绝不能因为锁失败就让下载流程失败**:
#   * 拿不到锁(超过 timeout) → 照旧往下走, 事后回调 on_degraded 让调用方记日志;
#   * 建不了锁文件 / 文件系统不支持 flock(NFS、某些 FUSE) → 同上。
# 也就是说"加锁失败"退回的是**修复前的行为**, 不会比现在更差。
_LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT = 5.0


def lock_file_for(path) -> Path:
    """与 path 配套的锁文件路径: 同目录下 `<name>.lock`。

    单独一个文件而不是锁数据文件本身: 数据文件会被 os.replace 换掉, inode 变了,
    锁就跟着丢了 —— 两边锁的其实是不同 inode, 等于没锁。
    """
    p = Path(path)
    return p.parent / (p.name + _LOCK_SUFFIX)


def _oserr(e: OSError) -> str:
    """OSError 的可读描述。Windows 上把 winerror 也带上 —— errno 13 既可能是
    "拒绝访问"(5) 也可能是"共享冲突"(32), 两者的处置完全不同, 不带就分不清。"""
    w = getattr(e, "winerror", None)
    return "%s%s" % (e, (" (winerror=%s)" % w) if w else "")


def _top_up_if_empty(lp: Path) -> None:
    """Windows: 把历史遗留的 0 字节锁文件补到 1 个字节。

    只补字节, **不删不换**: 一旦删/换, 别人此刻持有的那把锁就成了孤儿锁
    (句柄指着旧 inode, 而新来的人锁新 inode, 两边都以为自己独占)。
    """
    if os.name != "nt":
        return
    try:
        if lp.stat().st_size:
            return
    except OSError:
        return
    with contextlib.suppress(OSError):
        with open(lp, "r+b") as f:
            f.write(b"\0")


def _seed_lock_file(lp: Path) -> None:
    """创建锁文件(1 字节), 且**绝不改变已存在文件的 inode**。

    为什么非要 1 个字节: Windows 的字节范围锁对"文件末尾之外"的区域不生效 ——
    对 0 字节文件加锁会返回成功, 而另一个进程同样能加锁成功, 等于没锁
    (实测: 0 字节时两个进程都拿到锁; 1 字节时第二个被 errno 13 挡住)。

    为什么不用 os.replace(临时文件, lp) —— v1.8.0 就是这么写的, 错在这里:
      replace 会把 lp 指向**新 inode**。两个进程同时判定"文件不存在"时各 replace
      一次, 后到者把先到者的 inode 从目录里摘掉; 而先到者已经 open 的句柄仍指着
      旧 inode —— 于是两个进程 flock 的是两个不同 inode, 两个都返回"加锁成功",
      锁形同虚设。Windows 上碰巧安全(对已打开的文件 replace 抛 PermissionError),
      Linux 允许替换, 所以在 CI 上表现为 50 条并发写丢 1 条, 且**没有任何降级日志**
      (两边都认为自己拿到了锁)。

    为什么用 os.link(临时文件, lp):
      * 目标已存在时**原子失败**(FileExistsError), 不会覆盖别人的 inode —— 锁文件
        的 inode 从诞生到删除只出现一次, 谁来 open 打开的都是同一个;
      * 链接建立时文件已经带着那 1 个字节, 不存在"文件存在但 0 字节"的窗口
        (那正是上一版踩过的坑: 两个进程抢着写同一个字节, 后写的被强制锁挡成
        EACCES → 静默降级 → 丢更新)。
    """
    tmp = lp.parent / (".%s.%d.seed" % (lp.name, os.getpid()))
    try:
        with open(tmp, "wb") as f:
            f.write(b"\0")
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())      # 别让崩溃在盘上留下 0 字节锁文件
        try:
            os.link(tmp, lp)
        except FileExistsError:
            pass                          # 别人先建好了, 是同一个 inode, 直接用
        except OSError:
            _seed_lock_file_exclusive(lp)  # 不支持硬链接的文件系统(FAT/exFAT/部分 NFS)
    except OSError:
        pass                              # 建不出来 → 调用方走降级分支, 不失败
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _seed_lock_file_exclusive(lp: Path) -> None:
    """硬链接不可用时的退路: O_CREAT|O_EXCL, 只有一个人能建成。

    POSIX 上这就够了(flock 不看文件大小); Windows 上仍留一个极短的 0 字节窗口,
    但只会在"既没有硬链接、又跑 Windows"的组合里走到 —— FAT/exFAT 之类。
    """
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:                       # 含 FileExistsError: 别人建好了
        return
    try:
        os.write(fd, b"\0")
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _ensure_lock_file(lp: Path) -> None:
    """保证锁文件存在(且在 Windows 上至少 1 字节)。创建规则见 _seed_lock_file。"""
    with contextlib.suppress(OSError):
        lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        if lp.exists():
            _top_up_if_empty(lp)          # POSIX 的 flock 不看大小; Windows 才需要补
            return
    except OSError:
        pass
    _seed_lock_file(lp)


def _os_lock_once(fh) -> bool:
    """对已打开的锁文件做**一次**非阻塞加锁尝试; 拿不到返回 False。"""
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _open_and_lock(lp: Path, timeout: float):
    """在超时前反复尝试"打开锁文件 + 加锁", 返回 (fh, 是否拿到, 失败原因)。

    为什么**打开失败也要重试**: 锁文件刚诞生的那一瞬间, 这边的 open 可能拿到
    EACCES / 共享冲突 —— 那是毫秒级的瞬时状态。把它直接判成"没有跨进程锁"就会
    丢更新(实测: 50 条并发写里稳定丢 1 条, 全部发生在锁文件刚诞生那几次)。
    重试 50ms 之后再来一次就好。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    last = "超时"
    while True:
        fh = None
        try:
            _ensure_lock_file(lp)
            fh = open(lp, "r+b")           # 不用 "a+b": 追加写会撞上别人的强制锁
        except OSError as e:
            last = "无法打开锁文件 %s: %s" % (lp, _oserr(e))
        else:
            if _os_lock_once(fh):
                return fh, True, ""
            last = "等待跨进程锁 %s 超过 %.1fs" % (lp, timeout)
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.close()
        if time.monotonic() >= deadline:
            return None, False, last
        time.sleep(0.05)


def _os_lock_release(fh) -> None:
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass   # 关闭句柄时内核也会释放, 这里失败不影响正确性


# 同线程重入计数: 键是锁文件路径。
# 为什么必须自己数: flock 是"按打开的文件描述"加锁的, 同一进程再 open 一次拿到的
# 是另一个描述, 会**自己把自己锁住**(直接死等)。所以重入只能靠线程本地计数短路。
_cp_held = threading.local()


def _cp_held_map() -> dict:
    m = getattr(_cp_held, "map", None)
    if m is None:
        m = {}
        _cp_held.map = m
    return m


@contextlib.contextmanager
def cross_process_lock(path, timeout: float = LOCK_TIMEOUT, on_degraded=None):
    """对 path 加**跨进程**排他锁(同时含进程内线程锁)。用法::

        with config_files.cross_process_lock(jf):
            data = ...读...
            write_json_atomic(jf, data)

    可重入: 同一线程嵌套进入不会死锁(内层直接短路), 进程内不同线程按 file_lock 排队。

    on_degraded(reason: str) 在**锁全部释放之后**调用, 且只在这一种情况下调用:
    没能拿到跨进程锁, 于是退化为"只有进程内锁"。之所以放到锁外, 是本模块的硬约束
    3 —— 回调里可能去拿 app._lock 打日志, 持着 json 锁回调会构成环路死锁。
    不传回调则静默降级(等于修复前的行为)。
    """
    p = Path(path)
    lp = lock_file_for(p)
    held = _cp_held_map()
    key = str(lp)
    degraded = None

    try:
        with file_lock(p):                  # 先过进程内这一关(可重入)
            depth = held.get(key)
            if depth is not None:           # 同线程重入
                held[key] = depth + 1
                try:
                    yield
                finally:
                    held[key] -= 1
            else:
                fh, got, why = _open_and_lock(lp, timeout)
                if not got:
                    degraded = "%s, 本次仅用进程内锁" % why
                held[key] = 1
                try:
                    yield
                finally:
                    held.pop(key, None)
                    if fh is not None:
                        if got:
                            _os_lock_release(fh)
                        with contextlib.suppress(OSError):
                            fh.close()
    finally:
        # 必须在 file_lock 释放之后回调: 回调里可能去打日志(要 app._lock)
        if degraded and on_degraded is not None:
            on_degraded(degraded)


def read_json_raw(path) -> dict:
    """读取 JSON 对象: 文件不存在返回 {}; 内容非法抛 CorruptJsonFile(不挪动文件)。

    settings.json 在启用密钥时是 AES-GCM 密文, 这里统一以二进制读入再解密; 解密失败
    (密钥不对) 抛 SettingsDecryptError, 由调用方决定如何处置(绝不应静默当空配置)。
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = p.read_bytes()
    try:
        raw = _decrypt_settings(raw)
    except SettingsDecryptError:
        raise
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:   # JSONDecodeError 是 ValueError 子类
        raise CorruptJsonFile(p, None) from e
    if data is None:                   # 空内容 -> 当成空配置, 而不是损坏
        return {}
    if not isinstance(data, dict):
        raise CorruptJsonFile(p, None)
    return data


def write_json_atomic(path, data: dict | list) -> None:
    """原子写 JSON: 先落同目录临时文件, 再 os.replace 替换。

    读者永远只能看到"旧内容"或"完整的新内容", 不会读到被写了一半的 JSON
    (进程在 write_text 中途被 kill / 磁盘满, 都会留下半截文件 —— 那正是过去
    不得不靠 try/except 兜底的原因)。临时文件以 . 开头, 且在异常时清理,
    正常情况下不会残留在数据目录里。

    data 允许 dict 也允许 list: custom_sources.json / subscriptions.json 顶层
    就是数组(M2)。这两个文件**不能**走 update_json —— 那个函数为 dict 设计,
    遇到 list 会抛 CorruptJsonFile 并把文件当损坏件隔离。

    settings.json 在启用密钥时先整文件 AES-GCM 加密再落盘; 写完后 chmod 600(档位 A),
    收窄"能读 data 卷的进程/用户"范围(对 root 容器只能挡非 root 读者, 但仍有意义)。

    注意: 调用方应当已经持有 file_lock(path)。

    os.replace(重命名)在 Windows 上若目标文件正被别的句柄打开(并发读者 / 杀毒 /
    备份代理)会抛 WinError 5(拒绝访问); 这是瞬时的, 让出一点时间重试即可,
    不要让一次保存因为"刚好有人在读"就失败。Linux 上 rename 原子且不会触发此路径。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    out = _encrypt_settings(blob) if _is_settings(p) else blob
    # 名字里带 pid + 线程号: 即便将来有两个进程写同一文件, 也不会互相踩临时文件
    tmp = p.parent / (".%s.%d.%d.tmp" % (p.name, os.getpid(), threading.get_ident()))
    try:
        with open(tmp, "wb") as fh:
            fh.write(out)
            fh.flush()
            os.fsync(fh.fileno())     # 落盘后再替换, 避免掉电时拿到新内容的空壳
        if _is_settings(p):
            _chmod_600(tmp)
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
        if _is_settings(p):
            _chmod_600(p)
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
        if _is_settings(p):
            _chmod_600(target)   # 备份同样含凭据, 一并锁权限
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
        except SettingsDecryptError:
            # 密钥不对/文件被篡改: 绝不静默重置为空配置(否则会覆盖有效密文, 数据全丢)
            raise
        if callable(patch):
            patch(cur)
        else:
            cur.update(patch)
        write_json_atomic(p, cur)
    if backup is not None and on_quarantine is not None:
        on_quarantine(backup)
    return cur
