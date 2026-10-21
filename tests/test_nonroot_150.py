#!/usr/bin/env python3
"""v1.5.0 「非 root 运行 + 只读根文件系统」回归测试。

这一版改的是**运行身份**而不是业务逻辑, 所以测试的重点不是"功能对不对", 而是
"加固有没有被后续改动悄悄退掉"。历史上已经踩过一次: v1.4.0 首次部署时,
`docker run` 不继承旧容器的 HostConfig, 脚本又写成"旧容器已有 CapDrop 就跳过传 flag",
结果新容器 CapDrop 变成空数组 —— 容器起来了、服务也正常, 但加固没了, 没人发现。

因此这里用**否定式断言**锁住几件事:
  1. 镜像**必须**以固定 uid/gid 10001 运行(而不是 root), 且 useradd/chown 在 USER 之前
     (顺序错了构建就直接失败, 但错误信息难懂, 提前在测试里说清楚)。
  2. compose **必须**同时有 user / read_only / tmpfs(/tmp 与 /home/isohub) / cap_drop /
     no-new-privileges。`read_only: true` 单独存在而没 tmpfs, gpg 会因为没有可写
     homedir 而 Fatal —— 而且它不报错, 只是从此不再输出指纹。
  3. `read_only: true` 的两个前提不能被破坏:
       · 不写 __pycache__ → PYTHONDONTWRITEBYTECODE=1
       · gpg 不往默认 $HOME/.gnupg 写 → 全部走 tempfile + 显式 GNUPGHOME
         (连 `--show-keys` 这种看起来只读的调用也算: 实测它仍会创建 pubring.kbx)
  4. 升级迁移必须能被发现: 数据目录不可写时启动**必须中止**并打印 chown 命令,
     不能"服务起来了但每次下载都在写盘处炸"。
  5. 资源上限(v1.5.3) **必须**完整: mem_limit / cpus / pids_limit 三项齐全。
     少任何一项都能让容器在校验大文件时挤占整台 NAS —— 同样不会报错, 只会让邻居
     服务变慢, 属于必须靠断言锁住的那一类。
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "web"))

import app  # noqa: E402

DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.acr.yml",
                 "docker-compose.dockerhub.yml")
RUNTIME_UID = "10001"


def _code_lines(text: str) -> str:
    """去掉整行注释。

    必须这么做: 注释里写着 `USER 10001:10001` 也能让 assertIn 过关, 这种"文档骗测试"
    的情况在这个仓库里真的发生过(compose 注释里引用了不存在的 README 章节)。
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# --------------------------------------------------------------------------- 镜像
class DockerfileNonRootTest(unittest.TestCase):
    def setUp(self):
        self.raw = DOCKERFILE.read_text(encoding="utf-8")
        self.code = _code_lines(self.raw)

    def test_runs_as_fixed_uid_gid(self):
        self.assertIn(f"USER {RUNTIME_UID}:{RUNTIME_UID}", self.code)
        # 明确禁止回退: USER root / USER 0 一旦出现, 整套加固就是摆设
        for bad in ("USER root", "USER 0", "USER 0:0"):
            self.assertNotIn(bad, self.code)

    def test_group_and_user_created_before_switching(self):
        self.assertIn(f"groupadd -g {RUNTIME_UID}", self.code)
        self.assertIn(f"useradd -u {RUNTIME_UID} -g {RUNTIME_UID}", self.code)
        self.assertLess(self.code.index(f"groupadd -g {RUNTIME_UID}"),
                        self.code.index(f"USER {RUNTIME_UID}:{RUNTIME_UID}"))

    def test_privileged_installs_happen_before_dropping(self):
        """apt/pip 必须在 USER 之前 —— 非 root 既装不了包也写不了 site-packages。"""
        user_at = self.code.index(f"USER {RUNTIME_UID}:{RUNTIME_UID}")
        for marker in ("apt-get install", "pip install"):
            self.assertLess(self.code.index(marker), user_at, marker)

    def test_home_and_gnupghome_are_set(self):
        # 只读根文件系统下, 任何 $HOME 写入都是硬失败; 显式给出可写家目录是最后一道保险
        self.assertIn("HOME=/home/isohub", self.code)
        self.assertIn("GNUPGHOME=/home/isohub/.gnupg", self.code)

    def test_data_dir_owned_by_runtime_user(self):
        self.assertRegex(self.code, rf"chown -R {RUNTIME_UID}:{RUNTIME_UID}[^\n]*/home/isohub")
        self.assertRegex(self.code, rf"chown -R {RUNTIME_UID}:{RUNTIME_UID}[^\n]*/data")

    def test_bytecode_writing_is_disabled(self):
        """read_only: true 的前提之一: import 期不生成 __pycache__。"""
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", self.code)


