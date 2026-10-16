#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载韧性回归测试 (v1.3.17)

生产事故背景(2026-10-16 排查, Z4Pro iso-hub 实例):

  1. **Proxmox 死循环**: custom_sources 里 Proxmox 的 checksum 模板配成了
     ``{listing_url}SHA256SUMS``(镜像实际只提供 per-file ``xxx.iso.sha256``)。
     取不到摘要 → 被当成"校验失败" → 删除 .part 重下 → 下满后又取不到摘要 ……
     同时 Range 续传撞上 416(断点已越界)被当成错误"将重下", 于是完整 ISO
     与等大 .part 长期并存, UI 一直显示「下载停止」, 10 天未自愈。

  2. **瞬时抖动留永久疤**: ubuntu-26.04(.1)/archlinux-2026.08.01 三条下载在同一
     分钟内全部失败, 事后探测 URL 均 200 —— 纯网络抖动。但代码一次失败即记
     failure, 既无重试也无 TTL, 于是"一分钟的抖动"在 UI 上挂了 10 天。

本文件锁定四条修复:
  A. HTTP 416 必须被当作"已完成"信号, 直接进校验, **不得**重下
  B. verify_checksum_smart 三态: None(无法判定) 不得触发重下/删除
  C. 瞬时网络异常与 429/5xx 必须退避重试
  D. 失败记录带 err/url 且有 TTL(过期自动消失)
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import iso_runner  # noqa: E402
import download_linux as DL  # noqa: E402

import requests  # noqa: E402

PAYLOAD = b"A" * 4096
# PAYLOAD 的 SHA256(标准 sha256sums.txt 行: "<hash>  <name>")
PAYLOAD_SHA = __import__("hashlib").sha256(PAYLOAD).hexdigest()
NAME = "demo.iso"


