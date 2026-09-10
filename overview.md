# v1.2.9 — 修复「ISO 还没下完就开始校验」

## 用户报告的现象

日志（下载一个 1523 MiB 的 Arch ISO）：

```
19:45 候选源 1/4: https://mirrors.tuna.tsinghua.edu.cn/archlinux/iso/2026.08.01/archlinux-2026.08.01-x86_64.iso
19:47 尝试从URL获取最新校验和: .../sha256sums.txt
19:47 ✗ 校验和验证失败, 丢弃半成品重试: 所有校验和验证都失败
```

疑问：ISO 不应该下载完成才校验吗？为什么 19:45 开始、19:47 就在校验？56 MiB / 1523 MiB。

## 诊断结论：不是"校验时机"错，是"完整性判据被短路"

校验代码的位置**本来就是对的**（在下载循环之后）。真正的缺陷是判据：

```python
# 修改前
if total and part.stat().st_size != total:
    raise Exception(f"大小不匹配: 期望 {total}B, 实际 {part.stat().st_size}B")
```

`total` 来自响应头的 `Content-Length` / `Content-Range`。当镜像站**不返回 `Content-Length`**（或其非法/为 0）时 `total == 0`，`if total and ...` 直接短路 → **整个大小核对被静默跳过** → 一个只下了 56 MiB 的不完整文件直接进入 SHA256 比对 → 必然不符 → 报"校验和验证失败"。

日志上看起来像"没下完就校验"，实际是"没检查完整性，直接拿去校验了"。

已用本地 HTTP 服务器复现（不返回 `Content-Length` 的截断流 → 跳过 size 检查 → 校验失败），并确认镜像站协议本身正常（HEAD 返回正确 content-length、Range 可用）。

## 三处修复

### 1. 长度三级兜底 —— `_resolve_total(resp, have, head_total)`

优先级从最可靠到最兜底：

| 级别 | 来源 | 说明 |
|---|---|---|
| 1 | `Content-Range: bytes a-b/TOTAL` | 服务器明确告知全长，最可信 |
| 2 | `Content-Length` + 已下量 | 续传时 Content-Length 是剩余量，加上 have 才是全长 |
| 3 | `head_total`（调用方 HEAD 预取） | 服务器不给长度时的兜底 |
| 0 | 都没有 | 语义明确为"确实无从判断"，**而非放行** |

`web/iso_runner.py` 调用侧：`#TARGET` 上报的大小为 0 时，自动扫描后续候选源的 HEAD 补测，避免"第一个源恰好故障 → 进度条失去百分比基准"（与 v1.2.6 修过的"进度条卡 0%"同类病根）。

### 2. 区分「传输截断」与「内容损坏」

新增两个异常，语义不同、处置不同：

```python
class TruncatedTransfer(Exception):
    """响应流提前结束: 有预期长度但实际写入不足。保留 .part 供续传。"""

class CorruptPayload(Exception):
    """传输完成(或长度未知)但内容校验不通过。丢弃 .part 重下。"""
```

| 情形 | 判定 | 对 `.part` 的处置 |
|---|---|---|
| 有预期长度，实际写入**不足** | `TruncatedTransfer` | **保留**（有效前缀，可续传） |
| 写入**超过**预期长度 | `CorruptPayload` | 丢弃（内容不可信） |
| 长度达标，校验和**不符** | `CorruptPayload` | 丢弃（内容确实损坏） |
| 长度**未知**，校验和**不符** | `TruncatedTransfer`（保守） | **保留**（无法区分截断/损坏，宁多占磁盘不误删进度） |
| 长度未知，校验和通过 | 成功 | 改名为最终文件 |

修改前是一律删除 `.part` —— 一次网络抖动就把用户已下的几百 MB 进度全丢掉。

### 3. 订阅同步路径同步修复（`iso_download/download_linux.py`）

新增 `_head_content_length()` 方法，替换原有的 `if total_size and ...` 短路；仍拿不到长度时打印明确告警「⚠ 服务器未提供文件总大小，无法核对完整性，直接交由校验和判定」，不再静默放行。

## 验证

- **单元测试**：`tests/` 全量 **218 passed**（v1.2.8 为 200）。新增：
  - `TestTotalResolution`（5 例）—— 三级兜底的每一级 + 非法 Content-Length + 全无返回 0
  - `TestTruncationVsCorruption`（7 例）—— 截断保留 / 损坏丢弃 / 长度未知保守保留 / 超长丢弃 / HEAD 兜底判截断 / 对照组成功
  - `TestRunnerSourceContract` 新增 6 条静态护栏 —— 含 `assertNotIn("if total and part.stat().st_size != total:")` 防止回归
- **真实 HTTP 端到端**（`NO_PROXY=127.0.0.1`，真起 HTTP 服务器）：
  - A. 无 Content-Length + 截断 → 保留 `.part`，已下进度不丢
  - B. 无 Content-Length + 完整 → 成功改名
  - C. 有 Content-Length + 截断 → 保留 `.part`（`requests` 抛 `IncompleteRead`，被兜底 catch 接住）
  - D. 长度对 + 内容坏 → 丢弃 `.part`
  - 关键用户场景：第 1 轮截断 6666/20000 保留进度 → 第 2 轮带 `Range: bytes=6666-` 续传补齐 → 校验通过 → 改名为最终文件，内容与源**逐字节一致**。

## 意外发现（记录备查）

当服务器**确实声明了** `Content-Length` 却中途断流时，`requests` 的 `iter_content` 会**先抛出 `IncompleteRead`**，我自己的 `.part`-保留逻辑根本来不及执行。这条路径实际上由 requests 库保护，加上我 `except Exception` 分支同样保留 `.part`，形成双重保障。换句话说：本次 bug 只在"服务器不给长度"这一分支才真正暴露出来 —— 恰好就是用户遇到的那台镜像源。

## 改动文件

| 文件 | 改动 |
|---|---|
| `web/iso_runner.py` | +137 −24；新增 2 个异常类、`_resolve_total()`、`_download_file_with_failover()` 判据重写、`head_total` 参数、候选源大小补测 |
| `iso_download/download_linux.py` | +30 −8；新增 `_head_content_length()`、替换大小校验短路 |
| `web/static/index.html` | 版本号 1.2.8 → 1.2.9 |
| `tests/test_part_protocol.py` | +13 例（三级兜底 + 截断/损坏区分 + 6 条静态护栏） |
| `tests/test_download_status.py` | 版本断言升至 1.2.9 |
| `wiki/Home.md` | 补 1.2.9 里程碑 |

## 备注

生产环境上那个 339 MB 的 `archlinux-2026.07.01-x86_64.iso.part` 本次未动。修好之后**点继续下载就能接着用**，不会再被当作"损坏半成品"删掉。
