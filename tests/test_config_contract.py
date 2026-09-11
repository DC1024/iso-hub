#!/usr/bin/env python3
"""配置契约测试 —— 防「配置遮蔽」复发。

背景(真实 bug 品类):
    旧版本只在 /data/distributions.json **不存在**时才从镜像内置配置复制一次。
    此后镜像升级新增的字段(如 gpg_verify/gpg_key_url/gpg_key_fingerprint)永远
    同步不进已部署环境的运行时副本 → 功能静默失效。

    web/app.py 的 _migrate_distribution_fields() 每次启动按 download_url 索引、
    只补缺失字段地迁移。本文件锁死该契约:
      1. 内置清单必须存在 GPG 条目(防止误删后 CI 还全绿)
      2. 旧版运行时副本经迁移后, GPG 条目数与字段集合必须与内置一致
      3. 迁移不得覆盖用户已有值 / 不得动用户自定义条目 / 必须幂等

    CI 在全新环境运行, 没有 /data —— 因此本测试通过 patch web.app.JSON_FILE
    指向临时目录模拟「升级用户的旧版副本」, 与真实迁移函数逐字节对齐。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))
sys.path.insert(0, str(REPO_ROOT / "iso_download"))

import web.app as web_app  # noqa: E402

BUILTIN_JSON = REPO_ROOT / "iso_download" / "distributions.json"

GPG_FIELD_HINTS = ("gpg", "signature_url", "fingerprint")


def _builtin() -> dict:
    return json.loads(BUILTIN_JSON.read_text(encoding="utf-8"))


def _entries(cfg: dict) -> list:
    # 兼容两种键名: 内置用 distributions
    return cfg.get("distributions", [])


def _gpg_fields(cfg: dict) -> set:
    keys = set()
    for e in _entries(cfg):
        for k in e:
            if k.startswith("gpg") or k in GPG_FIELD_HINTS:
                keys.add(k)
    return keys


def _gpg_entry_count(cfg: dict) -> int:
    return sum(1 for e in _entries(cfg) if e.get("gpg_verify"))


def _strip_gpg_fields(cfg: dict) -> dict:
    """生成「旧版(v1.1.6 时代)运行时副本」: 删掉所有 GPG 相关字段。

    用内置清单动态生成而非静态 fixture —— 内置清单演进时 fixture 永不错位。
    """
    out = {"distributions": []}
    for e in _entries(cfg):
        clone = {k: v for k, v in e.items()
                 if not (k.startswith("gpg") or k in GPG_FIELD_HINTS)}
        out["distributions"].append(clone)
    return out


class TestBuiltinConfig(unittest.TestCase):
    """内置清单自身的契约 —— 防止有人误删 GPG 配置后 CI 还全绿。"""

    def test_builtin_exists_with_entries(self):
        cfg = _builtin()
        self.assertGreater(len(_entries(cfg)), 0, "内置 distributions.json 没有条目")

    def test_builtin_has_gpg_entries(self):
        self.assertGreater(
            _gpg_entry_count(_builtin()), 0,
            "内置 distributions.json 里没有任何 gpg_verify 条目 —— GPG 功能配置被误删",
        )

    def test_builtin_gpg_fields_consistent_across_gpg_entries(self):
        """所有 gpg_verify 条目的 GPG 字段集合应一致(便于迁移做字段级补齐)。"""
        cfg = _builtin()
        per_entry = []
        for e in _entries(cfg):
            if e.get("gpg_verify"):
                per_entry.append({k for k in e
                                  if k.startswith("gpg") or k in GPG_FIELD_HINTS})
        self.assertTrue(per_entry)
        self.assertEqual(len({frozenset(s) for s in per_entry}), 1,
                         f"gpg_verify 条目间 GPG 字段集合不一致: {per_entry}")


class TestMigrateContract(unittest.TestCase):
    """迁移契约: 旧版运行时副本经 _migrate_distribution_fields 后必须与内置对齐。"""

    def _run_migrate(self, runtime_cfg: dict) -> dict:
        """在临时目录里跑真实迁移函数。

        注意: web.app 的 DEFAULT_JSON 默认指向容器内路径(/app/iso_download),
        在 CI/本机不存在 -> 迁移函数会静默 return(这本身就是"降级无痕"的案例)。
        因此这里同时 patch DEFAULT_JSON(指向仓库真实内置配置)与 JSON_FILE
        (指向模拟的旧版运行时副本), 与生产语义逐一对齐。
        """
        with tempfile.TemporaryDirectory() as d:
            rp = Path(d) / "distributions.json"
            rp.write_text(json.dumps(runtime_cfg, ensure_ascii=False), encoding="utf-8")
            with patch.object(web_app, "JSON_FILE", rp), \
                 patch.object(web_app, "DEFAULT_JSON", BUILTIN_JSON):
                web_app._migrate_distribution_fields()
            return json.loads(rp.read_text(encoding="utf-8"))

    def test_migrate_backfills_gpg_entries(self):
        """旧版副本(无任何 GPG 字段)迁移后, GPG 条目数必须与内置一致 —— 防配置遮蔽复发。"""
        builtin = _builtin()
        runtime = self._run_migrate(_strip_gpg_fields(builtin))
        before = _gpg_entry_count(_strip_gpg_fields(builtin))
        after = _gpg_entry_count(runtime)
        self.assertEqual(
            after, _gpg_entry_count(builtin),
            f"迁移后运行时 GPG 条目数({after})与内置({_gpg_entry_count(builtin)})不一致"
            f"(迁移前 {before}) —— 配置遮蔽回归",
        )
        self.assertGreater(after, before, "迁移未生效: 运行时 GPG 条目数没有增长")

    def test_migrate_field_sets_match(self):
        """迁移后字段集合必须与内置一致, 缺失项即迁移漏字段。"""
        builtin = _builtin()
        runtime = self._run_migrate(_strip_gpg_fields(builtin))
        missing = _gpg_fields(builtin) - _gpg_fields(runtime)
        self.assertEqual(_gpg_fields(runtime), _gpg_fields(builtin),
                         f"迁移后 GPG 字段集合不一致, 缺失: {missing}")

    def test_migrate_never_overwrites_user_values(self):
        """用户已自定义的字段必须原样保留(迁移只补缺失, 绝不覆盖)。"""
        builtin = _builtin()
        runtime_cfg = _strip_gpg_fields(builtin)
        # 用户把第一个条目的注释字段改成自己的值
        first = runtime_cfg["distributions"][0]
        first["notes"] = "user-custom-note"
        migrated = self._run_migrate(runtime_cfg)
        got = next(e for e in migrated["distributions"]
                   if e.get("download_url") == first.get("download_url"))
        self.assertEqual(got.get("notes"), "user-custom-note",
                         "迁移覆盖了用户已有字段 —— 破坏用户数据")

    def test_migrate_preserves_user_custom_entries(self):
        """download_url 不在内置清单中的自定义条目必须原样保留。"""
        builtin = _builtin()
        runtime_cfg = _strip_gpg_fields(builtin)
        custom = {"distribution": "MyOS", "type": "linux",
                  "download_url": "https://example.invalid/myos.iso"}
        runtime_cfg["distributions"].append(custom)
        migrated = self._run_migrate(runtime_cfg)
        kept = [e for e in migrated["distributions"]
                if e.get("download_url") == "https://example.invalid/myos.iso"]
        self.assertEqual(len(kept), 1, "用户自定义条目被迁移删除")
        self.assertEqual(kept[0], custom, "用户自定义条目被迁移篡改")

    def test_migrate_idempotent(self):
        """迁移必须幂等: 跑两次结果与跑一次完全一致。"""
        builtin = _builtin()
        once = self._run_migrate(_strip_gpg_fields(builtin))
        twice = self._run_migrate(once)
        self.assertEqual(twice, once, "迁移不幂等: 第二次运行改变了数据")


if __name__ == "__main__":
    unittest.main()