class _Resp:
    """最小可用的假响应: 只实现 runner 实际用到的成员。"""

    def __init__(self, status=200, body=b"", headers=None, chunks=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else ([body] if body else [])
        self.closed = False

    @property
    def text(self):
        return self._body.decode("utf-8", "replace")

    def iter_content(self, chunk_size=8192):
        for c in self._chunks:
            yield c

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class _FakeDownloader:
    """只实现 _download_file_with_failover 用到的两个接口。"""

    def __init__(self, verify_result=(True, "ok")):
        self.headers = {}
        self.verify_result = verify_result
        self.calls = []

    def verify_checksum_smart(self, filepath, checksum_url, stored, dist=None):
        self.calls.append((Path(filepath).name, checksum_url))
        return self.verify_result


class TestRangeComplete416(unittest.TestCase):
    """A. 416 = 断点已满, 必须当作完整候选进校验, 不得重下。"""

    def _prepare(self, td: str, size: int):
        dl_dir = Path(td)
        (dl_dir / "linux").mkdir(parents=True, exist_ok=True)
        part = dl_dir / (NAME + ".part")
        part.write_bytes(b"A" * size)
        return part

    def test_416_is_treated_as_complete_not_error(self):
        """核心回归: 旧实现在这里 raise Exception('将重下') → 死循环。"""
        with tempfile.TemporaryDirectory() as td:
            dist_dir = Path(td)
            part = dist_dir / (NAME + ".part")
            part.write_bytes(PAYLOAD)
            filepath = dist_dir / NAME
            dl = _FakeDownloader(verify_result=(True, "URL校验和验证通过"))

            with patch.object(iso_runner.requests, "get",
                              lambda url, **kw: _Resp(416, headers={})):
                ok, _url = iso_runner._download_file_with_failover(
                    dl, {"checksum": ""}, [(f"https://m/{NAME}", f"https://m/{NAME}.sha256")],
                    NAME, dist_dir, filepath, head_total=len(PAYLOAD))

            self.assertTrue(ok, "416 且 .part 已满: 应视为完整文件并落定, 而不是失败")
            self.assertTrue(filepath.exists(), ".part 应被原子改名为最终文件")
            self.assertFalse(part.exists(), "落定后 .part 不应残留")

    def test_416_does_not_retransfer_bytes(self):
        """416 时不得再消费响应体(否则等于又下一次)。"""
        with tempfile.TemporaryDirectory() as td:
            dist_dir = Path(td)
            part = dist_dir / (NAME + ".part")
            part.write_bytes(PAYLOAD)
            filepath = dist_dir / NAME
            dl = _FakeDownloader()

            consumed = {"n": 0}

            def _resp(url, **kw):
                r = _Resp(416, chunks=[b"Z" * 100])
                _orig = r.iter_content

                def _counted(*a, **k):
                    for c in _orig(*a, **k):
                        consumed["n"] += len(c)
                        yield c
                r.iter_content = _counted
                return r

            with patch.object(iso_runner.requests, "get", _resp):
                iso_runner._download_file_with_failover(
                    dl, {"checksum": ""}, [(f"https://m/{NAME}", "")],
                    NAME, dist_dir, filepath, head_total=len(PAYLOAD))

            self.assertEqual(consumed["n"], 0, "416 分支不应再读取任何字节")


class TestUnverifiableIsNotFailure(unittest.TestCase):
    """B. 校验源不可用(None)不得触发"删除 .part / 重下"。"""

    def test_none_keeps_part_instead_of_deleting(self):
        """旧行为: success=False 且尺寸达标 → 删掉 .part(白白丢弃已下完的字节)。"""
        with tempfile.TemporaryDirectory() as td:
            dist_dir = Path(td)
            part = dist_dir / (NAME + ".part")
            part.write_bytes(PAYLOAD)
            filepath = dist_dir / NAME
            # None = 校验源不可达, 无法判定
            dl = _FakeDownloader(verify_result=(None, "无法判定: 校验源不可达"))

            _calls = {"n": 0}

            def _get(url, **kw):
                _calls["n"] += 1
                return _Resp(200, body=PAYLOAD,
                             headers={"content-length": str(len(PAYLOAD))})

            with patch.object(iso_runner.requests, "get", _get):
                ok, _ = iso_runner._download_file_with_failover(
                    dl, {"checksum": ""}, [(f"https://m/{NAME}", "")],
                    NAME, dist_dir, filepath)

            self.assertTrue(ok, "长度已达标 + 无法校验: 应落定而非判失败")
            self.assertTrue(filepath.exists())
            self.assertEqual(_calls["n"], 1, "不应因无法校验就换源重下")

    def test_mismatch_still_reported_as_failure(self):
        """对照: 真的比对不上(False)仍然必须判失败并丢弃坏数据。"""
        with tempfile.TemporaryDirectory() as td:
            dist_dir = Path(td)
            part = dist_dir / (NAME + ".part")
            part.write_bytes(b"B" * len(PAYLOAD))
            filepath = dist_dir / NAME
            dl = _FakeDownloader(verify_result=(False, "内容校验不符"))

            with patch.object(iso_runner.requests, "get",
                              lambda url, **kw: _Resp(
                                  200, body=b"B" * len(PAYLOAD),
                                  headers={"content-length": str(len(PAYLOAD))})):
                ok, _ = iso_runner._download_file_with_failover(
                    dl, {"checksum": ""}, [(f"https://m/{NAME}", "")],
                    NAME, dist_dir, filepath)

            self.assertFalse(ok, "确认不符必须判失败")
            self.assertFalse(part.exists(), "确认损坏的数据必须丢弃, 不能留给续传")


class TestRetryBackoff(unittest.TestCase):
    """C. 瞬时抖动与限流必须退避重试, 而不是一次失败就判死。"""

    def test_retries_on_transient_connection_error(self):
        seq = {"i": 0}

        def _flaky(url, **kw):
            seq["i"] += 1
            if seq["i"] < 3:
                raise requests.exceptions.ConnectionError("connection reset")
            return _Resp(200, body=PAYLOAD)

        with patch.object(iso_runner.requests, "get", _flaky), \
             patch.object(iso_runner.time, "sleep", lambda s: None):
            resp = iso_runner._http_get_with_retry("https://m/x", timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seq["i"], 3, "应重试到第 3 次才成功")

    def test_retries_on_429(self):
        seq = {"i": 0}

        def _limited(url, **kw):
            seq["i"] += 1
            return _Resp(429 if seq["i"] < 3 else 200, body=PAYLOAD)

        with patch.object(iso_runner.requests, "get", _limited), \
             patch.object(iso_runner.time, "sleep", lambda s: None):
            resp = iso_runner._http_get_with_retry("https://m/x", timeout=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seq["i"], 3)

    def test_does_not_retry_416(self):
        """416 不是故障, 重试它只会浪费一轮退避。"""
        seq = {"i": 0}

        def _resp(url, **kw):
            seq["i"] += 1
            return _Resp(416)

        with patch.object(iso_runner.requests, "get", _resp), \
             patch.object(iso_runner.time, "sleep", lambda s: None):
            resp = iso_runner._http_get_with_retry("https://m/x", timeout=5)
        self.assertEqual(resp.status_code, 416)
        self.assertEqual(seq["i"], 1, "416 不应触发重试")

    def test_gives_up_after_exhausting_retries(self):
        with patch.object(iso_runner.requests, "get",
                          lambda url, **kw: (_ for _ in ()).throw(
                              requests.exceptions.ConnectionError("down"))), \
             patch.object(iso_runner.time, "sleep", lambda s: None):
            with self.assertRaises(requests.exceptions.RequestException):
                iso_runner._http_get_with_retry("https://m/x", timeout=5, retries=3)


class TestFailureRecordFieldsAndTTL(unittest.TestCase):
    """D. 失败记录要能自证(带 err/url), 也要能自愈(TTL)。"""

    def test_record_carries_err_and_url(self):
        with tempfile.TemporaryDirectory() as td:
            iso_runner._record_failure(td, "linux", "Proxmox", "pve.iso", "hard",
                                       err="404 Client Error", url="https://m/pve.iso")
            data = json.loads((Path(td) / "download_failures.json").read_text(encoding="utf-8"))
            rec = data["linux/Proxmox/pve.iso"]
            self.assertEqual(rec["kind"], "hard")
            self.assertEqual(rec["err"], "404 Client Error")
            self.assertEqual(rec["url"], "https://m/pve.iso")

    def test_expired_records_dropped_on_write(self):
        with tempfile.TemporaryDirectory() as td:
            jf = Path(td) / "download_failures.json"
            old = int(time.time()) - 30 * 86400   # 30 天前
            jf.write_text(json.dumps({"linux/Ubuntu/old.iso": {"at": old, "kind": "hard"}}),
                          encoding="utf-8")
            iso_runner._record_failure(td, "linux", "Ubuntu", "new.iso", "hard")
            data = json.loads(jf.read_text(encoding="utf-8"))
            self.assertNotIn("linux/Ubuntu/old.iso", data, "超过 TTL 的旧疤应被清掉")
            self.assertIn("linux/Ubuntu/new.iso", data)

    def test_ttl_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            jf = Path(td) / "download_failures.json"
            old = int(time.time()) - 30 * 86400
            jf.write_text(json.dumps({"linux/Ubuntu/old.iso": {"at": old, "kind": "hard"}}),
                          encoding="utf-8")
            with patch.dict(os.environ, {"ISO_HUB_FAILURE_TTL_DAYS": "0"}):
                iso_runner._record_failure(td, "linux", "Ubuntu", "other.iso", "hard")
            data = json.loads(jf.read_text(encoding="utf-8"))
            self.assertIn("linux/Ubuntu/old.iso", data, "TTL=0 表示永不清理")

    def test_app_side_load_failures_filters_expired(self):
        """app.load_failures() 读取侧同样要过滤 —— 否则"再没被重试过的旧疤"消不掉。"""
        with tempfile.TemporaryDirectory() as td:
            os.environ["ISO_DATA_DIR"] = td
            for m in list(sys.modules):
                if m == "web.app" or m == "app":
                    del sys.modules[m]
            sys.path.insert(0, str(REPO_ROOT / "web"))
            import app as web_app  # noqa: E402
            jf = Path(td) / "download_failures.json"
            jf.parent.mkdir(parents=True, exist_ok=True)
            old = int(time.time()) - 30 * 86400
            fresh = int(time.time())
            jf.write_text(json.dumps({
                "linux/Ubuntu/old.iso": {"at": old, "kind": "hard"},
                "linux/Ubuntu/fresh.iso": {"at": fresh, "kind": "hard"},
            }), encoding="utf-8")
            got = web_app.load_failures()
            self.assertNotIn("linux/Ubuntu/old.iso", got)
            self.assertIn("linux/Ubuntu/fresh.iso", got)


class TestTriStateContractUpstream(unittest.TestCase):
    """上游 download_linux 侧的三态契约(与 iso_runner 保持一致)。"""

    def test_verify_returns_none_when_checksum_source_unreachable(self):
        d = DL.LinuxDistributionDownloader.__new__(DL.LinuxDistributionDownloader)
        d.headers = {}
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / NAME
            f.write_bytes(PAYLOAD)
            with patch.object(DL.requests, "get",
                              lambda *a, **k: _Resp(404, body=b"nope")):
                ok, msg = d.verify_checksum_smart(f, "https://m/SHA256SUMS", "")
        self.assertIsNone(ok, "校验源 404 应返回 None(无法判定), 不是 False")
        self.assertIn("无法判定", msg)

    def test_verify_returns_true_on_match(self):
        d = DL.LinuxDistributionDownloader.__new__(DL.LinuxDistributionDownloader)
        d.headers = {}
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / NAME
            f.write_bytes(PAYLOAD)
            body = f"{PAYLOAD_SHA}  {NAME}\n".encode()
            with patch.object(DL.requests, "get", lambda *a, **k: _Resp(200, body=body)):
                ok, _msg = d.verify_checksum_smart(f, "https://m/SHA256SUMS", "")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
