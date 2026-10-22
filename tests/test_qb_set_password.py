#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/qb_set_password.py 的护栏测试。

为什么必须测：这个脚本是「qBittorrent WebUI 密码」的唯一写入通道，而它的失败
**全是静默的**——命令跑完退出码 0、打印 "patched"，但 qB 起来之后照样 401。
三种典型静默失效：

1. PBKDF2 参数不对（哈希算法/轮数/盐长度/key 长度任一不符）⇒ qB 算不出同样的值，
   密码永远不对，但脚本会说"写好了"。
2. `optionxform` 没保留大小写 ⇒ configparser 把 `WebUI\\Password_PBKDF2` 写成
   小写，qB 按大小写敏感匹配，等于没写。
3. 漏掉 `LocalHostAuth=false` ⇒ qB 5.x 对所有非 localhost 来源直接 401，
   **即使密码完全正确**。这一条最容易在重构时被当成"多余的两个键"删掉。

所以本文件的重心不是"脚本能跑"，而是：
  * PBKDF2 输出能被 qB 的算法反向验证（盐 16B / key 64B / sha512 / 100000 轮）
  * 远端 patch 脚本**真的执行一遍**，落到合成 conf 上，再读回来验哈希
  * 四个键齐全、键名大小写保留、其它 section 不被破坏
  * SSH 密码进命令行前经 shlex.quote，不裸奔
  * 停容器 → 写配置 → 起容器 的顺序不能乱（顺序错了写进去会被 qB 退出时覆盖）
