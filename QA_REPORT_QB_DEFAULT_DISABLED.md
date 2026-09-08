# QA 报告：qBittorrent 默认禁用改造验证

## 任务信息
- 仓库路径：`C:\Users\15657.DC-PC\WorkBuddy\2026-09-08-16-46-27\iso-hub`
- 上游 GitHub：https://github.com/DC1024/iso-hub
- 验证目标：qBittorrent 默认禁用改造
- 执行人：software-qa-engineer
- 执行时间：2026-09-08

## 环境说明
- Python 解释器：`C:\Users\15657.DC-PC\.workbuddy\binaries\python\versions\3.13.12\python.exe`
- 工具安装方式：`pip install --target ./.qa-tools ruff==0.16.6 bandit==1.9.4 pytest`
- 运行方式：`PYTHONPATH=./.qa-tools python -m ...`

---

## 1. 代码静态检查

### 1.1 python -m py_compile 全量语法检查
- **状态**：PASS
- **结果**：对仓库内全部 9 个 `.py` 文件执行 `python -m py_compile`，全部通过，无语法错误。

### 1.2 ruff check . --select=F
- **状态**：PASS
- **结果**：`All checks passed!` 无 F 类致命错误。

### 1.3 bandit -r web iso_download scripts tests -ll
- **状态**：PASS
- **结果**：
  - Medium: 0
  - High: 0
  - Low: 15（仅 Low 级别，按任务要求不失败）
  - nosec 注释均合理（B104 监听地址、B105 硬编码凭据变量、B108 临时文件、B507 SSL 校验）

---

## 2. qBittorrent 默认禁用业务逻辑

### 2.1 DEFAULT_QB['enabled'] 默认为 False
- **状态**：PASS
- **位置**：`web/app.py:331-337`
- **验证**：`DEFAULT_QB = {"enabled": False, ...}`，默认值确实为 `False`。
- **测试覆盖**：`tests/test_app_qb.py::TestDefaultQbDisabled::test_default_qb_enabled_is_false` 通过。

### 2.2 load_qb_settings 默认返回禁用配置
- **状态**：PASS
- **位置**：`web/app.py:365-374`
- **验证**：当 `settings.json` 不存在时，`load_qb_settings()` 返回 `dict(DEFAULT_QB)`，因此 `enabled` 恒为 `False`。
- **测试覆盖**：`tests/test_app_qb.py::TestDefaultQbDisabled::test_load_qb_settings_defaults_to_disabled` 通过。

### 2.3 _ensure_qb_enabled 对所有 /api/torrent/* 接口在未启用时返回 403
- **状态**：PASS
- **位置**：`web/app.py:1681-1686`（函数定义），`web/app.py:1689-1893`（全部 /api/torrent 路由）
- **验证**：
  - `_ensure_qb_enabled` 在未启用时返回 `(False, (jsonify({"error": "..."}), 403))`。
  - 所有 8 个 `/api/torrent/*` 接口均在函数入口处调用 `_ensure_qb_enabled()`：
    - GET `/api/torrent/sources`
    - GET `/api/torrent/info`
    - POST `/api/torrent/add`
    - POST `/api/torrent/delete`
    - POST `/api/torrent/rss/add`
    - POST `/api/torrent/rss/remove`
    - POST `/api/torrent/link/add`
    - POST `/api/torrent/link/remove`
- **测试覆盖**：`tests/test_app_qb.py::TestEnsureQbEnabled::test_returns_403_when_disabled` 通过。

### 2.4 set_qb 启动容器、写入 PBKDF2 密码、重启生效；禁用时停止容器并移除自动重启策略
- **状态**：PASS
- **位置**：`web/app.py:383-447`
- **验证**：
  - 启用流程：`start` → `update RestartPolicy=unless-stopped` → 写入 `qBittorrent.conf`（含 PBKDF2 密码）→ `restart`。
  - 禁用流程：`update RestartPolicy=no` → `stop`。
  - `_make_qb_pbkdf2` 使用 PBKDF2-HMAC-SHA512，返回 `qBittorrent` 配置格式 `@ByteArray(salt_b64:key_b64)`。
- **测试覆盖**：`tests/test_app_qb.py::TestSetQb::test_enable_starts_updates_restart_and_restarts`、`test_disable_stops_and_removes_restart`、`test_pbkdf2_format` 均通过。

### 2.5 _sync_disabled_qb 在启动时若发现容器运行则停止
- **状态**：PASS
- **位置**：`web/app.py:725-735`
- **验证**：
  - 当 `qb.enabled=False` 且 `share_container_state(...)` 返回 `"running"` 时，调用 `set_qb(False, username, password)` 停止容器。
  - 容器未运行或已启用时不执行停止。
- **测试覆盖**：`tests/test_app_qb.py::TestSyncDisabledQb::test_*` 三个用例均通过。

---

## 3. Compose 配置

### 3.1 qbittorrent 服务 `restart: no`
- **状态**：PASS
- **位置**：
  - `docker-compose.yml:98`
  - `docker-compose.acr.yml:110`
  - `docker-compose.dockerhub.yml:112`
- **验证**：三个 compose 文件中 `qbittorrent` 服务均配置 `restart: no`，符合默认禁用策略。

