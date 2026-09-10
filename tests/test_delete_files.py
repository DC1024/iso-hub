#!/usr/bin/env python3
"""镜像删除功能 (POST /api/delete-files) 单元测试。

覆盖:
  * 正常删除: 清单内的已下载文件可被删除
  * 安全边界: 路径穿越 / 非法 type / 非法文件名 一律拒绝
  * 白名单约束: 不在当前清单内的文件拒绝删除(保护用户自有 ISO)
  * 受保护文件: 默认跳过, force=true 才删除
  * 任务互斥: 有下载任务在跑时返回 409
  * 参数校验: 空 items 返回 400
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


class DeleteFilesTestBase(unittest.TestCase):
    """搭好临时 DATA_DIR + 清单, 供各用例复用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        (self.data / "linux" / "Ubuntu").mkdir(parents=True)
        # 清单里声明两个文件
        self.manifest = {
            "updated_at": 0,
            "distributions": [
                {"distribution": "Ubuntu", "type": "linux", "download_url":
                 "https://example.com/ubuntu-26.04.iso"},
                {"distribution": "Ubuntu", "type": "linux", "download_url":
                 "https://example.com/ubuntu-24.04.iso"},
            ],
        }
        self.json_file = self.data / "distributions.json"
        self.json_file.write_text(json.dumps(self.manifest), encoding="utf-8")
        # 磁盘上放三个文件: 两个在清单内, 一个不在
        for fn in ("ubuntu-26.04.iso", "ubuntu-24.04.iso", "user-manual.iso"):
            (self.data / "linux" / "Ubuntu" / fn).write_bytes(b"x" * 128)
        self.settings = self.data / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")

        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "JSON_FILE", self.json_file),
            patch.object(app, "SETTINGS_JSON", self.settings),
            patch.object(app, "running_task", return_value=None),
            # 关掉登录门禁: 本测试只验证删除逻辑本身, 鉴权由 require_auth 独立覆盖
            patch.object(app, "REQUIRE_LOGIN", False),
            patch.object(app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()
        self.client = app.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def post(self, payload):
        return self.client.post("/api/delete-files", json=payload)


class TestDeleteSuccess(DeleteFilesTestBase):
    """正常删除路径。"""

    def test_deletes_manifest_file(self):
        """清单内的已下载文件可被成功删除。"""
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "ubuntu-26.04.iso"}]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["removed"], ["ubuntu-26.04.iso"])
        self.assertEqual(body["skipped"], [])
        self.assertFalse((self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").exists(),
                         "文件应已从磁盘删除")

    def test_deletes_multiple(self):
        """一次请求可删除多个文件。"""
        r = self.post({"items": [
            {"type": "linux", "distribution": "Ubuntu", "filename": "ubuntu-26.04.iso"},
            {"type": "linux", "distribution": "Ubuntu", "filename": "ubuntu-24.04.iso"},
        ]})
        body = r.get_json()
        self.assertEqual(sorted(body["removed"]), ["ubuntu-24.04.iso", "ubuntu-26.04.iso"])
        self.assertEqual(len(list((self.data / "linux" / "Ubuntu").glob("ubuntu-*.iso"))), 0)

    def test_missing_file_is_skipped_not_error(self):
        """文件在清单里但磁盘上不存在 -> 记为 skipped, 整体仍 200。"""
        (self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").unlink()
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "ubuntu-26.04.iso"}]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertEqual(len(body["skipped"]), 1)
        self.assertIn("文件不存在", body["skipped"][0])


class TestDeleteSecurity(DeleteFilesTestBase):
    """安全边界: 一切非法输入都必须被拒绝, 且不误删任何文件。"""

    def test_rejects_path_traversal_in_filename(self):
        """文件名含 ../ 必须拒绝。"""
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "../../../etc/passwd"}]})
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertTrue(any("非法文件名" in s for s in body["skipped"]))

    def test_rejects_slash_in_filename(self):
        """文件名含 / 必须拒绝。"""
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "sub/dir.iso"}]})
        self.assertEqual(r.get_json()["removed"], [])

    def test_rejects_dotdot_filename(self):
        """文件名为 .. 必须拒绝。"""
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": ".."}]})
        self.assertEqual(r.get_json()["removed"], [])

    def test_rejects_illegal_type(self):
        """type 不在白名单 -> 拒绝。"""
        r = self.post({"items": [{"type": "etc", "distribution": "Ubuntu",
                                  "filename": "ubuntu-26.04.iso"}]})
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertTrue(any("非法" in s for s in body["skipped"]))

    def test_rejects_distribution_with_traversal(self):
        """distribution 含 ../ -> 拒绝。"""
        r = self.post({"items": [{"type": "linux", "distribution": "../../etc",
                                  "filename": "ubuntu-26.04.iso"}]})
        self.assertEqual(r.get_json()["removed"], [])

    def test_rejects_file_not_in_manifest(self):
        """不在清单内的文件拒绝删除(保护用户自有 ISO)。"""
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "user-manual.iso"}]})
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertTrue(any("不在当前清单内" in s for s in body["skipped"]))
        self.assertTrue((self.data / "linux" / "Ubuntu" / "user-manual.iso").exists(),
                        "用户自有文件必须保留")

    def test_empty_items_returns_400(self):
        """items 为空 -> 400。"""
        r = self.post({"items": []})
        self.assertEqual(r.status_code, 400)

    def test_missing_items_returns_400(self):
        """完全没有 items 字段 -> 400。"""
        r = self.post({})
        self.assertEqual(r.status_code, 400)


