# ISO Hub · 网页版 Linux 发行版 ISO 自动更新器

> **项目源码：** <https://github.com/DC1024/iso-hub><br>
> **Docker Hub 镜像：** <https://hub.docker.com/r/dcchendockeruser/iso-hub><br>
> **GHCR 镜像：**`docker pull ghcr.io/dc1024/iso-hub`<br>
> **阿里云 ACR 镜像（国内推荐）：** `registry.cn-hangzhou.aliyuncs.com/dcchen/isohub`

ISO Hub 是一个带网页界面的 Docker 化 Linux 发行版 ISO 自动更新器，支持在网页上勾选发行版/版本进行实时下载与 SHA256 校验、自动抓取镜像站刷新最新版本元数据、订阅同步自动跟新版，并提供过期 ISO 可视化清理与 SMB/WebDAV 网络共享功能。

## 功能

| 分组 | 能力 |
|---|---|
| **下载与校验** | 发行版/版本分组列表 · 勾选单个文件或整组下载 · 实时进度条与日志 · SHA256 自动校验 |
| **多源容错** | 每个版本自动从多个镜像站下载，失败自动切换；全局支持「A 固定优先级」/「B 实测选最快」两种策略，也可逐行手动指定镜像源 |
| **自动化** | 元数据自动刷新（抓镜像站目录写入清单）· **订阅同步**（自动拉最新 N 版 → 删除过期 ISO，内置定时调度）· 网页自建定时任务（每天/每周/每月/每年/一次性） |
| **扩展来源** | **自定义源**：添加任意 ISO 直链或"发行版源"（按版本正则抓目录），独立保存不被刷新覆盖；**种子下载**：内置 DistroWatch 官方种子 RSS，支持手动粘贴磁力链接/自加 RSS 源，经内置 qBittorrent 下载后与直链统一管理 |
| **管理** | 过期清理（标记+一键删除旧版）· 镜像保护 🔒（锁定文件清理/删除时被硬拒绝）· 筛选（全部/已下载/未下载/已收藏）· 单管理员登录（PBKDF2 加密 + 会话） |
| **体验** | 日夜模式 · 中英双语（自动跟随系统）· 分组折叠 · SMB + WebDAV 网络共享（把 ISO 分享给 PVE/Windows 挂载） |

## 快速开始

三种部署方式任选其一：

| 方式 | 适合场景 | 镜像来源 |
|---|---|---|
| **① 阿里云 ACR**（国内推荐） | 国内服务器，拉取快、公开仓库无需登录 | `registry.cn-hangzhou.aliyuncs.com/dcchen/isohub` |
| **② Docker Hub** | 海外服务器 | `dcchendockeruser/iso-hub` |
| **③ 源码构建** | 需要改代码 / 自定义 | 本地构建 |

> **前置要求：Docker 20.10+ 与 Compose v2**（`docker compose`，带空格）。<br>
> 本项目的 samba / webdav / qbittorrent 边车使用 compose **profiles** 管理，<br>
> 旧版 `docker-compose`（v1，带连字符）不支持 profiles 且已 EOL，请勿使用。<br>
> 验证：`docker compose version` 应输出 `v2.x.x`。<br>

**① 阿里云 ACR（国内推荐）：**

```bash
mkdir iso-hub && cd iso-hub
curl -O https://raw.githubusercontent.com/DC1024/iso-hub/master/docker-compose.acr.yml
# 容器以 uid 10001 非 root 运行, 先把挂载目录交给它(见下方「非 root 运行」)
mkdir -p data backups qb-config webdav-config samba-config
sudo chown -R 10001:10001 data backups qb-config webdav-config samba-config

docker compose -f docker-compose.acr.yml up -d                    # 核心（必启）
docker compose -f docker-compose.acr.yml --profile share up -d    # + SMB/WebDAV 共享（可选）
docker compose -f docker-compose.acr.yml --profile bt up -d       # + 种子下载（可选）
```

**② Docker Hub（海外）：** 把上面命令换成 `docker-compose.dockerhub.yml` 即可。

**③ 源码构建：**

```bash
git clone https://github.com/DC1024/iso-hub.git && cd iso-hub
mkdir -p data backups qb-config webdav-config samba-config
sudo chown -R 10001:10001 data backups qb-config webdav-config samba-config
docker compose up -d --build
```

