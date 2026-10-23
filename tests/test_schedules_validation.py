#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3: 定时任务接口必须校验 type / time, 不能"什么都收"。

背景(线上功能测试 P3): `POST /api/schedules {"type":"every_second"}` 和
`{"time":"99:99"}` 都返回 200 并成功入库。前端下拉的取值限制只是"看起来有效",
直接打接口就能造出一条**永远不会触发**的任务 —— 调度器的 _sched_matches()
只认 daily/weekly/monthly/yearly/once, 其余类型一律 return False;
而 "99:99" 在 split(":") 后虽然能 int(), 但永远对不上 now.tm_hour。

这类脏数据的可怕之处是"安静": 界面显示得好好的, 任务就是不跑, 只能去翻
settings.json 才发现。本文件锁死:
  1. 非法 type 一律 400, 且**不落盘**;
  2. 非法 time 一律 400(含 24:00、9:5、99:99、非字符串);
  3. 合法的五种类型 + 边界时间 00:00 / 23:59 正常入库;
  4. once 必须带有效的 scheduled_at(否则同样永远不触发);
  5. 缺省值仍然可用(不传 type/time 时按 daily 00:00), 不能把老前端打挂。
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


class SchedulesValidationBase(unittest.TestCase):
    """临时 DATA_DIR + 关闭登录门禁(鉴权另有专门测试覆盖)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.settings = self.data / "settings.json"

        self._patches = [
            patch.object(app, "DATA_DIR", self.data),
            patch.object(app, "SETTINGS_JSON", self.settings),
            patch.object(app, "REQUIRE_LOGIN", False),
            patch.object(app, "AUTH_TOKEN", ""),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = app.app.test_client()

    def post(self, body):
        return self.client.post("/api/schedules", json=body)

    def stored(self):
        """读回落盘的调度列表(空/文件不存在都当空)。"""
        if not self.settings.exists():
            return []
        return json.loads(self.settings.read_text(encoding="utf-8")).get("schedules", [])

    def assert_rejected(self, body, why):
        resp = self.post(body)
        self.assertEqual(resp.status_code, 400, "%s: 应被拒绝, 实际 %s %s"
                         % (why, resp.status_code, resp.get_data(as_text=True)[:200]))
        self.assertTrue((resp.get_json() or {}).get("error"), "400 必须带可读的 error")
        self.assertEqual(self.stored(), [], "%s: 被拒绝的请求不允许落盘" % why)


class InvalidTypeTest(SchedulesValidationBase):
    def test_unknown_type_rejected(self):
        self.assert_rejected({"name": "x", "type": "every_second", "time": "00:00"},
                             "未知 type")

    def test_type_is_case_sensitive(self):
        self.assert_rejected({"type": "Daily", "time": "00:00"}, "大小写不同的 type")

    def test_non_string_type_rejected(self):
        self.assert_rejected({"type": 123, "time": "00:00"}, "type 非字符串")


class InvalidTimeTest(SchedulesValidationBase):
    def test_hour_out_of_range(self):
        self.assert_rejected({"type": "daily", "time": "24:00"}, "24:00")
        self.assert_rejected({"type": "daily", "time": "99:99"}, "99:99")

    def test_minute_out_of_range(self):
        self.assert_rejected({"type": "daily", "time": "09:60"}, "09:60")

    def test_not_zero_padded(self):
        self.assert_rejected({"type": "daily", "time": "9:5"}, "9:5")

    def test_garbage(self):
        for bad in ("abc", "02-00", "02:00:00", [2, 0], {"h": 2}):
            with self.subTest(bad=bad):
                self.assert_rejected({"type": "daily", "time": bad}, repr(bad))

    def test_surrounding_whitespace_is_normalised(self):
        """宽容输入、严格存储: 粘贴带来的前后空格不该 400, 但落盘的必须是干净值。"""
        resp = self.post({"type": "daily", "time": " 02:30 "})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.stored()[-1]["time"], "02:30")


class ValidPayloadTest(SchedulesValidationBase):
    def test_all_known_types_accepted(self):
        for i, typ in enumerate(("daily", "weekly", "monthly", "yearly")):
            with self.subTest(typ=typ):
                resp = self.post({"name": "t-" + typ, "type": typ, "time": "02:30"})
                self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:200])

    def test_once_with_future_timestamp_accepted(self):
        resp = self.post({"name": "once-ok", "type": "once", "time": "02:30",
                          "scheduled_at": 4102444800})
        self.assertEqual(resp.status_code, 200)

    def test_boundary_times_accepted(self):
        for i, hhmm in enumerate(("00:00", "23:59")):
            with self.subTest(t=hhmm):
                resp = self.post({"name": "b%d" % i, "type": "daily", "time": hhmm})
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(self.stored()[-1]["time"], hhmm)

    def test_defaults_still_work(self):
        """老前端/脚本不传 type/time 时不能被打挂。"""
        resp = self.post({"name": "no-type-time"})
        self.assertEqual(resp.status_code, 200)
        sched = self.stored()[-1]
        self.assertEqual(sched["type"], "daily")
        self.assertEqual(sched["time"], "00:00")

    def test_empty_time_falls_back_to_default(self):
        resp = self.post({"type": "daily", "time": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.stored()[-1]["time"], "00:00")


class OnceRequiresTimestampTest(SchedulesValidationBase):
    def test_once_without_scheduled_at_rejected(self):
        self.assert_rejected({"type": "once", "time": "02:30"}, "once 缺 scheduled_at")

    def test_once_with_zero_rejected(self):
        self.assert_rejected({"type": "once", "time": "02:30", "scheduled_at": 0},
                             "once scheduled_at=0")

    def test_once_with_garbage_rejected(self):
        self.assert_rejected({"type": "once", "time": "02:30", "scheduled_at": "abc"},
                             "once scheduled_at 非数字")


class FrontendStillMatchesBackendTest(unittest.TestCase):
    """前端下拉必须与后端白名单一致, 否则用户能在界面上选到会被 400 的类型。"""

    def test_index_options_are_subset_of_backend(self):
        import re
        html = (REPO_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        block = re.search(r'<select id="sch-type".*?</select>', html, re.S)
        self.assertIsNotNone(block, "找不到 #sch-type 下拉")
        opts = set(re.findall(r'<option value="([^"]+)"', block.group(0)))
        from api.schedules import SCHEDULE_TYPES
        self.assertEqual(opts, set(SCHEDULE_TYPES),
                         "前端可选值与后端白名单不一致(多了会 400, 少了用户选不到)")


if __name__ == "__main__":
    unittest.main()