class TestDeleteProtected(DeleteFilesTestBase):
    """锁定(受保护)文件: 硬拒绝删除, 无任何绕过参数。

    锁定优先级高于手动删除 —— 用户必须先解锁才能删。
    """

    def test_locked_file_is_rejected(self):
        """锁定文件不删, 磁盘文件保留, 返回 locked 列表。"""
        with patch.object(app, "load_protected", return_value=["linux/Ubuntu/ubuntu-26.04.iso"]):
            r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                      "filename": "ubuntu-26.04.iso"}]})
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertEqual(len(body["locked"]), 1)
        self.assertEqual(body["locked"][0]["filename"], "ubuntu-26.04.iso")
        self.assertFalse(body["ok"], "含锁定文件时 ok 应为 False")
        self.assertTrue(any("锁定" in s for s in body["skipped"]))
        self.assertTrue((self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").exists())

    def test_force_param_is_ignored(self):
        """即使传入 force=true 也不能删除锁定文件(force 后门已移除)。"""
        with patch.object(app, "load_protected", return_value=["linux/Ubuntu/ubuntu-26.04.iso"]):
            r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                      "filename": "ubuntu-26.04.iso"}], "force": True})
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertEqual(len(body["locked"]), 1)
        self.assertTrue((self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").exists())

    def test_locked_skipped_but_others_deleted(self):
        """混合场景: 锁定文件跳过, 未锁定文件照删, ok 为 False。"""
        with patch.object(app, "load_protected", return_value=["linux/Ubuntu/ubuntu-26.04.iso"]):
            r = self.post({"items": [
                {"type": "linux", "distribution": "Ubuntu", "filename": "ubuntu-26.04.iso"},
                {"type": "linux", "distribution": "Ubuntu", "filename": "ubuntu-24.04.iso"},
            ]})
        body = r.get_json()
        self.assertEqual(body["removed"], ["ubuntu-24.04.iso"])
        self.assertEqual(len(body["locked"]), 1)
        self.assertEqual(body["locked"][0]["filename"], "ubuntu-26.04.iso")
        self.assertFalse(body["ok"])
        # 锁定的保留, 未锁定的删除
        self.assertTrue((self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").exists())
        self.assertFalse((self.data / "linux" / "Ubuntu" / "ubuntu-24.04.iso").exists())

    def test_protected_by_bare_filename(self):
        """锁定名单按裸文件名匹配时同样硬拒绝。"""
        with patch.object(app, "load_protected", return_value=["ubuntu-26.04.iso"]):
            r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                      "filename": "ubuntu-26.04.iso"}]})
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertEqual(len(body["locked"]), 1)

    def test_unlock_then_delete_works(self):
        """解锁(名单不再含该文件)后即可正常删除。"""
        with patch.object(app, "load_protected", return_value=[]):
            r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                      "filename": "ubuntu-26.04.iso"}]})
        body = r.get_json()
        self.assertEqual(body["removed"], ["ubuntu-26.04.iso"])
        self.assertEqual(body["locked"], [])
        self.assertFalse((self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").exists())


class TestDeletePartFiles(DeleteFilesTestBase):
    """v1.2.7 回归: 「下载停止」的半成品(.part)也必须能删掉。

    Bug 背景: 下载改用 .part 原子改名协议后, 半成品在磁盘上叫 xxx.iso.part,
    而前端传的目标名是 xxx.iso。旧实现只查 target/xxx.iso -> 不存在 -> 报
    「文件不存在」被跳过, 用户明明在列表里看得到这个占着空间的停止文件却删不掉。

    同时删除后必须清掉 download_failures.json 里的失败记录, 否则 UI 会继续
    显示「下载停止」而文件其实已经没了。
    """

    def setUp(self):
        super().setUp()
        self.failures = self.data / "download_failures.json"
        self._patches.append(patch.object(app, "FAILURES_JSON", self.failures))
        self._patches[-1].start()

    def _put_part(self, fname, size=256):
        p = self.data / "linux" / "Ubuntu" / (fname + ".part")
        p.write_bytes(b"y" * size)
        return p

    def test_deletes_part_only_file(self):
        """只有 .part 存在时(下载停止)也应删除成功, 而不是报文件不存在。"""
        (self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").unlink()
        part = self._put_part("ubuntu-26.04.iso")
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "ubuntu-26.04.iso"}]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["skipped"], [])
        self.assertEqual(len(body["removed"]), 1)
        self.assertFalse(part.exists(), "半成品必须被删除")

    def test_deletes_both_final_and_part(self):
        """完整文件与半成品并存时两者一起删除(先删干净再让用户重下)。"""
        part = self._put_part("ubuntu-24.04.iso")
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "ubuntu-24.04.iso"}]})
        body = r.get_json()
        self.assertEqual(body["skipped"], [])
        self.assertFalse(part.exists())
        self.assertFalse((self.data / "linux" / "Ubuntu" / "ubuntu-24.04.iso").exists())

    def test_clears_failure_record_for_part(self):
        """删除停止的半成品后, 「下载停止」失败记录必须被清理。"""
        self.failures.write_text(json.dumps(
            {"linux/Ubuntu/ubuntu-26.04.iso": {"at": 1, "kind": "stopped"}}),
            encoding="utf-8")
        (self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").unlink()
        self._put_part("ubuntu-26.04.iso")
        self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                              "filename": "ubuntu-26.04.iso"}]})
        left = json.loads(self.failures.read_text(encoding="utf-8"))
        self.assertNotIn("linux/Ubuntu/ubuntu-26.04.iso", left,
                         "失败记录应被清理, 否则 UI 仍显示「下载停止」")

    def test_missing_both_still_skipped(self):
        """完整文件与 .part 都不存在 -> 仍然如实报「文件不存在」。"""
        (self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").unlink()
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "ubuntu-26.04.iso"}]})
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertEqual(len(body["skipped"]), 1)
        self.assertIn("文件不存在", body["skipped"][0])

    def test_part_path_traversal_rejected(self):
        """filename 本身非法时, .part 分支也不得绕过校验。"""
        r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                  "filename": "../../../etc/passwd"}]})
        body = r.get_json()
        self.assertEqual(body["removed"], [])
        self.assertTrue(any("非法文件名" in s for s in body["skipped"]))