> **profiles 说明**：`up -d` 默认只启动核心（iso-hub + socket-proxy）。<br>
> samba/webdav 归入`share` profile、qbittorrent 归入 `bt` profile，按需追加 `--profile` 启用；<br>
> 未启用的服务不创建容器、不拉镜像。也可在 `.env` 里写 `COMPOSE_PROFILES=share,bt` 一劳永逸。<br>

启动后访问 `http://<服务器IP>:8899`。

**公网部署必做**：在 compose 同目录建 `.env` 播种管理员，否则登录页会报「管理员账号未设置」<br>
（防抢注设计：未播种时仅允许从服务器本机 `127.0.0.1:8899` 首次建号，<br>
或走 SSH 隧道 `ssh -L 8899:127.0.0.1:8899 <user>@<服务器IP>`）：

```bash
cat > .env <<'EOF'
ISO_HUB_ADMIN_USER=admin
ISO_HUB_ADMIN_PASS=换成你的强密码
EOF
docker compose up -d   # 改 .env 后重启生效
```

首次登录后建议先点右上角「⤓ 抓取最新版本元数据」，把内置样例清单刷新为镜像站当前最新版本列表。

## 界面导览

顶部共 5 个页签：

| 页签 | 用途 |
|---|---|
| 📦 镜像列表 | 主界面：按发行版分组浏览/勾选下载，查看本地状态（已下载/下载中/下载停止），删除、锁定、收藏 |
| 🔗 自定义源 | 添加清单里没有的发行版：任意 ISO 直链，或"发行版源"（填目录地址+版本正则自动抓取），支持手动/定时刷新 |
| ⭐ 订阅同步 | 为发行版开启订阅并设保留版本数 N，自动"刷元数据 → 下最新 N 版 → 删过期"；可立即执行或交给定时任务 |
| 🧲 种子下载 | DistroWatch 官方种子 / 手动磁力链接 / 自定义 RSS 源，经内置 qBittorrent 下载（需 `--profile bt` 启用 sidecar） |
| ⚙️ 设置 | 用户登录与改密、语言、共享开关（SMB/WebDAV）、定时任务、受保护列表、自定义源自动刷新 |

## 常用操作

**下载 ISO**：镜像列表 → 勾选版本（可整组勾选）→「⬇ 下载所选」→ 顶部任务条实时显示进度/速率，日志面板滚动输出，下载完成后自动做 SHA256 校验。

**自动跟新版（订阅同步）**：订阅同步页 → 为发行版开启订阅、设保留版本数 → 「立即执行」或在设置里建定时任务。此后每次同步自动拉取最新 N 版并删除过期 ISO（🔒 锁定的文件不会被删）。

**多镜像源策略**：设置页可选「A 固定优先级」或「B 实测选最快」；镜像下载策略默认跟随设置优先级，也可通过列表每行的下拉框单独为某个版本指定镜像源。

**共享给 PVE / Windows**：启用 `--profile share` 后自动创建 SMB + WebDAV 只读共享（账号 `iso`，**密码需先在面板「设置 → 共享设置」里设置**——从 1.3.21 起不再内置 `iso123` 这类默认口令，没有密码会拒绝启用）：

| 协议 | 访问地址 | 说明 |
|---|---|---|
| SMB | `smb://<服务器IP>:1445/iso` | PVE：数据中心 → 存储 → 添加 SMB/CIFS，**端口填 1445** |
| WebDAV | `http://<服务器IP>:8081/dav` | 任何 WebDAV 客户端 |

> 端口 1445 是为避开 NAS 自带 Samba（445）的高位映射；无冲突的机器可在 `.env` 里 `SAMBA_PORT=445` 改回标准端口。账号密码可在「设置 → 共享设置」修改。

