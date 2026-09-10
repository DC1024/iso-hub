# iso-hub Docker 服务开关改造 — 需求说明

> 执行人：寇豆码
> 状态：待开发
> 关联改造：GPG 签名验证加固（已闭环，本次改动区域不重叠，可并行）
> 部署环境：腾讯云 `118.89.25.55:8899`，镜像走阿里云 ACR

---

## 一、背景与目标

当前实现对 Docker 的调用存在一个安全与体验的双重问题：

1. **权限过大**——主容器挂载裸 `/var/run/docker.sock`，等价于持有宿主机 root 权限，而服务对外暴露 8899 端口。
2. **状态失真**——smb / webdav / qbittorrent 三个 sidecar 预创建为 **stopped 容器**，被 `docker prune` 删除后功能静默失效，网页开关点了没反应，且无任何提示。
3. **资源浪费**——即使从不用，三个服务的镜像也会被全量拉取。

**目标**：权限收窄、状态诚实、资源按需。

| 目标 | 手段 |
|---|---|
| 权限收窄 | 引入 socket-proxy，主容器不再直连裸 sock |
| 状态诚实 | 四态开关，未部署给出明确命令提示 |
| 资源按需 | compose profiles，未启用服务不创建、不拉镜像 |

---

## 二、现状（已核实）

`web/app.py`（2184 行）的 `_docker_request`（699-711 行）用 **Python 标准库**直连 Unix socket 调 Docker Engine API，**既不依赖 docker SDK，也不依赖 docker CLI**：

```python
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

def _docker_request(method, path, body, timeout):
    import socket
    import http.client
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.connect(DOCKER_SOCK)
    conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
    ...
```

设计意图是刻意的：主容器为 slim 精简镜像，不安装 docker CLI，镜像更小、权限更收敛。**改造需保持这一原则——不引入 `docker` Python 包，不 invoke CLI。**

当前用到的 API 端点（全部为 `/containers`）：

- `GET /containers/{name}/json` — 查状态
- `POST /containers/{name}/start | stop | restart` — 启停
- `POST /containers/{name}/update` — 改 RestartPolicy
- `POST /containers/create?name=...` — 重建 samba 容器（改网络后）
- `DELETE /containers/{name}?force=1` — 删容器

---

## 三、需求清单

### P0（本轮必须完成）

**N1　`_docker_request` 支持 TCP 连接（约 10 行）**

新增 `DOCKER_HOST` 环境变量分支，走 TCP 连 socket-proxy；不设置时回退到现有 Unix socket 路径，**保持向后兼容**。

```python
DOCKER_HOST = os.environ.get("DOCKER_HOST", "")

def _conn(timeout):
    if DOCKER_HOST.startswith("tcp://"):
        return http.client.HTTPConnection(DOCKER_HOST[6:], timeout=timeout)
    conn = http.client.HTTPConnection("localhost", timeout=timeout)
    conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.sock.connect(DOCKER_SOCK)
    return conn
```

**N2　compose 新增 socket-proxy 服务**

按端点做白名单，禁掉镜像、卷、exec、系统四类高危端点。主容器设 `DOCKER_HOST=tcp://socket-proxy:2375`，**并摘掉 `docker.sock` 挂载**。

> ⚠️ socket-proxy 本身**不加 profile**——它是核心依赖，主容器需持续用它查状态。

**N3　smb / webdav / qbittorrent 加 profiles**

每个服务加一行 `profiles: [...]`。未启用时容器**根本不存在**，从而根治 prune 静默失效与镜像浪费。

**N4　四态服务开关**

`running` / `stopped` / `not_deployed` / `unknown`。

### P1

**N5　存量环境迁移说明**（清理旧 stopped 容器的一行命令）
**N6　README 补充 Compose v2 要求与启用命令**

---

## 四、技术方案

### N2　socket-proxy 配置

```yaml
services:
  iso-hub:
    image: xxx/iso-hub
    environment:
      - DOCKER_HOST=tcp://socket-proxy:2375
    # 删除原有的 /var/run/docker.sock 挂载

  socket-proxy:
    image: tecnativa/docker-socket-proxy
    environment:
      CONTAINERS: 1      # 允许 /containers/* 端点
      POST: 1            # 允许写操作（start/stop 是 POST）
      IMAGES: 0          # 禁止镜像操作
      VOLUMES: 0         # 禁止卷操作
      NETWORKS: 0        # 禁止网络操作
      EXEC: 0            # 禁止 exec
      SYSTEM: 0          # 禁止系统级操作
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

### N3　profiles 配置

```yaml
  smb:
    profiles: ["share"]
    ...
  webdav:
    profiles: ["share"]
    ...
  qbittorrent:
    profiles: ["bt"]
    ...
