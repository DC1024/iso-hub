#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存放模式(classified / flat)回归测试。

需求: 用户可切换 ISO + 种子下载的落盘方式 —— 按 <类型>/<发行版>/ 分文件夹,
还是全部平铺进同一个目录(DATA_DIR/iso)。切换只对**新下载**生效, 不迁移旧文件。

本文件锁死的契约:
  1. settings.json 的 storage_mode 读/写 / 非法值收敛 / 环境变量回退;
  2. /api/storage-mode 的 GET/POST;
  3. 四处路径构造(_safe_join / iso_runner / sync_subscriptions / download_linux)
     在两种模式下行为一致, 且**非法输入在任何模式下都被拒绝**
     (这条专治"切到 flat 就绕过穿越校验"的写法);
  4. 平铺模式下「清理过期」绝不能删掉别的发行版的 ISO —— 目录是全局共享的,
     这是本功能最危险的一条, 也是唯一可能造成不可逆数据丢失的地方;
  5. 平铺模式的 disk_inventory 归属 + 「下载中」状态识别;
  6. 种子保存路径跟随模式;
  7. 前端契约(i18n key / DOM id / 请求路径 / 接线);
  8. 四个模块的 FLAT 常量不漂移(它们是手工复制的三份, 最易分叉)。
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import app  # noqa: E402
import iso_runner  # noqa: E402
import sync_subscriptions  # noqa: E402
import download_linux  # noqa: E402

HTML = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

FLAT = "iso"           # 平铺目录名(DATA_DIR/iso)
VERSION = "beta 2.3"


