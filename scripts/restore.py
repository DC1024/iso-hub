#!/usr/bin/env python3
"""ISO Hub 配置恢复。

配合 scripts/backup.py 使用。设计上刻意"保守":

  * **白名单校验**: 只接受 settings.json / distributions.json / gpg_ledger.json /
    download_unverified.json 四个文件名。归档里出现任何别的路径(含 `../` 穿越)
    直接拒绝, 不依赖 tarfile 的 filter 版本差异。
  * **先存旧再覆盖**: 解压前把当前文件复制到 <备份目录>/pre-restore-<时间戳>/,
    恢复错了还能退回去。备份目录与 backup.py 同一套规则($ISO_HUB_BACKUP_DIR >
    <data-dir> 同级的 backups/), 因此**不在** Samba/WebDAV 的共享树里(M8)。
  * **默认只预览**: 不给 --yes 就只列归档内容, 不动磁盘。恢复是破坏性操作,
    不该因为多敲一个回车就发生。

用法:
  docker exec iso-hub python /app/scripts/restore.py /backups/isohub-backup-xxx.tar.gz
  docker exec iso-hub python /app/scripts/restore.py <archive> --yes

退出码: 0 成功 / 2 参数或归档非法 / 3 写入失败
"""

import argparse
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import NoReturn

BACKUP_FILES = (
    "settings.json",
    "distributions.json",
    "gpg_ledger.json",
    "download_unverified.json",
)
PRE_PREFIX = "pre-restore-"


def _default_data_dir() -> Path:
    return Path(os.environ.get("ISO_DATA_DIR") or "/data")


def _default_backup_dir(data_dir: Path) -> Path:
    """与 scripts/backup.py、web/app.py 同一套规则(有测试盯着三者一致)。"""
    env = (os.environ.get("ISO_HUB_BACKUP_DIR") or "").strip()
    if env:
        return Path(env)
    return data_dir.parent / "backups"


def _die(msg: str) -> NoReturn:
    print("错误: " + msg, file=sys.stderr)
    raise SystemExit(2)


def read_members(archive: Path):
    """校验归档, 返回 [(TarInfo, 安全文件名)]。

    白名单里的名字都是单段文件名, 所以任何带 `/` 或 `\\` 的成员自然被挡在外面
    —— 包括 `../settings.json` 与绝对路径。
    """
    try:
        tar = tarfile.open(str(archive), "r:gz")
    except (OSError, tarfile.TarError) as e:
        _die("打不开归档 %s: %s" % (archive, e))

    with tar:
        out = []
        for m in tar.getmembers():
            name = m.name
            if name.startswith("./"):
                name = name[2:]
            if name not in BACKUP_FILES:
                _die("归档里有不在白名单的成员: %r" % m.name)
            if not m.isfile():
                _die("归档成员不是普通文件: %r" % m.name)
            if m.size > 64 * 1024 * 1024:
                _die("归档成员过大(>64MiB), 拒绝恢复: %r" % m.name)
            out.append((m, name))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="从备份归档恢复 ISO Hub 配置")
    ap.add_argument("archive", help="isohub-backup-*.tar.gz 路径")
    ap.add_argument("--data-dir", default=str(_default_data_dir()),
                    help="数据目录(默认 $ISO_DATA_DIR 或 /data)")
    ap.add_argument("--yes", action="store_true", help="确认执行(不加则只预览)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    archive = Path(args.archive)
    if not data_dir.is_dir():
        _die("数据目录不存在: %s" % data_dir)
    if not archive.is_file():
        _die("归档不存在: %s" % archive)

    members = read_members(archive)
    if not members:
        _die("归档是空的")

    print("[restore] 归档: %s" % archive)
    print("[restore] 目标: %s" % data_dir)
    print("[restore] 内容: %s" % ", ".join(n for _m, n in members))

    if not args.yes:
        print("[restore] 这是预览。确认无误后加 --yes 执行。")
        return 0

    # 先存旧: 恢复错了还能退回去
    pre = _default_backup_dir(data_dir) / (PRE_PREFIX + time.strftime("%Y%m%d-%H%M%S"))
    try:
        pre.mkdir(parents=True, exist_ok=True)
        pre.chmod(0o700)
    except OSError as e:
        # 存不下旧文件就不该覆盖 —— 这道保险本身就是"恢复错了还能退回去"的前提。
        # 常见原因: 宿主 ./backups 属主不是 10001(容器内改不了)。
        print("错误: 无法创建恢复前的快照目录 %s: %s" % (pre, e), file=sys.stderr)
        print("提示: 该目录由宿主机决定属主, 请执行 sudo chown -R 10001:10001 <备份目录>;"
              " 或用 ISO_HUB_BACKUP_DIR 指到可写位置。", file=sys.stderr)
        return 3
    saved = []
    for _m, name in members:
        src = data_dir / name
        if src.is_file():
            shutil.copy2(str(src), str(pre / name))
            saved.append(name)
    if saved:
        print("[restore] 当前文件已备份到 %s (%s)" % (pre, ", ".join(saved)))

    # 逐个 extractfile 写盘: 不调用 extractall, 因此与 tarfile 的 filter 版本差异无关
    try:
        with tarfile.open(str(archive), "r:gz") as tar:
            for m, name in members:
                src = tar.extractfile(m)
                if src is None:
                    _die("读不出成员内容: %r" % m.name)
                dst = data_dir / name
                tmp = data_dir / (name + ".restore.tmp")
                with open(str(tmp), "wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(str(tmp), str(dst))
                # settings.json 里有会话 token 与 SMTP 授权码
                try:
                    os.chmod(str(dst), 0o600)
                except OSError:
                    pass
                print("[restore] 已写入 %s" % dst)
    except (OSError, tarfile.TarError) as e:
        print("错误: 写入失败: %s" % e, file=sys.stderr)
        print("提示: 恢复前的文件仍在 %s" % pre, file=sys.stderr)
        return 3

    print("[restore] 完成。建议重启容器使配置立即生效: docker restart iso-hub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