## 配置参考

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ISO_HUB_PORT` | `8899` | Web 端口（映射到容器 8080） |
| `ISO_HUB_ADMIN_USER` / `ISO_HUB_ADMIN_PASS` | 空 | 管理员播种：用户表为空时自动创建；**公网部署必设** |
| `ISO_HUB_ADMIN_PASS_FILE` / `ISO_HUB_TOKEN_FILE` | 空 | 上面两项的只读文件版本（推荐）。设了就只看文件，值不进 `docker inspect` 的 `.Config.Env` |
| `ISO_HUB_REQUIRE_LOGIN` | `1` | `1`=强制登录才能使用；`0`=关闭门禁 |
| `ISO_HUB_TOKEN` | 空 | 附加访问令牌（所有写操作需携带）；建议公网环境开启或置于反代之后 |
| `ISO_HUB_SYNC_INTERVAL` | `86400` | 订阅自动同步间隔（秒）；`0`=关闭内置调度器 |
| `ISO_HUB_BACKUP_DIR` | `/backups` | 配置备份输出目录。刻意在 `./data` 之外（共享树里就是 `settings.json` 明文），改这里要同步 compose 挂载 |
| `SAMBA_PORT` | `1445` | SMB 映射端口（原 445） |
| `SAMBA_USER` / `SAMBA_PASS` | `iso` / 空 | SMB 账号/密码（**密码必填**，为空时拒绝启用共享） |
| `WEBDAV_PORT` | `8081` | WebDAV 端口 |
| `WEBDAV_USER` / `WEBDAV_PASS` | `iso` / 空 | WebDAV 账号/密码（**密码必填**，为空时拒绝启用共享） |
| `QB_PORT` | `8090` | qBittorrent WebUI 端口 |
| `TZ` | `Asia/Shanghai` | 时区 |

### 数据目录

全部数据持久化在宿主机 `./data`：

```
data/
├── distributions.json      # 发行版清单（元数据刷新生成）
├── custom_sources.json     # 自定义源（独立保存，不被刷新覆盖）
├── subscriptions.json      # 订阅配置
└── linux/                  # ISO 落盘目录：{类型}/{发行版}/{文件}
    ├── Ubuntu/ubuntu-26.04-live-server-amd64.iso
    └── Arch/archlinux-2026.09.01-x86_64.iso
