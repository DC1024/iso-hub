#!/usr/bin/env python3
"""下载状态 (downloaded/partial/stopped/failed/none) 与登录错误精确提示 的单元测试。

覆盖:
  * disk_inventory 识别 .part / .aria2 / .!qB / .tmp 半成品并归到目标文件名下
  * _entry_status 六种判定路径(完整/半成品/失败记录 hard/stopped/无记录)
  * build_distros 输出 status + partial_size 字段
  * 登录: 用户名不存在 -> no_user; 密码错误 -> bad_pass; 正确 -> ok
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402


class BaselineTestBase(unittest.TestCase):
    """搭好临时 DATA_DIR; 不启动 Flask, 直接调函数。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        (self.data / "linux" / "Ubuntu").mkdir(parents=True)
        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.data / "distributions.json"),
            patch.object(app, "SETTINGS_JSON", self.data / "settings.json"),
            patch.object(app, "FAILURES_JSON", self.data / "download_failures.json"),
            patch.object(app, "AUTO_SYNC_LAST", {"t": 0}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def write_failures(self, data: dict):
        (self.data / "download_failures.json").write_text(
            json.dumps(data), encoding="utf-8")


class TestPartialDetection(BaselineTestBase):
    """半成品文件识别。"""

    def test_part_suffix_maps_to_target(self):
        """xxx.iso.part 应归到 xxx.iso 名下并标 partial。"""
        (self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso.part").write_bytes(b"x" * 500)
        inv = app.disk_inventory()
        files = inv[("linux", "Ubuntu")]
        hit = [f for f in files if f["name"] == "ubuntu-26.04.iso"]
        self.assertEqual(len(hit), 1, "半成品应归到目标文件名下")
        self.assertTrue(hit[0]["partial"])
        self.assertEqual(hit[0]["size"], 500)

    def test_aria2_suffix_recognised(self):
        (self.data / "linux" / "Ubuntu" / "f.iso.aria2").write_bytes(b"x" * 10)
        names = [f["name"] for f in app.disk_inventory()[("linux", "Ubuntu")]]
        self.assertIn("f.iso", names)

    def test_qb_suffix_recognised(self):
        (self.data / "linux" / "Ubuntu" / "f.iso.!qB").write_bytes(b"x" * 10)
        names = [f["name"] for f in app.disk_inventory()[("linux", "Ubuntu")]]
        self.assertIn("f.iso", names)

    def test_tmp_suffix_recognised(self):
        (self.data / "linux" / "Ubuntu" / "f.iso.tmp").write_bytes(b"x" * 10)
        names = [f["name"] for f in app.disk_inventory()[("linux", "Ubuntu")]]
        self.assertIn("f.iso", names)

    def test_normal_file_not_partial(self):
        (self.data / "linux" / "Ubuntu" / "ok.iso").write_bytes(b"x" * 10)
        files = app.disk_inventory()[("linux", "Ubuntu")]
        hit = [f for f in files if f["name"] == "ok.iso"]
        self.assertEqual(len(hit), 1)
        self.assertFalse(hit[0]["partial"])

    def test_part_file_not_listed_under_raw_name(self):
        """回归: .part 不得以原始名(带 .part)出现在清单里。

        否则 build_distros 会把 xxx.iso.part 当成"不在最新清单元数据中"的
        过期文件(stray), UI 显示「通常已被更新淘汰的旧版 ISO」并给出
        「清理过期」按钮 —— 而它其实是正在下载、可续传的半成品。
        """
        (self.data / "linux" / "Ubuntu" / "a.iso.part").write_bytes(b"x" * 500)
        names = [f["name"] for f in app.disk_inventory()[("linux", "Ubuntu")]]
        self.assertIn("a.iso", names, "应以目标名出现")
        self.assertNotIn("a.iso.part", names, "不得以带 .part 的原始名出现")

    def test_part_file_not_in_stray_files(self):
        """回归: 半成品不能进 stray_files(它对应清单里的文件, 不是过期文件)。"""
        (self.data / "distributions.json").write_text(json.dumps({
            "updated_at": 0,
            "distributions": [{"distribution": "Ubuntu", "type": "linux",
                               "download_url": "https://example.com/a.iso"}],
        }), encoding="utf-8")
        (self.data / "linux" / "Ubuntu" / "a.iso.part").write_bytes(b"x" * 500)
        g = next(g for g in app.build_distros()["groups"] if g["name"] == "Ubuntu")
        stray_names = [f["name"] for f in g["stray_files"]]
        self.assertEqual(stray_names, [], f"半成品不应算过期文件, 实际: {stray_names}")
        self.assertEqual(g["entries"][0]["status"], "partial")

    def test_genuine_stray_still_detected(self):
        """确实不在清单里的完整文件仍要报 stray(不能因修 bug 而漏报)。"""
        (self.data / "distributions.json").write_text(json.dumps({
            "updated_at": 0,
            "distributions": [{"distribution": "Ubuntu", "type": "linux",
                               "download_url": "https://example.com/a.iso"}],
        }), encoding="utf-8")
        (self.data / "linux" / "Ubuntu" / "old-2020.iso").write_bytes(b"x" * 100)
        g = next(g for g in app.build_distros()["groups"] if g["name"] == "Ubuntu")
        self.assertEqual([f["name"] for f in g["stray_files"]], ["old-2020.iso"])

    def test_local_total_counts_physical_file_once(self):
        """半成品与其同名完整文件并存时, local_total 不应重复计数。"""
        (self.data / "linux" / "Ubuntu" / "a.iso").write_bytes(b"x" * 100)
        (self.data / "linux" / "Ubuntu" / "a.iso.part").write_bytes(b"x" * 30)
        (self.data / "distributions.json").write_text(json.dumps({
            "updated_at": 0,
            "distributions": [{"distribution": "Ubuntu", "type": "linux",
                               "download_url": "https://example.com/a.iso"}],
        }), encoding="utf-8")
        g = next(g for g in app.build_distros()["groups"] if g["name"] == "Ubuntu")
        self.assertEqual(g["local_total"], 130, "两个物理文件应各算一次(100+30)")

    def test_non_partial_suffix_ignored(self):
        """没有半成品后缀的文件不会被误判。"""
        self.assertEqual(app._partial_base_name("ubuntu-26.04.iso"), "")
        self.assertEqual(app._partial_base_name("readme.txt"), "")

    def test_case_insensitive_suffix(self):
        """后缀大小写不敏感(.PART / .Part 都认)。"""
        self.assertEqual(app._partial_base_name("a.iso.PART"), "a.iso")
        self.assertEqual(app._partial_base_name("a.iso.Part"), "a.iso")


class TestEntryStatus(BaselineTestBase):
    """_entry_status 判定矩阵。"""

    KEY = ("linux", "Ubuntu")

    def test_complete_file_is_downloaded(self):
        st, ps = app._entry_status(self.KEY, "a.iso", {"size": 100, "partial": False}, {})
        self.assertEqual(st, "downloaded")
        self.assertEqual(ps, 0)

    def test_partial_file_is_partial(self):
        st, ps = app._entry_status(self.KEY, "a.iso", {"size": 42, "partial": True}, {})
        self.assertEqual(st, "partial")
        self.assertEqual(ps, 42)

    def test_partial_beats_failure_record(self):
        """半成品存在时, 无论历史记录是什么都算可续传(partial)。"""
        fails = {"linux/Ubuntu/a.iso": {"at": 1, "kind": "hard"}}
        st, _ = app._entry_status(self.KEY, "a.iso", {"size": 42, "partial": True}, fails)
        self.assertEqual(st, "partial")

    def test_no_file_with_hard_failure_is_failed(self):
        fails = {"linux/Ubuntu/a.iso": {"at": 1, "kind": "hard"}}
        st, ps = app._entry_status(self.KEY, "a.iso", None, fails)
        self.assertEqual(st, "failed")
        self.assertEqual(ps, 0)

    def test_no_file_with_stopped_failure_is_stopped(self):
        fails = {"linux/Ubuntu/a.iso": {"at": 1, "kind": "stopped"}}
        st, _ = app._entry_status(self.KEY, "a.iso", None, fails)
        self.assertEqual(st, "stopped")

    def test_no_file_no_record_is_none(self):
        st, _ = app._entry_status(self.KEY, "a.iso", None, {})
        self.assertEqual(st, "none")

    def test_rel_key_must_match_type_and_name(self):
        """失败记录的 key 是 type/name/文件名, 不匹配则不生效。"""
        fails = {"linux/Other/a.iso": {"at": 1, "kind": "hard"}}
        st, _ = app._entry_status(self.KEY, "a.iso", None, fails)
        self.assertEqual(st, "none")


class TestBuildDistrosStatus(BaselineTestBase):
    """build_distros 端到端输出 status 字段。"""

    def _manifest(self, *fnames):
        (self.data / "distributions.json").write_text(json.dumps({
            "updated_at": 0,
            "distributions": [
                {"distribution": "Ubuntu", "type": "linux",
                 "download_url": f"https://example.com/{fn}"} for fn in fnames
            ],
        }), encoding="utf-8")

    def _statuses(self):
        d = app.build_distros()
        g = next(g for g in d["groups"] if g["name"] == "Ubuntu")
        return {e["filename"]: e for e in g["entries"]}

    def test_downloaded_entry(self):
        self._manifest("full.iso")
        (self.data / "linux" / "Ubuntu" / "full.iso").write_bytes(b"x" * 100)
        e = self._statuses()["full.iso"]
        self.assertEqual(e["status"], "downloaded")
        self.assertEqual(e["local_size"], 100)

    def test_partial_entry_reports_partial_size(self):
        self._manifest("half.iso")
        (self.data / "linux" / "Ubuntu" / "half.iso.part").write_bytes(b"x" * 30)
        e = self._statuses()["half.iso"]
        self.assertEqual(e["status"], "partial")
        self.assertEqual(e["partial_size"], 30)
        self.assertEqual(e["local_size"], 30, "兼容: local_size 也反映半成品大小")

    def test_failed_entry(self):
        self._manifest("bad.iso")
        self.write_failures({"linux/Ubuntu/bad.iso": {"at": 1, "kind": "hard"}})
        self.assertEqual(self._statuses()["bad.iso"]["status"], "failed")

    def test_stopped_entry(self):
        self._manifest("stop.iso")
        self.write_failures({"linux/Ubuntu/stop.iso": {"at": 1, "kind": "stopped"}})
        self.assertEqual(self._statuses()["stop.iso"]["status"], "stopped")

    def test_never_downloaded_entry(self):
        self._manifest("new.iso")
        e = self._statuses()["new.iso"]
        self.assertEqual(e["status"], "none")
        self.assertEqual(e["local_size"], 0)

    def test_complete_file_wins_over_partial_leftover(self):
        """完整文件比残留半成品新 → 已下载(.part 是过期残留, 已被 rename 覆盖)。"""
        self._manifest("both.iso")
        (self.data / "linux" / "Ubuntu" / "both.iso.part").write_bytes(b"x" * 7)
        (self.data / "linux" / "Ubuntu" / "both.iso").write_bytes(b"x" * 100)
        # 让完整文件的 mtime 明确晚于 .part
        import os as _os
        t = (self.data / "linux" / "Ubuntu" / "both.iso.part").stat().st_mtime
        _os.utime(self.data / "linux" / "Ubuntu" / "both.iso", (t + 10, t + 10))
        self.assertEqual(self._statuses()["both.iso"]["status"], "downloaded")

    def test_part_newer_than_stale_file_is_stopped(self):
        """半成品比完整文件新 → 下载被中断, 应显示「下载停止」而非「已下载」。

        回归用例: 手动点「停止任务」后, iso_runner 留下 xxx.iso.part;
        若目录里恰好还有一份旧的同名 xxx.iso(上次残留), 旧实现固定优先完整文件,
        会把残缺文件误判为已下载。
        """
        self._manifest("both.iso")
        (self.data / "linux" / "Ubuntu" / "both.iso").write_bytes(b"x" * 100)
        (self.data / "linux" / "Ubuntu" / "both.iso.part").write_bytes(b"x" * 7)
        import os as _os
        t = (self.data / "linux" / "Ubuntu" / "both.iso").stat().st_mtime
        _os.utime(self.data / "linux" / "Ubuntu" / "both.iso.part", (t + 10, t + 10))
        e = self._statuses()["both.iso"]
        self.assertEqual(e["status"], "partial")
        self.assertEqual(e["partial_size"], 7)

    def test_stopped_download_never_reported_as_downloaded(self):
        """核心回归: 只有 .part 存在(无完整文件)时绝不能是 downloaded/stopped 以外的态。"""
        self._manifest("cut.iso")
        (self.data / "linux" / "Ubuntu" / "cut.iso.part").write_bytes(b"x" * 61825024)
        e = self._statuses()["cut.iso"]
        self.assertEqual(e["status"], "partial")
        self.assertNotEqual(e["status"], "downloaded")
        self.assertEqual(e["partial_size"], 61825024)


class TestFailuresPersistence(BaselineTestBase):
    """失败记录的写/读/清。"""

    def test_write_then_load(self):
        app.write_fail_record("linux/Ubuntu/a.iso", "hard")
        self.assertEqual(app.load_failures()["linux/Ubuntu/a.iso"]["kind"], "hard")

    def test_write_default_kind_is_hard(self):
        app.write_fail_record("linux/Ubuntu/a.iso")
        self.assertEqual(app.load_failures()["linux/Ubuntu/a.iso"]["kind"], "hard")

    def test_clear_removes_entry(self):
        app.write_fail_record("linux/Ubuntu/a.iso", "stopped")
        app.clear_failure("linux/Ubuntu/a.iso")
        self.assertNotIn("linux/Ubuntu/a.iso", app.load_failures())

    def test_clear_missing_is_noop(self):
        """清除不存在的记录不应抛错。"""
        app.clear_failure("linux/Ubuntu/nope.iso")
        self.assertEqual(app.load_failures(), {})

    def test_write_is_merge_not_overwrite(self):
        """多次写入不同文件应累积, 不互相覆盖。"""
        app.write_fail_record("linux/Ubuntu/a.iso", "hard")
        app.write_fail_record("linux/Ubuntu/b.iso", "stopped")
        data = app.load_failures()
        self.assertEqual(len(data), 2)

    def test_load_corrupt_file_returns_empty(self):
        (self.data / "download_failures.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(app.load_failures(), {})


class TestVerifyLogin(BaselineTestBase):
    """登录错误精确区分: 用户名不存在 vs 密码错误。"""

    def setUp(self):
        super().setUp()
        import secrets as _secrets
        salt = _secrets.token_hex(8)
        users = {"alice": {"salt": salt, "password_hash": app._hash_pw("correct-pw", salt),
                           "created_at": 0}}
        (self.data / "settings.json").write_text(
            json.dumps({"users": users}), encoding="utf-8")
        self.client = app.app.test_client()
        self._p2 = [
            patch.object(app, "REQUIRE_LOGIN", False),
            patch.object(app, "AUTH_TOKEN", ""),
        ]
        for p in self._p2:
            p.start()

    def tearDown(self):
        for p in self._p2:
            p.stop()
        super().tearDown()

    def test_correct_credentials_ok(self):
        self.assertEqual(app._verify_login("alice", "correct-pw"), "ok")

    def test_unknown_user_reports_no_user(self):
        self.assertEqual(app._verify_login("bob", "whatever"), "no_user")

    def test_wrong_password_reports_bad_pass(self):
        self.assertEqual(app._verify_login("alice", "wrong"), "bad_pass")

    def test_empty_password_reports_bad_pass(self):
        self.assertEqual(app._verify_login("alice", ""), "bad_pass")

    def test_check_login_bool_wrapper(self):
        """_check_login 仍作为布尔包装可用(其它调用点不破)。"""
        self.assertTrue(app._check_login("alice", "correct-pw"))
        self.assertFalse(app._check_login("alice", "wrong"))
        self.assertFalse(app._check_login("bob", "x"))

    def test_login_api_unknown_user_message(self):
        r = self.client.post("/api/user/login", json={"username": "bob", "password": "x"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["code"], "no_user")
        self.assertIn("用户名不存在", r.get_json()["error"])

    def test_login_api_wrong_password_message(self):
        r = self.client.post("/api/user/login", json={"username": "alice", "password": "x"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["code"], "bad_pass")
        self.assertIn("密码错误", r.get_json()["error"])

    def test_login_api_success(self):
        r = self.client.post("/api/user/login",
                             json={"username": "alice", "password": "correct-pw"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(r.get_json()["token"])


class TestFrontendWiring(unittest.TestCase):
    """前端静态断言: 保证 UI 与后端契约一致。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_status_badges_rendered(self):
        """行渲染必须输出 data-status 供筛选使用。"""
        self.assertIn("data-status=", self.html)

    def test_filter_supports_incomplete(self):
        """筛选必须支持「下载失败/停止」。"""
        self.assertIn("dl_incomplete", self.html)

    def test_delete_no_longer_sends_force(self):
        """手动删除接口不得再传 force。"""
        self.assertNotIn("items,force}", self.html)
        self.assertNotIn('"force":true', self.html)

    def test_lock_blocks_delete_before_request(self):
        """锁定文件应在发请求前就被拦下并提示先解锁。"""
        self.assertIn("delLockedBlocked", self.html)
        self.assertIn("delLockedHint", self.html)

    def test_login_error_mapping_exists(self):
        self.assertIn("function loginErrMsg", self.html)
        self.assertIn("loginNoUser", self.html)
        self.assertIn("loginBadPass", self.html)

    def test_jpost_passes_login_401_through(self):
        """回归: jpost 不得把 /api/user/login 的 401 替换成笼统的"请先登录后再操作"。

        曾出现的问题: jpost 对任何 401 都返回 {error: t('pleaseLogin')}, 把登录接口
        返回的 code/error(用户名不存在 / 密码错误) 吞掉了, 导致用户永远只看到
        "请先登录后再操作"。必须存在白名单让登录接口的 401 原样透传。
        """
        self.assertIn("AUTH_ENDPOINTS", self.html)
        self.assertIn("'/api/user/login'", self.html)
        # jpost/jget 的 401 分支必须带上白名单判断, 不能无条件改写。
        # jpost 是单行实现; jget 自 v1.2.8 起改为多行, 需按函数体边界取。
        jpost_line = next((l for l in self.html.splitlines()
                           if l.startswith("async function jpost(")), "")
        self.assertTrue(jpost_line, "未找到 jpost")
        self.assertIn("AUTH_ENDPOINTS.includes(u)", jpost_line,
                      "jpost 的 401 分支缺少登录接口白名单判断")

        i = self.html.index("async function jget(u){")
        jget_body = self.html[i:self.html.index("\n}", i)]
        self.assertIn("AUTH_ENDPOINTS.includes(u)", jget_body,
                      "jget 的 401 分支缺少登录接口白名单判断")

    def test_version_bumped(self):
        self.assertIn("APP_VERSION='1.3.17'", self.html)

    def test_poll_refreshes_list_while_running(self):
        """回归: 任务运行期间也要刷新列表。

        旧实现只在"任务结束"那一刻 loadDistros(), 导致用户下载中途看列表时
        状态还是旧的(例如仍是「下载停止」), 误以为重新下载没生效。
        """
        self.assertIn("__lastDistroRefresh", self.html)
        # 该刷新必须出现在 RUNNING 分支内(return 之前), 否则任务运行中不会触发
        i_running = self.html.index("if(RUNNING&&s&&s.task){")
        i_refresh = self.html.index("__lastDistroRefresh")
        # v1.2.8: 轮询结束后的刷新改为 silent(避免与主提示叠加), 断言只认前缀
        i_wasrunning = self.html.index("if(wasRunning)loadDistros(")
        self.assertTrue(i_running < i_refresh < i_wasrunning,
                        "运行中刷新列表的逻辑必须在 RUNNING 分支内")

    def test_delete_sel_accepts_partial_states(self):
        """v1.2.7 回归: 「删除所选」不能只认完整文件。

        半成品(.part)/下载停止/下载失败 同样占着磁盘, 用户勾选删除时若被前端
        guard 过滤掉, 就会落到后端报「文件不存在」-> 出现
        「已删除 0 个文件 · 1 跳过」。
        """
        i = self.html.index("async function deleteSel()")
        body = self.html[i:i + 1200]
        self.assertIn("partial_size", body,
                      "deleteSel 的 guard 必须把半成品算作本地已有数据")
        for st in ("'partial'", "'stopped'", "'failed'"):
            self.assertIn(st, body, f"deleteSel 应接受 {st} 状态")

    def test_row_delete_button_shown_for_stopped_and_failed(self):
        """v1.2.7 回归: 下载停止/失败的行也应显示行内删除按钮。"""
        marker = 'class="btn-d btn-rowdel"'
        i = self.html.index(marker)
        line = self.html[max(0, i - 200):i]
        self.assertIn("isOk||isStop||isFail", line,
                      "行内删除按钮的显示条件应包含下载停止/失败")

    def test_delete_nothing_hint_i18n_exists(self):
        """v1.2.7: 「一个都没删掉」的提示文案必须真实存在, 否则 UI 显示原始 key。"""
        self.assertIn("'delNothingRemoved'", self.html)
        self.assertIn("t('delNothingRemoved')", self.html)


class TestRefreshListFeedback(unittest.TestCase):
    """v1.2.8 回归: 「刷新列表」必须给用户反馈, 且失败不能被静默吞掉。

    Bug 背景: 用户报「点了没反应」。排除了鉴权/请求失败(用户已登录且下载正常)后
    定位为**缺反馈**: loadDistros() 成功时不弹任何提示, 数据无变化时重绘结果与
    当前画面完全一致, 用户无从判断点击是否被受理。
    同时发现 jget() 的 401 分支无条件 `return {}`, 把失败信号也一并吞了——
    这次不是它导致的, 但同属"静默"隐患, 一并修掉。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_button_has_id_for_loading_state(self):
        """刷新按钮必须有 id, 才能挂 loading 态。"""
        self.assertIn('id="btnRefresh"', self.html)
        self.assertIn("onclick=\"loadDistros()\"", self.html)

    def test_loaddistros_toasts_on_success(self):
        """成功刷新必须提示, 否则用户看到"点了没反应"。"""
        i = self.html.index("async function loadDistros(opts){")
        body = self.html[i:i + 1400]
        self.assertIn("t('listRefreshed')", body,
                      "loadDistros 成功时应 toast 列表已刷新")

    def test_loaddistros_reports_failure(self):
        """请求失败时必须有错误提示, 不能静默渲染空列表。"""
        i = self.html.index("async function loadDistros(opts){")
        body = self.html[i:i + 1400]
        self.assertIn("LAST_GET_FAILED", body, "loadDistros 应检查 jget 的失败标记")
        self.assertIn("t('refreshFailed')", body, "失败时应提示刷新失败")
        self.assertIn("t('pleaseLogin')", body, "401 时应提示请先登录")

    def test_loaddistros_has_loading_state(self):
        """点击后应立刻有 loading 视觉反馈。"""
        i = self.html.index("async function loadDistros(opts){")
        body = self.html[i:i + 1400]
        self.assertIn("btnRefresh", body)
        self.assertIn("classList.add('loading')", body)
        self.assertIn("classList.remove('loading')", body)
        self.assertIn(".btn-s.loading", self.html)

    def test_jget_exposes_failure_flag(self):
        """jget 不得再无条件静默 return {}; 必须把失败写进 LAST_GET_FAILED。"""
        i = self.html.index("async function jget(u){")
        body = self.html[i:i + 600]
        self.assertIn("LAST_GET_FAILED=null", body, "每次调用先清空标记")
        self.assertIn("LAST_GET_FAILED={status:r.status", body, "失败时写入标记")
        # 结构必须保持 {} 以兼容其余 20+ 个调用点
        self.assertIn("return{}", body, "仍需返回 {} 以兼容既有调用方")

    def test_incidental_refreshes_are_silent(self):
        """顺带刷新(删除后/轮询结束等)不应重复弹成功提示, 必须传 silent。"""
        self.assertIn("loadDistros({silent:true})", self.html)
        # 显式按钮点击必须非 silent, 否则又变回"没反应"
        self.assertIn('onclick="loadDistros()"', self.html)

    def test_refresh_i18n_keys_exist(self):
        """v1.2.8 新增文案必须真实存在于字典, 否则 UI 显示原始 key。"""
        for k in ("'listRefreshed'", "'refreshFailed'"):
            self.assertIn(k, self.html, f"缺少 i18n key {k}")
            self.assertIn(f"t({k})", self.html, f"{k} 未被引用")


if __name__ == "__main__":
    unittest.main()
