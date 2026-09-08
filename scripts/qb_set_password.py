#!/usr/bin/env python3
"""给 qBittorrent 设置固定 WebUI 密码(写入 qBittorrent.conf 的 PBKDF2 哈希).

新版 qBittorrent(4.6+/5.x) 不再接受默认 adminadmin, 首次启动会生成临时密码,
导致 iso-hub 无法用 QB_PASS 登录。此脚本在**容器停止**时改写配置, 设置固定密码。

用法: python qb_set_password.py --host <服务器IP> --user <用户> --pw <SSH密码> [--qbpass adminadmin]

注意: 不要提交任何真实密码到版本库; --host/--user/--pw 均为必填, 无默认值。
"""
import argparse
import base64
import hashlib
import os
import shlex
import sys

import paramiko

REMOTE_CONF = "/opt/iso-hub/qb-config/qBittorrent/qBittorrent.conf"
CONTAINER = "iso-hub-qbittorrent"

PATCH_SH = r'''#!/bin/sh
# 用法: sh "$1" '<pbkdf2值>' '<用户名>' '<conf路径>'
SCRIPT="$1"; shift
python3 - "$@" <<'PYEOF'
import sys, configparser
val, user, path = sys.argv[1], sys.argv[2], sys.argv[3]
c = configparser.ConfigParser(strict=False, allow_no_value=True)
c.optionxform = str  # 保留键名大小写
c.read(path, encoding='utf-8')
if not c.has_section('Preferences'):
    c.add_section('Preferences')
c.set('Preferences', 'WebUI\\Username', user)
c.set('Preferences', 'WebUI\\Password_PBKDF2', val)
# 关键: 关闭 LocalHostAuth, 否则 qB 5.x 拒绝所有非 localhost(公网) 来源的 WebAPI 登录,
# 即使密码正确也返回 401. 必须在容器停止时写入(启动时 qB 会清掉 conf 里手动加的未知键).
c.set('Preferences', 'WebUI\\LocalHostAuth', 'false')
c.set('Preferences', 'WebUI\\HostHeaderValidation', 'false')
with open(path, 'w', encoding='utf-8') as f:
    c.write(f)
print('patched', path)
PYEOF
'''


def make_pbkdf2(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return "@ByteArray(" + base64.b64encode(salt).decode() + ":" + base64.b64encode(key).decode() + ")"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="SSH 服务器地址/IP")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", required=True, help="SSH 用户名")
    ap.add_argument("--pw", required=True, help="SSH 密码(不写默认值, 用环境变量或命令行传入)")
    ap.add_argument("--qbpass", default="adminadmin", help="要设置的 qBittorrent WebUI 密码")
    ap.add_argument("--qbuser", default="admin")
    ap.add_argument("--sudo", action="store_true", default=True)
    ap.add_argument(
        "--auto-add-host-key", action="store_true",
        help="自动接受未知主机密钥(默认使用 WarningPolicy; 生产环境建议先配置 known_hosts)",
    )
    args = ap.parse_args()

    val = make_pbkdf2(args.qbpass)
    print(f"generated Password_PBKDF2 (len {len(val)}) for user {args.qbuser}")

    ssh = paramiko.SSHClient()
    # 默认不信任未知主机密钥; 仅当用户显式 --auto-add-host-key 时才自动接受
    if args.auto_add_host_key:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # nosec B507
    else:
        ssh.set_missing_host_key_policy(paramiko.WarningPolicy())  # nosec B507
    ssh.connect(args.host, port=args.port, username=args.user, password=args.pw, timeout=30)

    def run(cmd: str, timeout=120) -> str:
        print(f"$ {cmd[:120]}")
        # 所有动态参数均经 shlex.quote 转义, 且命令由脚本硬编码模板生成。
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)  # nosec B601
        # 忽略 stdin 关闭报错(某些 sudo 配置会报 "sudo: no tty present")
        try:
            stdin.close()
        except Exception:  # noqa: BLE001
            pass
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print("[stderr]", err.rstrip())
        return out

    # 使用随机临时脚本路径, 避免固定 /tmp 被预占或注入
    remote_script = f"/tmp/_qbpatch_{os.urandom(8).hex()}.sh"  # nosec B108
    sftp = ssh.open_sftp()
    try:
        # 1. 停容器(避免退出时被覆盖)
        run(f"echo {shlex.quote(args.pw)} | sudo -S -p '' docker stop {shlex.quote(CONTAINER)}")
        # 2. 上传 patch 脚本
        with sftp.open(remote_script, "w") as f:
            f.write(PATCH_SH)
        run(f"echo {shlex.quote(args.pw)} | sudo -S -p '' chmod 755 {shlex.quote(remote_script)}")
        run(f"echo {shlex.quote(args.pw)} | sudo -S -p '' chmod 666 {shlex.quote(REMOTE_CONF)}")
        # 3. 写入哈希(用 shlex.quote 包裹所有参数, 避免 PBKDF2 值中的特殊字符被 shell 解析)
        run(
            f"echo {shlex.quote(args.pw)} | sudo -S -p '' sh {shlex.quote(remote_script)} "
            f"{shlex.quote(val)} {shlex.quote(args.qbuser)} {shlex.quote(REMOTE_CONF)}"
        )
        # 4. 校验
        run(
            f"echo {shlex.quote(args.pw)} | sudo -S -p '' grep -a "
            f"'WebUI\\\\Username\\|WebUI\\\\Password_PBKDF2' {shlex.quote(REMOTE_CONF)} | cut -c1-60"
        )
        # 5. 启动
        run(f"echo {shlex.quote(args.pw)} | sudo -S -p '' docker start {shlex.quote(CONTAINER)}")
        run(f"echo {shlex.quote(args.pw)} | sudo -S -p '' rm -f {shlex.quote(remote_script)}")
    finally:
        sftp.close()
        ssh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
