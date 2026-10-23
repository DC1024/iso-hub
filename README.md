# ISO Hub · 网页版 Linux 发行版 ISO 自动更新器

> **项目源码：** <https://github.com/DC1024/iso-hub><br>
> **Docker Hub 镜像：** <https://hub.docker.com/r/dcchendockeruser/iso-hub><br>
> **GHCR 镜像：** `docker pull ghcr.io/dc1024/iso-hub`<br>
> **阿里云 ACR 镜像（国内推荐）：** `registry.cn-hangzhou.aliyuncs.com/dcchen/isohub`

ISO Hub 把命令行工具 [Sowevo/iso_download](https://github.com/Sowevo/iso_download) 封装成一个**带网页界面的 Docker 服务**：在浏览器里勾选发行版 → 后台多镜像源下载 → SHA256 / GPG 校验 → 落盘 → 局域网共享，全程不用 SSH。

**适合谁用**

- 有一台常年开机的 NAS 或小主机，想让它**自己追** Ubuntu / Debian / Arch 的新版 ISO，而不是每次手动去镜像站翻目录。
- 给 PVE / 装机 U 盘备料，需要一个**可信的本地 ISO 库** —— 每个文件都有校验证据，不是"下完就算"。
- 内网多台机器要取同一批 ISO，希望用 SMB / WebDAV 共享一次、到处挂载。

## 核心能力

| 分组 | 能力 |
|---|---|
| **下载与校验** | 发行版/版本分组列表 · 勾选单个文件或整组下载 · 实时进度与日志 · SHA256 + GPG 签名校验（公钥与硬编码指纹比对，消除 TOFU） |
| **多源容错** | 每个版本自动从多个镜像站下载，失败自动切换；全局支持「A 固定优先级」/「B 实测选最快」，也可逐行手动指定镜像源 |
| **自动化** | 元数据自动刷新（抓镜像站目录写入清单）· **订阅同步**（拉最新 N 版 → 删过期 ISO，内置定时调度）· 网页自建定时任务（每天/每周/每月/每年/一次性） |
| **扩展来源** | **自定义源**：任意 ISO 直链，或"发行版源"（按版本正则抓目录），独立保存不被刷新覆盖；**种子下载**：内置 DistroWatch 种子 RSS，支持手动磁力链接 / 自加 RSS，经 qBittorrent 下载后与直链统一管理 |
| **管理** | 过期清理（标记 + 一键删除旧版）· 镜像保护 🔒（锁定文件在清理/删除时被硬拒绝）· 筛选（全部/已下载/未下载/下载中/下载失败停止/已收藏）· 单管理员登录（PBKDF2 + 会话） |
| **体验** | 日夜模式 · 中英双语（自动跟随系统）· 分组折叠 · 下载到本机（无需额外共享服务）· SMB + WebDAV 网络共享 |

## 技术栈

| 层次 | 选型 | 说明 |
|---|---|---|
| 运行时 | Python **3.12.15**（`python:3.12.15-slim`，钉 digest） | 基础镜像锁 digest，同一 commit 两次构建产物一致 |
| Web 框架 | **Flask 3.1.3** + **waitress 3.0.2** | 54 条路由拆成 9 个 Blueprint；waitress 多线程，不引入 gunicorn |
| 前端 | **单文件 `web/static/index.html`**（原生 HTML/CSS/JS） | 零构建、零 npm；改完刷新即生效 |
| 校验 | **SHA256** + **GPG**（`gnupg` / `gpgv`） | 覆盖 detached / 只签 ISO / 内嵌 clearsigned 三种签名机制 |
| 持久化 | 纯 JSON（`settings.json`、`distributions.json` 等） | 无数据库；`settings.json` 可选 AES-GCM 整文件加密 |
| 容器 | Docker + **Compose v2 profiles**；`haproxy` 作 socket-proxy | 默认只起 2 个容器；主容器非 root（uid 10001）+ 只读根文件系统 |
| 质量门禁 | unittest + **ruff** `--select=F` + **bandit** `-ll` | 12 条 GitHub Actions：测试 / 静态检查 / 三仓库镜像 / 容器冒烟 / CVE 增量基线 |

## 快速开始

> **前置要求：Docker 20.10+ 与 Compose v2**（`docker compose`，带空格）。<br>
> 本项目的 samba / webdav / qbittorrent 边车用 compose **profiles** 管理，<br>
> 旧版 `docker-compose`（v1，带连字符）不支持 profiles 且已 EOL，请勿使用。<br>
> 验证：`docker compose version` 应输出 `v2.x.x`。

三种部署方式任选其一：

| 方式 | 适合场景 | 镜像来源 |
|---|---|---|
| **① 阿里云 ACR**（国内推荐） | 国内服务器，拉取快、公开仓库无需登录 | `registry.cn-hangzhou.aliyuncs.com/dcchen/isohub` |
| **② Docker Hub** | 海外服务器 | `dcchendockeruser/iso-hub` |
| **③ 源码构建** | 需要改代码 / 自定义 | 本地构建 |

**① 阿里云 ACR（国内推荐）：**

```bash
mkdir iso-hub && cd iso-hub
curl -O https://raw.githubusercontent.com/DC1024/iso-hub/master/docker-compose.acr.yml
# 容器以 uid 10001 非 root 运行, 先把挂载目录交给它(见「非 root 运行」)
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

> **profiles 说明**：`up -d` 默认只启动核心（iso-hub + socket-proxy 两个容器）。<br>
> samba/webdav 归入 `share` profile、qbittorrent 归入 `bt` profile，按需追加 `--profile` 启用；<br>
> 未启用的服务不创建容器、不拉镜像。也可在 `.env` 里写 `COMPOSE_PROFILES=share,bt` 一劳永逸。

### 首次启动后三件事

1. **访问** `http://<服务器IP>:8899`。
2. **建管理员**。公网部署**必做**：在 compose 同目录建 `.env` 播种管理员，否则登录页会报「管理员账号未设置」
   （防抢注设计：未播种时仅允许从服务器本机 `127.0.0.1:8899` 首次建号，或走 SSH 隧道 `ssh -L 8899:127.0.0.1:8899 <user>@<服务器IP>`）：

   ```bash
   cat > .env <<'EOF'
   ISO_HUB_ADMIN_USER=admin
   ISO_HUB_ADMIN_PASS=换成你的强密码
   EOF
   docker compose up -d   # 改 .env 后重启生效
   ```

3. **抓一次元数据**。登录后点右上角「⤓ 抓取最新版本元数据」，把内置样例清单刷新为镜像站当前最新版本列表。

## 界面导览

顶部共 5 个页签：

| 页签 | 用途 |
|---|---|
| 📦 镜像列表 | 主界面：按发行版分组浏览/勾选下载，查看本地状态（下载中/已下载/下载停止/下载失败/未下载），删除、锁定、收藏 |
| 🔗 自定义源 | 添加清单里没有的发行版：任意 ISO 直链，或"发行版源"（填目录地址 + 版本正则自动抓取），支持手动/定时刷新 |
| ⭐ 订阅同步 | 为发行版开启订阅并设保留版本数 N，自动"刷元数据 → 下最新 N 版 → 删过期"；可立即执行或交给定时任务 |
| 🧲 种子下载 | DistroWatch 官方种子 / 手动磁力链接 / 自定义 RSS 源，经 qBittorrent 下载（需 `--profile bt` 启用 sidecar） |
| ⚙️ 设置 | 用户登录与改密、语言、共享开关（SMB/WebDAV）、定时任务、受保护列表、自定义源自动刷新、存放模式 |

## 常用操作

**下载 ISO**：镜像列表 → 勾选版本（可整组勾选）→「⬇ 下载所选」→ 顶部任务条实时显示进度/速率，日志面板滚动输出，下载完成后自动校验。

**自动跟新版（订阅同步）**：订阅同步页 → 为发行版开启订阅、设保留版本数 → 「立即执行」或在设置里建定时任务。此后每次同步自动拉取最新 N 版并删除过期 ISO（🔒 锁定的文件不会被删）。

**多镜像源策略**：设置页可选「A 固定优先级」或「B 实测选最快」（对应 `ISO_HUB_SOURCE_STRATEGY=A|B`）；镜像下载策略默认跟随设置优先级，也可通过列表每行的下拉框单独为某个版本指定镜像源。

**共享给 PVE / Windows**：启用 `--profile share` 后创建 SMB + WebDAV 只读共享（账号 `iso`，**密码需先在面板「设置 → 共享设置」里设置**——从 1.3.21 起不再内置 `iso123` 这类默认口令，没有密码会拒绝启用）：

| 协议 | 访问地址 | 说明 |
|---|---|---|
| SMB | `smb://<服务器IP>:1445/iso` | PVE：数据中心 → 存储 → 添加 SMB/CIFS，**端口填 1445** |
| WebDAV | `http://<服务器IP>:8081/dav` | 任何 WebDAV 客户端 |

> 端口 1445 是为避开 NAS 自带 Samba（445）的高位映射；无冲突的机器可在 `.env` 里 `SAMBA_PORT=445` 改回标准端口。账号密码可在「设置 → 共享设置」修改。

**下载到本机**：镜像列表或种子页勾选已完成的文件 →「⬇ 下载到本机」。走短时票据（默认 10 分钟）直传，支持断点续传，不需要额外开共享服务。

## 配置参考

### 常用环境变量

只列最常改的；**完整清单（含 `ISO_HUB_STORAGE_MODE`、`ISO_HUB_SECRET_KEY`、
`ISO_HUB_SESSION_TTL`、`ISO_HUB_DL_TICKET_TTL`、`ISO_HUB_FAILURE_TTL_DAYS` 等 20 多个）见
[wiki · 环境变量与路径](wiki/环境变量与路径.md)**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `ISO_HUB_PORT` | `8899` | Web 端口（映射到容器 8080） |
| `ISO_HUB_ADMIN_USER` / `ISO_HUB_ADMIN_PASS` | 空 | 管理员播种：用户表为空时自动创建；**公网部署必设** |
| `ISO_HUB_ADMIN_PASS_FILE` / `ISO_HUB_TOKEN_FILE` | 空 | 上面两项的只读文件版本（推荐）。值不进 `docker inspect` 的 `.Config.Env`；两者都设时环境变量优先并告警 |
| `ISO_HUB_REQUIRE_LOGIN` | `1` | `1`=强制登录才能使用；`0`=关闭门禁 |
| `ISO_HUB_TRUST_PROXY` | 空 | 前置反向代理时设 `1`（从 `X-Forwarded-For` 取真实 IP）。**没有反代不要开**，否则客户端可自称 `127.0.0.1` |
| `ISO_HUB_TOKEN` | 空 | 附加访问令牌（所有写操作需携带）；建议公网环境开启或置于反代之后 |
| `ISO_HUB_SYNC_INTERVAL` | `86400` | 订阅自动同步间隔（秒）；`0`=关闭内置调度器 |
| `ISO_HUB_SOURCE_STRATEGY` | `A` | 多源策略：`A` 固定优先级 / `B` 实测选最快 |
| `ISO_HUB_BACKUP_DIR` | `/backups` | 配置备份输出目录。刻意在 `./data` 之外（共享树里就是 `settings.json` 明文），改这里要同步 compose 挂载 |
| `SAMBA_PORT` / `SAMBA_PASS` | `1445` / 空 | SMB 端口与密码（**密码必填**，为空时拒绝启用共享） |
| `WEBDAV_PORT` / `WEBDAV_PASS` | `8081` / 空 | WebDAV 端口与密码（同上） |
| `QB_PORT` | `8090` | qBittorrent WebUI 端口。**容器内外同号**，可填任意 `>=1024` 的端口；端口映射两侧、qB 的 `--webui-port`、后端 `QB_URL` 都会自动跟随 |
| `TZ` | `Asia/Shanghai` | 时区 |

### 数据目录

全部数据持久化在宿主机 `./data`：

```
data/
├── distributions.json      # 发行版清单（元数据刷新生成）
├── custom_sources.json     # 自定义源（独立保存，不被刷新覆盖）
├── subscriptions.json      # 订阅配置
├── settings.json           # 用户/会话/共享凭据/定时任务（可选 AES-GCM 加密）
└── linux/                  # ISO 落盘目录：{类型}/{发行版}/{文件}
    ├── Ubuntu/ubuntu-26.04-live-server-amd64.iso
    └── Arch/archlinux-2026.09.01-x86_64.iso
```

存放模式可切 `classified`（默认，按 `类型/发行版/文件` 分层）或 `flat`（全部平铺到 `iso/`）；切换只影响之后的下载，已下载文件不搬迁。

### 非 root 运行与升级迁移（v1.5.0 起）

容器**不以 root 运行**，而是固定 `uid/gid = 10001`，且根文件系统**只读**（`read_only: true`，只额外挂内存盘 `/tmp`）。

| 项 | v1.4.x 及以前 | v1.5.0 起 |
|---|---|---|
| 进程身份 | root（`cap_drop: ALL`） | **uid/gid 10001**（+ `cap_drop: ALL` + `no-new-privileges`） |
| 根文件系统 | 可写 | **只读**（写不了 `/app`、site-packages） |
| 可写位置 | 任意 | 仅 `./data` `./backups` `./qb-config` `./webdav-config` `./samba-config`，外加内存盘 `/tmp`、`/home/isohub` |

**从 v1.4.x 升级（breaking change）：** 绑定挂载的属主由**宿主机**决定，容器内改不了。升级前必须先在 compose 所在目录执行：

```bash
docker compose down
sudo chown -R 10001:10001 ./data ./backups ./qb-config ./webdav-config ./samba-config
docker compose pull && docker compose up -d
```

漏了会怎样：容器启动时自检发现数据目录不可写，**立即退出**并在日志里打印上面那条 `chown` 命令（不会出现"服务起来了但每次下载都在写盘处炸"的半死状态）。`docker compose logs iso-hub` 即可看到。

**几个容易踩的点：**

- **qBittorrent 必须同 uid**：`QB_PUID` / `QB_PGID` 默认已是 `10001`。若仍让它在 root 下写 `/data`，主容器（uid 10001 且无 `DAC_OVERRIDE`）既校验不了也删不掉那些 ISO。
- **`read_only` 依赖的路径**：gpg 验签全部走 `tempfile.TemporaryDirectory()`（落在 `/tmp`）并显式指定 `GNUPGHOME`；业务写盘全部落在上面五个挂载卷，日志在 `./data/logs/`。`/home/isohub` 也挂了一块内存盘兜底。
- **备份为什么单独一个卷**：`./data` 会整块经 Samba/WebDAV 只读共享给局域网，而配置备份归档里就是 `settings.json`（会话 token / SMTP 授权码 / 共享口令）。所以备份写到 `./backups`，`samba` / `webdav` 两个服务**不挂**它。
- **NFS / SMB 挂载的 data 目录**：需在服务端把 uid 映射成 10001（或 `all_squash` + `anonuid=10001`），否则本机 `chown` 无效。
- **Podman / rootless Docker**：没有真正的 uid 0，用 `userns_mode: keep-id`，或把宿主目录直接交给容器内的映射用户。

### 常用命令

```bash
docker compose logs -f iso-hub                  # 看服务日志
docker compose restart iso-hub                  # 重启
docker compose pull && docker compose up -d     # 升级到最新镜像
docker exec iso-hub python /app/scripts/backup.py   # 立即备份配置
```

## 目录结构

```
web/                       Flask 应用（内核 + 路由 + 前端）
├── app.py                 内核：配置读写、任务线程、启动自检、鉴权钩子
├── api/                   54 条路由，按域拆成 9 个 Blueprint
├── static/index.html      单文件前端（HTML + CSS + JS，无构建步骤）
├── iso_runner.py          网页下载执行器：.part 原子落盘 + HTTP Range 续传
├── config_files.py        共享 JSON 的唯一读写通道（独立锁 + 原子写 + 损坏隔离）
├── version.py             版本单一真源（徽标 / API / 响应头同源）
├── url_guard.py           出站 URL 校验（SSRF 防线，全站共用一份）
├── notifier.py            SMTP 邮件通知
└── gpg_ledger.py          GPG 验签账本（/api/health 据此报验签状态）
iso_download/              上游 CLI：多源下载、元数据解析、GPG/SHA256 校验
tests/                     58 个测试文件（unittest 回归 + 契约守卫）
scripts/                   备份/恢复、CVE 基线比对、路由静态检查、变异测试
docker/socket-proxy/       haproxy 静态白名单（socket-proxy 的唯一配置）
wiki/                      17 篇使用与运维文档
.github/workflows/         12 条流水线（测试 / 静态检查 / 三仓库镜像 / 容器冒烟 / CVE）
docker-compose*.yml        三个镜像源各一份（源码构建 / Docker Hub / 阿里云 ACR）
```

## 开发与贡献

### 本地跑起来

```bash
pip install -r web/requirements.txt -r iso_download/requirements.txt   # Python 3.12+
ISO_DATA_DIR=./data python web/app.py                # 开发模式（监听 8080）
ISO_DATA_DIR=./data python -m waitress web.app:app   # waitress 生产模式
```

`ISO_DATA_DIR` 必须显式指定：默认值是容器内路径 `/data`，本机跑时不改会被启动自检拒绝。

### 提交前必跑（与 CI 同一套）

```bash
python -m unittest discover -s tests -p "test_*.py"   # 全量回归
python -m ruff check . --select=F                     # 未定义名 / 未使用导入
python -m bandit -r web iso_download scripts -ll      # 只报 Medium 及以上
python -m py_compile web/app.py                       # 语法
```

前端是单文件、无构建、无 lint 工具链。**改了 `index.html` 就在浏览器里实机点一遍** ——
这个项目出过的几起"测试全绿、界面明显不对"的问题，全都落在这一层。

### 提 PR 的规矩

- **`master` 有分支保护**：直接 push 会被拒（`required status checks are expected`），一律走 PR。
- **一次只改一个功能。** 改 5 个文件同时出 bug，你无法定位是谁的错。
- **改完立刻运行。** 不运行就继续改，等于在黑暗里叠积木。
- **跑通立刻提交 Git。** 这样改坏了能一键回到上一个好状态。
- **改行为就加测试。** 这里的测试多数是契约测试 —— 它们钉的是"不能退化成什么样"，
  比覆盖率数字有用得多。改实现去迁就过时测试，是最坏的一种"修绿"。
- **改文档会被守卫检查**（见下）。

### 文档一致性守卫

文档不是随便改的，有四条测试盯着：

| 守卫 | 检查什么 |
|---|---|
| `tests/test_env_docs.py` | 代码里 `os.environ.get("X")` 读过的每个变量，`wiki/` 与仓库根的 `.md` 里必须出现；反向，文档中反引号包裹的 `ISO_*` / `QB_*` / `SAMBA_*` 等前缀名字必须真实存在（防"幽灵变量"） |
| `tests/test_api_docs.py` | 路由装饰器与 `wiki/API.md` 的表格行**双向**对齐，多一条少一条都会红 |
| `tests/test_version_consistency.py` | `web/version.py` 是唯一真源：格式为 `X.Y.Z`、前端兜底常量一致、`/api/health` 返回 `version`、`GET /` 响应头带 `X-Version` |
| `tests/test_wiki_deploy_doc.py` | `wiki/部署-快速开始.md` 里的 compose 代码块必须符合 socket-proxy 与 profiles 约定 |

## 常见问题

**登录报「管理员账号未设置」？**
`.env` 里没配 `ISO_HUB_ADMIN_USER` / `ISO_HUB_ADMIN_PASS`，配好后 `docker compose up -d` 重启。

**启动就退出，日志里写着「启动中止: 数据目录不可写」？**
容器以 uid 10001 运行，宿主挂载目录属主对不上。按日志提示执行 `sudo chown -R 10001:10001 ./data ./backups ./qb-config ./webdav-config ./samba-config` 后重启，详见上文「非 root 运行与升级迁移」。

**面板能打开，但下载/删除都报 Permission denied？**
同上，属主没迁移。也可能是 qBittorrent 仍以 root 写盘（`.env` 里 `QB_PUID=0`），改成 `10001`。`curl http://127.0.0.1:8899/api/health` 的 `writable` 字段会直接标出哪个挂载点写不了。

**点「刷新列表」没反应？**
先看徽标版本号是否最新。「刷新列表」只重读本地清单，**不联网**；联网抓新版本是「⤓ 抓取最新版本元数据」。若仍是 401，说明未登录。

**国内拉不动 Docker Hub 镜像？**
改用方式① 阿里云 ACR 源。

**删除文件被拒绝？**
该文件被 🔒 锁定保护，先在列表里解锁；保护列表也可在「设置」里批量管理。

> 更多（含国内镜像加速、反代配置、外部 qBittorrent、备份恢复、时区不生效等）见
> **[wiki · 常见问题 FAQ](wiki/常见问题-FAQ.md)** 与 **[wiki · 已知坑与避坑经验](wiki/已知坑与避坑经验.md)**。

## 文档导航

| 想做什么 | 看这篇 |
|---|---|
| 从零部署（含反代、非 root、profiles 详解） | [部署 · 快速开始](wiki/部署-快速开始.md) |
| 查端口 / 目录 / 全部环境变量 | [环境变量与路径](wiki/环境变量与路径.md) |
| 了解每个页面能干什么 | [界面与功能总览](wiki/界面与功能.md) |
| 接 API / 写脚本调用 | [API 一览](wiki/API.md) |
| 备份与恢复配置 | [备份与恢复](wiki/备份与恢复.md) |
| 监控运行状态与验签账本 | [服务质量与监控](wiki/服务质量与监控.md) |
| 遇到问题 | [常见问题 FAQ](wiki/常见问题-FAQ.md) · [已知坑与避坑经验](wiki/已知坑与避坑经验.md) |
| 想改代码 / 提 PR | [贡献指南](wiki/贡献指南.md) · [项目概览](wiki/项目概览.md) |

## 致谢与许可

- 上游核心能力来自 [Sowevo/iso_download](https://github.com/Sowevo/iso_download)（Mozilla Public License 2.0）
- 本项目同样以 [MPL-2.0](LICENSE) 开源
- 数据源：清华 TUNA、中科大、网易等公开镜像站，种子源为 DistroWatch
