#!/usr/bin/env python3
"""环境变量必须有文档 —— 防止再出现"代码读了、文档没写"的漂移。

盘点时发现 8 个变量在代码里被读取、wiki 与 README 里却查无此名, 其中
ISO_HUB_DL_TICKET_TTL / ISO_HUB_SESSION_TTL / ISO_HUB_FAILURE_TTL_DAYS 是能
直接改变运行时行为的开关 —— 用户想调的时候根本不知道它存在。

这里只做一件事: 代码里读过的每个环境变量名, 文档里都得出现。
清单本身刻意不写死(新增变量自动纳入), 否则又会变成第二份要同步的清单。
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_DIRS = [REPO_ROOT / "wiki", REPO_ROOT]
CODE_DIRS = ["web", "iso_download", "scripts"]

# os.environ.get("X") / os.getenv("X") 两种写法都算"读过"。
# 只取字面量常量, 变量拼出来的名字静态扫不到, 不假装能扫。
ENV_RE = re.compile(r'os\.(?:environ\.get|getenv)\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']')


def _code_env_names() -> set:
    names = set()
    for d in CODE_DIRS:
        for p in (REPO_ROOT / d).rglob("*.py"):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            names.update(ENV_RE.findall(text))
    return names


def _doc_text() -> str:
    parts = []
    for d in DOC_DIRS:
        for p in sorted(d.rglob("*.md")):
            if ".git" in p.parts:
                continue
            try:
                parts.append(p.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(parts)


class TestEveryEnvDocumented(unittest.TestCase):
    def test_all_read_env_vars_appear_in_docs(self):
        names = _code_env_names()
        self.assertGreater(len(names), 20, "扫描到的环境变量太少, 正则可能失效了")
        doc = _doc_text()
        missing = sorted(n for n in names if n not in doc)
        self.assertEqual(
            missing, [],
            "以下环境变量在代码里被读取但文档里没出现:\n  " + "\n  ".join(missing)
            + "\n\n补到 wiki/环境变量与路径.md 的表格里。")

    def test_docs_do_not_advertise_removed_vars(self):
        """反向也兜一道: 文档里列的变量, 代码或 compose 得真的认。

        防止"文档写着能配、其实早删了"——比缺文档更坑, 因为用户会照着配然后困惑。
        """
        compose = "\n".join(
            (REPO_ROOT / f).read_text(encoding="utf-8")
            for f in ["docker-compose.yml", "docker-compose.acr.yml",
                      "docker-compose.dockerhub.yml", "Dockerfile"]
            if (REPO_ROOT / f).is_file()
        )
        known = _code_env_names() | set(
            re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", compose))
        doc = _doc_text()
        documented = {
            n for n in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", doc)
            if n.startswith(("ISO_", "QB_", "SAMBA_", "WEBDAV_", "DOCKER_",
                             "SHARE_", "TZ", "PUID", "PGID", "GPG_"))
        }
        ghosts = sorted(n for n in documented if n not in known)
        self.assertEqual(ghosts, [],
                         "文档提到了但代码/compose 里找不到的变量: " + ", ".join(ghosts))


if __name__ == "__main__":
    unittest.main()
