#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""种子分类测试 —— 锁死 v1.3 规格 §六 的用例。

本文件守卫的是**架构决定**, 不只是某段逻辑:
  1. 未匹配条目必须进「未分类」, 绝不丢条目(用例 5)
  2. load_categories() 每次实时读盘, 不得模块级缓存(用例 8, 配变异体 2)
  3. fold_key 由后端下发且不含本地化文本/计数(用例 11)
  4. 配置损坏必须可见(warnings), 不能静默变空(用例 14)
  5. 未分类恒排最后(用例 15)
  6. classify 全程无网络(用例 10)

另有一条"反例守卫": v1.2 的规格曾用 `src` 误伤 `description` 举例, 实测
`'src' in 'description'` 为 False —— 真正的反例是 `source` / `resource`。
"""

import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import torrent_categories as tcats  # noqa: E402
import web.app as web_app  # noqa: E402

BUILTIN_JSON = REPO_ROOT / "iso_download" / "torrent_categories.json"


def _preset_cats() -> list:
    return json.loads(BUILTIN_JSON.read_text(encoding="utf-8"))["categories"]


def _item(title: str, **kw) -> dict:
    base = {"title": title, "url": "https://example.test/%s" % title,
            "pubDate": "", "source": "test", "builtin": True}
    base.update(kw)
    return base


class TestMatches(unittest.TestCase):
    """两种匹配语义(规格 §3.3): 关键词求召回, 排除词求精确。"""

    # ---- 排除词: 完整词匹配 ----
    def test_word_match_avoids_false_positive(self):
        """真反例是 source/resource(src/description 那个例子本身是假的)。"""
        self.assertFalse(tcats._word_match("a resource file", ["source"]))
        self.assertFalse(tcats._word_match("srcfoo bar", ["src"]))
        self.assertTrue(tcats._word_match("debian source dvd", ["source"]))

    def test_src_does_not_match_description(self):
        """回归守卫: 规格里那个例子本身就错了, 但结论(不该误伤)要守住。"""
        self.assertNotIn("src", "description")
        self.assertFalse(tcats._word_match("description of x", ["src"]))

    def test_word_match_still_hits_real_src_entry(self):
        self.assertTrue(tcats._word_match("ubuntu-24.04-src.iso", ["src"]))

    # ---- 关键词: 词首匹配 ----
    def test_prefix_match_covers_derivatives(self):
        """关键词必须覆盖单 token 派生版 —— 实测踩过的回归。"""
        for title in ("ubuntukylin-24.04.5-desktop-amd64.iso.torrent",
                      "ubuntucinnamon-24.04.5-desktop-amd64.iso.torrent",
                      "ubuntustudio-26.04.1-desktop-amd64.iso.torrent",
                      "ubuntu-24.04.5-desktop-amd64.iso.torrent",
                      "archlinux-2026.09.01-x86_64.iso.torrent"):
            self.assertTrue(tcats._prefix_match(title, ["ubuntu", "archlinux"]),
                            title)

    def test_prefix_match_still_blocks_midword_false_positive(self):
        """词首匹配不等于裸子串: 中间命中的仍要挡住。"""
        self.assertFalse(tcats._prefix_match("search engine", ["arch"]))
        self.assertFalse(tcats._prefix_match("research data", ["arch"]))
        self.assertFalse(tcats._prefix_match("resource page", ["source"]))
        self.assertFalse(tcats._prefix_match("kubuntu-24.04.iso", ["ubuntu"]))

    def test_hyphenated_keyword(self):
        """连字符关键词要能命中真实命名。"""
        self.assertTrue(tcats._prefix_match("proxmox-ve_8.1-1.iso.torrent", ["proxmox-ve"]))
        self.assertTrue(tcats._prefix_match("arch-linux-2026.09.01.iso", ["arch-linux"]))

    def test_degenerate_input(self):
        for fn in (tcats._prefix_match, tcats._word_match):
            self.assertFalse(fn("anything", []))
            self.assertFalse(fn("anything", ["", None, 123]))

    def test_case_insensitive_keyword(self):
        self.assertTrue(tcats._prefix_match("fedora workstation 40", ["FEDORA"]))
        self.assertTrue(tcats._word_match("fedora source", ["SOURCE"]))


class TestClassifyOne(unittest.TestCase):
    """单条分类与冲突裁决(用例 1、2、4)。"""

    def setUp(self):
        self.cats = _preset_cats()

    def test_keyword_hit(self):
        """用例 1。"""
        cid, reason = tcats.classify_one("ubuntu-24.04.5-desktop-amd64.iso.torrent",
                                         self.cats)
        self.assertEqual(cid, "ubuntu")
        self.assertEqual(reason, "keyword")

    def test_exclude_wins_over_keyword(self):
        """用例 2: cloudimg 不该进 ubuntu。"""
        cid, _ = tcats.classify_one("ubuntu-24.04-cloudimg-amd64.img", self.cats)
        self.assertEqual(cid, tcats.UNCATEGORIZED)

    def test_longest_keyword_wins(self):
        """用例 4: kubuntu 比 ubuntu 长, 应胜出。"""
        cid, _ = tcats.classify_one("kubuntu-24.04.5-desktop-amd64.iso.torrent",
                                    self.cats)
        self.assertEqual(cid, "ubuntu")     # 同分类, 但裁决走的是最长关键词分支

    def test_longest_keyword_across_categories(self):
        """跨分类裁决: 命中词更长的分类胜出。"""
        cats = [
            {"id": "generic", "order": 1, "keywords": ["linux"], "exclude": []},
            {"id": "specific", "order": 2, "keywords": ["endeavouros"], "exclude": []},
        ]
        cid, _ = tcats.classify_one("EndeavourOS_Titan-Nova-2026.08.15.iso.torrent", cats)
        self.assertEqual(cid, "specific")

    def test_disabled_category_skipped(self):
        cats = [dict(c) for c in self.cats]
        for c in cats:
            if c["id"] == "ubuntu":
                c["enabled"] = False
        cid, _ = tcats.classify_one("ubuntu-24.04-desktop-amd64.iso.torrent", cats)
        self.assertEqual(cid, tcats.UNCATEGORIZED)

    def test_derivative_distros_hit(self):
        """关键词已按现网标题校准: 派生版不该全落未分类。"""
        cases = {
            "TUXEDO-OS-202609101011.iso.torrent": "ubuntu",
            "EndeavourOS_Titan-Nova-2026.08.15.iso.torrent": "arch",
            "butterbian-xfce-0.4.1-trixie-20260831.torrent": "debian",
            "proxmox-ve_8.1-1.iso.torrent": "proxmox",
        }
        for title, want in cases.items():
            cid, _ = tcats.classify_one(title, self.cats)
            self.assertEqual(cid, want, "%s 应归入 %s, 实际 %s" % (title, want, cid))

    def test_ubuntu_flavor_family_not_split(self):
        """真实回归: 统一成整词匹配后 ubuntukylin/ubuntucinnamon/ubuntustudio
        曾整批掉进未分类。Ubuntu 家族必须整组归入 ubuntu。
        """
        flavors = ["ubuntu", "kubuntu", "xubuntu", "lubuntu", "edubuntu",
                   "ubuntukylin", "ubuntucinnamon", "ubuntustudio",
                   "ubuntu-mate", "ubuntu-budgie", "ubuntu-unity"]
        for f in flavors:
            cid, _ = tcats.classify_one("%s-24.04.5-desktop-amd64.iso.torrent" % f,
                                        self.cats)
            self.assertEqual(cid, "ubuntu", "%s 掉出了 ubuntu 分类" % f)


class TestClassify(unittest.TestCase):
    """分组结果契约(用例 5、11、15)。"""

    def setUp(self):
        self.cats = _preset_cats()

    def test_unmatched_goes_to_uncategorized(self):
        """用例 5: 绝不丢条目。"""
        items = [_item("haiku-r1beta6-x86_64.torrent"), _item("NetBSD-9.5-amd64.iso.torrent")]
        res = tcats.classify(items, self.cats)
        self.assertEqual(res["counts"][tcats.UNCATEGORIZED], 2)
        non_empty = [c for c in res["categories"] if c["items"]]
        self.assertEqual([c["id"] for c in non_empty], [tcats.UNCATEGORIZED])

    def test_no_item_lost(self):
        """总条目数必须守恒(含畸形条目被跳过的边界)。"""
        items = [_item("ubuntu-24.04.iso.torrent"), _item("weird thing.torrent"),
                 _item("debian-13.iso.torrent")]
        res = tcats.classify(items, self.cats)
        self.assertEqual(sum(res["counts"].values()), 3)

    def test_uncategorized_always_last(self):
        """用例 15。"""
        cats = [dict(c) for c in self.cats]
        for c in cats:                       # 把未分类之外的最大 order 拉到极大值
            if c["id"] == "ubuntu":
                c["order"] = 10 ** 5
        items = [_item("no-match-at-all.torrent"), _item("ubuntu-24.04.iso.torrent")]
        res = tcats.classify(items, cats)
        self.assertEqual(res["categories"][-1]["id"], tcats.UNCATEGORIZED)

    def test_fold_key_contract(self):
        """用例 11: fold_key = "tcat:" + id, 且不含 name/计数/本地化文本。

        注意: 只断言不含"本地化文本与计数" —— 用户分类 id 形如 user_a1b2c3
        本身含数字, 所以"不含数字"是错的(那是 v1.2 规格里的自相矛盾)。
        """
        items = [_item("ubuntu-24.04.iso.torrent"), _item("什么都不匹配.torrent")]
        res = tcats.classify(items, self.cats)
        self.assertTrue(res["categories"])
        for c in res["categories"]:
            self.assertEqual(c["fold_key"], "tcat:" + c["id"])
            self.assertNotIn(c["name"]["zh"], c["fold_key"])
            self.assertNotIn(c["name"]["en"], c["fold_key"])
            self.assertNotIn(str(len(c["items"])), c["fold_key"].split(":", 1)[1])

    def test_empty_categories_still_returned(self):
        """空分类也要返回(由前端按 show_empty 决定显示与否)。"""
        res = tcats.classify([_item("ubuntu-24.04.iso.torrent")], self.cats)
        ids = [c["id"] for c in res["categories"]]
        self.assertIn("fedora", ids)
        self.assertEqual(res["counts"]["fedora"], 0)

    def test_classify_drops_contentless_items(self):
        """非 dict / 无 title 无 url 的条目丢弃, 不造空标题幽灵行。

        注意与用例 5 的区别: "绝不丢条目"针对**真实条目**;
        连 title 和 url 都没有的东西不是条目。
        """
        res = tcats.classify([None, "str", 42, {}, {"title": "   "},
                              {"url": "https://x.test/ubuntu-24.04.iso.torrent"}], self.cats)
        self.assertEqual(sum(res["counts"].values()), 1)
        self.assertEqual(res["counts"]["ubuntu"], 1)

    def test_counts_match_bucket_sizes(self):
        items = [_item("ubuntu-24.04.iso.torrent"), _item("kubuntu-24.04.iso.torrent"),
                 _item("unknown-thing.torrent")]
        res = tcats.classify(items, self.cats)
        for c in res["categories"]:
            self.assertEqual(res["counts"][c["id"]], len(c["items"]))

    def test_item_fields_preserved(self):
        """builtin 必须透传(规格 §2.3)。"""
        res = tcats.classify([_item("ubuntu-24.04.iso.torrent", builtin=False)],
                             self.cats)
        got = [c for c in res["categories"] if c["id"] == "ubuntu"][0]["items"][0]
        self.assertFalse(got["builtin"])
        self.assertEqual(sorted(got), ["builtin", "pubDate", "source", "title", "url"])


class TestLoadCategories(unittest.TestCase):
    """配置读取与合并(用例 6、7、8、10、14)。"""

    def _write(self, path: Path, doc: dict) -> None:
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def test_merge_user_category(self):
        """用例 6。"""
        with tempfile.TemporaryDirectory() as d:
            user = Path(d) / "uc.json"
            self._write(user, {"user_categories": [
                {"id": "user_a1b2c3", "name": {"zh": "我的 NAS 工具盘", "en": "My NAS"},
                 "enabled": True, "keywords": ["truenas", "unraid"], "exclude": [],
                 "order": 100}]})
            with patch.object(tcats, "PRESET_JSON", BUILTIN_JSON), \
                 patch.object(tcats, "USER_JSON", user):
                cats = tcats.load_categories()
                ids = [c["id"] for c in cats]
                self.assertIn("user_a1b2c3", ids)
                cid, _ = tcats.classify_one("truenas-scale-24.04.iso.torrent", cats)
                self.assertEqual(cid, "user_a1b2c3")

    def test_override_disables_preset(self):
        """用例 7。"""
        with tempfile.TemporaryDirectory() as d:
            user = Path(d) / "uc.json"
            self._write(user, {"overrides": {"tools": {"enabled": False}}})
            with patch.object(tcats, "PRESET_JSON", BUILTIN_JSON), \
                 patch.object(tcats, "USER_JSON", user):
                cats = tcats.load_categories()
                cid, _ = tcats.classify_one("clonezilla-live-3.1.0-amd64.iso.torrent", cats)
                self.assertEqual(cid, tcats.UNCATEGORIZED)

    def test_override_cannot_add_new_category(self):
        with tempfile.TemporaryDirectory() as d:
            user = Path(d) / "uc.json"
            self._write(user, {"overrides": {"ghost": {"enabled": True}}})
            with patch.object(tcats, "PRESET_JSON", BUILTIN_JSON), \
                 patch.object(tcats, "USER_JSON", user):
                self.assertNotIn("ghost", [c["id"] for c in tcats.load_categories()])

    def test_rule_change_takes_effect_immediately(self):
        """用例 8 —— 变异体 2 的守卫。

        配置改完之后**再次调用 load_categories()** 必须拿到新规则。
        若有人在 load_categories 里加了模块级缓存, 这条立刻变红。
        """
        with tempfile.TemporaryDirectory() as d:
            user = Path(d) / "uc.json"
            self._write(user, {})
            with patch.object(tcats, "PRESET_JSON", BUILTIN_JSON), \
                 patch.object(tcats, "USER_JSON", user):
                before = tcats.load_categories()
                cid, _ = tcats.classify_one("truenas-scale-24.04.iso.torrent", before)
                self.assertEqual(cid, tcats.UNCATEGORIZED)

                self._write(user, {"user_categories": [
                    {"id": "user_x", "name": {"zh": "NAS", "en": "NAS"},
                     "enabled": True, "keywords": ["truenas"], "exclude": [],
                     "order": 5}]})
                after = tcats.load_categories()
                cid2, _ = tcats.classify_one("truenas-scale-24.04.iso.torrent", after)
                self.assertEqual(cid2, "user_x")
                self.assertNotEqual(len(before), len(after))

    def test_missing_files_do_not_raise(self):
        missing = Path(tempfile.gettempdir()) / "definitely-not-here-9f8e7d.json"
        with patch.object(tcats, "PRESET_JSON", missing), \
             patch.object(tcats, "USER_JSON", missing):
            self.assertEqual(tcats.load_categories(), [])

    def test_corrupt_json_is_reported_not_silent(self):
        """用例 14: 配置损坏必须进 warnings。"""
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("{ this is not json", encoding="utf-8")
            report = []
            with patch.object(tcats, "PRESET_JSON", bad), \
                 patch.object(tcats, "USER_JSON", bad):
                cats = tcats.load_categories(report=report)
            self.assertEqual(cats, [])
            self.assertEqual(len(report), 2)
            self.assertEqual({r["file"] for r in report}, {"preset", "user"})

    def test_corrupt_user_keeps_preset(self):
        """一侧损坏不该连累另一侧(尽量多返回可用配置)。"""
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("[[[", encoding="utf-8")
            with patch.object(tcats, "PRESET_JSON", BUILTIN_JSON), \
                 patch.object(tcats, "USER_JSON", bad):
                cats = tcats.load_categories()
            self.assertIn("ubuntu", [c["id"] for c in cats])

    def test_no_network_during_load_and_classify(self):
        """用例 10: 全程无网络调用(替换 v1.2 那个永不触发的 mock 断言)。"""
        class _NoNet(socket.socket):
            def __init__(self, *a, **k):
                raise AssertionError("分类过程不应发起任何网络连接")

        with tempfile.TemporaryDirectory() as d:
            user = Path(d) / "uc.json"
            self._write(user, {})
            with patch.object(tcats, "PRESET_JSON", BUILTIN_JSON), \
                 patch.object(tcats, "USER_JSON", user), \
                 patch.object(socket, "socket", _NoNet), \
                 patch.object(socket, "create_connection", _NoNet):
                cats = tcats.load_categories()
                res = tcats.classify([_item("ubuntu-24.04.iso.torrent")], cats)
            self.assertEqual(res["counts"]["ubuntu"], 1)

    def test_show_empty_defaults_false(self):
        with tempfile.TemporaryDirectory() as d:
            user = Path(d) / "uc.json"
            self._write(user, {})
            with patch.object(tcats, "USER_JSON", user):
                self.assertFalse(tcats.load_show_empty())
            self._write(user, {"show_empty": True})
            with patch.object(tcats, "USER_JSON", user):
                self.assertTrue(tcats.load_show_empty())


class TestPresetFile(unittest.TestCase):
    """预置配置本身的守卫(防误删/防泛化词回潮)。"""

    def setUp(self):
        self.cats = _preset_cats()

    def test_builtin_file_parses_and_has_all_categories(self):
        self.assertEqual([c["id"] for c in self.cats],
                         ["ubuntu", "fedora", "arch", "debian", "proxmox", "tools"])

    def test_all_preset_marked_builtin(self):
        for c in self.cats:
            self.assertTrue(c.get("builtin"), c["id"])
            self.assertTrue(c.get("enabled"), c["id"])

    def test_names_are_bilingual(self):
        for c in self.cats:
            self.assertTrue(c["name"].get("zh") and c["name"].get("en"), c["id"])

    def test_no_generic_keywords(self):
        """泛化词会误伤(如 'arch' 命中 'search'), 预置里不许出现。"""
        banned = {"iso", "linux", "x64", "arch", "img", "ve"}
        for c in self.cats:
            for k in c["keywords"]:
                self.assertNotIn(k.lower(), banned, "%s 含泛化词 %s" % (c["id"], k))

    def test_keywords_lowercase_and_no_blank(self):
        for c in self.cats:
            for k in c["keywords"]:
                self.assertTrue(k and k == k.lower(), k)

    def test_calibrated_keywords_present(self):
        """按现网数据校准过的关键词不能被删掉。"""
        by_id = {c["id"]: c["keywords"] for c in self.cats}
        for cid, kws in (("ubuntu", ["tuxedo"]),
                         ("arch", ["endeavouros", "manjaro"]),
                         ("debian", ["butterbian", "trixie"]),
                         ("proxmox", ["proxmox-ve"])):
            for k in kws:
                self.assertIn(k, by_id[cid], "%s 缺少校准关键词 %s" % (cid, k))

    def test_orders_unique(self):
        orders = [c["order"] for c in self.cats]
        self.assertEqual(len(orders), len(set(orders)))


class TestClassifyApi(unittest.TestCase):
    """接口契约(用例 13、14 及响应形态)。"""

    def setUp(self):
        web_app.app.config["TESTING"] = True
        # 与 test_managed_sidecars / test_download_status 同一套路:
        # app.py 有全局 @app.before_request require_auth, REQUIRE_LOGIN 默认 1,
        # 不关掉的话所有接口测试都会拿到 401 而不是被测的状态码。
        self.client = web_app.app.test_client()
        self._patches = [
            patch.object(tcats, "PRESET_JSON", BUILTIN_JSON),
            patch.object(web_app, "REQUIRE_LOGIN", False),
            patch.object(web_app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _post(self, payload):
        return self.client.post("/api/torrent/classify", json=payload)

    def test_happy_path(self):
        r = self._post({"items": [_item("ubuntu-24.04.5-desktop-amd64.iso.torrent")]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["counts"]["ubuntu"], 1)
        self.assertIn("show_empty", body)

    def test_arg_types(self):
        self.assertEqual(self._post({"items": "nope"}).status_code, 400)
        self.assertEqual(self._post({}).status_code, 400)
        self.assertEqual(self._post({"items": []}).status_code, 200)
        # 数组本身合法, 元素是垃圾 -> 逐个丢弃(容忍), 不是 400
        r = self._post({"items": [1, 2, None]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(sum(r.get_json()["counts"].values()), 0)

    def test_too_many_items_rejected(self):
        """用例 13。"""
        big = [_item("t-%d.iso.torrent" % i) for i in range(web_app.MAX_CLASSIFY_ITEMS + 1)]
        r = self._post({"items": big})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_field_whitelist_drops_unknown_and_coerces(self):
        r = self._post({"items": [
            {"title": "ubuntu-24.04.iso.torrent", "url": 1, "evil": "x", "builtin": "yes"}]})
        got = [c for c in r.get_json()["categories"] if c["id"] == "ubuntu"][0]["items"][0]
        self.assertEqual(sorted(got), ["builtin", "pubDate", "source", "title", "url"])
        self.assertEqual(got["url"], "")
        self.assertTrue(got["builtin"])

    def test_title_falls_back_to_url(self):
        r = self._post({"items": [{"url": "https://x.test/debian-13.0.0-amd64.iso.torrent"}]})
        self.assertEqual(r.get_json()["counts"]["debian"], 1)

    def test_warnings_surface_config_damage(self):
        """用例 14(接口层): 损坏配置要以 warnings 暴露, 而不是静默空分类。"""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as d:
            bad = Path(d) / "bad.json"
            bad.write_text("not json at all", encoding="utf-8")
            with patch.object(tcats, "USER_JSON", bad):
                r = self._post({"items": [_item("ubuntu-24.04.iso.torrent")]})
            body = r.get_json()
            self.assertTrue(body["ok"])
            self.assertIn("warnings", body)
            self.assertEqual(body["warnings"][0]["file"], "user")

    def test_uncategorized_present_when_nothing_matches(self):
        r = self._post({"items": [_item("完全不匹配-xyz.torrent")]})
        body = r.get_json()
        self.assertEqual(body["counts"][tcats.UNCATEGORIZED], 1)
        self.assertEqual(body["categories"][-1]["id"], tcats.UNCATEGORIZED)

    def test_does_not_depend_on_qbittorrent(self):
        """分类是纯展示, 不得被 qBittorrent 可用性绑架。"""
        with patch.object(web_app, "_ensure_qb_enabled",
                          side_effect=AssertionError("不应调用 _ensure_qb_enabled")):
            r = self._post({"items": [_item("ubuntu-24.04.iso.torrent")]})
        self.assertEqual(r.status_code, 200)


class TestUserDocValidation(unittest.TestCase):
    """设置页提交内容的校验(信任边界)。"""

    def test_accepts_minimal_doc(self):
        self.assertEqual(tcats.validate_user_doc({"user_categories": [
            {"id": "user_abc123", "name": {"zh": "我的盘", "en": "Mine"},
             "keywords": ["truenas"]}]}), [])

    def test_rejects_bad_id(self):
        for bad in ("ubuntu", "user_X", "user_", "u_abc", ""):
            errs = tcats.validate_user_doc({"user_categories": [
                {"id": bad, "name": {"zh": "x"}, "keywords": ["a"]}]})
            self.assertTrue(errs, "id %r 应被拒绝" % bad)

    def test_rejects_preset_id_collision(self):
        """用户分类不得占用预置 id(否则会静默遮蔽预置)。"""
        errs = tcats.validate_user_doc({"user_categories": [
            {"id": "ubuntu", "name": {"zh": "x"}, "keywords": ["a"]}]})
        self.assertTrue(errs)

    def test_rejects_duplicate_id(self):
        doc = {"user_categories": [
            {"id": "user_a", "name": {"zh": "1"}, "keywords": ["a"]},
            {"id": "user_a", "name": {"zh": "2"}, "keywords": ["b"]}]}
        self.assertTrue(any("重复" in e for e in tcats.validate_user_doc(doc)))

    def test_rejects_missing_name_or_keywords(self):
        self.assertTrue(tcats.validate_user_doc({"user_categories": [
            {"id": "user_a", "keywords": ["a"]}]}))
        self.assertTrue(tcats.validate_user_doc({"user_categories": [
            {"id": "user_a", "name": {"zh": "x"}, "keywords": []}]}))

    def test_rejects_too_many(self):
        doc = {"user_categories": [
            {"id": "user_%03d" % i, "name": {"zh": "x"}, "keywords": ["a"]}
            for i in range(tcats.MAX_USER_CATEGORIES + 1)]}
        self.assertTrue(tcats.validate_user_doc(doc))

    def test_rejects_non_object(self):
        self.assertTrue(tcats.validate_user_doc("nope"))
        self.assertTrue(tcats.validate_user_doc({"overrides": "nope"}))


class TestSaveUserDoc(unittest.TestCase):
    """原子写入与归一化。"""

    def test_save_normalizes_words(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "uc.json"
            with patch.object(tcats, "USER_JSON", p):
                tcats.save_user_doc({"user_categories": [
                    {"id": "user_a", "name": "我的盘",
                     "keywords": [" TrueNAS ", "TRUENAS", "", None, "unraid"]}]})
            saved = json.loads(p.read_text(encoding="utf-8"))
            cat = saved["user_categories"][0]
            self.assertEqual(cat["keywords"], ["truenas", "unraid"])   # 去空白/小写/去重
            self.assertEqual(cat["name"]["zh"], "我的盘")
            self.assertEqual(cat["name"]["en"], "我的盘")               # 单语自动补双语

    def test_save_rejects_invalid_and_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "uc.json"
            with patch.object(tcats, "USER_JSON", p):
                with self.assertRaises(ValueError):
                    tcats.save_user_doc({"user_categories": [{"id": "bad"}]})
            self.assertFalse(p.exists())

    def test_save_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nested" / "uc.json"
            with patch.object(tcats, "USER_JSON", p):
                tcats.save_user_doc({"user_categories": []})
            self.assertTrue(p.exists())

    def test_no_tmp_left_behind(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "uc.json"
            with patch.object(tcats, "USER_JSON", p):
                tcats.save_user_doc({"user_categories": []})
            leftovers = [f.name for f in Path(d).iterdir() if f.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_save_then_load_roundtrip(self):
        """写盘后 load_categories 必须立刻看到新分类(依赖"不缓存"这一条)。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "uc.json"
            with patch.object(tcats, "PRESET_JSON", BUILTIN_JSON), \
                 patch.object(tcats, "USER_JSON", p):
                tcats.save_user_doc({"user_categories": [
                    {"id": "user_nas", "name": {"zh": "NAS", "en": "NAS"},
                     "keywords": ["truenas"]}]})
                cats = tcats.load_categories()
                cid, _ = tcats.classify_one("truenas-scale-24.04.iso.torrent", cats)
                self.assertEqual(cid, "user_nas")

    def test_extra_keys_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "uc.json"
            with patch.object(tcats, "USER_JSON", p):
                tcats.save_user_doc({"user_categories": [], "evil": "x", "version": 99})
            saved = json.loads(p.read_text(encoding="utf-8"))
            self.assertNotIn("evil", saved)
            self.assertEqual(saved["version"], 1)


