#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wiki 快速开始文档里的 compose 示例必须符合仓库的 Docker 权限约定。

背景
----
`wiki/部署-快速开始.md` 是用户**照抄部署**的地方。它过去让主容器直接挂载裸
`/var/run/docker.sock`(等价宿主机 root), 与仓库 `docker-compose*.yml` 的
socket-proxy 方案相反 —— 照抄文档就会部署出一个权限过大的实例。

仓库内的 compose 有 `test_docker_service_state.TestComposeConfig` 盯着, 但它只读
`COMPOSE_FILES` 那三个文件, **文档里的代码块没人管**, 于是悄悄退化了。
本测试把该文档中每个 ```yaml 块当作 compose 文件解析, 复用同一套断言。

范围说明: 只检查这份「完整 compose 示例」所在的文档。像 `wiki/种子下载.md` 里
片段式的 YAML(只是给某个服务追加一条 volumes)不是完整 compose, 不在此列。
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "wiki" / "部署-快速开始.md"

WHITELIST_ON = {"CONTAINERS": "1", "POST": "1"}
WHITELIST_OFF = {"IMAGES": "0", "VOLUMES": "0", "NETWORKS": "0",
                 "EXEC": "0", "SYSTEM": "0"}


def _env_dict(service):
    """environment 可能是 list 或 dict, 统一成 {k: v}。"""
    env = service.get("environment", [])
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out = {}
    for item in env:
        k, _, v = str(item).partition("=")
        out[k] = v
    return out


class TestWikiDeployDocCompose(unittest.TestCase):
    """文档里的 compose 示例与仓库 compose 守同一套安全约定。"""

    @classmethod
    def setUpClass(cls):
        try:
            import yaml
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("pyyaml 不可用, 跳过文档 compose 校验")
        cls._text = DOC.read_text(encoding="utf-8")
        blocks = re.findall(r"```yaml\n(.*?)```", cls._text, re.S)
        cls._docs = [yaml.safe_load(b) for b in blocks]

    def test_doc_exists_with_compose_examples(self):
        """文档存在, 且至少有一个可解析、含 iso-hub 服务的 compose 示例。"""
        self.assertTrue(DOC.exists(), "%s 不存在" % DOC)
        self.assertGreaterEqual(len(self._docs), 1, "文档里没有 ```yaml 示例")
        for i, doc in enumerate(self._docs):
            with self.subTest(block=i):
                self.assertIsInstance(doc, dict)
                self.assertIn("iso-hub", (doc.get("services") or {}),
                              "第 %d 个 yaml 块里没有 iso-hub 服务" % i)

    def test_main_container_drops_raw_sock(self):
        """主容器不得挂载裸 /var/run/docker.sock(它等价宿主机 root)。"""
        for i, doc in enumerate(self._docs):
            with self.subTest(block=i):
                ih = doc["services"]["iso-hub"]
                for v in ih.get("volumes", []):
                    self.assertNotIn("docker.sock", str(v),
                                     "文档示例 %d: 主容器仍挂载裸 docker.sock!" % i)

    def test_main_container_uses_socket_proxy(self):
        """主容器经 socket-proxy 访问 Docker API, 并等它就绪再启动。"""
        for i, doc in enumerate(self._docs):
            with self.subTest(block=i):
                ih = doc["services"]["iso-hub"]
                self.assertEqual(_env_dict(ih).get("DOCKER_HOST"),
                                 "tcp://socket-proxy:2375",
                                 "文档示例 %d: 主容器 DOCKER_HOST 未指向 socket-proxy" % i)
                dep = ih.get("depends_on", [])
                deps = list(dep.keys()) if isinstance(dep, dict) else list(dep)
                self.assertIn("socket-proxy", deps,
                              "文档示例 %d: 主容器缺 depends_on: socket-proxy" % i)

    def test_socket_proxy_service_hardened(self):
        """socket-proxy: 存在、镜像锁 digest、白名单收窄、裸 sock 只读挂载。"""
        for i, doc in enumerate(self._docs):
            with self.subTest(block=i):
                svc = (doc.get("services") or {}).get("socket-proxy")
                self.assertIsNotNone(svc, "文档示例 %d: 缺 socket-proxy 服务" % i)

                image = str(svc.get("image", ""))
                self.assertIn("@sha256:", image, "文档示例 %d: 镜像未锁 digest" % i)
                self.assertNotIn(":latest", image, "文档示例 %d: 镜像不得用 :latest" % i)
                self.assertEqual(len(image.split("@sha256:", 1)[1]), 64,
                                 "文档示例 %d: digest 长度应为 64" % i)

                env = _env_dict(svc)
                for k, v in WHITELIST_ON.items():
                    self.assertEqual(env.get(k), v, "文档示例 %d: %s 应为 %s" % (i, k, v))
                for k, v in WHITELIST_OFF.items():
                    self.assertEqual(env.get(k), v, "文档示例 %d: %s 应为 %s" % (i, k, v))

                vols = [v for v in svc.get("volumes", []) if "docker.sock" in str(v)]
                self.assertEqual(len(vols), 1, "文档示例 %d: 应恰好挂 1 个 docker.sock" % i)
                self.assertTrue(str(vols[0]).endswith(":ro"),
                                "文档示例 %d: docker.sock 须只读挂载" % i)

    def test_doc_documents_no_socket_degradation(self):
        """文档必须说明「不挂 socket」时的降级行为(否则用户会以为面板坏了)。"""
        self.assertIn("降级", self._text, "文档未说明不挂 socket 时的降级行为")
        self.assertIn("关于 Docker 权限", self._text, "文档缺少「关于 Docker 权限」说明小节")

    def test_wiki_mirror_in_sync_with_pages(self):
        """文档里的每个 compose 示例不得再出现「主容器直连 sock」式的旧写法。"""
        # 反向对照: 旧写法(裸挂载, 无 :ro)若回归, 上面的断言会红;
        # 这里额外钉住"裸挂载"这个具体字符串不再出现。
        self.assertNotIn("- /var/run/docker.sock:/var/run/docker.sock\n", self._text,
                         "文档里重新出现了裸 docker.sock 挂载")


if __name__ == "__main__":
    unittest.main()
