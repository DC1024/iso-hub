# 网络共享（SMB / WebDAV）

ISO Hub 的 compose 会额外启动**两个只读共享容器**，把 `./data` 里的 ISO 分享给局域网/PVE/其他设备挂载。

| 协议 | 访问地址 | 默认账号/密码 |
|---|---|---|
| **SMB**（PVE/Windows 挂载） | `smb://<服务器IP>:1445/iso` | `iso` / `iso123` |
| **WebDAV** | `http://<服务器IP>:8081/dav` | `iso` / `iso123` |

两个共享都**只读**，防止误改 ISO。

## 在哪里管理

「设置 → 共享设置」面板可：
- 实时**启用/停用**每个共享容器（调 `/api/shares` 启停对应 docker 容器）
- **修改账号密码**（写入 `./data/settings.json`；运行中的共享需 restart 生效）

## PVE 挂载 SMB（把 ISO 当 PVE 存储）

数据中心 → 存储 → 添加 → **SMB/CIFS**：

| 字段 | 填 |
|---|---|
| 服务器 | `<服务器IP>` |
| 端口 | **1445** |
| 共享名 | `iso` |
| 用户名 / 密码 | `iso` / `iso123`（或你改过的） |

挂好后即可在 PVE 里直接选该存储的 ISO 装系统。

## Windows 挂载

- **SMB**：文件资源管理器地址栏输入 `\\<服务器IP>\iso`，或用 `net use`：
  ```bat
  net use X: \\118.89.25.55\iso /user:iso iso123
  ```
  若端口不是 445 而是 1445，Windows 需用 `\\<IP>\iso` 走默认 445；高位端口 1445 更适合 PVE/支持自定义端口的客户端，或临时映射。也可在系统里添加网络位置时指定端口。
- **WebDAV**：资源管理器「添加网络位置」，地址 `http://<IP>:8081/dav`。

## compose 片段（samba + webdav）

```yaml
  samba:
    image: dperson/samba:latest
    container_name: iso-hub-samba
    restart: unless-stopped
    ports:
      - "${SAMBA_PORT:-1445}:445"
      - "${SAMBA_NETBIOS:-1137}:137/udp"
      - "${SAMBA_138:-1138}:138/udp"
      - "${SAMBA_139:-1139}:139"
    volumes:
      - ./data:/srv/iso:ro
    environment:
      - TZ=Asia/Shanghai
      - USERID=${SAMBA_UID:-0}
      - GROUPID=${SAMBA_GID:-0}
    command: >-
      -p
      -u "${SAMBA_USER:-iso};${SAMBA_PASS:-iso123}"
      -s "iso;/srv/iso;no;no;no;${SAMBA_USER:-iso},${SAMBA_PASS:-iso123}"

  webdav:
    image: hacdias/webdav:latest
    container_name: iso-hub-webdav
    restart: unless-stopped
    ports:
      - "${WEBDAV_PORT:-8081}:6065"
    volumes:
      - ./data:/data:ro
      - ./data/webdav.yml:/config.yml:ro
    environment:
      - TZ=Asia/Shanghai
    command: ["-c", "/config.yml"]
```

`samba` 的 `-s "iso;/srv/iso;no;no;no;user,pass"` 含义：

| 字段 | 含义 |
|---|---|
| `iso` | 共享名 |
| `/srv/iso` | 容器内路径（映射到宿主机 `./data`） |
| `no` | 可写？(只读) |
| `no` | 仅列共享？ |
| `no` | 允许访客？ |
| `user,pass` | 可访问的用户 |

## 端口冲突说明

若宿主机**自带 Samba**（如极空间 Z4Pro 占用了 445/139/137/138），容器版 Samba 需映射到高位端口 **1445/1139/1137/1138** 避免冲突。若你的机器没跑原生 Samba，可用 `.env` 改回标准端口：

```ini
SAMBA_PORT=445
SAMBA_NETBIOS=137
SAMBA_138=138
SAMBA_139=139
```

> 改端口后，访问地址里的 `:1445` 也要同步改。

## 挂载失败排查

网络通、服务正常，但客户端挂载失败时，十有八九是**客户端填法**问题（尤其是极空间等 NAS 的挂载向导）：

- **不要**在地址栏写 `smb://` 前缀（很多向导不认协议前缀，协议是单独选的）。
- **拆字段填**：服务器IP 单独一栏、端口单独一栏、共享名 `iso` 单独一栏。
- 若只能填一个整体串：`<IP>:1445/iso`（不带 `smb://`）。

已在 Z4Pro 用 `smbclient` 实测：`//118.89.25.55/iso -p 1445 -U iso2%newpass888` 可成功列目录。服务端正常时，问题基本在客户端字段填法。