class TestCategoriesApi(unittest.TestCase):
    """/api/torrent/categories 读写契约。"""

    def setUp(self):
        web_app.app.config["TESTING"] = True
        self.client = web_app.app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.user_json = Path(self._tmp.name) / "torrent_categories.json"
        self._patches = [
            patch.object(tcats, "PRESET_JSON", BUILTIN_JSON),
            patch.object(tcats, "USER_JSON", self.user_json),
            patch.object(web_app, "REQUIRE_LOGIN", False),
            patch.object(web_app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_get_returns_preset(self):
        body = self.client.get("/api/torrent/categories").get_json()
        self.assertTrue(body["ok"])
        self.assertIn("ubuntu", [c["id"] for c in body["categories"]])
        self.assertEqual(body["user_categories"], [])
        self.assertFalse(body["show_empty"])

    def test_get_surfaces_warnings_when_user_file_corrupt(self):
        self.user_json.write_text("{{{", encoding="utf-8")
        body = self.client.get("/api/torrent/categories").get_json()
        self.assertTrue(body["ok"])
        self.assertIn("warnings", body)

    def test_post_then_get_roundtrip(self):
        payload = {"show_empty": True, "user_categories": [
            {"id": "user_nas", "name": {"zh": "NAS 工具盘", "en": "NAS Tools"},
             "keywords": ["truenas"], "exclude": [], "order": 100}],
            "overrides": {"tools": {"enabled": False}}}
        r = self.client.post("/api/torrent/categories", json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

        body = self.client.get("/api/torrent/categories").get_json()
        self.assertTrue(body["show_empty"])
        self.assertIn("user_nas", [c["id"] for c in body["categories"]])
        tools = [c for c in body["categories"] if c["id"] == "tools"][0]
        self.assertFalse(tools["enabled"])
        # 预置没被删除, 只是被禁用
        self.assertEqual(len([c for c in body["categories"] if c["builtin"]]), 6)

    def test_post_rejects_invalid(self):
        r = self.client.post("/api/torrent/categories",
                             json={"user_categories": [{"id": "ubuntu"}]})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_post_rejects_non_object(self):
        self.assertEqual(self.client.post("/api/torrent/categories",
                                          json=[1, 2]).status_code, 400)

    def test_post_does_not_touch_preset_file(self):
        before = BUILTIN_JSON.read_bytes()
        self.client.post("/api/torrent/categories", json={"user_categories": []})
        self.assertEqual(BUILTIN_JSON.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