"""
import base64
import configparser
import hashlib
import importlib.util
import io
import re
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "qb_set_password.py"


def _install_fake_paramiko():
    """往 sys.modules 里塞一个 paramiko 替身。

    dev/CI 的 isohub venv 里没有 paramiko（它只在运维机的运维环境里需要，
    且带编译依赖）。为了测纯逻辑把整条依赖链装进 CI 不划算，因此注入替身：
    被测模块做 `import paramiko` 时拿到的是这个 stub，模块级代码能正常执行。
    """
    real = sys.modules.get("paramiko")
    if real is not None:
        return real
    fake = types.ModuleType("paramiko")

    class SSHException(Exception):
        pass

    class SSHClient:  # 真正的替身在用例里替换
        pass

    class AutoAddPolicy:
        pass

    class WarningPolicy:
        pass

    fake.SSHException = SSHException
    fake.SSHClient = SSHClient
    fake.AutoAddPolicy = AutoAddPolicy
    fake.WarningPolicy = WarningPolicy
    sys.modules["paramiko"] = fake
    return fake


PARAMIKO = _install_fake_paramiko()

_spec = importlib.util.spec_from_file_location("qb_set_password", str(SCRIPT))
qb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qb)


# --------------------------------------------------------------------------
# PBKDF2
# --------------------------------------------------------------------------

class MakePbkdf2Test(unittest.TestCase):
    """`@ByteArray(salt:hash)` 必须能被 qBittorrent 的算法反向验证。"""

    VAL = re.compile(r"^@ByteArray\(([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)\)$")

    def _split(self, val):
        m = self.VAL.match(val)
        self.assertIsNotNone(m, f"格式不是 @ByteArray(salt:hash)：{val!r}")
        return m.group(1), m.group(2)

    def test_format(self):
        val = qb.make_pbkdf2("adminadmin")
        self._split(val)

    def test_salt_and_key_lengths(self):
        """盐 16 字节、派生 key 64 字节 —— qB 写出来就是这个长度。"""
        salt_b64, key_b64 = self._split(qb.make_pbkdf2("x"))
        self.assertEqual(len(base64.b64decode(salt_b64)), 16, "盐长度不对")
        self.assertEqual(len(base64.b64decode(key_b64)), 64, "派生 key 长度不对")

    def test_verifiable_by_qbittorrent_algorithm(self):
        """核心断言：拿输出里的盐重新派生，必须等于输出里的 key。

        这一条同时钉死算法（sha512）、轮数（100000）、key 长度（64）与
        「盐在前、哈希在后」的顺序。任一参数漂移，qB 侧就永远验不过，
        而脚本依旧打印成功 —— 正是最该被抓到的静默失效。
        """
        password = "s3cret-p@ss"
        salt_b64, key_b64 = self._split(qb.make_pbkdf2(password))
        salt = base64.b64decode(salt_b64)
        expect = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100000, 64)
        self.assertEqual(base64.b64encode(expect).decode(), key_b64)

    def test_salt_is_fresh_every_call(self):
        """同一个密码两次生成必须不同 —— 盐写死等于全世界的哈希都一样。"""
        a = self._split(qb.make_pbkdf2("same"))
        b = self._split(qb.make_pbkdf2("same"))
        self.assertNotEqual(a[0], b[0], "两次调用的盐相同，说明盐被固定了")
        self.assertNotEqual(a[1], b[1], "盐不同则派生值必然不同")

    def test_non_ascii_password(self):
        """密码按 UTF-8 编码派生；中文密码不能抛异常，且可被反向验证。"""
        password = "中文密码-含符号:!@#"
        salt_b64, key_b64 = self._split(qb.make_pbkdf2(password))
        salt = base64.b64decode(salt_b64)
        expect = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100000, 64)
        self.assertEqual(base64.b64encode(expect).decode(), key_b64)

    def test_output_has_no_colon_outside_separator(self):
        """`:` 是盐与哈希的分隔符，base64 字符集里不含它，否则解析会错位。"""
        vals = [qb.make_pbkdf2(p) for p in ("a", "", "long" * 40)]
        for v in vals:
            self.assertEqual(v.count(":"), 1, f"分隔符不唯一：{v[:40]}...")


# --------------------------------------------------------------------------
# 远端 patch 脚本：静态契约
# --------------------------------------------------------------------------

def _payload() -> str:
    """从 PATCH_SH 里抠出内嵌的 Python 程序（heredoc 正文）。

    真正改配置的是这段 Python，不是外面那层 sh 包装。抠出来直接跑，
    既能测到真实逻辑，又不必让 CI 依赖 `sh` / `python3` 命令名。
    """
    m = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF\n", qb.PATCH_SH, re.S)
    assert m, "PATCH_SH 里找不到 'PYEOF' heredoc —— 结构被改坏了"
    return m.group(1)


class PatchScriptContractTest(unittest.TestCase):
    """PATCH_SH 是唯一在远端以 root 执行的东西，先把它的静态契约钉住。"""

    def test_heredoc_is_quoted(self):
        """`<<'PYEOF'` 带引号 ⇒ 正文不做任何 shell 展开。

        漏掉引号的话，正文里的 `$`、反引号会被远端 shell 先解释一遍，
        而这段代码里有 `$@`、`$1` 之类的东西，改错一个字就静默跑偏。
        """
        self.assertIn("<<'PYEOF'", qb.PATCH_SH)
        self.assertNotIn('<<"PYEOF"', qb.PATCH_SH)

    def test_passes_argv_through(self):
        """sh 包装必须把参数原样转交给 python（shift 掉脚本自身路径）。"""
        self.assertIn('SCRIPT="$1"; shift', qb.PATCH_SH)
        self.assertIn('python3 - "$@"', qb.PATCH_SH)

    def test_sets_all_four_keys(self):
        payload = _payload()
        for key in ("WebUI\\\\Username", "WebUI\\\\Password_PBKDF2",
                    "WebUI\\\\LocalHostAuth", "WebUI\\\\HostHeaderValidation"):
            self.assertIn(f"'{key}'", payload,
                          f"payload 里没有 {key.replace(chr(92) * 2, chr(92))}")

    def test_disables_local_host_auth(self):
        """qB 5.x 的关键开关：不开这个，密码对了也 401。

        注释里写得很清楚是「关键」，重构时最容易被当成冗余删掉，故单独钉一条。
        """
        payload = _payload()
        self.assertRegex(payload, r"LocalHostAuth',\s*'false'")
        self.assertRegex(payload, r"HostHeaderValidation',\s*'false'")

    def test_preserves_key_case(self):
        """`c.optionxform = str` 是「大小写保留」的唯一开关。

        默认的 ConfigParser 会把键名转小写 → 写出来的键 qB 认不出来 →
        表现为"改了但登录还是失败"，且退出码 0。
        """
        self.assertIn("c.optionxform = str", _payload())

    def test_parser_is_lenient(self):
        """真实 conf 里存在重复键与无值键，strict 解析会直接抛异常。"""
        payload = _payload()
        self.assertIn("strict=False", payload)
        self.assertIn("allow_no_value=True", payload)

    def test_reads_and_writes_utf8(self):
        payload = _payload()
        self.assertIn("encoding='utf-8'", payload)


# --------------------------------------------------------------------------
# 远端 patch 脚本：真跑一遍
# --------------------------------------------------------------------------

FIXTURE = """[BitTorrent]
Session\\DefaultSavePath=/downloads
Session\\Port=6881

