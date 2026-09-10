# Docker 服务开关改造 — 存量环境迁移说明

> 关联文档：`docs/Docker服务开关改造需求说明.md`
> 适用对象：已部署旧版（主容器直接挂载 `/var/run/docker.sock`，sidecar 预创建为 stopped 容器）的环境
> 目标：迁移到 socket-proxy 权限收窄 + compose profiles 按需启用架构

---

## 一、这次改了什么

| 项 | 旧行为 | 新行为 |
|---|---|---|
| Docker 访问路径 | 主容器挂载裸 `/var/run/docker.sock`（等价宿主机 root） | 主容器设 `DOCKER_HOST=tcp://socket-proxy:2375`，经 `socket-proxy` 白名单转发 |
| sidecar 生命周期 | 预创建 samba/webdav/qbittorrent（stopped），镜像全量拉取 | 加 `profiles`，未启用不创建容器、不拉镜像 |
| 网页开关状态 | running / created / exited / none | **四态**：running ● / stopped ○ / not_deployed ✗（灰态+命令提示）/ unknown ?（灰态+排障提示） |

**向后兼容**：若 `DOCKER_HOST` 未设置，`web/app.py` 会完全回退到既有 Unix socket 路径，存量部署不受影响。

---

## 二、前置要求：Compose v2

`profiles` 需要 **Compose v2**（`docker compose`，带空格）。Python 版 `docker-compose`（v1，带连字符）不支持且已 EOL。

```bash
docker compose version      # 期望 Docker Compose version v2.x.x
```

若只有 v1，请升级 Docker 引擎（20.10+ 自带 v2 插件）。

---

## 三、迁移步骤

### 步骤 1：备份现有配置

```bash
cd /opt/iso-hub
cp docker-compose.yml docker-compose.yml.bak
cp -r data data.bak         # 至少备份 data/settings.json
```

### 步骤 2：更新 compose 文件

用新版 `docker-compose.yml` / `docker-compose.acr.yml` / `docker-compose.dockerhub.yml` 替换旧文件（核心变化：新增 `socket-proxy` 服务、主容器摘掉 docker.sock 挂载、sidecar 加 `profiles`）。

### 步骤 3：清理旧 stopped 容器（N5 核心）

旧环境中预创建的 samba/webdav/qbittorrent 容器在加 `profiles` 后会变成**孤儿容器**（compose 不再管理它们），需手动清理：

```bash
# 先确认要清理的容器(应显示 3 个 stopped 容器)
docker ps -a --filter name=iso-hub-samba --filter name=iso-hub-webdav --filter name=iso-hub-qbittorrent

# 清理旧 stopped sidecar 容器(仅删容器, 不动镜像和数据卷)
docker compose rm -s smb webdav qbittorrent 2>/dev/null \
  || docker rm -f iso-hub-samba iso-hub-webdav iso-hub-qbittorrent
```

> ⚠️ **实测确认**：以上行为须在测试环境跑通后再上生产。`docker compose rm -s` 只清理 compose 管理的容器；
> 若旧容器已因缺 profile 变成孤儿，命令无匹配项，此时用 `docker rm -f` 兜底。
> 清理只删容器，`./data`、`./qb-config`、`./webdav-config` 等挂载目录数据完全保留。

### 步骤 4：拉起新版（含 socket-proxy）

```bash
docker compose pull
docker compose up -d                       # 核心: iso-hub + socket-proxy
```

### 步骤 5：按需启用 sidecar

```bash
docker compose --profile share up -d                   # 需要 SMB/WebDAV
docker compose --profile bt up -d                      # 需要 qBittorrent
docker compose --profile share --profile bt up -d      # 全部
```

启用后回到网页「设置」页，服务开关状态应为 `● 运行中`。

---

## 四、验证清单

```bash
# 1. 主容器不再持有裸 sock(期望: No such file or directory)
docker exec iso-hub sh -c 'ls /var/run/docker.sock'

# 2. 主容器 DOCKER_HOST 指向 socket-proxy(期望: tcp://socket-proxy:2375)
docker exec iso-hub env | grep DOCKER_HOST

# 3. socket-proxy 已运行
docker ps --filter name=iso-hub-socket-proxy

# 4. proxy 拦住高危端点(从主容器内访问 /images/json 应被拒)
docker exec iso-hub python -c "import http.client;\
c=http.client.HTTPConnection('socket-proxy:2375',timeout=5);\
c.request('GET','/images/json');print('HTTP',c.getresponse().status)"   # 期望 403

# 5. profiles 生效(未启用时无输出)
docker ps -a --filter name=iso-hub-samba

# 6. 回归: 改网络后重建 samba 分支仍可用(设置页改 samba 凭据后确认容器被重建且可用)
```

**网页端四态验证**：

| 操作 | 期望 UI |
|---|---|
| 未启用 `--profile share` | ✗ 未部署 + 命令提示 + 一键复制 |
| 启用后 | ● 运行中；点停止 → ○ 已停止；点启动 → 恢复 |
| `docker stop iso-hub-socket-proxy` | ? 未知（**不能**误报「未部署」） |

---

## 五、回滚

若需回退到旧架构：

```bash
cd /opt/iso-hub
cp docker-compose.yml.bak docker-compose.yml
docker compose up -d
```

旧版主容器挂载裸 sock 的路径由 `DOCKER_HOST` 未设置时的 Unix socket 回退分支保证，无需改动 `web/app.py`。