class TestDeleteTaskMutex(DeleteFilesTestBase):
    """任务互斥: 下载进行中不接受删除请求。"""

    def test_409_when_task_running(self):
        """有任务在跑 -> 409, 且不删除任何文件。"""
        with patch.object(app, "running_task", return_value={"task": "download"}):
            r = self.post({"items": [{"type": "linux", "distribution": "Ubuntu",
                                      "filename": "ubuntu-26.04.iso"}]})
        self.assertEqual(r.status_code, 409)
        self.assertTrue((self.data / "linux" / "Ubuntu" / "ubuntu-26.04.iso").exists())


class TestFrontendFilterScope(unittest.TestCase):
    """回归: 筛选作用域必须收窄到 #groups。

    Bug 背景: applyFilter 曾用 document.querySelectorAll('.card') 全文档扫描,
    而「自定义源/订阅同步/设置」等面板复用了 .card/.card-head/.g-name/.chip,
    于是选「已收藏」时这些面板卡片被当成不匹配的分组卡片 -> display:none -> 面板空白。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_apply_filter_scoped_to_groups(self):
        """applyFilter 必须用 #groups 作为作用域, 不得全文档扫 .card。"""
        import re
        m = re.search(r"function applyFilter\(\)\s*\{(.*?)\n\}", self.html, re.S)
        self.assertIsNotNone(m, "未找到 applyFilter 函数")
        body = m.group(1)
        self.assertIn("getElementById('groups')", body,
                      "applyFilter 必须通过 #groups 收窄作用域")
        self.assertNotIn("document.querySelectorAll('.card')", body,
                         "applyFilter 不得全文档扫描 .card(会误伤其他面板)")

    def test_filter_no_match_hint_exists(self):
        """筛选无结果时应有提示文案, 避免用户以为页面坏了。"""
        self.assertIn("filterNoMatch", self.html)

    def test_delete_sel_button_exists(self):
        """操作区应有「删除所选」按钮并绑定 deleteSel。"""
        self.assertIn('id="btnDel"', self.html)
        self.assertIn("onclick=\"deleteSel()\"", self.html)

    def test_delete_sel_defined(self):
        """deleteSel 函数必须存在且调用 /api/delete-files。"""
        self.assertIn("async function deleteSel()", self.html)
        self.assertIn("/api/delete-files", self.html)

    def test_select_all_skips_hidden_rows(self):
        """全选/计数必须跳过被筛选隐藏的行。"""
        import re
        m = re.search(r"function toggleAllGroups\(v\)\s*\{(.*?)\n\}", self.html, re.S)
        self.assertIsNotNone(m)
        self.assertIn("display==='none'", m.group(1),
                      "全选应跳过隐藏行")


if __name__ == "__main__":
    unittest.main()
