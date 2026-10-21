#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1.5.2 回归: 下载前磁盘空间预检 + WebDAV 可写性不再假阳性

两个线上可观测的问题:

1. **磁盘写满才发现(功能缺口)**。此前全仓没有任何剩余空间检查, 20GB 的 ISO 会
   一直下到 ENOSPC 才失败, 只留下一个不完整 .part。现在在写第一个字节前, 用
   Content-Length / Content-Range / HEAD 兜底得到的"本次剩余字节数"比一次可用空间,
   不足则拒单。断言:
     * 空间不足时**不创建 .part**、不调用校验、**不换下一个候选源**(换源也写不下);
     * 空间未知时放行(与既有的"大小未知"语义一致), 但明确告警;
     * 拿不到 disk_usage(部分 FUSE/网络盘)时 fail-open, 不能把正常下载全挡死;
     * 两条下载路径(web/iso_runner.py 与 iso_download/download_linux.py)行为一致。

2. **webdav_config 恒为 true(假阳性)**。可写探针此前用 _webdav_conf_path().parent,
   WEBDAV_CONF 未设时它等于 DATA_DIR —— /data 是必探且必然可写的, 于是哪怕根本没
   部署 WebDAV sidecar, /api/health 也报 webdav_config=true。现在未配置独立挂载点
   记 not_configured, 布尔视图为 false, 并新增 writable_states 暴露四态, 让
   "没挂载"与"属主不对"可以区分。
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import app  # noqa: E402
import download_linux as DL  # noqa: E402
import iso_runner  # noqa: E402


class _Resp:
    """最小假响应: 只实现下载路径实际用到的成员。"""

    def __init__(self, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else []
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size=8192):
        self.iterated = True
        for c in self._chunks:
            yield c

    def raise_for_status(self):
        if self.status_code >= 400:
            raise iso_runner.requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class _FakeDownloader:
    def __init__(self):
        self.headers = {}
        self.verify_calls = 0

    def verify_checksum_smart(self, filepath, checksum_url, stored, dist=None):
        self.verify_calls += 1
        return True, "ok"


def _usage(free):
    return types.SimpleNamespace(total=free * 2, used=free, free=free)


# --------------------------------------------------------------------- 空间预检
class DiskSpaceHelperTest(unittest.TestCase):
    """两条下载路径的预检语义必须完全一致。"""

    MODULES = (iso_runner, DL)

    def test_unknown_size_is_allowed_with_warning(self):
        for mod in self.MODULES:
            fn = getattr(mod, "_check_disk_space", None) or mod.check_free_space
            ok, why = fn("/tmp", 0)
            self.assertTrue(ok, mod.__name__)
            self.assertIn("未知", why, mod.__name__)

    def test_enough_space(self):
        for mod in self.MODULES:
            fn = getattr(mod, "_check_disk_space", None) or mod.check_free_space
            with patch("shutil.disk_usage", lambda p: _usage(10 * 1024 ** 3)):
                ok, why = fn("/tmp", 1024 ** 3)
            self.assertTrue(ok, mod.__name__)
            self.assertEqual(why, "", mod.__name__)

    def test_shortage_is_rejected_and_message_is_actionable(self):
        for mod in self.MODULES:
            fn = getattr(mod, "_check_disk_space", None) or mod.check_free_space
            with patch("shutil.disk_usage", lambda p: _usage(100 * 1024 ** 2)):
                ok, why = fn("/tmp", 1024 ** 3)
            self.assertFalse(ok, mod.__name__)
            self.assertIn("磁盘空间不足", why, mod.__name__)
            # 必须给出"还需 / 可用"两个数字, 否则用户不知道该清多少
            self.assertIn("还需", why, mod.__name__)
            self.assertIn("当前可用", why, mod.__name__)

    def test_margin_is_enforced(self):
        """刚好等于 need+margin 放行, 少一个字节就拒 —— 余量不是摆设。"""
        need = 1024 ** 3
        margin = 256 * 1024 ** 2
        for mod in self.MODULES:
            fn = getattr(mod, "_check_disk_space", None) or mod.check_free_space
            with patch("shutil.disk_usage", lambda p: _usage(need + margin)):
                self.assertTrue(fn("/tmp", need)[0], mod.__name__)
            with patch("shutil.disk_usage", lambda p: _usage(need + margin - 1)):
                self.assertFalse(fn("/tmp", need)[0], mod.__name__)

    def test_stat_failure_fails_open(self):
        """拿不到剩余空间时放行: 部分 FUSE/网络盘不支持 statvfs, 不能全挡死。"""
        def boom(_p):
            raise OSError("not supported")

        for mod in self.MODULES:
            fn = getattr(mod, "_check_disk_space", None) or mod.check_free_space
            with patch("shutil.disk_usage", boom):
                ok, why = fn("/tmp", 1024 ** 3)
            self.assertTrue(ok, mod.__name__)
            self.assertIn("跳过", why, mod.__name__)

    def test_both_modules_share_the_same_margin(self):
        self.assertEqual(iso_runner.DISK_SAFETY_MARGIN, DL.DISK_SAFETY_MARGIN)