[LegalNotice]
Accepted=true

[Preferences]
WebUI\\Port=8080
WebUI\\Username=admin
WebUI\\Password_PBKDF2="@ByteArray(b2xkc2FsdA==:b2xkaGFzaA==)"
WebUI\\LocalHostAuth=true
WebUI\\HostHeaderValidation=true
WebUI\\Address=*
WebUI\\Address=0.0.0.0
WebUI\\RootFolder=
"""


class PatchScriptExecutionTest(unittest.TestCase):
    """把 payload 真跑在合成 conf 上，再读回来验证 —— 端到端，不是纸面检查。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "qBittorrent.conf"
        self.conf.write_text(FIXTURE, encoding="utf-8")

    def _apply(self, password="newpass", user="admin"):
        val = qb.make_pbkdf2(password)
        r = subprocess.run(
            [sys.executable, "-", val, user, str(self.conf)],
            input=_payload(), text=True, capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0,
                         f"payload 执行失败:\nstdout={r.stdout}\nstderr={r.stderr}")
        self.assertIn("patched", r.stdout)
        return val

    def _read(self) -> configparser.ConfigParser:
        c = configparser.ConfigParser(strict=False, allow_no_value=True)
        c.optionxform = str
        c.read(str(self.conf), encoding="utf-8")
        return c

    def test_hash_written_and_verifiable(self):
        """写进 conf 的哈希，必须能用同一个密码反算出来。"""
        val = self._apply("newpass")
        prefs = self._read()["Preferences"]
        got = prefs["WebUI\\Password_PBKDF2"]
        self.assertEqual(got, val, "conf 里的值与生成值不一致")

        salt_b64, key_b64 = re.match(r"^@ByteArray\(([^:]+):(.+)\)$", got).groups()
        salt = base64.b64decode(salt_b64)
        expect = hashlib.pbkdf2_hmac("sha512", b"newpass", salt, 100000, 64)
        self.assertEqual(base64.b64encode(expect).decode(), key_b64)

    def test_key_case_survives_roundtrip(self):
        """读回来的键名必须是原来的大小写，不能变成 webui\\password_pbkdf2。"""
        self._apply()
        keys = list(self._read()["Preferences"].keys())
        self.assertIn("WebUI\\Password_PBKDF2", keys)
        self.assertIn("WebUI\\Username", keys)
        self.assertFalse([k for k in keys if k.startswith("webui")],
                         f"键名被转成小写了: {keys}")

    def test_all_four_keys_set(self):
        self._apply(user="operator")
        prefs = self._read()["Preferences"]
        self.assertEqual(prefs["WebUI\\Username"], "operator")
        self.assertEqual(prefs["WebUI\\LocalHostAuth"], "false")
        self.assertEqual(prefs["WebUI\\HostHeaderValidation"], "false")
        self.assertTrue(prefs["WebUI\\Password_PBKDF2"].startswith("@ByteArray("))

    def test_other_sections_preserved(self):
        """只动 Preferences 里的四个键，别把 [BitTorrent] / [LegalNotice] 抹掉。

        抹掉 [LegalNotice] 会让 qB 重新弹协议、重生成临时密码；
        抹掉 [BitTorrent] 的下载路径会让已有的种子全部指向默认目录。
        """
        self._apply()
        c = self._read()
        self.assertEqual(c["LegalNotice"]["Accepted"], "true")
        self.assertEqual(c["BitTorrent"]["Session\\DefaultSavePath"], "/downloads")
        self.assertEqual(c["BitTorrent"]["Session\\Port"], "6881")

    def test_unrelated_preferences_untouched(self):
        self._apply()
        prefs = self._read()["Preferences"]
        self.assertEqual(prefs["WebUI\\Port"], "8080")

    def test_duplicate_key_does_not_raise(self):
        """夹具里 `WebUI\\Address` 写了两遍 —— strict=True 会直接抛，脚本就废了。"""
        self._apply()
        self.assertEqual(self._read()["Preferences"]["WebUI\\Address"], "0.0.0.0")

    def test_plaintext_password_never_written(self):
        """conf 里只能出现哈希，明文密码一个字节都不许落地。"""
        self._apply(password="PlainTextShouldNotLeak")
        self.assertNotIn("PlainTextShouldNotLeak",
                         self.conf.read_text(encoding="utf-8"))

    def test_creates_preferences_section_when_absent(self):
        """conf 是空的（全新安装）也要能建出 [Preferences]。"""
        self.conf.write_text("", encoding="utf-8")
        self._apply()
        self.assertIn("Preferences", self._read().sections())
        self.assertTrue(self._read()["Preferences"]["WebUI\\Password_PBKDF2"])


