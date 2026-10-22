# API 一览

面向需要拿脚本调 iso-hub、或要排查某个按钮到底发了什么请求的人。前端 `index.html`
是这些接口的唯一消费者，本页按模块整理，路径与 `web/api/*.py` 里的路由装饰器
一一对应（v1.7.0 起路由按域分文件；有测试守住：新增路由不写进本页会红）。

> 改路由前先读 `web/api/_ctx.py` 的模块注释：视图里访问内核符号必须写
> `core.名字`（现查），**不要** `from app import 名字` —— 后者会让 340 处
> `patch.object(app, ...)` 静默失效，测试照样绿但桩没打上。

## 鉴权模型

三档，取最严的那一档生效：

| 配置 | 效果 |
|---|---|
| 都不设 | 全部接口放行。**仅限内网可信环境** |
| `ISO_HUB_TOKEN` | 写操作需带令牌；`GET /`、`/static/*`、`/api/health` 仍放行 |
| `ISO_HUB_REQUIRE_LOGIN=1`（默认） | 除登录相关三个接口外，**全部**接口都要有效登录会话或令牌 |

认证头是 **`X-Auth-Token`**，不是 `Authorization: Bearer`。用后者验证会一律 401，
很容易误判成服务坏了。

始终放行的路径（无论哪档）：

- `POST /api/user/login`、`GET /api/user/me`、`POST /api/user/logout` —— 门禁自己
- `GET /`、`/static/*`、`/api/health` —— 页面壳子与健康检查，前端靠 `/api/user/me` 判断是否弹登录遮罩
- `GET /api/files/get` —— 浏览器顶层导航带不上自定义头，该路由**自带**短时票据鉴权
  （票据由 `/api/files/ticket` 在校验会话后签发），无票/过期票一律 403，所以放行不等于开公网

未授权返回 `401 {"error":"unauthorized"}`。

## 通用约定

- 所有 API 响应带 `Cache-Control: no-store`，避免前端「刷新列表」拿到浏览器缓存的旧数据。
  唯一例外是 `/api/files/get`（大文件必须允许缓存分片，否则 Range 续传失效）。
- 错误体统一为 `{"error": "..."}`，部分接口附带 `code` 字段。
- 下载/刷新/同步这类长任务都是**后台异步**的：POST 立即返回，进度走 `/api/state`
  与 `/api/logs`（增量拉取）。同一时刻只允许一个任务，冲突返回 `409`。

## 页面与账号

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端单页（`index.html`，禁用缓存） |
| GET | `/api/health` | 健康与自检：`gpg_ledger` 账本统计、`writable` / `writable_states` 四态探针 |
| GET | `/api/user/me` | 当前登录状态（不强制已登录），附 `require_login` / `auth_configured` 供前端判定是否弹遮罩 |
| POST | `/api/user/login` | 登录，成功返回 `token`。失败统一 `code=bad_credentials`，不区分用户名不存在与密码错误 |
| POST | `/api/user/logout` | 注销当前会话 |
| POST | `/api/user/password` | 修改当前登录用户密码（需会话 token） |

## 清单与下载

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/distros` | 清单分组 + 本地磁盘状态（含自定义源展开结果） |
| POST | `/api/download` | 下载选定条目（后台任务） |
| GET | `/api/state` | 当前任务状态，含实时文件大小 |
| GET | `/api/logs` | 增量拉取任务日志 |
| POST | `/api/stop` | 终止当前任务 |
| POST | `/api/update-meta` | 抓镜像站刷新发行版清单 |
| POST | `/api/prune` | 清理某组「不在当前清单」的过期 ISO |
| POST | `/api/delete-files` | 删除清单内已下载的 ISO |
| POST | `/api/verify` | 对已落定的文件补一次校验（不重新下载） |

## 文件下发到本机

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/files/ticket` | 为单个文件签发短时下载票据（默认 600 秒，`ISO_HUB_DL_TICKET_TTL` 可改） |
| GET | `/api/files/get` | 凭票据下发文件，支持 Range（浏览器可暂停/续传） |

## 存放模式与选源

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/api/storage-mode` | 读取/切换 ISO 存放模式（`classified` 分类 或 `flat` 平铺）。只影响之后的新下载，已有文件不迁移 |
| GET / POST | `/api/source-strategy` | 读取/切换选源策略（`A` 固定优先级 / `B` 实测最快） |

## 自定义镜像源

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/custom-sources` | 列出自定义源 |
| POST | `/api/custom-sources` | 新增自定义源（`listing_url` 走 http/https 白名单） |
| DELETE | `/api/custom-sources` | 删除自定义源 |
| POST | `/api/custom-sources/refresh` | 手动触发展开（后台子进程，非阻塞） |
| GET / POST | `/api/custom-sources/auto-refresh` | 自动刷新开关与间隔（默认关闭） |

## 订阅与定时

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/subscriptions` | 订阅配置（自动拉最新 + 删旧） |
| POST | `/api/subscriptions` | 保存订阅配置 |
| POST | `/api/sync-subscriptions` | 对所有已启用订阅执行：刷新清单 → 下载最新 N → 删旧版 |
| GET / POST / DELETE | `/api/schedules` | 定时任务增删改查 |
| POST | `/api/schedules/run` | 手动触发一次订阅同步 |

## 网络共享（Samba / WebDAV）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/shares` | 共享开关与状态。**无密码启用会被拒绝（400）** —— v1.3.21 起不再有默认口令 |
| POST | `/api/shares` | 保存共享设置与凭据 |
| GET / POST | `/api/protected` | 受保护文件清单的读取与开关（受保护的文件不会被 prune 删掉） |

## 邮件通知

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/notify` | 读取通知配置。**密码不回传**，只用 `password_set` 告诉前端有没有配过 |
| POST | `/api/notify` | 保存配置，局部提交即可，密码留空表示不改 |
| POST | `/api/notify/test` | 发一封测试邮件（**不落盘**），先验证凭据再决定要不要保存 |

## 种子（qBittorrent）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/qb/settings` | qB 设置 + 容器实时状态。外部 qB（URL 不是 compose 内部地址）只走 Web API，绝不启停容器 |
| POST | `/api/qb/settings` | 保存设置并启停/重启容器 |
| GET | `/api/torrent/sources` | 扫描 DistroWatch 官方源 + 用户自加 RSS/链接 |
| POST | `/api/torrent/classify` | 按分类分组返回。**纯计算**：不抓外网、不落盘、不依赖 qB |
| GET / POST | `/api/torrent/categories` | 分类配置的读取与保存 |
| GET | `/api/torrent/info` | qB 连接状态 + 传输信息 + 种子列表（含本地是否已存在） |
| POST | `/api/torrent/add` | 把种子（URL/磁力）交给 qB 下载 |
| POST | `/api/torrent/delete` | 从 qB 删除种子，`delete_files` 控制是否连文件一起删 |
| POST | `/api/torrent/rss/add` | 添加自定义 RSS 源 |
| POST | `/api/torrent/rss/remove` | 移除自定义 RSS 源 |
| POST | `/api/torrent/link/add` | 把当前链接存为常驻手动源 |
| POST | `/api/torrent/link/remove` | 移除常驻手动源 |
