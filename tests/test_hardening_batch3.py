#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 3 批加固的验收测试 —— 对应审查报告 H2 / M9。

这两项改的是**部署面**, 本地没有 Docker 可跑, 所以测试做成"配置契约":
把 haproxy.cfg 与 docker-compose.yml 当成被测对象, 断言它们表达出来的放行范围
恰好等于应用真正用到的范围 —— 既不能多(安全), 也不能少(功能)。

  H2  旧规则 `^…/containers(/|$)` 把 /containers 下所有 GET 都放行了, 其中
      /containers/<id>/archive 等于任意文件读, /containers/json 等于拓扑枚举。
      现在收窄到三个 sidecar 的 /json。
  M9  socket-proxy 此前全无加固, 却持有裸 docker.sock(等价宿主 root)。

为什么要有"名字必须与内核常量一致"这条:
  容器名是硬编码在 app.py 里的。若哪天有人改了常量却忘了改 haproxy.cfg,
  功能会静默失效(sidecar 状态永远 unknown), 而所有单测仍然绿。
"""

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import yaml  # noqa: E402

CFG_PATH = REPO_ROOT / "docker" / "socket-proxy" / "haproxy.cfg"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
CFG = CFG_PATH.read_text(encoding="utf-8")


def _cfg_code():
    """去掉注释行后的配置正文。

    注释里会引用旧规则原文(`containers(/|$)`)来解释为什么改, 所以"某字符串
    是否还在配置里"这类断言必须只看正文, 否则会被自己的说明文字误判。
    """
    return "\n".join(ln for ln in CFG.splitlines()
                     if not ln.lstrip().startswith("#"))


def _http_rules(kind):
    """取出所有 `http-request <kind> ...` 规则行。"""
    return [ln.strip() for ln in CFG.splitlines()
            if ln.strip().startswith(f"http-request {kind}")]


def _get_allow():
    hits = [r for r in _http_rules("allow") if "METH_GET" in r]
    assert len(hits) == 1, f"应恰好一条 GET allow 规则, 实际 {len(hits)} 条: {hits}"
    return hits[0]


def _post_allow():
    hits = [r for r in _http_rules("allow") if "METH_POST" in r]
    assert len(hits) == 1, f"应恰好一条 POST allow 规则, 实际 {len(hits)} 条: {hits}"
    return hits[0]


class TestH2AclNarrowed(unittest.TestCase):
    """H2: 放行范围必须收敛到应用真正在用的那几个端点。"""

    def test_broad_containers_rule_is_gone(self):
        """旧宽规则 `^…/containers(/|$)` 必须彻底消失。"""
        self.assertNotIn("containers(/|$)", _cfg_code(),
                         "宽规则还在: 它把 /archive、/json 列表、/export 一起放行了")

    def test_get_only_allows_three_sidecars_inspect(self):
        rule = _get_allow()
        for name in ("iso-hub-samba", "iso-hub-webdav", "iso-hub-qbittorrent"):
            self.assertIn(name, rule, f"GET 白名单缺少 {name}, 该 sidecar 状态会永远 unknown")
        # 规则形如 `... /containers/(a|b|c)/json$ }` —— 结尾还有闭合花括号。
        self.assertRegex(rule, r"/json\$\s*\}$",
                         "GET 白名单必须以 /json$ 收尾 —— 否则 /json/xxx 子路径也会被放行")
        self.assertNotIn("containers/json", rule.replace("/json$", ""),
                         "不应放行容器列表端点")

    def test_post_only_allows_lifecycle_on_three_sidecars(self):
        rule = _post_allow()
        for name in ("iso-hub-samba", "iso-hub-webdav", "iso-hub-qbittorrent"):
            self.assertIn(name, rule)
        self.assertIn("(start|stop|restart|update)", rule)
        for forbidden in ("create", "delete", "exec", "kill"):
            self.assertNotIn(forbidden, rule,
                             f"写白名单里不该出现 {forbidden}")

    def test_dangerous_endpoints_explicitly_denied(self):
        """即使将来正则被放宽, 这几个端点也要有独立 deny 兜底。"""
        denied = " ".join(_http_rules("deny"))
        for ep in ("archive", "export", "logs", "exec", "attach"):
            self.assertIn(f"/{ep}", denied, f"{ep} 缺少显式 deny 兜底")

    def test_healthcheck_endpoint_still_allowed(self):
        """compose 的 healthcheck 靠 /_ping, 收窄 ACL 时不能把它一起收掉。"""
        allows = " ".join(_http_rules("allow"))
        self.assertIn("_ping", allows)

    def test_names_match_kernel_constants(self):
        """haproxy.cfg 里的容器名必须与 app.py 的硬编码常量完全一致。"""
        import app
        expected = set(app.SHARE_CONTAINERS.values()) | {app.QB_CONTAINER}
        in_rule = set(re.findall(r"iso-hub-[a-z0-9-]+", _get_allow()))
        self.assertEqual(in_rule, expected,
                         "haproxy.cfg 与 app.py 的容器名不一致: 改名后 sidecar 管理会静默失效")


class TestM9SocketProxyHardened(unittest.TestCase):
    """M9: socket-proxy 容器自身的加固。"""

    def setUp(self):
        self.compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        self.svc = self.compose["services"]["socket-proxy"]

    def test_read_only_rootfs(self):
        self.assertIs(self.svc.get("read_only"), True)

    def test_capabilities_dropped(self):
        self.assertEqual(self.svc.get("cap_drop"), ["ALL"])

    def test_no_new_privileges(self):
        self.assertIn("no-new-privileges:true", self.svc.get("security_opt") or [])

    def test_tmpfs_for_runtime_dirs(self):
        mounts = self.svc.get("tmpfs") or []
        self.assertIn("/tmp", mounts)

    def test_does_not_shadow_docker_sock_with_tmpfs(self):
        """红线: 不能在 /run 上挂 tmpfs。

        容器里 /var/run 是 /run 的软链接, 而 docker.sock 按 /var/run/docker.sock
        挂进来。给 /run 挂 tmpfs 会把这个 bind mount 整个遮住: haproxy 照样启动、
        ACL 照样放行, 但每个请求都以 `503 SC--`(连不上后端) 结束, 面板上三个
        sidecar 的状态全变 unknown, 而日志里没有任何 ACL 相关线索。

        这条真的踩过 —— 第一版 M9 就加了 /run, 被 CI 的 socket-proxy 探针抓出来。
        """
        mounts = self.svc.get("tmpfs") or []
        self.assertNotIn("/run", mounts,
                         "/run 上的 tmpfs 会遮住 /var/run/docker.sock, 代理起来但全 503")

    def test_not_published_to_host(self):
        """只应在 compose 内部网络可达 —— 一旦映射到宿主, 等于把 Docker API 挂到网上。"""
        self.assertFalse(self.svc.get("ports"),
                         "socket-proxy 不得对外发布端口")

    def test_still_mounts_docker_sock_readonly(self):
        vols = self.svc.get("volumes") or []
        self.assertTrue(any("docker.sock" in v and v.endswith(":ro") for v in vols),
                        "docker.sock 必须以 :ro 挂载")

    def test_does_not_force_unknown_uid(self):
        """红线: 不能在没有宿主 docker 组 gid 的情况下把 user 写死成 10001。

        该容器必须打开 root:docker 0660 的 docker.sock; 换成 10001 会打不开,
        sidecar 管理直接全废, 而 CI 发现不了(只跑主镜像 smoke)。
        """
        self.assertNotIn("user", self.svc,
                         "降非 root 必须先拿到宿主 docker 组 gid 并实机验证, 不能盲写")


if __name__ == "__main__":
    unittest.main()