class StorageModeBase(unittest.TestCase):
    """临时 DATA_DIR + 关闭登录门禁(鉴权另有专门测试覆盖)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.write_settings({})
        (self.data / "distributions.json").write_text(
            json.dumps({"updated_at": 0, "distributions": []}), encoding="utf-8")

        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.data / "distributions.json"),
            patch.object(app, "SETTINGS_JSON", self.data / "settings.json"),
            patch.object(app, "FAILURES_JSON", self.data / "download_failures.json"),
            patch.object(app, "running_task", return_value=None),
            patch.object(app, "REQUIRE_LOGIN", False),
            patch.object(app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)   # setUp 半途失败也兜底, 且不重复 stop
        self.client = app.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    # ---- helpers -------------------------------------------------------
    def write_settings(self, obj: dict) -> None:
        (self.data / "settings.json").write_text(
            json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def read_settings(self) -> dict:
        return json.loads((self.data / "settings.json").read_text(encoding="utf-8"))

    def set_mode(self, mode: str) -> None:
        self.write_settings({"storage_mode": mode})

    def write_manifest(self, entries: list) -> None:
        (self.data / "distributions.json").write_text(
            json.dumps({"updated_at": 0, "distributions": entries},
                       ensure_ascii=False), encoding="utf-8")

    def flat_dir(self) -> Path:
        return self.data / FLAT


# --------------------------------------------------------------------- A. 设置读写
class TestStorageModeSetting(StorageModeBase):

    def test_default_is_classified(self):
        self.assertEqual(app.load_storage_mode(), "classified")

    def test_env_var_honoured_when_settings_missing(self):
        with patch.dict(os.environ, {"ISO_HUB_STORAGE_MODE": "flat"}):
            self.assertEqual(app.load_storage_mode(), "flat")

    def test_settings_win_over_env(self):
        self.set_mode("classified")
        with patch.dict(os.environ, {"ISO_HUB_STORAGE_MODE": "flat"}):
            self.assertEqual(app.load_storage_mode(), "classified")

    def test_roundtrip_persists(self):
        app.save_storage_mode("flat")
        self.assertEqual(app.load_storage_mode(), "flat")
        self.assertEqual(self.read_settings().get("storage_mode"), "flat")

    def test_illegal_stored_value_falls_back(self):
        self.set_mode("banana")
        self.assertEqual(app.load_storage_mode(), "classified")

    def test_case_and_whitespace_normalised(self):
        self.set_mode(" FLAT ")
        self.assertEqual(app.load_storage_mode(), "flat")

    def test_save_rejects_illegal_value(self):
        app.save_storage_mode("nope")
        self.assertEqual(app.load_storage_mode(), "classified")

    def test_save_preserves_other_keys(self):
        """切换存放模式不得顺手清掉选源策略 / 受保护名单。"""
        self.write_settings({"source_strategy": "B",
                             "protected": ["linux/Ubuntu/a.iso"]})
        app.save_storage_mode("flat")
        saved = self.read_settings()
        self.assertEqual(saved.get("source_strategy"), "B")
        self.assertEqual(saved.get("protected"), ["linux/Ubuntu/a.iso"])
        self.assertEqual(saved.get("storage_mode"), "flat")


# --------------------------------------------------------------------- B. API
class TestStorageModeApi(StorageModeBase):

    def test_get_default(self):
        r = self.client.get("/api/storage-mode")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["mode"], "classified")

    def test_post_then_get_reflects(self):
        r = self.client.post("/api/storage-mode", json={"mode": "flat"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["mode"], "flat")
        self.assertEqual(self.client.get("/api/storage-mode").get_json()["mode"], "flat")
        self.assertEqual(app.load_storage_mode(), "flat")

    def test_flat_get_reports_target_dir(self):
        self.client.post("/api/storage-mode", json={"mode": "flat"})
        d = self.client.get("/api/storage-mode").get_json().get("dir") or ""
        self.assertTrue(d.endswith(FLAT), d)

    def test_classified_get_has_no_dir(self):
        self.assertEqual(self.client.get("/api/storage-mode").get_json().get("dir"), "")

    def test_post_uppercase_accepted(self):
        r = self.client.post("/api/storage-mode", json={"mode": "FLAT"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(app.load_storage_mode(), "flat")

    def test_post_illegal_rejected_and_not_persisted(self):
        r = self.client.post("/api/storage-mode", json={"mode": "banana"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(app.load_storage_mode(), "classified")

    def test_post_missing_mode_rejected(self):
        self.assertEqual(self.client.post("/api/storage-mode", json={}).status_code, 400)


# --------------------------------------------------------------------- C. _safe_join
class TestSafeJoinPerMode(StorageModeBase):

    def test_classified_shape(self):
        self.set_mode("classified")
        self.assertEqual(app._safe_join("linux", "Ubuntu"),
                         (self.data / "linux" / "Ubuntu").resolve())

    def test_flat_collapses_everything_into_one_dir(self):
        self.set_mode("flat")
        for typ, name in (("linux", "Ubuntu"), ("bsd", "FreeBSD"),
                          ("windows", "Win11"), ("macos", "Sonoma")):
            self.assertEqual(app._safe_join(typ, name), app._flat_iso_dir())

    def test_flat_dir_is_data_dir_subdir(self):
        self.assertEqual(app._flat_iso_dir(), (self.data / FLAT).resolve())

    def test_traversal_rejected_in_both_modes(self):
        """校验必须先于模式判断 —— 切到 flat 不能让穿越输入蒙混过关。"""
        bad = [("linux", ".."), ("linux", "a/b"), ("linux", "a\\b"),
               ("linux", ""), ("", "Ubuntu"), ("bogus", "Arch"),
               ("linux", " ."), ("linux", "x "), ("linux", ".")]
        for mode in ("classified", "flat"):
            self.set_mode(mode)
            for typ, name in bad:
                self.assertIsNone(app._safe_join(typ, name),
                                  f"{mode} 模式下 {typ!r}/{name!r} 应被拒绝")


# --------------------------------------------------------------------- D. runner 三份拷贝
class TestRunnerDistDirCopies(unittest.TestCase):
    """iso_runner / sync_subscriptions / download_linux 各自的 _safe_dist_dir。"""

    MODULES = (iso_runner, sync_subscriptions, download_linux)

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_classified_default_for_every_copy(self):
        for m in self.MODULES:
            self.assertEqual(m._safe_dist_dir(self.d, "linux", "Arch"),
                             (self.d / "linux" / "Arch").resolve(),
                             m.__name__)

    def test_flat_for_every_copy(self):
        for m in self.MODULES:
            self.assertEqual(m._safe_dist_dir(self.d, "linux", "Arch", flat=True),
                             (self.d / FLAT).resolve(), m.__name__)

    def test_traversal_rejected_in_both_modes_for_every_copy(self):
        bad = [("linux", ".."), ("linux", "a/b"), ("linux", "a\\b"), ("linux", ""),
               ("bogus", "Arch"), ("linux", "x ")]
        for m in self.MODULES:
            for flat in (False, True):
                for typ, name in bad:
                    self.assertIsNone(m._safe_dist_dir(self.d, typ, name, flat=flat),
                                      f"{m.__name__} flat={flat} {typ!r}/{name!r}")

    def test_flat_of_reads_instance_attr(self):
        class Obj:
            pass
        o = Obj()
        self.assertFalse(download_linux._flat_of(o), "缺属性时应按 classified")
        o.storage_mode = "flat"
        self.assertTrue(download_linux._flat_of(o))
        o.storage_mode = "CLASSIFIED"
        self.assertFalse(download_linux._flat_of(o))
        o.storage_mode = "banana"
        self.assertFalse(download_linux._flat_of(o))


# --------------------------------------------------------------------- E. 常量一致性
class TestFlatConstantsAgree(unittest.TestCase):
    """三份手工复制的常量最容易分叉 —— 分叉即"某个入口写到别的目录去"。"""

    def test_flat_dirname_identical(self):
        self.assertEqual(app.FLAT_ISO_DIRNAME, FLAT)
        self.assertEqual(iso_runner.FLAT_DIRNAME, FLAT)
        self.assertEqual(sync_subscriptions.FLAT_DIRNAME, FLAT)
        self.assertEqual(download_linux.FLAT_DIRNAME, FLAT)

    def test_storage_modes_identical(self):
        for m in (iso_runner, sync_subscriptions, download_linux):
            self.assertEqual(tuple(m.STORAGE_MODES), tuple(app.STORAGE_MODES))

    def test_runner_uses_cli_flag_not_settings_file(self):
        """iso_runner 必须经 --storage-mode 拿模式, 不得自己读写 settings.json。

        与 tests/test_v1316_settings_guard.py 同一条规则: 含 "settings.json" 的行
        不得出现 open( / write_text / write_bytes —— 那会绕过 config_files 的
        跨进程锁与原子写。(纯文档里提到文件名不算违规。)
        """
        src = (REPO_ROOT / "web" / "iso_runner.py").read_text(encoding="utf-8")
        self.assertIn('"--storage-mode"', src)
        self.assertIn("choices=list(STORAGE_MODES)", src)
        offenders = [ln for ln in src.splitlines()
                     if "settings.json" in ln
                     and re.search(r"\b(write_text|write_bytes|open\()", ln)]
        self.assertEqual(offenders, [], offenders)

    def test_sync_uses_cli_flag_and_forwards_to_downloader(self):
        src = (REPO_ROOT / "web" / "sync_subscriptions.py").read_text(encoding="utf-8")
        self.assertIn('"--storage-mode"', src)
        self.assertIn("downloader.storage_mode = args.storage_mode", src)

    # 下面三条是"接线"断言: 光有 `flat=` 参数而调用点不传, 功能照样静默失效,
    # 而纯行为测试(直接调 _safe_dist_dir(flat=True))抓不到这种情况。
    def test_iso_runner_call_site_passes_flat(self):
        src = (REPO_ROOT / "web" / "iso_runner.py").read_text(encoding="utf-8")
        self.assertIn("flat=args.storage_mode ==", src)
        self.assertIn("downloader.storage_mode = args.storage_mode", src)

    def test_sync_call_site_passes_flat(self):
        src = (REPO_ROOT / "web" / "sync_subscriptions.py").read_text(encoding="utf-8")
        self.assertIn("flat=args.storage_mode ==", src)

    def test_download_linux_call_sites_pass_flat(self):
        """download_linux 里两处 dist_dir 计算(下载 + 清理)都必须带上模式。"""
        src = (REPO_ROOT / "iso_download" / "download_linux.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("flat=_flat_of(self)"), 2, src.count("flat=_flat_of(self)"))

    def test_app_passes_mode_to_both_runners(self):
        """手动下载(iso_runner) 与 订阅同步(sync_subscriptions) 两条子进程都要带上。

        只按行断言(不跨行), 免得行尾 CRLF/LF 差异把测试弄红。
        """
        src = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(src.count('"--storage-mode", load_storage_mode(),'), 2,
                         "app.py 应同时为 iso_runner 与 sync_subscriptions 传 --storage-mode")


# --------------------------------------------------------------------- F. 平铺清理安全性
class TestFlatPruneDoesNotTouchOtherDistros(StorageModeBase):
    """平铺模式下所有发行版共用一个目录 —— 「清理过期」必须仍然只清本发行版。

    守卫点: app.py 用 `_is_known_file(typ, name, fname)` 作为第二道闸门, 只删
    "曾出现在**本发行版**历史清单里"的文件。若把这道闸门去掉(或改成"任何历史"),
    清理 Ubuntu 就会把 Fedora 的 ISO 一起删掉 —— 这正是本测试要抓的突变。
    """

    def setUp(self):
        super().setUp()
        self.flat = self.flat_dir()
        self.flat.mkdir(parents=True, exist_ok=True)
        (self.flat / "ubuntu-22.04.iso").write_bytes(b"old-ubuntu")
        (self.flat / "fedora-40.iso").write_bytes(b"old-fedora")
        (self.flat / "user-own.iso").write_bytes(b"mine")
        self.write_manifest([
            {"distribution": "Ubuntu", "type": "linux",
             "download_url": "https://example.org/ubuntu-26.04.iso"},
            {"distribution": "Fedora", "type": "linux",
             "download_url": "https://example.org/fedora-41.iso"},
        ])
        self.write_settings({
            "storage_mode": "flat",
            "manifest_history": {
                "linux/Ubuntu": ["ubuntu-22.04.iso"],
                "linux/Fedora": ["fedora-40.iso"],
            },
        })

    def test_prune_removes_only_own_obsolete_file(self):
        r = self.client.post("/api/prune", json={"distribution": "Ubuntu", "type": "linux"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["removed"], ["ubuntu-22.04.iso"], body)
        self.assertFalse((self.flat / "ubuntu-22.04.iso").exists())

    def test_other_distro_iso_survives(self):
        self.client.post("/api/prune", json={"distribution": "Ubuntu", "type": "linux"})
        self.assertTrue((self.flat / "fedora-40.iso").exists(),
                        "平铺模式下清理 Ubuntu 竟删掉了 Fedora 的 ISO —— 数据丢失")

    def test_user_owned_file_survives(self):
        self.client.post("/api/prune", json={"distribution": "Ubuntu", "type": "linux"})
        self.assertTrue((self.flat / "user-own.iso").exists(),
                        "用户自有(从未进过任何清单)的文件被删")

    def test_skipped_reports_the_kept_foreign_file(self):
        body = self.client.post("/api/prune",
                                json={"distribution": "Ubuntu", "type": "linux"}).get_json()
        self.assertIn("fedora-40.iso", " ".join(body["skipped"]), body["skipped"])

    def test_current_manifest_file_never_pruned(self):
        (self.flat / "ubuntu-26.04.iso").write_bytes(b"current")
        body = self.client.post("/api/prune",
                                json={"distribution": "Ubuntu", "type": "linux"}).get_json()
        self.assertNotIn("ubuntu-26.04.iso", body["removed"])
        self.assertTrue((self.flat / "ubuntu-26.04.iso").exists())


# --------------------------------------------------------------------- G. 平铺磁盘清单
class TestFlatDiskInventory(StorageModeBase):

    FNAME = "archlinux-2026.09.01-x86_64.iso"

    def setUp(self):
        super().setUp()
        self.set_mode("flat")
        self.flat = self.flat_dir()
        self.flat.mkdir(parents=True, exist_ok=True)
        (self.flat / self.FNAME).write_bytes(b"i" * 64)
        (self.flat / (self.FNAME + ".part")).write_bytes(b"h" * 8)
        (self.flat / "not-in-manifest.iso").write_bytes(b"x")
        self.write_manifest([
            {"distribution": "Arch", "type": "linux",
             "download_url": "https://example.org/" + self.FNAME},
        ])

    def test_manifest_file_attributed_to_its_key(self):
        names = {r["name"] for r in app.disk_inventory().get(("linux", "Arch"), [])}
        self.assertIn(self.FNAME, names)

    def test_partial_reported_under_base_name(self):
        recs = app.disk_inventory().get(("linux", "Arch"), [])
        partials = [r for r in recs if r.get("partial")]
        self.assertTrue(partials, recs)
        self.assertEqual(partials[0]["name"], self.FNAME)
        self.assertEqual(partials[0]["partial_name"], self.FNAME + ".part")

    def test_unrelated_file_not_attributed(self):
        allnames = {r["name"] for recs in app.disk_inventory().values() for r in recs}
        self.assertNotIn("not-in-manifest.iso", allnames)

    def test_classified_mode_ignores_flat_dir(self):
        """切回分类模式时, flat 目录不该被当成"某个 type 目录"而误报条目。"""
        self.set_mode("classified")
        self.assertEqual(app.disk_inventory(), {})


# --------------------------------------------------------------------- H. 平铺「下载中」状态
class TestFlatDownloadingStatus(StorageModeBase):
    """回归: 平铺模式下 .part 路径也要能匹配上, 否则正在下载的文件会显示「下载停止」。"""

    FNAME = "archlinux-2026.09.01-x86_64.iso"

    def setUp(self):
        super().setUp()
        self.set_mode("flat")
        self.flat = self.flat_dir()
        self.flat.mkdir(parents=True, exist_ok=True)
        self.local = {"name": self.FNAME, "partial": True, "size": 4096}

    def test_flat_part_path_recognised_as_downloading(self):
        part = str((self.flat / (self.FNAME + app.PART_SUFFIX)).resolve())
        status, size = app._entry_status(("linux", "Arch"), self.FNAME, self.local,
                                         {}, frozenset({part}))
        self.assertEqual(status, "downloading")
        self.assertEqual(size, 4096)

    def test_classified_form_would_not_match(self):
        """负面对照: 若 app 侧仍按 <type>/<name>/ 拼路径, 平铺模式就匹配不上。"""
        wrong = str((self.data / "linux" / "Arch" / (self.FNAME + app.PART_SUFFIX)).resolve())
        status, _ = app._entry_status(("linux", "Arch"), self.FNAME, self.local,
                                      {}, frozenset({wrong}))
        self.assertEqual(status, "partial")

    def test_build_distros_uses_flat_inventory(self):
        (self.flat / self.FNAME).write_bytes(b"i" * 64)
        self.write_manifest([
            {"distribution": "Arch", "type": "linux",
             "download_url": "https://example.org/" + self.FNAME},
        ])
        out = app.build_distros()
        entries = [e for g in out["groups"] for e in g.get("entries", [])]
        hit = [e for e in entries if e.get("distribution") == "Arch"]
        self.assertTrue(hit, "平铺模式下 build_distros 应仍能列出 Arch")
        self.assertEqual(hit[0]["status"], "downloaded", hit[0])


# --------------------------------------------------------------------- I. 种子保存路径
class TestTorrentSavePathFollowsMode(StorageModeBase):

    def _post(self, captured: list, mode: str):
        """发一条推断不出发行版的种子(用 body.distro 兜底), 捕获 savepath。"""

        class FakeQB:
            def add_torrent(self, urls, save_path=None, category=""):
                captured.append(save_path)
                return {"ok": True}

        self.set_mode(mode)
        with patch.object(app, "TORRENT_AVAILABLE", True), \
             patch.object(app, "_ensure_qb_enabled", return_value=(True, None)), \
             patch.object(app, "_qb", return_value=FakeQB()):
            return self.client.post("/api/torrent/add", json={
                "urls": ["https://example.org/UnknownThing-9.9.iso"],
                "distro": "Ubuntu", "type": "linux"})

    def test_classified_uses_type_distro_dir(self):
        cap = []
        r = self._post(cap, "classified")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(Path(cap[0]), (self.data / "linux" / "Ubuntu").resolve())

    def test_flat_uses_single_dir(self):
        cap = []
        r = self._post(cap, "flat")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(Path(cap[0]), app._flat_iso_dir())

    def test_fallback_dir_follows_mode(self):
        self.set_mode("classified")
        self.assertEqual(app._torrent_fallback_dir(), (self.data / "_torrents"))
        self.set_mode("flat")
        self.assertEqual(app._torrent_fallback_dir(), app._flat_iso_dir())


# --------------------------------------------------------------------- J. 前端契约
class TestFrontendStorageModeContract(unittest.TestCase):

    def test_i18n_keys_present(self):
        for k in ("'storageMode'", "'storageChip'", "'storageDesc'",
                  "'storClassified'", "'storFlat'", "'storActiveClassified'",
                  "'storActiveFlat'", "'storSaved'"):
            self.assertIn(k, HTML, f"缺少 i18n key {k}(UI 会显示原始 key)")

    def test_dom_anchors_present(self):
        for i in ('id="storage-mode-opts"', 'id="stor-classified"',
                  'id="stor-flat"', 'id="storage-hint"'):
            self.assertIn(i, HTML, f"缺少 DOM 锚点 {i}")

    def test_buttons_wired(self):
        self.assertIn("setStorageMode('classified')", HTML)
        self.assertIn("setStorageMode('flat')", HTML)

    def test_setter_posts_to_api(self):
        i = HTML.index("function setStorageMode(")
        body = HTML[i:i + 320]
        self.assertIn("/api/storage-mode", body)
        self.assertIn("mode:", body)

    def test_loader_reads_api_and_is_called_on_settings_load(self):
        i = HTML.index("function loadStorageMode(")
        self.assertIn("/api/storage-mode", HTML[i:i + 220])
        j = HTML.index("function loadSettings(){")
        self.assertIn("loadStorageMode()", HTML[j:j + 400],
                      "loadSettings 未接线 loadStorageMode()")

    def test_paint_toggles_both_buttons(self):
        i = HTML.index("function paintStorageMode(")
        body = HTML[i:i + 420]
        self.assertIn("stor-classified", body)
        self.assertIn("stor-flat", body)
        self.assertIn("'on'", body)

    def test_version_tag_bumped(self):
        self.assertIn(f"APP_VERSION='{VERSION}'", HTML)
        self.assertIn(f"VERSION_TAG='{VERSION}'", HTML)


# ----------------------------------------------------------------- K. 打包/文档契约
class TestPackagingStorageModeContract(unittest.TestCase):
    """环境变量 ISO_HUB_STORAGE_MODE 必须在三份 compose 与 .env.example 里都露面。

    这条不是形式主义: 三份 compose 是**手工同步**的三份副本, 历史上已经漏过一次
    (SOURCE_STRATEGY 只在其中两份里出现)。用户按文档改了 .env 却发现 compose 没透传,
    表现就是"设置里改了没用, 重启后弹回默认", 排查成本极高。
    """

    COMPOSES = ("docker-compose.yml", "docker-compose.acr.yml",
                "docker-compose.dockerhub.yml")

    def test_env_var_declared_in_all_composes(self):
        for c in self.COMPOSES:
            src = (REPO_ROOT / c).read_text(encoding="utf-8")
            self.assertIn("ISO_HUB_STORAGE_MODE=", src, f"{c} 未透传 ISO_HUB_STORAGE_MODE")
            self.assertIn("${ISO_HUB_STORAGE_MODE:-classified}", src,
                          f"{c} 的默认值不是 classified(与代码默认不一致)")

    def test_env_example_documents_it(self):
        src = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("ISO_HUB_STORAGE_MODE=classified", src,
                      ".env.example 缺少 ISO_HUB_STORAGE_MODE 默认项")

    def test_composes_still_parse(self):
        """改 YAML 最容易把缩进写坏 —— 直接解析一遍兜底。"""
        try:
            import yaml
        except ImportError:  # pragma: no cover
            self.skipTest("pyyaml 未安装")
        for c in self.COMPOSES:
            with self.subTest(compose=c):
                doc = yaml.safe_load((REPO_ROOT / c).read_text(encoding="utf-8"))
                self.assertIsInstance(doc, dict)
                svc = doc.get("services", {}).get("iso-hub", {})
                envs = svc.get("environment", [])
                self.assertIn("ISO_HUB_STORAGE_MODE=${ISO_HUB_STORAGE_MODE:-classified}", envs)


if __name__ == "__main__":
    unittest.main()
