#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""供应链可复现性护栏。

为什么单独建一个测试: 「依赖锁定」和「运行时加固」一样, 属于删掉不会报错、只在
某天重建镜像时才发现结果不一样的那类东西。v1.5.3 之前:

  * 两个 requirements 全是最低下限(`>=`), 没有 constraints —— 同一 commit 隔月
    重建会装到不同的 Flask / cryptography;
  * 基础镜像 `python:3.12-slim` 浮动, 上游每发一次补丁镜像就变;
  * 三个 sidecar 用 `:latest`, 实测 qBittorrent 的 latest 就是当天更新的。

这个文件把上述四件事钉住, 防止后续"顺手改回去"。

覆盖点:
  * 基础镜像必须 tag@sha256:<64hex>
  * 两条 pip install 都必须带 `-c /app/constraints.txt`, 且 COPY 在 install 之前
  * constraints.txt 必须覆盖两个 requirements 里的每一个直接依赖, 且是 `==`
  * 三份 compose 的可选 sidecar 镜像都必须带 digest
  * dependabot 必须监控 docker 生态
"""
import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
CONSTRAINTS = REPO_ROOT / "constraints.txt"
REQUIREMENTS = (REPO_ROOT / "web" / "requirements.txt",
                REPO_ROOT / "iso_download" / "requirements.txt")
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.acr.yml",
                 "docker-compose.dockerhub.yml")
SIDECARS = ("samba", "webdav", "qbittorrent")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}\b")


def _package_names(path: Path):
    """从 requirements / constraints 里取包名(忽略注释、空行、环境标记)。"""
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if ";" in line:
            # 条件依赖(如 `pathlib2; python_version < "3.4"`), 在当前环境下不安装,
            # 不该要求 constraints 锁它
            continue
        name = re.split(r"[<>=!~;\[\]\s]", line, maxsplit=1)[0].strip().lower()
        if name:
            names.add(name)
    return names


class BaseImageTest(unittest.TestCase):
    def test_from_is_pinned_by_digest(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        froms = [ln for ln in text.splitlines() if ln.strip().upper().startswith("FROM ")]
        self.assertEqual(len(froms), 1, f"预期单阶段构建, 实际: {froms}")
        self.assertRegex(
            froms[0], r"^FROM\s+python:3\.\d+\.\d+-slim@sha256:[0-9a-f]{64}$",
            "基础镜像必须是 python:<具体小版本>-slim@sha256:<digest> —— "
            "只写 3.12-slim 或漏掉 digest 都会让构建结果漂移",
        )


class ConstraintsWiringTest(unittest.TestCase):
    def setUp(self):
        self.text = DOCKERFILE.read_text(encoding="utf-8")

    def test_constraints_copied_before_install(self):
        copy_at = self.text.find("COPY constraints.txt")
        self.assertGreater(copy_at, 0, "Dockerfile 没有 COPY constraints.txt")
        # 注释里也会出现 "pip install"(解释为什么运行期不用 pip), 只看真实指令行
        real_installs = [i for i, ln in enumerate(self.text.splitlines())
                         if "pip install" in ln and not ln.strip().startswith("#")]
        self.assertGreaterEqual(len(real_installs), 2, "预期两条 pip install")
        first_install_line = real_installs[0]
        install_at = sum(len(ln) + 1 for ln in
                         self.text.splitlines()[:first_install_line])
        self.assertLess(copy_at, install_at,
                        "constraints.txt 必须在任何 pip install 之前 COPY")

    def test_every_pip_install_uses_constraints(self):
        # 注释里也会出现 "pip install" 字样(解释为什么不用 pip), 必须排除
        installs = [ln for ln in self.text.splitlines()
                    if "pip install" in ln and not ln.strip().startswith("#")]
        self.assertGreaterEqual(len(installs), 2, "预期两条 pip install")
        for line in installs:
            with self.subTest(line=line.strip()):
                self.assertIn("-c /app/constraints.txt", line,
                              "pip install 少了 -c, 依赖会重新飘起来")

    def test_constraints_cover_all_direct_requirements(self):
        pinned = _package_names(CONSTRAINTS)
        for path in REQUIREMENTS:
            for name in _package_names(path):
                with self.subTest(requirement=path.name, package=name):
                    self.assertIn(name, pinned,
                                  f"{name} 在 {path.name} 里声明了但 constraints.txt 没锁")

    def test_constraints_use_exact_pins(self):
        for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            with self.subTest(line=line):
                self.assertRegex(line, r"^[A-Za-z0-9_.\-]+==[0-9]",
                                 "constraints 只接受 `==` 精确锁定")


class SidecarImageTest(unittest.TestCase):
    def test_sidecar_images_are_pinned_by_digest(self):
        for name in COMPOSE_FILES:
            data = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
            services = data["services"]
            for svc in SIDECARS:
                with self.subTest(compose=name, service=svc):
                    image = str(services[svc]["image"])
                    self.assertRegex(
                        image, DIGEST_RE,
                        f"{name} 的 {svc} 仍是浮动 tag: {image} —— "
                        "sidecar 行为变更可直接改变共享/下载结果, 必须锁 digest",
                    )


class DependabotTest(unittest.TestCase):
    def test_docker_ecosystem_is_monitored(self):
        data = yaml.safe_load(
            (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
        ecosystems = {u.get("package-ecosystem") for u in data.get("updates", [])}
        self.assertIn("docker", ecosystems,
                      "dependabot 缺 docker 生态 —— 镜像 CVE 修复不会被发现")
        self.assertIn("pip", ecosystems)
        self.assertIn("github-actions", ecosystems)


if __name__ == "__main__":
    unittest.main()