# --------------------------------------------------------------------------
# main()：SSH 编排
# --------------------------------------------------------------------------

class _FakeStream:
    def __init__(self, data=b""):
        self._data = data
        self.closed = False

    def read(self):
        return self._data

    def close(self):
        self.closed = True


class _FakeSFTP:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False

    def open(self, path, mode="r"):
        sftp = self

        class _FH:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, data):
                sftp.owner.uploaded[path] = data

        return _FH()

    def close(self):
        self.closed = True


class _FakeSSH:
    instances = []

    def __init__(self):
        self.commands = []
        self.policy = None
        self.connect_kw = None
        self.uploaded = {}
        self.closed = False
        self.sftp_closed = False
        _FakeSSH.instances.append(self)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, host, **kw):
        self.connect_kw = (host, kw)

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        return _FakeStream(), _FakeStream(b""), _FakeStream(b"")

    def open_sftp(self):
        self._sftp = _FakeSFTP(self)
        self.sftp_closed = False

        def close():
            self.sftp_closed = True

        self._sftp.close = close
        return self._sftp

    def close(self):
        self.closed = True


class MainTest(unittest.TestCase):
    """编排顺序、密钥引用方式、主机密钥策略。SSH 全程替身，不联网。"""

    def setUp(self):
        _FakeSSH.instances.clear()
        self._real_client = PARAMIKO.SSHClient
        PARAMIKO.SSHClient = _FakeSSH
        self.addCleanup(setattr, PARAMIKO, "SSHClient", self._real_client)

    def _run(self, extra=(), pw="sshpw"):
        argv = ["qb_set_password.py", "--host", "10.0.0.1", "--user", "root", "--pw", pw]
        argv.extend(extra)
        old = sys.argv
        sys.argv = argv
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = qb.main()
        finally:
            sys.argv = old
        return rc, _FakeSSH.instances[-1], buf.getvalue()

    def test_returns_zero_and_closes(self):
        rc, ssh, _out = self._run()
        self.assertEqual(rc, 0)
        self.assertTrue(ssh.closed, "SSH 连接必须关闭，否则运维机上会攒僵尸通道")
        self.assertTrue(ssh.sftp_closed, "SFTP 必须关闭")

    def test_connect_uses_explicit_timeout(self):
        _rc, ssh, _o = self._run()
        host, kw = ssh.connect_kw
        self.assertEqual(host, "10.0.0.1")
        self.assertEqual(kw["username"], "root")
        self.assertEqual(kw["timeout"], 30, "没有超时会让脚本永久挂住")

    def test_stop_patch_start_ordering(self):
        """顺序即正确性：容器还在跑时写的 conf，qB 退出时会整份覆盖回去。

        这是本脚本最容易"看起来能跑但实际无效"的地方，必须在测试里钉死。
        """
        _rc, ssh, _o = self._run()
        i_stop = next(i for i, c in enumerate(ssh.commands) if "docker stop" in c)
        i_patch = next(i for i, c in enumerate(ssh.commands) if "qBittorrent.conf" in c
                       and "grep" not in c)
        i_start = next(i for i, c in enumerate(ssh.commands) if "docker start" in c)
        self.assertLess(i_stop, i_patch, "必须先停容器再改配置")
        self.assertLess(i_patch, i_start, "必须先改配置再起容器")
        self.assertIn("iso-hub-qbittorrent", ssh.commands[i_stop])

    def test_ssh_password_is_quoted(self):
        """SSH 密码来自命令行，含 shell 元字符时绝不能裸奔进远端命令。"""
        pw = "p'a s;s$wd`id`"
        _rc, ssh, _o = self._run(pw=pw)
        quoted = shlex.quote(pw)
        self.assertNotEqual(quoted, pw, "夹具密码应需要转义")
        for cmd in ssh.commands:
            if pw in cmd:
                # 只允许以转义后的形态出现
                self.assertNotIn(pw, cmd.replace(quoted, ""),
                                 f"密码未转义就进了命令: {cmd[:120]}")

    def test_uses_sudo_with_stdin_password(self):
        """`sudo -S -p ''`：密码走 stdin，且不打印提示语污染输出解析。"""
        _rc, ssh, _o = self._run()
        sudo_cmds = [c for c in ssh.commands if "sudo" in c]
        self.assertTrue(sudo_cmds)
        for c in sudo_cmds:
            self.assertIn("sudo -S -p ''", c)

    def test_remote_script_path_is_random(self):
        """临时脚本名必须每次不同 —— 固定路径可被预先占位/替换。"""
        _r1, s1, _o = self._run()
        _r2, s2, _o = self._run()
        p1 = [p for p in s1.uploaded]
        p2 = [p for p in s2.uploaded]
        self.assertEqual(len(p1), 1)
        self.assertTrue(p1[0].startswith("/tmp/_qbpatch_"))
        self.assertNotEqual(p1[0], p2[0], "两次运行用了同一个临时路径")

    def test_uploads_the_patch_verbatim(self):
        """上传的必须就是 PATCH_SH 本体，不能被就地改写。"""
        _rc, ssh, _o = self._run()
        (path, body), = ssh.uploaded.items()
        self.assertEqual(body, qb.PATCH_SH)

    def test_cleans_up_remote_script(self):
        _rc, ssh, _o = self._run()
        last = ssh.commands[-1]
        self.assertIn("rm -f", last)
        self.assertIn("/tmp/_qbpatch_", last)

    def test_default_host_key_policy_rejects_unknown(self):
        """默认只告警不自动接受 —— 自动接受未知主机密钥等于关掉 MITM 防护。"""
        _rc, ssh, _o = self._run()
        self.assertIsInstance(ssh.policy, PARAMIKO.WarningPolicy)
        self.assertNotIsInstance(ssh.policy, PARAMIKO.AutoAddPolicy,
                                 "默认不能自动接受未知主机密钥")

    def test_opt_in_auto_add_host_key(self):
        _rc, ssh, _o = self._run(extra=["--auto-add-host-key"])
        self.assertIsInstance(ssh.policy, PARAMIKO.AutoAddPolicy)


