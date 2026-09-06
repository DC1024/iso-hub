# ISO Hub · 网页版 Linux 发行版 ISO 自动更新器

> **项目源码 / 完整文档：** <https://github.com/DC1024/iso-hub> 　·　
> **Docker Hub 镜像：** <https://hub.docker.com/r/dcchendockeruser/iso-hub>

把上游 [Sowevo/iso_download](https://github.com/Sowevo/iso_download)（纯 CLI）封装成
**带网页界面的 Docker 服务**：网页上勾选要下载的发行版/版本，后台实时下载、SHA256 校验；
一键抓取镜像站目录自动刷新"最新版本"元数据；过期旧 ISO 可视化清理。

## 功能

| 能力 | 说明 |
|---|---|
| 📋 发行版/版本列表 | 按发行版分组展示（Arch/Ubuntu/Fedora/Deepin/Proxmox/CentOS），含本地是否已下载 |
| ⬇ 选择性下载 | 可勾选**单个版本文件**或整组，后台顺序下载，日志实时输出 |
| ⤓ 元数据自动更新 | 抓取清华镜像站目录，把最新版本写入持久化清单（默认每组保留最新 3 个版本） |
| ✓ SHA256 校验 | 下载后自动校验（在线 checksum 优先，其次清单内置值） |
| 🗑 过期清理 | 标记并一键删除"不在最新清单中"的旧 ISO（如 Ubuntu 25.10 → 26.04 后被淘汰的版本） |
| 📊 实时进度 | 顶部任务条显示当前文件、已下载大小与速率；下方日志面板实时滚动 |
| 🔗 自定义源 | 「自定义源」页可添加官方/第三方镜像站的**任意直链**，独立保存、不会被元数据刷新覆盖 |
| ⭐ 订阅同步 | 每个发行版可设「订阅」：自动刷元数据→下载最新 N 版→删旧版，内置调度器定时全自动执行 |
| 🌓 日夜模式 | 右上角一键切换日/夜间主题，记忆到本地并跟随系统深色偏好 |
| 📡 网络共享 | 内置 SMB + WebDAV 共享容器，把 `./data` 里的 ISO 分享给 PVE/Windows/其他设备挂载 |
| 🗂 分组折叠 | 每个发行版组头部可折叠/展开版本列表，状态记忆到浏览器 |
| 🌐 中英双语 | 全界面支持 中文 / English / 自动（跟随系统）三态切换，记忆到本地 |
| ⚙️ 设置菜单 | 统一面板：用户登录、语言切换、网络共享、定时任务、受保护列表 |
| 🔍 筛选 + 收藏 | 镜像列表按 全部/已下载/未下载/已收藏 筛选；每行可 ★ 收藏 |
| 🔒 镜像保护 | 每行可锁定，受保护文件在过期清理/订阅同步时不会被删除 |
| 👤 用户登录 | 单管理员账号（PBKDF2-SHA256 加密 + 会话），首次登录自动创建管理员 |
| ⏰ 定时任务 | 网页自建调度（每天/每周/每月/每年/一次性），定时自动触发订阅同步 |
| 🖼 Logo | 内嵌 Syncbox Logo（base64 data URI，零外部资源） |

默认数据源为**清华 TUNA 镜像站**（国内速度快）。支持的版本策略见上游 `sources_config.json`。

## Docker Hub 镜像

已发布到 Docker Hub，可直接拉取运行（镜像由 GitHub Actions 在每次 push 到 `master` 时自动构建并推送）：

```bash
docker pull dcchendockeruser/iso-hub:latest

# 运行（把 8899 换成你想要的宿主机端口，./data 为 ISO 数据目录）
docker run -d --name iso-hub \
  -p 8899:8080 \
  -v "$PWD/data:/data" \
  dcchendockeruser/iso-hub:latest
```

- 仓库地址：https://hub.docker.com/r/dcchendockeruser/iso-hub
- 通过 GitHub Actions 构建（workflow：`.github/workflows/docker-push.yml`），每次 push 到 `master` 自动重建并推送 `latest` 标签。
- 推荐用源码 `docker compose up -d --build` 方式以获得完整功能（含 SMB/WebDAV 共享容器）；Docker Hub 镜像为单容器版，不含共享容器。

## 快速开始

```bash
git clone 本项目  # 或直接拷贝 iso-hub/ 目录到服务器
cd iso-hub

# 构建并启动（可选设置访问令牌）
# echo "ISO_HUB_TOKEN=换成你的随机密码" > .env
docker compose up -d --build
```

打开 `http://<服务器IP>:8899` 即可使用。**首次登录后建议先点右上角「抓取最新版本元数据」**，
将内置样例清单刷新为当前镜像站上的最新版本列表。

### 常用命令

```bash
docker compose logs -f iso-hub   # 看服务日志
docker compose restart iso-hub   # 重启
docker exec iso-hub python /app/iso_download/update_distributions.py --output /data/distributions.json --pretty  # 手动刷元数据
```

## 端口 / 目录 / 环境变量

| 项目 | 默认 | 说明 |
|---|---|---|
| 端口 | `8899:8080` | 左侧宿主机端口，可用 `ISO_HUB_PORT=xxxx docker compose up -d` 覆盖 |
| 数据卷 | `./data:/data` | ISO 全部落盘于此：`data/linux/<发行版>/<版本>.iso`，另有 `distributions.json`/`custom_sources.json`/`subscriptions.json` 元数据 |
| `ISO_HUB_TOKEN` | 空 | 设置后所有写操作需网页弹窗输入令牌，建议公网/多设备环境开启 |
| `ISO_HUB_REQUIRE_LOGIN` | `1` | 强制登录门禁：1=必须登录管理员账号才能使用(默认)，0=关闭门禁直接可用 |
| `ISO_HUB_SYNC_INTERVAL` | `86400` | 订阅自动同步间隔(秒)；0=关闭内置调度器 |
| `SAMBA_PORT` | `1445` | SMB 共享 445 端口在宿主机映射的端口（Z4Pro 原生 Samba 占 445 故用高位；未占用的机器可改回 445） |
| `SAMBA_USER`/`SAMBA_PASS` | `iso`/`iso123` | SMB 共享账号/密码 |
| `SAMBA_NETBIOS` | `1137` | NetBIOS 137/udp 宿主端口 |
| `SAMBA_138` | `1138` | NetBIOS 138/udp 宿主端口 |
| `SAMBA_139` | `1139` | NetBIOS 139/tcp 宿主端口 |
| `WEBDAV_PORT`/`WEBDAV_USER`/`WEBDAV_PASS` | `8081`/`iso`/`iso123` | WebDAV 共享端口与凭据 |
| `TZ` | `Asia/Shanghai` | 时区 |

> 内置**用户登录**（设置面板可设/改管理员密码）。若暴露到公网建议同时配置 `ISO_HUB_TOKEN` 或置于反代（Caddy/Nginx Basic Auth）之后。

## 数据目录结构

```
data/
├── distributions.json      # 当前发行版清单(手动/自动更新生成, 持久化)
├── custom_sources.json     # 自定义镜像源(独立保存, 不被刷新覆盖)
├── subscriptions.json      # 订阅配置(发行版/保留版本数/开关/上次运行)
└── linux/                  # 下载目录: {type}/{发行版}/{文件}
    ├── Ubuntu/ubuntu-26.04-live-server-amd64.iso
    └── Proxmox/proxmox-ve_9.1-1.iso
```

## 网络共享 (SMB / WebDAV)

下载的 ISO 已映射到宿主机 `./data`，compose 额外启动两个共享容器把同一目录暴露给局域网：

| 协议 | 访问地址 | 账号/密码 |
|---|---|---|
| **SMB** (PVE/Windows 挂载) | `smb://<服务器IP>:1445/iso` | 默认 `iso` / `iso123` |
| **WebDAV** | `http://<服务器IP>:8081/dav` | 默认 `iso` / `iso123` |

- 两个共享都是**只读**，防止误改 ISO。
- 在 `.env` 改 `SAMBA_USER/SAMBA_PASS` 和 `WEBDAV_USER/WEBDAV_PASS` 修改账号密码。
- 网页端可在「设置 → 共享设置」里**实时启停**共享容器、改账号密码（写入 `./data/settings.json` 覆盖 `.env` 默认值；运行中的共享需 `docker compose restart samba webdav` 生效）。
- **PVE 使用**：数据中心 → 存储 → 添加 → SMB/CIFS，服务器填 IP，**端口填 1445**，共享填 `iso`，用户名密码 `iso/iso123`，即可把 ISO 挂成 PVE 的 ISO 存储直接安装。

> 端口说明：Z4Pro 宿主机**自带 Samba** 已占用 445/139/137/138，故容器版 Samba 映射到高位端口 **1445(445)/1139(139)/1137(137)/1138(138)** 避免冲突。若你部署的机器没跑原生 Samba，可用 `SAMBA_PORT=445` 等环境变量改回标准端口。访问地址里的 `:1445` 也要随端口改动。

### compose 示例（可直接粘贴使用）

完整 compose 见仓库根目录 `docker-compose.yml`（含 iso-hub 主服务 + samba + webdav 三个容器）。这里单独列出两个共享容器的片段，方便只想加共享、或单独部署共享时参考：

```yaml
  # SMB 共享 (PVE 挂载用它):  smb://<服务器IP>:1445/iso   (账号 iso / 密码 iso123)
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
      - ./data:/srv/iso:ro          # 只读共享,ISO 不会被误改
    environment:
      - TZ=Asia/Shanghai
      - USERID=${SAMBA_UID:-0}
      - GROUPID=${SAMBA_GID:-0}
    command: >-
      -p
      -u "${SAMBA_USER:-iso};${SAMBA_PASS:-iso123}"
      -s "iso;/srv/iso;no;no;no;${SAMBA_USER:-iso},${SAMBA_PASS:-iso123}"

  # WebDAV (Windows/其他挂载用它):  http://<服务器IP>:8081/dav   (账号 iso / 密码 iso123)
  webdav:
    image: hacdias/webdav:latest
    container_name: iso-hub-webdav
    restart: unless-stopped
    ports:
      - "${WEBDAV_PORT:-8081}:6065"
    volumes:
      - ./data:/data:ro             # 只读共享
      - ./data/webdav.yml:/config.yml:ro   # 凭据配置(主容器可改写此文件+restart 实现改密码)
    environment:
      - TZ=Asia/Shanghai
    command: ["-c", "/config.yml"]
```

> 说明：`samba` 的 `-s "iso;/srv/iso;no;no;no;user,pass"` 意思是——共享名 `iso`、路径 `/srv/iso`、只读、仅列共享、不允许访客，可写用户为 `user`。`.env` 里可配 `SAMBA_UID/GROUPID`（默认 0=root，映射文件属主）。WebDAV 的账号密码写在 `./data/webdav.yml`（初始来自 `.env` 的 `WEBDAV_USER/PASS`），网页端改密后主容器会重写该文件。

## 自定义源 (官方/镜像站任意直链)

「自定义源」页可添加清单里没有的发行版或版本（如 Kali、特定 LTS、官方地址等）：

- 只需填**发行版名 + ISO 直链**，可选校验文件地址。
- 独立保存到 `custom_sources.json`，**「抓取最新版本元数据」不会覆盖它**；订阅同步也会把它并入候选池。
- 会在「镜像列表」中自动合并显示、可勾选下载。

## 订阅同步 (全自动拉新+删旧)

「订阅同步」页对每个发行版组可开启订阅：

1. 设置 **保留 N 个最新版本**（默认 2）。
2. 开启订阅后，会**自动**：刷新该发行版元数据 → 下载最新 N 版 → 删除组目录中不在最新 N 的过期 ISO。
3. **内置调度器**默认每天（`ISO_HUB_SYNC_INTERVAL`，秒，设 0 关闭）自动执行一次。
4. 也可在服务器用 cron 定时调接口实现任意时刻全自动同步：
   ```bash
   # 每天凌晨 3 点执行订阅同步 (替换端口/令牌)
   0 3 * * * curl -s -X POST -H "X-Auth-Token: 你的令牌" http://127.0.0.1:8899/api/sync-subscriptions
   ```
5. UI 右上角「🔄 订阅同步」可手动立即触发一次。

## 日夜模式

右上角 🌓 按钮一键切换日/夜间主题，选择会记忆到浏览器 localStorage；若未手动选过则自动跟随系统深色偏好（`prefers-color-scheme`）。

## 工作原理（对应上游两个脚本）

1. **元数据刷新** → 运行 `update_distributions.py --config sources_config.json --output /data/distributions.json --pretty`：
   请求清华镜像站目录 → 正则提取版本目录 → 组合下载/校验 URL → 排序写入清单（每组保留最新 N 个版本）。
2. **选择性下载** → 本项目自带 `web/iso_runner.py`：复用上游 `LinuxDistributionDownloader`，但只下载所选条目、
   **禁用自动清理**（避免误删同组未选文件），下载完做 SHA256 校验；校验失败自动删文件重下。

## 升级上游脚本

```bash
# 在 iso-hub 目录内
rm -rf iso_download && git clone --depth 1 https://github.com/Sowevo/iso_download.git iso_download
# (国内无法直连 GitHub 时, 可下载加速镜像 zip: ghfast.top / gh-proxy.com / ghproxy.net 前缀)
docker compose up -d --build
```

## 说明与限制

- 同一时刻只运行**一个任务**（下载或刷元数据互斥），UI 上有任务进度条。
- 中断/停止任务会留下不完整文件，下次下载该校验失败会自动覆盖重下。
- 清单元数据默认保留各组**最新 3 个版本**（上游策略），想调整请在
  `iso_download/sources_config.json` 修改各 source 的 `max_entries` 后重建镜像。
- 上游项目 README 免责声明：脚本由 AI 生成，使用风险自负（代码均已本地审查，逻辑简单清晰）。