```

启用命令：

```bash
docker compose up -d                                  # 仅核心
docker compose --profile share up -d                  # + SMB/WebDAV
docker compose --profile share --profile bt up -d     # 全部
```

### N4　四态判定与 UI

```python
def service_state(name):
    try:
        c = client.containers.get(f"iso-hub-{name}")
        return "running" if c.status == "running" else "stopped"
    except docker.errors.NotFound:
        return "not_deployed"     # 未部署 → 灰态 + 命令提示
    except docker.errors.APIError:
        return "unknown"          # API 不通 → 排障提示
```

UI 映射：

| 状态 | 显示 | 可操作 |
|---|---|---|
| running | ● 运行中 | 「停止」 |
| stopped | ○ 已停止 | 「启动」 |
| not_deployed | ✗ 未部署 | 灰态 + 命令提示 + 一键复制 |
| unknown | ? 未知 | 灰态 + 排障提示 |

> `unknown` 态必须有，否则 socket-proxy 挂掉时会误报成"未部署"，用户照提示敲命令无效，白白浪费一轮排查。

---

## 五、关键约束与已知风险

**1. 未部署态只提示命令，不提供「启动」按钮**

应用**读不到宿主机的 compose 文件**（看不到 image / volumes / env），无法正确 create；硬编码一份参数又会与 compose 漂移。因此"未部署 → 自动创建"在本架构下**不可行**，不是保守选择。

**2. 现有 samba 重建逻辑保留 `POST=1`**

tecnativa 的 `POST=1` 粒度较粗，会连带开放 `/containers/create`。但**现有的"改网络后重建 samba"功能依赖 create**，本轮保留，接受 create 的残余风险——proxy 至少已挡掉镜像、卷、exec、系统四类更高危端点。彻底收敛需重构该功能的参数来源，列入后续。

**3. Compose v2 前置要求**

`profiles` 要求 Compose v2（`docker compose` 带空格）。Python 版 `docker-compose` v1 不支持且已 EOL，README 需写明并提供迁移说明。

**4. 存量环境的孤儿容器**

老环境中已存在的 stopped 容器，加 profile 后可能变成孤儿容器。需提供清理命令，且**迁移行为要在测试环境实测确认一次**再写进文档。

```bash
docker compose rm -s smb webdav qbittorrent   # 清理旧 stopped 容器（实测后确定）
```

**5. 状态检测要懒加载**

设置页打开时才查 Docker，**不要让主流程依赖它**；查询失败降级为 `unknown` 而非崩溃。

---

## 六、验收标准

```bash
# 1. 权限收窄 —— 主容器不再持有裸 sock
docker exec <iso-hub> sh -c 'ls /var/run/docker.sock'   # 期望：不存在
docker exec <iso-hub> env | grep DOCKER_HOST            # 期望：tcp://socket-proxy:2375

# 2. proxy 确实拦住了高危端点（返回 403）
#    从主容器内尝试访问 /images/json、/volumes → 应被拒绝

# 3. profiles 生效 —— 未启用服务容器不存在
docker ps -a --filter name=smb                          # 期望：无输出（不是 stopped）

# 4. 四态判定正确
#    a. 未启用 profile → UI 显示 ✗ 未部署 + 命令提示
#    b. 启用后 → ● 运行中；点停止 → ○ 已停止；点启动 → 恢复
#    c. 停掉 socket-proxy 容器 → UI 显示 ? 未知（不能误报未部署）

# 5. 回归：samba 重建逻辑仍可用（切换 proxy 后最易失效的功能）
#    走一遍"改网络后重建 samba"分支，确认成功

# 6. prune 不再造成静默失效
docker container prune    # 未启用的服务因不存在而免受影响；已启用的显示为 ✗ 未部署
```

---

## 七、范围外（本轮不做）

1. **彻底收掉 `POST /containers/create`** —— 需重构 samba 重建的参数来源，后续单独立项
2. **自研极简 proxy 替代 tecnativa** —— 可做到只白名单 start/stop/inspect 三个端点、彻底禁 create；当前方案已拦住主要高危面，收益递减
3. **服务健康检查 / 自动拉起编排** —— 与本次无关

---

## 八、建议执行顺序

```
N1 TCP 分支（10 行，向后兼容）
  ↓
N2 socket-proxy + 摘 sock 挂载
  ↓
★ 回归 samba 重建分支        ← 最易回退点，必须先验
  ↓
N3 profiles + 清理旧容器
  ↓
N4 四态开关
  ↓
N5/N6 文档
```

⚠️ 切换 socket-proxy 后**必须先在测试环境跑通 samba 重建分支**再上生产，否则改完发现该功能失效需要回退。