class CliTest(unittest.TestCase):
    """凭据参数没有默认值；缺了就非零退出，不能猜一个去连。"""

    def test_required_args_have_no_default(self):
        """三个凭据参数都没有默认值 —— 缺任一个就 argparse 直接退出(2)。

        有默认值意味着脚本会拿一个猜的值去连生产机，比直接失败危险得多。
        """
        vals = {"--host": "10.0.0.1", "--user": "root", "--pw": "p"}
        for missing in vals:
            with self.subTest(missing=missing):
                argv = ["qb_set_password.py"]
                for flag, v in vals.items():
                    if flag != missing:
                        argv += [flag, v]
                old = sys.argv
                sys.argv = argv
                try:
                    with redirect_stderr(io.StringIO()), \
                            self.assertRaises(SystemExit) as cm:
                        qb.main()
                finally:
                    sys.argv = old
                self.assertEqual(cm.exception.code, 2,
                                 f"{missing} 缺失时不应进入主流程")

    def test_help_mentions_usage(self):
        """`--help` 必须正常退出并说清用法。

        走进程内调用而不是 subprocess：dev/CI 的 venv 里没有 paramiko，
        子进程会在 import 阶段就炸掉，测的就不是 argparse 了。
        本文件的 paramiko 替身只在当前进程的 sys.modules 里。
        """
        old = sys.argv
        sys.argv = ["qb_set_password.py", "--help"]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    qb.main()
        finally:
            sys.argv = old
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("SSH", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
