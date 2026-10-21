#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/backup.py 与 scripts/restore.py 的护栏测试。

为什么必须测: 这两个脚本是「settings.json 加密」的前置条件。加密之后, 密钥与
密文任一丢失都不可恢复, 备份是唯一退路 —— 一个静默写坏的备份比没有备份更危险,
因为它会让人误以为"有兜底"。

覆盖点:
  * 只打包白名单里的四个文件, 且缺文件不算错(下载记录本来就可能不存在)
  * 缺 settings.json = 目录指错了, 退出码 3
  * --dry-run 不落任何文件
  * --keep 按 mtime 轮转(同秒多次备份也不能删掉最新的那个)
  * 归档先写 .part 再原子替换, 不留半成品
  * restore 默认只预览, 不动磁盘
  * restore 拒绝路径穿越 / 未知成员 / 目录成员
  * restore 覆盖前先把旧文件存到 pre-restore-*, 且文件权限收敛到 600
"""
import importlib.util
import os
import stat
import io
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        mod_name, str(REPO_ROOT / "scripts" / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bk = _load("isohub_backup", "backup.py")
rs = _load("isohub_restore", "restore.py")


def _run(mod, argv):
    """跑一次 main(), 返回 (exit_code, stdout, stderr)。"""
    old = sys.argv
    sys.argv = [mod.__name__] + list(argv)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = mod.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = old
    return code, out.getvalue(), err.getvalue()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.write("settings.json", '{"users": {"admin": {}}}')
        self.write("distributions.json", '{"distributions": []}')

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, text: str):
        (self.data / name).write_text(text, encoding="utf-8")

    def backups(self):
        d = self.data / "backups"
        return sorted(d.glob("isohub-backup-*.tar.gz")) if d.is_dir() else []


class BackupTest(_Base):
    def test_archives_only_whitelisted_present_files(self):
        self.write("download_unverified.json", '{"a": 1}')
        (self.data / "archlinux.iso").write_bytes(b"\0" * 1024)  # 绝不能进备份

        code, out, _err = _run(bk, ["--data-dir", str(self.data)])
        self.assertEqual(code, 0, out)

        archives = self.backups()
        self.assertEqual(len(archives), 1)
        with tarfile.open(str(archives[0]), "r:gz") as tar:
            names = sorted(m.name for m in tar.getmembers())
        self.assertEqual(names, ["distributions.json", "download_unverified.json",
                                 "settings.json"])
        self.assertNotIn("archlinux.iso", names)

    def test_missing_settings_means_wrong_directory(self):
        (self.data / "settings.json").unlink()
        code, _out, err = _run(bk, ["--data-dir", str(self.data)])
        self.assertEqual(code, 3)
        self.assertIn("settings.json", err)

    def test_dry_run_writes_nothing(self):
        code, out, _err = _run(bk, ["--data-dir", str(self.data), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertEqual(self.backups(), [])
        self.assertFalse((self.data / "backups").exists())

    def test_keep_prunes_oldest_and_keeps_newest(self):
        # 同秒内连跑三次会触发 "-1"/"-2" 后缀, 正是排序最容易出错的情形
        for _ in range(3):
            code, out, _err = _run(bk, ["--data-dir", str(self.data), "--keep", "2"])
            self.assertEqual(code, 0, out)
        archives = self.backups()
        self.assertEqual(len(archives), 2)
        # 最新一份的内容必须还在(不能把刚写的删掉)
        newest = max(archives, key=lambda p: p.stat().st_mtime_ns)
        with tarfile.open(str(newest), "r:gz") as tar:
            self.assertIn("settings.json", [m.name for m in tar.getmembers()])

    @unittest.skipIf(os.name == "nt",
                     "Windows 的 os.chmod 只能开关只读位, 表达不了 0600")
    def test_archive_is_not_world_readable(self):
        """归档里是会话 token 与 SMTP 授权码, 不能 644。

        /data 整块会通过 SMB / WebDAV 共享出去, 备份目录的 0700 挡不住
        "共享配置改了"这种变化; 文件自身的 0600 才是不依赖上下文的那道锁。
        """
        _run(bk, ["--data-dir", str(self.data)])
        newest = max(self.backups(), key=lambda p: p.stat().st_mtime_ns)
        self.assertEqual(stat.S_IMODE(newest.stat().st_mode), 0o600)

    def test_no_leftover_part_files(self):
        _run(bk, ["--data-dir", str(self.data)])
        leftovers = list((self.data / "backups").glob("*.part.tar.gz"))
        self.assertEqual(leftovers, [])

    def test_json_output_is_machine_readable(self):
        import json
        code, out, _err = _run(bk, ["--data-dir", str(self.data), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["files"], ["settings.json", "distributions.json"])
        self.assertTrue(Path(payload["archive"]).is_file())


class RestoreTest(_Base):
    def _make_backup(self) -> Path:
        code, _out, _err = _run(bk, ["--data-dir", str(self.data)])
        self.assertEqual(code, 0)
        return self.backups()[-1]

    def _malicious(self, member: str) -> Path:
        p = self.root / "evil.tar.gz"
        with tarfile.open(str(p), "w:gz") as tar:
            info = tarfile.TarInfo(member)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        return p

    def test_preview_does_not_touch_disk(self):
        archive = self._make_backup()
        self.write("settings.json", '{"changed": true}')
        before = (self.data / "settings.json").read_text(encoding="utf-8")

        code, out, _err = _run(rs, [str(archive), "--data-dir", str(self.data)])
        self.assertEqual(code, 0)
        self.assertIn("预览", out)
        self.assertEqual((self.data / "settings.json").read_text(encoding="utf-8"), before)

    def test_restore_overwrites_and_keeps_previous_copy(self):
        archive = self._make_backup()
        self.write("settings.json", '{"changed": true}')

        code, out, _err = _run(
            rs, [str(archive), "--data-dir", str(self.data), "--yes"])
        self.assertEqual(code, 0, out)
        self.assertIn("admin", (self.data / "settings.json").read_text(encoding="utf-8"))

        pre = list((self.data / "backups").glob("pre-restore-*"))
        self.assertEqual(len(pre), 1)
        self.assertIn("changed", (pre[0] / "settings.json").read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "Windows 没有 POSIX 权限位, chmod 600 不生效")
    def test_restored_files_are_0600(self):
        archive = self._make_backup()
        _run(rs, [str(archive), "--data-dir", str(self.data), "--yes"])
        mode = (self.data / "settings.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_rejects_path_traversal(self):
        code, _out, err = _run(
            rs, [str(self._malicious("../settings.json")),
                 "--data-dir", str(self.data), "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("白名单", err)

    def test_rejects_unknown_member(self):
        code, _out, err = _run(
            rs, [str(self._malicious("authorized_keys")),
                 "--data-dir", str(self.data), "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("白名单", err)

    def test_rejects_directory_member(self):
        p = self.root / "dir.tar.gz"
        with tarfile.open(str(p), "w:gz") as tar:
            info = tarfile.TarInfo("settings.json")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        code, _out, err = _run(
            rs, [str(p), "--data-dir", str(self.data), "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("普通文件", err)

    def test_missing_archive_is_exit_2(self):
        code, _out, err = _run(
            rs, [str(self.root / "nope.tar.gz"), "--data-dir", str(self.data)])
        self.assertEqual(code, 2)
        self.assertIn("不存在", err)


if __name__ == "__main__":
    unittest.main()
