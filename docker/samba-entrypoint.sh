#!/bin/sh
# iso-hub SMB sidecar 启动脚本
#
# 为什么存在:
#   dperson/samba 只接受命令行参数 `-u "user;pass"` / `-s "...;users"` 来声明账号。
#   若把凭据直接写进 compose 的 command, 明文会长期留在容器的 .Config.Cmd 里 ——
#   `docker inspect`、`docker ps --no-trunc`、`docker compose config` 都能看到,
#   而改口令又必须"删除 + 重建容器", 正好需要 socket-proxy 放行 create/delete。
#   改成"启动时从只读文件生成参数"后, 密码只存在于该文件(0600)和本次启动的进程参数里,
#   容器创建后不再持有明文, proxy 也就可以永久关掉 create/delete。
#
# 账号文件格式(由主容器写入, 见 app.py::_apply_samba_creds):
#   每行 `用户名;密码`; 空行与 `#` 开头为注释。
#   没有有效账号时**拒绝启动** —— 宁可起不来, 也不能退化成一个匿名可读的共享。

set -eu

CONF="${SAMBA_USERS_FILE:-/samba-config/samba-users.conf}"

set -- -p
users=""

if [ -r "$CONF" ]; then
    while IFS=';' read -r _user _pass _rest; do
        [ -n "$_user" ] || continue
        case "$_user" in \#*) continue ;; esac
        [ -n "$_pass" ] || continue
        set -- "$@" -u "$_user;$_pass"
        users="${users:+$users,}$_user"
    done < "$CONF"
fi

if [ -z "$users" ]; then
    echo "[iso-hub-samba] 未在 $CONF 找到有效账号, 拒绝启动" >&2
    echo "[iso-hub-samba] 请先在 iso-hub 面板的「共享设置」里保存 SMB 凭据" >&2
    exit 1
fi

# 只读共享: browse=no readonly=yes guest=no, 允许的用户来自账号文件
set -- "$@" -s "iso;/srv/iso;no;yes;no;$users"

echo "[iso-hub-samba] 启动共享 iso -> /srv/iso, 账号: $users" >&2

if [ -x /usr/bin/samba.sh ]; then
    exec /usr/bin/samba.sh "$@"
fi

echo "[iso-hub-samba] 未找到 /usr/bin/samba.sh, 镜像与预期不符" >&2
exit 1