### 3.2 主服务挂载 `./qb-config:/qb-config:rw`
- **状态**：PASS
- **位置**：
  - `docker-compose.yml:18`
  - `docker-compose.acr.yml:31`
  - `docker-compose.dockerhub.yml:33`
- **验证**：主 `iso-hub` 服务均挂载 `./qb-config:/qb-config:rw`，允许主容器直接改写 `qBittorrent.conf`。

---

## 4. 前端验证

### 4.1 qBittorrent 设置卡片
- **状态**：PASS
- **位置**：`web/static/index.html:442-457`
- **验证**：存在 `qBittorrent 设置` 卡片，含启用开关 `id="sw-qb"`、账号/密码输入框、保存按钮。

### 4.2 种子页禁用遮罩
- **状态**：PASS
- **位置**：`web/static/index.html:511-516`
- **验证**：存在 `id="qb-disabled-overlay"` 遮罩，提示用户先启用 qBittorrent。

### 4.3 checkQbEnabled 控制逻辑
- **状态**：PASS
- **位置**：`web/static/index.html:1739-1748`
- **验证**：
  - `checkQbEnabled()` 调用 `GET /api/qb/settings`。
  - 根据 `cfg.enabled` 调用 `paintQbOverlay(enabled)` 切换遮罩与内容区域显示。
  - 启用后加载种子源和下载状态。

---

## 5. 安全回归检查

### 5.1 SSRF 防护
- **状态**：PASS
- **验证**：
  - `/api/torrent/add`、rss/add、link/add 均对 URL 使用 `_is_http_url()` 校验，仅允许 `http/https/magnet`。
  - RSS 解析使用 `defusedxml` 或标准库安全解析（`distro_torrents.py`）。
  - 本改造未新增可接受任意 URL 的接口。

### 5.2 路径穿越防护
- **状态**：PASS
- **验证**：
  - `/api/torrent/add` 使用 `_safe_join(typ, distro)` 校验保存路径，非法输入回退到 `DATA_DIR/_torrents`。
  - 改造未引入新的文件系统写入点。

### 5.3 共享凭据处理
- **状态**：PASS（已知风险无新增）
- **验证**：
  - qBittorrent WebUI 密码经 PBKDF2 哈希后写入 `qBittorrent.conf`（非明文存储）。
  - 凭据通过 `settings.json` 和 `QB_USER/QB_PASS` 环境变量传递，与现有 Samba/WebDAV 共享凭据模式一致。
  - 默认凭据 `admin/adminadmin` 随容器首次启动即被强制改写，未引入新的凭据泄露面。

### 5.4 容器权限
- **状态**：INFO（已有明确安全提示）
- **位置**：`docker-compose*.yml` 中主服务挂载 `/var/run/docker.sock`。
- **说明**：该风险在 compose 文件中已标注“仅在可信内网/单机使用，并开启 ISO_HUB_REQUIRE_LOGIN=1”。本次改造未改变该挂载或权限模型。

---

## 6. 新增测试

为本次改造在 `tests/test_app_qb.py` 中新增 11 个单元测试，全部通过：

```text
TestDefaultQbDisabled::test_default_qb_enabled_is_false PASSED
TestDefaultQbDisabled::test_load_qb_settings_defaults_to_disabled PASSED
TestDefaultQbDisabled::test_load_qb_settings_preserves_enabled_true PASSED
TestEnsureQbEnabled::test_returns_403_when_disabled PASSED
TestEnsureQbEnabled::test_returns_true_when_enabled PASSED
TestSetQb::test_disable_stops_and_removes_restart PASSED
TestSetQb::test_enable_starts_updates_restart_and_restarts PASSED
TestSyncDisabledQb::test_does_nothing_when_enabled PASSED
TestSyncDisabledQb::test_does_nothing_when_not_running PASSED
TestSyncDisabledQb::test_stops_running_container_when_disabled PASSED
TestQbPasswordHash::test_pbkdf2_format PASSED
```

---

## 7. 总结

| 检查项 | 状态 |
|--------|------|
| python -m py_compile 全量语法检查 | PASS |
| ruff check . --select=F | PASS |
| bandit -r web iso_download scripts tests -ll（仅 Medium/High 失败） | PASS |
| DEFAULT_QB['enabled'] 默认为 False | PASS |
| _ensure_qb_enabled 对 /api/torrent/* 返回 403 | PASS |
| set_qb 启停与重启策略更新 | PASS |
| _sync_disabled_qb 启动同步停止 | PASS |
| 三份 compose 中 qbittorrent restart: no | PASS |
| 主服务挂载 ./qb-config:/qb-config:rw | PASS |
| 前端 qBittorrent 设置卡片 | PASS |
| 前端种子页禁用遮罩 | PASS |
| 前端 checkQbEnabled 控制 | PASS |
| SSRF 防护无新增风险 | PASS |
| 路径穿越防护无新增风险 | PASS |
| 共享凭据处理无新增风险 | PASS |
| 新增单元测试 | 11/11 PASS |

## 缺陷记录
无。

## 结论
**IS_PASS: YES**

本次 qBittorrent 默认禁用改造通过了全部静态检查、业务逻辑验证、Compose 配置验证、前端验证和安全回归检查，未发现源码 Bug。新增 11 个单元测试全部通过。代码可进入后续推送流程。
