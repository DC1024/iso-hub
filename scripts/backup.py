#!/usr/bin/env python3
"""ISO Hub 配置备份。

为什么要有这个脚本:
  开启 settings.json 静态加密(ISO_HUB_SECRET_KEY_FILE)之后,**密钥与密文任一丢失
  都不可恢复** —— 账号、26 个会话、SMTP 授权码、订阅与定时任务全在同一个文件里。
  所以备份必须先于加密上线, 而不是出事之后再补。

备份什么(只有这四个 JSON, 不含 ISO 本体 —— 那是 TB 级, 该由 NAS 快照负责):
  settings.json             账号 / 会话 / 通知 / qB / 共享 / 定时任务
  distributions.json        发行版清单(含用户自建源)
  gpg_ledger.json           验签账本
  download_unverified.json  未验签 / 不符记录(可能不存在, 缺了不算错)

为什么用 tar 而不是 zip: 保留 mtime 与权限位, restore 时能原样回去。

用法:
  docker exec iso-hub python /app/scripts/backup.py
  python scripts/backup.py --data-dir /path/to/data --keep 14
  python scripts/backup.py --json          # 给 cron / 监控消费

退出码: 0 成功 / 2 参数错 / 3 没有可备份文件 / 4 写入失败
"""

import argparse
import json
import os
import sys
import tarfile
import time
from pathlib import Path

# 白名单: 只备份这四个。多一个都不加 —— 备份目录会随 --keep 轮转删除,
# 把 ISO 或别的用户数据混进来会变成"看起来很安全, 实际越滚越大还删不干净"。
BACKUP_FILES = (
    "settings.json",
    "distributions.json",
    "gpg_ledger.json",
    "download_unverified.json",
)
REQUIRED = ("settings.json",)          # 一个都没有 = 目录指错了
PREFIX = "isohub-backup-"
SUFFIX = ".tar.gz"
TMP_SUFFIX = ".part.tar.gz"


def _default_data_dir() -> Path:
    return Path(os.environ.get("ISO_DATA_DIR") or "/data")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def collect(data_dir: Path):
    """返回 (存在的文件列表, 跳过的文件名列表)。"""
    present, skipped = [], []
    for name in BACKUP_FILES:
        p = data_dir / name
        if p.is_file():
            present.append(p)
        else:
            skipped.append(name)
    return present, skipped


def make_archive(data_dir: Path, out_dir: Path, files) -> Path:
    """写 tar.gz。先写 .part 再 os.replace —— 中途被 kill 不会留下半个"看起来
    正常"的备份文件(restore 时才发现的坏备份比没有备份更糟)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        out_dir.chmod(0o700)   # 备份里有会话 token / SMTP 授权码
    except OSError:
        pass

    final = out_dir / (PREFIX + _stamp() + SUFFIX)
    tmp = out_dir / (PREFIX + _stamp() + TMP_SUFFIX)
    # 同一秒内跑两次时不要互相覆盖
    n = 1
    while final.exists():
        final = out_dir / ("%s%s-%d%s" % (PREFIX, _stamp(), n, SUFFIX))
        n += 1

    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for p in files:
                tar.add(str(p), arcname=p.name, recursive=False)
        # 归档本身也锁 0600: 备份目录虽然已经 0700, 但 /data 整块会通过 SMB /
        # WebDAV 共享出去, 而 settings.json 里有会话 token 与 SMTP 授权码。
        # 目录权限挡不住"共享配置改了"这种变化, 文件权限能。
        os.chmod(tmp, 0o600)
        os.replace(str(tmp), str(final))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print("错误: 备份写入失败: %s" % e, file=sys.stderr)
        raise SystemExit(4)
    return final


def prune(out_dir: Path, keep: int):
    """只留最后 keep 份。

    排序用 mtime(ns) 而不是文件名: 同一秒内连跑两次时文件名会带 `-1` 后缀,
    字典序会把 `...-004500-1.tar.gz` 排到 `...-004500.tar.gz` **前面**, 于是
    最新的反而先被删。mtime 没有这个问题。
    """
    if keep <= 0:
        return []
    items = [p for p in out_dir.glob(PREFIX + "*" + SUFFIX)
             if p.is_file() and not p.name.endswith(TMP_SUFFIX)]
    items.sort(key=lambda p: (p.stat().st_mtime_ns, p.name))
    doomed = items[:-keep] if len(items) > keep else []
    removed = []
    for p in doomed:
        try:
            p.unlink()
            removed.append(p.name)
        except OSError:
            pass
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="备份 ISO Hub 的四个 JSON 配置文件")
    ap.add_argument("--data-dir", default=str(_default_data_dir()),
                    help="数据目录(默认 $ISO_DATA_DIR 或 /data)")
    ap.add_argument("--out-dir", default="",
                    help="备份输出目录(默认 <data-dir>/backups)")
    ap.add_argument("--keep", type=int, default=7,
                    help="保留份数, 0=不清理(默认 7)")
    ap.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    ap.add_argument("--dry-run", action="store_true", help="只报告, 不写文件")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print("错误: 数据目录不存在: %s" % data_dir, file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir) if args.out_dir else data_dir / "backups"

    files, skipped = collect(data_dir)
    if not files or not any(p.name == REQUIRED[0] for p in files):
        print("错误: %s 下没有 %s, 请确认 --data-dir" % (data_dir, REQUIRED[0]),
              file=sys.stderr)
        return 3

    result = {
        "data_dir": str(data_dir),
        "out_dir": str(out_dir),
        "files": [p.name for p in files],
        "skipped": skipped,
        "archive": None,
        "size": 0,
        "pruned": [],
        "dry_run": bool(args.dry_run),
    }

    if args.dry_run:
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("[dry-run] 将备份: %s" % ", ".join(result["files"]))
            if skipped:
                print("[dry-run] 不存在, 跳过: %s" % ", ".join(skipped))
        return 0

    archive = make_archive(data_dir, out_dir, files)
    result["archive"] = str(archive)
    try:
        result["size"] = archive.stat().st_size
    except OSError:
        pass
    result["pruned"] = prune(out_dir, args.keep)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("[backup] 已写入 %s (%.1f KiB)" % (archive, result["size"] / 1024.0))
        print("[backup] 内容: %s" % ", ".join(result["files"]))
        if skipped:
            print("[backup] 不存在, 跳过: %s" % ", ".join(skipped))
        if result["pruned"]:
            print("[backup] 清理旧备份 %d 份: %s"
                  % (len(result["pruned"]), ", ".join(result["pruned"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
