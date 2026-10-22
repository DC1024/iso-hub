#!/usr/bin/env python3
"""给 qBittorrent 设置固定 WebUI 密码(写入 qBittorrent.conf 的 PBKDF2 哈希).

新版 qBittorrent(4.6+/5.x) 不再接受默认 adminadmin, 首次启动会生成临时密码,
导致 iso-hub 无法用 QB_PASS 登录。此脚本在**容器停止**时改写配置, 设置固定密码。

用法: python qb_set_password.py --host <服务器IP> --user <用户> --pw <SSH密码> --qbpass <qB密码>

注意: 不要提交任何真实密码到版本库; --host/--user/--pw/--qbpass 均为必填, 无默认值。
     --qbpass 也可以用环境变量 QB_PASS 传入, 免得留在 shell 历史里。
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
# LocalHostAuth=false 是**取消** localhost 免密登录(默认 true = 本机访问不验密码)。
# 这里是收紧, 不是放宽: iso-hub 从容器网络访问 qB, 不该享受免密。
c.set('Preferences', 'WebUI\\LocalHostAuth', 'false')
# Host 头校验: 旧版本写 false, 只为让浏览器经映射端口访问 WebUI, 代价是把 DNS
# rebinding 防护整个拆掉。现在恢复 qB 的默认值(true)。
# qB 的规则是「Host 头里的端口必须等于连接落在的本端端口」, 所以 8080 一旦被映射
# 成别的号(如 8090), 浏览器访问会 401; 后端走 http://qbittorrent:8080 端口一致,
# 不受影响。必须显式写 true —— 老部署的 conf 里已经被写成 false, 不写不会自愈。
c.set('Preferences', 'WebUI\\HostHeaderValidation', 'true')
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
    ap.add_argument(
        "--qbpass", default=None,
        help="要设置的 qBittorrent WebUI 密码。没有默认值(旧版本的 adminadmin 等于公开口令); "
             "也可以改用环境变量 QB_PASS 传入, 免得留在 shell 历史里",
    )
    ap.add_argument("--qbuser", default="admin")
    ap.add_argument(
        "--no-sudo", action="store_true",
        help="远端以 root 直连时跳过 sudo(默认走 sudo -S, 口令只从 stdin 喂)",
    )
    ap.add_argument(
        "--auto-add-host-key", action="store_true",
        help="自动接受未知主机密钥(默认使用 WarningPolicy; 生产环境建议先配置 known_hosts)",
    )
    args = ap.parse_args()

    # 旧版本这里默认 adminadmin —— 那是 qB 的出厂口令, 等于没设密码。
    qbpass = args.qbpass or os.environ.get("QB_PASS") or ""
    if not qbpass:
        ap.error("必须提供 --qbpass 或环境变量 QB_PASS(不再有 adminadmin 默认值)")

    val = make_pbkdf2(qbpass)
    print(f"generated Password_PBKDF2 (len {len(val)}) for user {args.qbuser}")

    ssh = paramiko.SSHClient()
    # 默认不信任未知主机密钥; 仅当用户显式 --auto-add-host-key 时才自动接受
    if args.auto_add_host_key:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # nosec B507
    else:
        ssh.set_missing_host_key_policy(paramiko.WarningPolicy())  # nosec B507
    ssh.connect(args.host, port=args.port, username=args.user, password=args.pw, timeout=30)

    def run(cmd: str, timeout=120, sudo: bool = True) -> str:
        full = f"sudo -S -p '' {cmd}" if sudo else cmd
        print(f"$ {full[:120]}")
        # 所有动态参数均经 shlex.quote 转义, 且命令由脚本硬编码模板生成。
        stdin, stdout, stderr = ssh.exec_command(full, timeout=timeout)  # nosec B601
        if sudo:
            # 口令只走 stdin 通道。旧写法 `echo <pw> | sudo -S` 会让口令出现在远端
            # echo 进程的 argv 里 —— 同机任何用户 `ps aux` 都能看到, 且会进 shell 历史。
            # sudo -S 本来就从 stdin 读口令, 直接写进去即可, 不需要 echo。
            try:
                stdin.write(args.pw + "\n")
                stdin.flush()
            except (OSError, paramiko.SSHException):
                pass  # 通道已断; 下面的 stderr 会说明失败原因
        # 忽略 stdin 关闭报错(某些 sudo 配置会报 "sudo: no tty present")
        try:
            stdin.close()
        except (OSError, paramiko.SSHException):  # 通道已断时 close 报错可忽略
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
        sudo = not args.no_sudo
        # 1. 停容器(避免退出时被覆盖)
        run(f"docker stop {shlex.quote(CONTAINER)}", sudo=sudo)
        # 2. 上传 patch 脚本
        with sftp.open(remote_script, "w") as f:
            f.write(PATCH_SH)
        run(f"chmod 755 {shlex.quote(remote_script)}", sudo=sudo)
        run(f"chmod 666 {shlex.quote(REMOTE_CONF)}", sudo=sudo)
        # 3. 写入哈希(用 shlex.quote 包裹所有参数, 避免 PBKDF2 值中的特殊字符被 shell 解析)
        run(
            f"sh {shlex.quote(remote_script)} "
            f"{shlex.quote(val)} {shlex.quote(args.qbuser)} {shlex.quote(REMOTE_CONF)}",
            sudo=sudo,
        )
        # 4. 校验
        run(
            f"grep -a 'WebUI\\\\Username\\|WebUI\\\\Password_PBKDF2' "
            f"{shlex.quote(REMOTE_CONF)} | cut -c1-60",
            sudo=sudo,
        )
        # 5. 启动
        run(f"docker start {shlex.quote(CONTAINER)}", sudo=sudo)
        run(f"rm -f {shlex.quote(remote_script)}", sudo=sudo)
    finally:
        sftp.close()
        ssh.close()

    print(
        "\n注意: 已打开 qB 的 Host 头校验(qB 默认值)。它要求 Host 里的端口等于连接落在的\n"
        "      本端端口, 所以容器内 8080 被映射成别的号时(compose 默认 8090), 浏览器直接\n"
        "      访问 http://<IP>:8090 会返回 401。要么把映射改回 8080:8080(QB_PORT=8080),\n"
        "      要么只经 iso-hub 面板使用 qB。iso-hub 后端走 http://qbittorrent:8080, 不受影响。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