```

### 非 root 运行与升级迁移（v1.5.0 起）

容器**不再以 root 运行**，而是固定 `uid/gid = 10001`，并且根文件系统**只读**（`read_only: true`，只额外挂一个 64MB 的内存盘 `/tmp`）。

| 项 | v1.4.x 及以前 | v1.5.0 起 |
|---|---|---|
| 进程身份 | root（`cap_drop: ALL`） | **uid/gid 10001**（+ `cap_drop: ALL` + `no-new-privileges`） |
| 根文件系统 | 可写 | **只读**（写不了 `/app`、site-packages） |
| 可写位置 | 任意 | 仅 `./data` `./backups` `./qb-config` `./webdav-config` `./samba-config`，外加两块内存盘 `/tmp`、`/home/isohub` |

**从 v1.4.x 升级（breaking change）：** 绑定挂载的属主由**宿主机**决定，容器内改不了。升级前必须先在 compose 所在目录执行：

```bash
docker compose down
sudo chown -R 10001:10001 ./data ./backups ./qb-config ./webdav-config ./samba-config
docker compose pull && docker compose up -d
```

漏了会怎样：容器启动时自检发现数据目录不可写，**立即退出**并在日志里打印上面那条 `chown` 命令（不会出现"服务起来了但每次下载都在写盘处炸"的半死状态）。`docker compose logs iso-hub` 即可看到。

**几个容易踩的点：**

- **qBittorrent 必须同 uid**：`qbittorrent` 服务的 `QB_PUID` / `QB_PGID` 默认已改为 `10001`。若仍让它在 root 下写 `/data`，主容器（uid 10001 且无 `DAC_OVERRIDE`）既校验不了也删不掉那些 ISO。老部署在 `.env` 里显式写死了 `QB_PUID=0` 的，请一并改掉。
- **`read_only` 依赖的路径**：gpg 验签全部走 `tempfile.TemporaryDirectory()`（落在 `/tmp` 内存盘）并显式指定 `GNUPGHOME`，不碰 `$HOME/.gnupg`；业务写盘全部落在上面五个挂载卷。日志在 `./data/logs/`。`/home/isohub` 也挂了一块 8MB 内存盘兜底 —— `gpg --show-keys` 这类"看起来只读"的调用仍会在 homedir 里建 `pubring.kbx`，不可写时它会 `Fatal` 且不输出指纹（表现为"缓存 keyring 指纹不匹配、每轮重建"）。
- **备份为什么单独一个卷**：`./data` 会整块经 Samba/WebDAV 只读共享给局域网，而配置备份归档里就是 `settings.json`（会话 token / SMTP 授权码 / 共享口令）。所以备份写到 `./backups`，`samba` / `webdav` 两个服务**不挂**它。1.8.0 之前留下的 `./data/backups/` 不会自动搬家，启动日志会点名提醒。
- **NFS / SMB 挂载的 data 目录**：需在服务端把 uid 映射成 10001（或 `all_squash` + `anonuid=10001`），否则本机 `chown` 无效。
- **Podman / rootless Docker**：没有真正的 uid 0，用 `userns_mode: keep-id`，或把宿主目录直接交给容器内的映射用户。
- **临时回退**：实在来不及迁移，可在 compose 里给 `iso-hub` 加 `user: "0:0"` 并删掉 `read_only: true`，即可回到 v1.4.x 的行为——但这会同时失去本次的全部收益，仅作应急。

### 常用命令

```bash
docker compose logs -f iso-hub     # 看服务日志
docker compose restart iso-hub     # 重启
docker compose pull && docker compose up -d   # 升级到最新镜像
```

## 常见问题

**登录报「管理员账号未设置」？**<br>
`.env` 里没配 `ISO_HUB_ADMIN_USER` / `ISO_HUB_ADMIN_PASS`，配好后 `docker compose up -d` 重启。首次登录后可在「设置 → 修改密码」改密。

**点「刷新列表」没反应？**<br>
多为未登录导致 401 被静默处理，先登录；仍有问题看 `docker compose logs iso-hub`。

**国内拉不动 Docker Hub 镜像？**<br>
改用方式① 阿里云 ACR 源。

**Compose 报 `profiles` 不支持？**<br>
在用 v1（`docker-compose`），升级 Docker 或安装 Compose v2 插件，统一用 `docker compose`。

**想改 SMB 端口/账号？**<br>
`.env` 设 `SAMBA_PORT` / `SAMBA_USER` / `SAMBA_PASS`，或网页「设置 → 共享设置」改（改后 `docker compose restart samba webdav` 生效）。

**删除文件被拒绝？**<br>
该文件被 🔒 锁定保护，先在列表里解锁；保护列表也可在「设置」里批量管理。

**启动就退出，日志里写着「启动中止: 数据目录不可写」？**<br>
v1.5.0 起容器以 uid 10001 运行，宿主挂载目录的属主对不上。按日志提示执行 `sudo chown -R 10001:10001 ./data ./backups ./qb-config ./webdav-config ./samba-config` 后重启即可，详见「[非 root 运行与升级迁移](#非-root-运行与升级迁移v150-起)」。

**面板能打开，但下载/删除都报 Permission denied？**<br>
同上，属主没迁移。也可能是 qBittorrent 仍以 root 写盘（`.env` 里 `QB_PUID=0`），改成 `10001`。`curl http://127.0.0.1:8899/api/health` 的 `writable` 字段会直接标出哪个挂载点写不了。

## 开发

```bash
# 本地运行（Python 3.12+）
pip install -r web/requirements.txt -r iso_download/requirements.txt
# 默认 DATA_DIR=/data（容器内路径），本机跑要显式指到仓库内，否则启动自检会拒绝启动
ISO_DATA_DIR=./data python web/app.py          # 开发模式
ISO_DATA_DIR=./data python -m waitress web.app:app   # 或 waitress 生产模式

# 测试与静态检查（提交前必跑）
python -m unittest discover -s tests -p "test_*.py"
python -m ruff check . --select=F
python -m bandit -r web iso_download scripts -ll
```

项目结构：

```
web/            # Flask 应用 + 单文件前端 (static/index.html)
iso_download/   # 上游 CLI 封装：多源下载、元数据解析、GPG/SHA256 校验
tests/          # unittest 回归测试
scripts/        # 辅助脚本
```

CI：push 到 `master` 自动跑 Lint & Security，并构建推送 Docker Hub 与阿里云 ACR 镜像。

## 致谢与许可

- 上游核心能力来自 [Sowevo/iso_download](https://github.com/Sowevo/iso_download)（Mozilla Public License 2.0）
- 本项目同样以 [MPL-2.0](LICENSE) 开源
- 数据源：清华 TUNA、中科大、网易等公开镜像站，种子源为 DistroWatch