class RunnerDiskPrecheckTest(unittest.TestCase):
    """生产真实路径: iso_runner._download_file_with_failover。"""

    NAME = "demo.iso"
    SIZE = 10 ** 9

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.filepath = self.dir / self.NAME

    def _run(self, candidates, calls, free, size=None):
        size = self.SIZE if size is None else size
        dl = _FakeDownloader()
        errs = []

        def _get(url, **kw):
            calls.append(url)
            return _Resp(200, headers={"content-length": str(size)},
                         chunks=[b"x" * size])

        with patch.object(iso_runner.requests, "get", _get), \
                patch("shutil.disk_usage", lambda p: _usage(free)):
            ok, used = iso_runner._download_file_with_failover(
                dl, {"type": "linux", "distribution": "Demo", "checksum": ""},
                candidates, self.NAME, self.dir, self.filepath,
                head_total=size, err_out=errs)
        return ok, used, errs, dl

    def test_shortage_aborts_before_writing(self):
        calls = []
        ok, used, errs, dl = self._run(
            [("https://m1/demo.iso", ""), ("https://m2/demo.iso", "")],
            calls, free=100 * 1024 ** 2)

        self.assertFalse(ok)
        self.assertIsNone(used)
        self.assertFalse(self.filepath.exists())
        # 关键: 连 .part 都不该出现 —— 否则 UI 会显示"下载停止", 用户以为可以续传
        self.assertFalse((self.dir / (self.NAME + ".part")).exists())
        self.assertEqual(dl.verify_calls, 0, "空间不足不应进入校验")
        self.assertIn("磁盘空间不足", str(errs[-1]))

    def test_shortage_does_not_try_next_source(self):
        """换源解决不了磁盘满, 每个候选源都重试一遍只是把同一个错误刷 N 次。"""
        calls = []
        self._run([("https://m1/demo.iso", ""), ("https://m2/demo.iso", ""),
                   ("https://m3/demo.iso", "")], calls, free=1)
        self.assertEqual(len(calls), 1, "空间不足必须直接放弃, 不得换源")

    def test_enough_space_proceeds(self):
        calls = []
        ok, used, _errs, dl = self._run([("https://m1/demo.iso", "")], calls,
                                        free=10 * 1024 ** 3, size=4096)
        self.assertTrue(ok, "空间足够 + 长度一致 + 校验通过 → 应正常落定")
        self.assertEqual(used, "https://m1/demo.iso")
        self.assertEqual(dl.verify_calls, 1)
        self.assertTrue(self.filepath.exists())
        self.assertEqual(self.filepath.read_bytes(), b"x" * 4096)


