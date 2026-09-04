# ISO 下载助手

该工具通过 `download_linux.py` 解析 `distributions.json`，自动下载多个 Linux 发行版 ISO，并根据可用的校验信息进行验证。配套的 `update_distributions.py` 能抓取镜像站目录，自动刷新元数据，减少手工维护成本。

## 运行环境

- Python 3.10+
- `requests`, `tqdm` 等依赖（执行 `pip install -r requirements.txt`）
- 可选：虚拟环境 `python -m venv .venv && source .venv/bin/activate`

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `download_linux.py` | 命令行入口，支持列出、筛选、下载、校验发行版 |
| `distributions.json` | 下载元数据；由更新脚本或人工维护 |
| `update_distributions.py` | 根据 `sources_config.json` 抓取镜像目录并生成新的 `distributions.json` |
| `sources_config.json` | 描述各发行版的镜像 URL、匹配正则、模板等规则 |

## 自动更新 distributions.json

```bash
python update_distributions.py --pretty          # 生成并写入 JSON
python update_distributions.py --dry-run --pretty  # 只在终端预览
```

流程：
1. 编辑 `sources_config.json`，定义各发行版的抓取策略（目录 URL、正则、模板等）。
2. 运行 `update_distributions.py`，脚本会请求镜像站列表、提取版本与 ISO 名称、组合下载/校验地址，并输出排序后的 `distributions` 数组。
3. 若某个源匹配失败，脚本会打印 `[WARN]` 但继续处理其他发行版。

### sources_config.json 编写提示

- 每个对象描述一个发行版或子渠道，公共字段包括 `distribution`、`type`、`strategy`、`max_entries`、`download_template`、`checksum_template` 等；`checksum` 留空表示运行时从 `checksum_url` 获取。
- `strategy` 选项（示例参考现有 Fedora/Deepin 配置）。
- `dated_directory`：遍历子目录（如 `25.04/`）。可在条目中配置 `overrides`，按版本正则切换不同模板（Deepin 20.x 与 23.x 目录结构不同时适用）。
- `flat_listing`：针对直接列出 ISO 的目录，`artifact_regex` 捕获文件名。
- `versioned_flat_listing`：先解析版本目录，再进入子目录匹配 ISO（Fedora 使用）。需额外的 `sub_listing_template`。
- `static`：固定版本列表，无需抓取。
- 模板字符串采用 `str.format` 语法，可引用 `{version}`、`{listing_url}`、`{sub_listing_url}`、`{match}`、`{release}` 等上下文变量。
- 正则表达式建议使用命名捕获组（`(?P<value>...)`），方便在模板中复用；示例参考现有 Fedora/Deepin 配置。
- 修改后先执行 `python update_distributions.py --dry-run --pretty` 验证输出，确保 URL、校验路径正确。

## download_linux.py 用法

```bash
python download_linux.py --list                                  # 查看所有发行版
python download_linux.py --list --filter-name Ubuntu             # 名称过滤
python download_linux.py --list --filter-type linux              # 类型过滤
python download_linux.py --download "Ubuntu"                     # 下载指定发行版
python download_linux.py --download-all --no-verify              # 下载全部并跳过校验
python download_linux.py --download "Ubuntu" --download-dir ~/isos  # 指定目录
python download_linux.py --json-file my_distributions.json --list   # 使用自定义 JSON
```

## 校验和策略

脚本会按以下优先级验证下载结果：
1. `checksum_url`：在线拉取最新校验和（支持标准与 PGP 包裹格式）。
2. `checksum` 字段：使用 JSON 中预置的哈希值。
3. 若两者都缺失，会提示并跳过校验。

## 下载目录结构

```
linux/
├── Ubuntu/
│   ├── ubuntu-25.04-desktop-amd64.iso
│   └── ubuntu-25.04-live-server-amd64.iso
├── CentOS/
│   └── CentOS-Stream-9-latest-x86_64-dvd1.iso
└── ...
```

## distributions.json 结构

```json
{
  "distributions": [
    {
      "distribution": "发行版名称",
      "type": "操作系统类型",
      "download_url": "下载链接",
      "checksum_url": "校验和文件链接",
      "checksum": "SHA256校验和"
    }
  ]
}
```

目前支持的操作系统类型：
- **linux**（已实现）
- **windows / macos**（预留）

## 免责声明

此仓库的所有代码与文档均由 Cursor + Codex 联合“自动生成”，如若因为脚本导致宇宙毁灭、磁盘暴走或咖啡变凉，一切后果都均与任何人类无关。