# --------------------------------------------------------------------------- compose
class ComposeHardeningTest(unittest.TestCase):
    def _service(self, name: str) -> dict:
        data = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
        return data["services"]["iso-hub"]

    def test_all_compose_files_run_non_root_and_readonly(self):
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                svc = self._service(name)
                self.assertEqual(str(svc.get("user")), f"{RUNTIME_UID}:{RUNTIME_UID}")
                self.assertIs(svc.get("read_only"), True)
                tmpfs = [str(t) for t in (svc.get("tmpfs") or [])]
                self.assertTrue(any(t.startswith("/tmp") for t in tmpfs),
                                f"{name}: read_only 没有配 tmpfs /tmp, gpg 会失败")
                self.assertTrue(any(t.startswith("/home/isohub") for t in tmpfs),
                                f"{name}: 没给 gpg homedir 挂 tmpfs, --show-keys 会 Fatal")

    def test_existing_hardening_not_regressed(self):
        """cap_drop 与 no-new-privileges 是 v1.3.21 的成果, 不能被这次改动顺手改掉。"""
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                svc = self._service(name)
                self.assertEqual(svc.get("cap_drop"), ["ALL"])
                self.assertIn("no-new-privileges:true", svc.get("security_opt") or [])

    def test_resource_limits_set_on_every_compose(self):
        """v1.5.3: 主容器必须有资源上限。

        为什么归在"加固"里: 它和 cap_drop 一样属于「删掉也不会报错, 出事才发现」的项。
        实测这台 NAS 15GB 内存常态只剩 4GB 可用, 而校验一个 30GB ISO 是长时间的
        CPU + IO 密集操作; 没有上限时容器能吃到整机内存, 连带同机的 PVE 备份卡死。
        生产用 `docker run` 部署, compose 只是同构参考 —— 所以部署脚本也必须透传
        --memory / --cpus / --pids-limit, 见 wiki/部署-快速开始.md。
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                svc = self._service(name)
                self.assertEqual(str(svc.get("mem_limit")), "1g",
                                 f"{name}: 缺 mem_limit, 容器可吃满宿主内存")
                self.assertEqual(float(svc.get("cpus") or 0), 2.0,
                                 f"{name}: 缺 cpus, 校验/下载会占满所有核心")
                self.assertEqual(int(svc.get("pids_limit") or 0), 256,
                                 f"{name}: 缺 pids_limit, 进程数失控时没有兜底")

    def test_qbittorrent_writes_as_the_same_uid(self):
        """qB 必须以 10001 落盘。

        反例很具体: qB 若仍以 root(PUID=0)写 /data, 主容器拿着 uid 10001 且没有
        DAC_OVERRIDE, 既校验不了也删不掉那些 ISO —— 下载功能表面正常, 一删除就 500。
        """
        for name in COMPOSE_FILES:
            with self.subTest(compose=name):
                data = yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))
                env = data["services"]["qbittorrent"]["environment"]
                self.assertIn(f"PUID=${{QB_PUID:-{RUNTIME_UID}}}", env)
                self.assertIn(f"PGID=${{QB_PGID:-{RUNTIME_UID}}}", env)


# --------------------------------------------------------------------------- 只读根文件系统的前提
class ReadonlyRootfsPrereqTest(unittest.TestCase):
    def test_gpg_never_touches_the_default_keyring(self):
        """gpg 的导入/导出/验签必须全部锁在 tempfile 里。

        上游最初是 `subprocess.run(["gpg", "--import"], ...)` 直接写默认 keyring
        ($HOME/.gnupg)。根文件系统只读后这条路直接断, 而且是很不直观的
        "gpg: failed to create temporary file" 报错。
        """
        src = (REPO_ROOT / "iso_download" / "download_linux.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("tempfile.TemporaryDirectory()"), 2)
        self.assertGreaterEqual(src.count("GNUPGHOME=home"), 2)

    def test_show_keys_gets_a_temp_home_too(self):
        """`gpg --show-keys` 同样会写 homedir —— 这是最容易漏的一条。

        实测(gpg 2.4.9): 对一个有效公钥跑 `--show-keys`, 即使只是打印指纹, 也会在
        GNUPGHOME 里创建 pubring.kbx / trustdb.gpg。homedir 不可写时 gpg 输出
        "Fatal: ... directory does not exist!" 且**一行指纹都不给**; 上层于是判定
        "缓存 keyring 指纹不匹配" → 每轮下载都删掉重建、重拉公钥, 公钥源恰好不可达
        时还会降级成只校验 SHA256。全程没有报错, 只有一条容易被无视的 ⚠ 日志。
        """
        src = (REPO_ROOT / "iso_download" / "download_linux.py").read_text(encoding="utf-8")
        # 锚在实际调用而不是文档字符串里的同名文字(第一次出现的位置是 docstring)
        at = src.index('"--show-keys", str(keyring)')
        self.assertIn("env=env", src[at:at + 400])

    def test_no_module_writes_inside_the_image_tree(self):
        """业务写盘不许落在 /app(镜像树)里 —— 那在 read_only 下必然失败。

        允许的写盘根: DATA_DIR、LOG_DIR、/qb-config、/webdav-config、/samba-config。
        这里只钉最容易复发的一条: 往模块自身目录(BASE_DIR)写文件。
        """
        for py in (REPO_ROOT / "web").glob("*.py"):
            src = py.read_text(encoding="utf-8")
            for bad in ("BASE_DIR /", "Path(__file__).resolve().parent /"):
                for line in src.splitlines():
                    if bad in line and ("open(" in line or "write_text" in line):
                        self.fail(f"{py.name}: 疑似往镜像树写文件 -> {line.strip()}")


# --------------------------------------------------------------------------- 启动自检
class PreflightTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    @staticmethod
    def _targets(*rows):
        return rows

    @staticmethod
    def _quiet():
        """自检会往 stdout 打诊断, 测试里没必要污染 CI 日志。"""
        return contextlib.redirect_stdout(io.StringIO())

    def test_writable_data_dir_passes(self):
        targets = self._targets(("data", "数据目录", self.dir, True))
        with patch.object(app, "_writable_targets", lambda: targets), self._quiet():
            self.assertTrue(app.preflight_writable())

    def test_unwritable_data_dir_aborts_startup(self):
        """核心断言: 数据目录写不了就不能继续启动, 且必须打印可照抄的迁移命令。"""
        targets = self._targets(("data", "数据目录", self.dir, True))
        buf = io.StringIO()
        with patch.object(app, "_writable_targets", lambda: targets), \
                patch.object(app, "_writable_probe", lambda d: "Permission denied"), \
                contextlib.redirect_stdout(buf):
            self.assertFalse(app.preflight_writable())
        out = buf.getvalue()
        self.assertIn("启动中止", out)
        # 断言"真的打印了", 而不只是常量里写着 —— 两者断链过一次就够呛了
        self.assertIn(f"chown -R {RUNTIME_UID}:{RUNTIME_UID}", out)

    def test_optional_mount_failure_does_not_abort(self):
        """qb/webdav/samba 是可选 sidecar: 它们的卷写不了不该拖垮核心功能。"""
        opt = self.dir / "qb"
        opt.mkdir()

        def probe(d):
            return "Permission denied" if Path(d) == opt else ""

        targets = self._targets(("data", "数据目录", self.dir, True),
                                ("qb_config", "qBittorrent 配置", opt, False))
        with patch.object(app, "_writable_targets", lambda: targets), \
                patch.object(app, "_writable_probe", probe), self._quiet():
            self.assertTrue(app.preflight_writable())
            self.assertFalse(app.writable_snapshot()["qb_config"])

    def test_missing_optional_mount_is_tolerated(self):
        missing = self.dir / "not-mounted"
        targets = self._targets(("data", "数据目录", self.dir, True),
                                ("samba_config", "Samba 凭据", missing, False))
        with patch.object(app, "_writable_targets", lambda: targets), self._quiet():
            self.assertTrue(app.preflight_writable())
            # 未挂载 = 确实写不进去, 快照如实报 False; 但它不是必需项, 所以不拦启动
            self.assertFalse(app.writable_snapshot()["samba_config"])

    def test_missing_required_dir_also_aborts(self):
        """必需目录"不存在"与"写不了"一样致命。

        反例: `docker run` 忘了 `-v ...:/data`, /data 就不存在。若这里放行, 结果是
        "服务能起、面板能开, 第一次刷新元数据就 FileNotFoundError" —— 比直接退出难查。
        """
        targets = self._targets(("data", "数据目录", self.dir / "nope", True))
        with patch.object(app, "_writable_targets", lambda: targets), self._quiet():
            self.assertFalse(app.preflight_writable())

    def test_probe_leaves_no_residue(self):
        self.assertEqual(app._writable_probe(self.dir), "")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_hint_contains_the_exact_migration_command(self):
        """报错信息必须能照着抄 —— 否则用户第一反应是回滚版本。"""
        self.assertIn(f"chown -R {RUNTIME_UID}:{RUNTIME_UID}", app._PREFLIGHT_HINT)
        for d in ("./data", "./qb-config", "./webdav-config", "./samba-config"):
            self.assertIn(d, app._PREFLIGHT_HINT)


# --------------------------------------------------------------------------- health
class HealthWritableTest(unittest.TestCase):
    KEYS = ("data", "logs", "qb_config", "webdav_config", "samba_config")

    def test_health_reports_every_mount(self):
        resp = app.app.test_client().get("/api/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("ok"))
        self.assertIn("writable", body)
        for key in self.KEYS:
            self.assertIn(key, body["writable"])
            self.assertIsInstance(body["writable"][key], bool)

    def test_writable_does_not_flip_ok(self):
        """写不了盘也不该让容器被判 unhealthy。

        容器重启治不了"宿主目录属主不对", 只会把启动自检打印的那条 chown 命令
        淹没在反复重启的日志里。诊断信息放在 writable 字段里, 由人或脚本去读。
        """
        with patch.object(app, "writable_snapshot", lambda *a, **k: {"data": False}):
            body = app.app.test_client().get("/api/health").get_json()
        self.assertTrue(body.get("ok"))


class ContainerSmokeWorkflowTest(unittest.TestCase):
    """CI 必须真的把容器跑起来一次。

    上面所有断言看的都是**文本**: Dockerfile 里写着 USER 10001、compose 里写着
    read_only, 断言就过。可 v1.5.0 那次事故恰恰是"声明全在、运行时没有" ——
    `docker run` 不继承旧容器 HostConfig, CapDrop 悄悄变成空数组, 服务依然 200,
    加固却没了。文本断言对这类问题零防御力。

    所以 container-smoke.yml 不是可选项: 它是唯一一个"镜像真的能起来、且是带着
    加固起来的"证据。这里锁的是它**不会退化** —— 要么 flag 被删, 要么换了个
    不带加固的启动方式, 要么 python 命令在 runner 上根本不存在。
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "container-smoke.yml"

    def setUp(self):
        self.assertTrue(self.WORKFLOW.is_file(),
                        "缺 .github/workflows/container-smoke.yml —— "
                        "没有它, 加固只剩下文本断言, 运行时退化无人发现")
        self.text = self.WORKFLOW.read_text(encoding="utf-8")

    def test_starts_a_real_container(self):
        self.assertIn("docker build", self.text)
        self.assertIn("docker run", self.text)

    def test_uses_the_same_flags_as_production(self):
        for flag in ("--user 10001:10001", "--read-only", "--cap-drop=ALL",
                     "no-new-privileges"):
            self.assertIn(flag, self.text, f"冒烟缺 {flag}, 测的就不是生产配置")

    def test_applies_resource_limits(self):
        for flag in ("--memory", "--cpus", "--pids-limit"):
            self.assertIn(flag, self.text, f"冒烟缺 {flag}")

    def test_probes_health_not_docker_healthcheck(self):
        """轮询 HTTP 而不是 docker healthcheck。

        healthcheck interval=60s, 靠它判断就绪会把 3 秒的验证拖到一分多钟,
        而且那是**容器自己**报的健康, 不是外部观测。
        """
        self.assertIn("/api/health", self.text)

    def test_inline_python_uses_python3_or_setup_python(self):
        """`python - <<'PY'` 在没装 setup-python 的 job 里会 command not found。

        ubuntu-latest 只有 python3。这是一类很典型的"本地能跑、CI 挂"的差异,
        而且失败信息完全指不到根因。
        """
        if "setup-python" in self.text:
            return
        self.assertNotIn("python - <<", self.text,
                         "内联脚本用了 python, 但 job 没有 setup-python")


if __name__ == "__main__":
    unittest.main()