class DownloadLinuxDiskPrecheckTest(unittest.TestCase):
    """CLI 路径: LinuxDistributionDownloader.download_distribution。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.json_path = self.dir / "distributions.json"
        self.json_path.write_text(json.dumps({
            "distributions": [{
                "distribution": "Demo", "type": "linux",
                "download_url": "https://m/demo.iso", "checksum": "",
            }]
        }), encoding="utf-8")
        self.download_dir = self.dir / "downloads"
        self.download_dir.mkdir()

    def _run(self, free, size=1024):
        dl = DL.LinuxDistributionDownloader(str(self.json_path), str(self.download_dir))
        resp = _Resp(200, headers={"content-length": str(size)}, chunks=[b"x" * size])

        with patch.object(DL.requests, "get", lambda url, **kw: resp), \
                patch("shutil.disk_usage", lambda p: _usage(free)), \
                patch.object(DL, "tqdm", _NoopBar):
            ok = dl.download_distribution("Demo", verify_checksum=False)
        return ok, resp

    def test_shortage_skips_without_partial_file(self):
        ok, resp = self._run(free=1)
        self.assertFalse(ok)
        self.assertFalse(resp.iterated, "空间不足时不应读取任何响应字节")
        self.assertEqual(list(self.download_dir.rglob("*.part")), [])

    def test_enough_space_downloads(self):
        ok, resp = self._run(free=10 * 1024 ** 3, size=1024)
        self.assertTrue(resp.iterated)
        self.assertTrue(ok)
        self.assertEqual(
            (self.download_dir / "linux" / "Demo" / "demo.iso").read_bytes(),
            b"x" * 1024)


class _NoopBar:
    """tqdm 的静默替身: 测试里不需要进度条, 也不该往 stderr 写 \\r。"""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, _n):
        pass


# --------------------------------------------------------------- WebDAV 可写性
class WebdavWritableTargetTest(unittest.TestCase):
    def test_unset_env_means_not_configured(self):
        with patch.dict(os.environ, {"WEBDAV_CONF": ""}):
            self.assertIsNone(app._webdav_conf_dir())

    def test_conf_points_to_its_own_mount(self):
        with patch.dict(os.environ, {"WEBDAV_CONF": "/webdav-config/webdav.yml"}):
            self.assertEqual(app._webdav_conf_dir(), Path("/webdav-config"))

    def test_target_does_not_fall_back_to_data_dir(self):
        """/data 是必探且必然可写的 —— 回退到它就是这个假阳性的根因。"""
        with patch.dict(os.environ, {"WEBDAV_CONF": ""}):
            targets = {t[0]: t for t in app._writable_targets()}
        self.assertIsNone(targets["webdav_config"][2])

    def test_states_marks_not_configured(self):
        with patch.dict(os.environ, {"WEBDAV_CONF": ""}):
            rows = {r[0]: r for r in app._check_writable_targets()}
        self.assertEqual(rows["webdav_config"][4], "not_configured")
        # 布尔视图仍为 False, 与"写不进去"的既有语义一致
        with patch.dict(os.environ, {"WEBDAV_CONF": ""}):
            self.assertFalse(app.writable_snapshot(force=True)["webdav_config"])
            self.assertEqual(app.writable_states(force=True)["webdav_config"],
                             "not_configured")

    def test_configured_dir_is_probed_for_real(self):
        with tempfile.TemporaryDirectory() as td:
            conf = Path(td) / "webdav.yml"
            with patch.dict(os.environ, {"WEBDAV_CONF": str(conf)}):
                self.assertEqual(app.writable_snapshot(force=True)["webdav_config"], True)
                self.assertEqual(app.writable_states(force=True)["webdav_config"], "ok")


class HealthWritableStatesTest(unittest.TestCase):
    KEYS = ("data", "logs", "qb_config", "webdav_config", "samba_config")
    STATES = ("ok", "missing", "denied", "not_configured")

    def test_health_exposes_states_alongside_booleans(self):
        # 其他测试会 patch _writable_targets 并写入模块级缓存, 这里先按真实目标重算,
        # 否则断言会看到上一组测试留下的 {data: ok}。
        app.writable_states(force=True)
        body = app.app.test_client().get("/api/health").get_json()
        self.assertIn("writable_states", body)
        for key in self.KEYS:
            self.assertIn(key, body["writable_states"])
            self.assertIn(body["writable_states"][key], self.STATES)
            # 两个视图必须自洽: 只有 ok 才对应 true
            self.assertEqual(body["writable"][key],
                             body["writable_states"][key] == "ok")

    def test_states_do_not_flip_ok(self):
        with patch.object(app, "writable_states",
                          lambda *a, **k: {k2: "denied" for k2 in self.KEYS}):
            body = app.app.test_client().get("/api/health").get_json()
        self.assertTrue(body.get("ok"))


if __name__ == "__main__":
    unittest.main()
