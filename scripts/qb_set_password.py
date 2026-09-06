#!/usr/bin/env python3
"""给 qBittorrent 设置固定 WebUI 密码(写入 qBittorrent.conf 的 PBKDF2 哈希).

新版 qBittorrent(4.6+/5.x) 不再接受默认 adminadmin, 首次启动会生成临时密码,
导致 iso-hub 无法用 QB_PASS 登录。此脚本在**容器停止**时改写配置, 设置固定密码。

用法: python qb_set_password.py [--host 118.89.25.55] [--pw adminadmin]
"""
import argparse
import base64
import hashlib
import os
import sys

import paramiko

REMOTE_CONF = "/opt/iso-hub/qb-config/qBittorrent/qBittorrent.conf"
CONTAINER = "iso-hub-qbittorrent"

PATCH_SH = r'''#!/bin/sh
# 用法: sh /tmp/_qbpatch.sh '<pbkdf2值>' '<用户名>' '<conf路径>'
VAL="$1"; USER="$2"; CONF="$3"
python3 - "$VAL" "$USER" "$CONF" <<'PYEOF'
import sys, configparser
val, user, path = sys.argv[1], sys.argv[2], sys.argv[3]
c = configparser.ConfigParser(strict=False, allow_no_value=True)
c.optionxform = str  # 保留键名大小写
c.read(path, encoding='utf-8')
if not c.has_section('Preferences'):
    c.add_section('Preferences')
c.set('Preferences', 'WebUI\\Username', user)
c.set('Preferences', 'WebUI\\Password_PBKDF2', val)
with open(path, 'w', encoding='utf-8') as f:
    c.write(f)
print('patched', path)
PYEOF
'''


def make_pbkdf2(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return "@ByteArray(" + base64.b64encode(salt).decode() + ":" + base64.b64encode(key).decode() + ")"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="118.89.25.55")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", default="ubuntu")
    ap.add_argument("--pw", default="vbnDC2582")
    ap.add_argument("--qbpass", default="adminadmin", help="要设置的 qBittorrent WebUI 密码")
    ap.add_argument("--qbuser", default="admin")
    ap.add_argument("--sudo", action="store_true", default=True)
    args = ap.parse_args()

    val = make_pbkdf2(args.qbpass)
    print(f"generated Password_PBKDF2 (len {len(val)}) for user {args.qbuser}")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(args.host, port=args.port, username=args.user, password=args.pw, timeout=30)

    def run(cmd: str, timeout=120) -> str:
        print(f"$ {cmd[:100]}")
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print("[stderr]", err.rstrip())
        return out

    sftp = ssh.open_sftp()
    try:
        # 1. 停容器(避免退出时被覆盖)
        run(f"echo {args.pw} | sudo -S -p '' docker stop {CONTAINER}")
        # 2. 上传 patch 脚本
        with sftp.open("/tmp/_qbpatch.sh", "w") as f:
            f.write(PATCH_SH)
        run(f"echo {args.pw} | sudo -S -p '' chmod 755 /tmp/_qbpatch.sh")
        run(f"echo {args.pw} | sudo -S -p '' chmod 666 {REMOTE_CONF}")
        # 3. 写入哈希(注意 PBKDF2 值含特殊字符, 用单引号包裹)
        run(f"echo {args.pw} | sudo -S -p '' sh /tmp/_qbpatch.sh '{val}' '{args.qbuser}' '{REMOTE_CONF}'")
        # 4. 校验
        run(f"echo {args.pw} | sudo -S -p '' grep -a 'WebUI\\\\Username\\|WebUI\\\\Password_PBKDF2' {REMOTE_CONF} | cut -c1-60")
        # 5. 启动
        run(f"echo {args.pw} | sudo -S -p '' docker start {CONTAINER}")
        run(f"echo {args.pw} | sudo -S -p '' rm -f /tmp/_qbpatch.sh")
    finally:
        sftp.close()
        ssh.close()


if __name__ == "__main__":
    sys.exit(main())