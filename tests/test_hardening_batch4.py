#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 4 批加固的验收测试 —— 对应审查报告 M8 / M10 / H1。

这批三项都跨"代码 + 部署面", 本地没有 Docker 可跑, 所以测试分两层:

  * **纯函数层**: 直接调 app.py 里的判定函数(IP 归属、备份目录解析、口令文件读取),
    这类断言不依赖容器;
  * **配置契约层**: 把 docker-compose*.yml 当成被测对象, 断言它们表达的隔离关系
    —— M8 的"备份卷不能被共享服务挂到"是**结构性**约束, 只在代码里改是拦不住的,
    谁往 samba 的 volumes 里加一行 `./backups:/srv/bk:ro` 就全废了。

为什么 M8 要盯着三个地方解析同一套规则:
  backup.py(写归档)、restore.py(写 pre-restore 快照)、app.py(/api/health 的可写
  探测与启动告警)各自独立解析输出目录。三份实现一旦漂移, 症状是"备份写到 A、
  恢复去 B 找、健康检查报 C" —— 单看任何一个文件都正常。所以这里做**一致性**断言。
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402

COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dockerhub.yml",
                 "docker-compose.acr.yml")


def _load_script(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        mod_name, str(REPO_ROOT / "scripts" / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bk = _load_script("batch4_backup", "backup.py")
rs = _load_script("batch4_restore", "restore.py")


def _env_dict(service) -> dict:
    """compose 的 environment 既可能是 list 也可能是 dict。"""
    env = service.get("environment", [])
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out = {}
    for item in env:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


def _host_paths(service) -> list:
    """volumes 里的宿主机侧路径。"""
    return [str(v).split(":", 1)[0] for v in service.get("volumes", [])]


# --------------------------------------------------------------- M8: 备份位置
class BackupDirResolutionTest(unittest.TestCase):
    """backup.py / restore.py / app.py 必须用同一套规则解析输出目录。"""

    DATA_DIRS = (Path("/data"), Path("/srv/iso-hub/data"), Path("/tmp/x"))

    def test_three_implementations_agree_without_env(self):
        env = {k: v for k, v in os.environ.items() if k != "ISO_HUB_BACKUP_DIR"}
        with patch.dict(os.environ, env, clear=True):
            for d in self.DATA_DIRS:
                with self.subTest(data_dir=str(d)):
                    want = bk._default_backup_dir(d)
                    self.assertEqual(rs._default_backup_dir(d), want)
                    self.assertEqual(app._default_backup_dir(d), want)

    def test_three_implementations_agree_with_env(self):
        with patch.dict(os.environ, {"ISO_HUB_BACKUP_DIR": "/mnt/nas/bk"}):
            for d in self.DATA_DIRS:
                with self.subTest(data_dir=str(d)):
                    for mod in (bk, rs, app):
                        self.assertEqual(mod._default_backup_dir(d),
                                         Path("/mnt/nas/bk"))

    def test_default_is_outside_the_data_dir(self):
        """M8 的根: 默认目录不能落在 data 里 —— 那是 SMB/WebDAV 的共享树。"""
        for d in self.DATA_DIRS:
            with self.subTest(data_dir=str(d)):
                out = app._default_backup_dir(d)
                self.assertNotEqual(out, d / "backups")
                with self.assertRaises(ValueError):
                    out.relative_to(d)

    def test_empty_env_is_treated_as_unset(self):
        """空字符串必须回落到默认值, 否则 .env 里留空的 `ISO_HUB_BACKUP_DIR=` 会把
        输出目录变成当前工作目录。"""
        with patch.dict(os.environ, {"ISO_HUB_BACKUP_DIR": "   "}):
            self.assertEqual(app._default_backup_dir(Path("/data")), Path("/backups"))


class ComposeBackupIsolationTest(unittest.TestCase):
    """M8 的部署面: 备份卷只挂给主容器, 共享服务一份都碰不到。"""

    @classmethod
    def setUpClass(cls):
        cls.docs = {n: yaml.safe_load((REPO_ROOT / n).read_text(encoding="utf-8"))
                    for n in COMPOSE_FILES}

    def test_main_container_mounts_dedicated_volume(self):
        for name, doc in self.docs.items():
            with self.subTest(compose=name):
                self.assertIn("./backups:/backups",
                              doc["services"]["iso-hub"]["volumes"])

    def test_main_container_env_points_at_that_mount(self):
        for name, doc in self.docs.items():
            with self.subTest(compose=name):
                env = _env_dict(doc["services"]["iso-hub"])
                raw = env.get("ISO_HUB_BACKUP_DIR", "")
                # 允许两种写法: 写死 /backups, 或 ${ISO_HUB_BACKUP_DIR:-/backups}
                # (后者才是可覆盖的, 也是我们要的默认值)
                self.assertIn("/backups", raw)

    def test_sidecars_never_mount_the_backup_volume(self):
        """这一条是整项 M8 的命门: 加一行挂载就把归档重新交出去了。"""
        for name, doc in self.docs.items():
            for svc in ("samba", "webdav", "qbittorrent"):
                for host in _host_paths(doc["services"][svc]):
                    with self.subTest(compose=name, service=svc, volume=host):
                        self.assertNotIn("backups", host)

    def test_sidecars_still_share_the_iso_tree(self):
        """反向兜底: 上面的断言不能靠"把共享挂载删掉"来满足。"""
        for name, doc in self.docs.items():
            with self.subTest(compose=name):
                self.assertIn("./data", _host_paths(doc["services"]["samba"]))
                self.assertIn("./data", _host_paths(doc["services"]["webdav"]))

    def test_backup_volume_is_outside_every_shared_host_path(self):
        """把"备份在哪"和"共享了什么"接起来断言 —— 这才是 M8 真正要保证的关系。"""
        backup_host = Path("./backups")
        for name, doc in self.docs.items():
            for svc in ("samba", "webdav"):
                for host in _host_paths(doc["services"][svc]):
                    with self.subTest(compose=name, service=svc, shared=host):
                        shared = Path(host)
                        self.assertNotEqual(backup_host, shared)
                        self.assertNotIn(shared, backup_host.parents,
                                         "备份目录落在共享路径之下, 归档会经共享泄露")


class BackupWritableTargetTest(unittest.TestCase):
    def test_backups_is_probed_but_optional(self):
        rows = {r[0]: r for r in app._writable_targets()}
        self.assertIn("backups", rows)
        _key, _label, path, required = rows["backups"]
        self.assertEqual(path, app.BACKUP_DIR)
        self.assertFalse(required, "备份卷是可选挂载, 写不了只该告警, 不该拦启动")

    def test_legacy_dir_warning_fires(self):
        """升级后旧归档不会自己搬家 —— 启动时必须点名, 否则会以为已经安全了。"""
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "backups").mkdir(parents=True)
            (data / "backups" / "isohub-backup-20260101-000000.tar.gz").write_bytes(b"x")
            msgs = []
            with patch.object(app, "DATA_DIR", data), \
                    patch.object(app, "BACKUP_DIR", data.parent / "backups"), \
                    patch.object(app, "log", msgs.append):
                self.assertTrue(app._warn_if_legacy_backup_dir())
            self.assertTrue(any("旧备份目录" in m for m in msgs), msgs)
            self.assertTrue(any("chown" not in m and "mv " in m for m in msgs), msgs)

    def test_legacy_warning_is_quiet_when_clean(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "backups").mkdir(parents=True)      # 目录在但没归档
            msgs = []
            with patch.object(app, "DATA_DIR", data), \
                    patch.object(app, "BACKUP_DIR", data.parent / "backups"), \
                    patch.object(app, "log", msgs.append):
                self.assertFalse(app._warn_if_legacy_backup_dir())
            self.assertEqual(msgs, [])

    def test_legacy_warning_skipped_when_backup_dir_is_legacy_dir(self):
        """用户显式把 ISO_HUB_BACKUP_DIR 指回 data/backups 时不该报"需要迁移"。"""
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "backups").mkdir(parents=True)
            (data / "backups" / "isohub-backup-20260101-000000.tar.gz").write_bytes(b"x")
            with patch.object(app, "DATA_DIR", data), \
                    patch.object(app, "BACKUP_DIR", data / "backups"), \
                    patch.object(app, "log", lambda _m: None):
                self.assertFalse(app._warn_if_legacy_backup_dir())


if __name__ == "__main__":
    unittest.main()
