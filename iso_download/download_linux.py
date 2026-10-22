#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Linux发行版下载器
用于下载、更新和验证各种Linux发行版的ISO文件
"""

import contextlib
import json
import os
import sys
import hashlib
import shutil
import subprocess
import requests
import argparse
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse
import time
from tqdm import tqdm


ALLOWED_TYPES = {"linux", "bsd", "windows", "macos"}
# 下载期间写 <最终名>.part, 全部校验通过后才原子改名为最终名(见 os.replace)。
# 校验阶段拿到的文件名因此可能带这个后缀, 凡是要拿文件名去外部数据源
# (sha256sums.txt 等)查表的地方, 都必须先剥离它。
PART_SUFFIX = ".part"
# M4/M5: 半成品的「来源标记」(<part>.src), 记住这个 .part 是从哪个 URL 下的。
# 有了多源 failover 之后, 跨源续传就成了真问题: 两个镜像上的同名文件可能是
# 不同版本, 前一半来自 A、后一半来自 B 拼出来的文件只能靠最终 SHA256 兜底。
# 必须与 web/iso_runner.py、web/app.py 的同名常量保持一致(有测试盯着)。
PART_SRC_SUFFIX = ".part.src"
# ISO 存放模式: classified=<type>/<发行版>/ 分类存放(默认); flat=统一单目录。
# 必须与 web/app.py 的 STORAGE_MODES / FLAT_ISO_DIRNAME 保持一致。
STORAGE_MODES = ("classified", "flat")
FLAT_DIRNAME = "iso"

# v1.3.17: 网络抖动/镜像限流的统一重试策略。
# 背景(生产实测): ubuntu-26.04(.1) 与 archlinux-2026.08.01 三条下载在同一分钟内
# 全部 hard 失败, 事后探测 URL 均 200 —— 典型的出口瞬时抖动。旧代码一次失败就
# 记 failure 且**永不重试**(failure 记录也没有 TTL), 于是"一分钟的网络抖动"在 UI 上
# 留了一道永久的失败疤。这里在 HTTP 层加指数退避重试, 覆盖:
#   * 连接/读取超时、连接被重置(瞬时抖动)
#   * 429(被限流)与 5xx(镜像站临时故障)
# 注意: 只对幂等的 GET/HEAD 生效, 且 **不重试 416**(416 是"已完成"信号, 见下载主流程)。
RETRY_TOTAL = 3
RETRY_BACKOFF = 1.5
RETRY_STATUSES = (429, 500, 502, 503, 504)

# v1.5.2: 下载前剩余空间预检(与 web/iso_runner.py 的同名策略保持一致)。
# 旧行为: 20GB ISO 下到 95% 才 ENOSPC, 只留一个坏掉的 .part。现在写第一个字节前
# 就按 Content-Length/Content-Range 比一次可用空间, 不足直接拒单。
# 预留 256 MiB 给校验和文件、日志与"刚好写满"导致的其他写盘失败。
DISK_SAFETY_MARGIN = 256 * 1024 * 1024


# --------------------------------------------------------------------------- 半成品来源标记
def part_src_path(part) -> Path:
    """半成品来源标记的路径: ``<part>.src``。"""
    return Path(str(part) + PART_SRC_SUFFIX)


def read_part_src(part) -> str:
    """读取半成品是从哪个 URL 下的; 没有标记(旧版本留下的 .part)返回空串。"""
    try:
        return part_src_path(part).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_part_src(part, url: str) -> None:
    """记下当前半成品的来源。写不进去只是失去同源判定, 不影响下载本身。"""
    try:
        part_src_path(part).write_text((url or "") + "\n", encoding="utf-8")
    except OSError:
        pass


def forget_part_src(part) -> None:
    try:
        part_src_path(part).unlink()
    except OSError:
        pass


def discard_part(part) -> None:
    """删半成品, 连同它的来源标记 —— 留着标记会污染下一次续传的判断。"""
    for p in (Path(part), part_src_path(part)):
        try:
            p.unlink()
        except OSError:
            pass


def _human_bytes(n: int) -> str:
    """人类可读的字节数。低空间时 GiB 两位小数会显示成 0.00, 必须往下退一级。"""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GiB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MiB"
    return f"{n / 1024:.0f} KiB"


def check_free_space(directory, need_bytes: int, margin: int = DISK_SAFETY_MARGIN) -> tuple:
    """返回 (是否够用, 说明)。need_bytes<=0 表示大小未知 —— 无从判断, 放行。"""
    if need_bytes <= 0:
        return True, "文件大小未知, 跳过剩余空间预检"
    try:
        free = shutil.disk_usage(str(directory)).free
    except OSError as e:
        # 部分 FUSE/网络盘不支持 statvfs, 不能因此把正常下载全挡死
        return True, f"无法获取剩余空间({e}), 跳过预检"
    if free >= need_bytes + margin:
        return True, ""
    return False, (
        f"磁盘空间不足: 还需 {_human_bytes(need_bytes)}, "
        f"另需预留 {_human_bytes(margin)}, 当前可用 {_human_bytes(free)} "
        f"({directory})"
    )


def http_get_with_retry(url: str, *, headers=None, stream: bool = False,
                        timeout=None, retries: int = RETRY_TOTAL):
    """带指数退避的 GET: 瞬时网络抖动/镜像限流不再直接判死。

    刻意**不走** requests.Session + HTTPAdapter(Retry): 本仓库的测试与生产代码都
    围绕模块级 ``requests.get`` 打桩(``patch.object(requests, "get", ...)``), 换成
    Session 会让全部既有桩点失效。这里保持调用 ``requests.get``, 只在它外面加退避。

    重试条件(缺一不可):
      * 抛 RequestException(连接超时/重置/读超时) —— 典型瞬时抖动
      * 响应码落在 RETRY_STATUSES(429 限流 / 5xx 镜像临时故障)

    **不重试** 416: 那是"断点已满"的完成信号(见 download_distribution), 重试无意义。
    """
    last_exc = None
    for attempt in range(max(1, retries)):
        try:
            resp = requests.get(url, headers=headers, stream=stream, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt + 1 < max(1, retries):
                _delay = RETRY_BACKOFF * (2 ** attempt)
                print(f"  网络异常({e.__class__.__name__}), {_delay:.1f}s 后重试"
                      f" ({attempt + 1}/{retries})")
                time.sleep(_delay)
                continue
            raise
        code = getattr(resp, "status_code", 0) or 0
        if code in RETRY_STATUSES and attempt + 1 < max(1, retries):
            _delay = RETRY_BACKOFF * (2 ** attempt)
            try:
                resp.close()
            except OSError:  # 关闭失败不影响重试
                pass
            print(f"  服务器返回 {code}, {_delay:.1f}s 后重试 ({attempt + 1}/{retries})")
            time.sleep(_delay)
            continue
        return resp
    if last_exc is not None:
        raise last_exc
    raise requests.exceptions.RequestException("重试耗尽且无响应")


def _safe_dist_dir(download_dir: Path, typ: str, name: str,
                   flat: bool = False) -> Path | None:
    """把 (type, name) 安全解析为该发行版 ISO 的**存放目录**, 拒绝路径穿越/非法字符。

    校验顺序刻意放在模式判断之前 —— 非法 (type, name) 在任何模式下都返回 None。
      * flat=False(默认) -> download_dir/<type>/<name>/
      * flat=True        -> download_dir/iso/  (全发行版平铺同一目录)
    """
    if not typ or not name or typ not in ALLOWED_TYPES:
        return None
    for comp in (typ, name):
        comp = str(comp)
        if comp != comp.strip() or comp in (".", ".."):
            return None
        if "/" in comp or "\\" in comp:
            return None
    target = (download_dir / FLAT_DIRNAME) if flat else (download_dir / typ / name)
    target = target.resolve()
    try:
        target.relative_to(download_dir.resolve())
    except ValueError:
        return None
    return target


def _flat_of(downloader) -> bool:
    """读取下载器实例的存放模式; 缺失/非法一律按 classified(默认) 处理。"""
    return getattr(downloader, "storage_mode", "classified") == "flat"


class LinuxDistributionDownloader:
    def __init__(self, json_file: str = "distributions.json", download_dir: Optional[str] = None):
        """初始化下载器"""
        self.json_file = json_file
        # ISO 存放模式: 由调用方 runner(iso_runner / sync_subscriptions)注入
        # (`downloader.storage_mode = args.storage_mode`)。默认 classified, 保证单独
        # 运行本脚本(--all / 直接调用)时行为与历史版本完全一致。
        self.storage_mode = "classified"

        # 设置下载目录，默认为脚本所在目录
        if download_dir:
            self.download_dir = Path(download_dir)
        else:
            # 获取脚本所在目录
            script_dir = Path(__file__).parent
            self.download_dir = script_dir

        self.download_dir.mkdir(exist_ok=True)
        self.distributions = self.load_distributions()
        
        # 镜像站封禁浏览器 UA(反爬), 用通用默认 UA 更稳(实测 Chrome UA->403, 默认UA->200)
        self.headers = {
            'User-Agent': 'iso-hub/1.0 (Linux distribution ISO auto-updater)',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
    def load_distributions(self) -> Dict:
        """加载发行版信息"""
        try:
            with open(self.json_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"错误: 找不到文件 {self.json_file}")
            sys.exit(1)
        except json.JSONDecodeError:
            print(f"错误: {self.json_file} 不是有效的JSON文件")
            sys.exit(1)
    
    def list_distributions(self, filter_name: Optional[str] = None, 
                          filter_type: Optional[str] = None) -> None:
        """列出所有发行版信息"""
        print("可用的操作系统发行版:")
        print("=" * 80)
        
        # 按发行版名称分组
        dist_groups = {}
        for dist in self.distributions["distributions"]:
            dist_name = dist["distribution"]
            if dist_name not in dist_groups:
                dist_groups[dist_name] = []
            dist_groups[dist_name].append(dist)
        
        for dist_name, dists in dist_groups.items():
            # 应用名称过滤器
            if filter_name and filter_name.lower() not in dist_name.lower():
                continue
            
            # 应用类型过滤器
            if filter_type and dists[0]["type"].lower() != filter_type.lower():
                continue

            print(f"发行版: {dist_name}")
            print(f"类型: {dists[0]['type']}")
            
            if len(dists) > 1:
                print("可用版本:")
                for i, dist in enumerate(dists, 1):
                    filename = os.path.basename(urlparse(dist["download_url"]).path)
                    print(f"  {i}. {filename}")
            else:
                filename = os.path.basename(urlparse(dists[0]["download_url"]).path)
                print(f"版本: {filename}")
            
            print(f"下载链接: {dists[0]['download_url']}")
            print("-" * 80)
    
    def get_checksum_from_url(self, checksum_url: str, filename: str) -> Optional[str]:
        """从校验和URL获取指定文件的校验和。

        v1.3.1 修复(重要): 下载期间的校验对象是 ``xxx.iso.part``, 而
        sha256sums.txt 里登记的是 ``xxx.iso``。旧实现在行内做子串包含判断
        (``if filename in line``), 查询串比行内容更长("...iso.part" 不在
        "...iso" 里) → 永远匹配不上 → 返回 None → 每个源都报"校验和验证失败"。
        这正是"清华源和科大源都校验失败"的真因(与镜像站质量无关)。

        现在: 查表前先剥离 .part 后缀, 并改为**按空白切分后比对文件名字段**
        (而非子串包含), 避免 "archlinux-x86_64.iso" 误命中
        "archlinux-2026.09.01-x86_64.iso" 这类前缀相同的行。
        """
        # .part 是本地原子落盘的中间名, 任何外部查表都必须用最终文件名
        query_name = filename
        if query_name.endswith(PART_SUFFIX):
            query_name = query_name[: -len(PART_SUFFIX)]

        try:
            # v1.3.17: 带退避重试 —— checksum 是几十字节的小文件, 一次 502/超时
            # 就判"校验失败"代价太大(历史上因此误删/重下了完整 ISO)。
            response = http_get_with_retry(checksum_url, headers=self.headers, timeout=30)
            response.raise_for_status()

            checksum_content = response.text

            # 处理PGP签名的CHECKSUM格式
            lines = checksum_content.split('\n')
            in_pgp_section = False
            pgp_lines = []

            for line in lines:
                line = line.strip()

                # 检测PGP签名开始
                if line.startswith('-----BEGIN PGP SIGNED MESSAGE-----'):
                    in_pgp_section = True
                    continue

                # 检测PGP签名结束
                if line.startswith('-----BEGIN PGP SIGNATURE-----'):
                    in_pgp_section = False
                    break

                # 如果在PGP签名区域内，收集内容
                if in_pgp_section and line and not line.startswith('Hash:'):
                    pgp_lines.append(line)

            # 如果有PGP内容，使用PGP内容；否则使用原始内容
            content_to_parse = '\n'.join(pgp_lines) if pgp_lines else checksum_content

            found = self._extract_checksum_for(content_to_parse, query_name)
            if found:
                return found

            # 精确匹配未命中时, 给出可诊断的原因(区分"文件名不存在"与"格式异常"),
            # 而不是让调用方笼统地报"校验和验证失败"。
            if query_name != filename:
                print(f"  校验文件中未找到 {query_name} (本地文件为 {filename}, 已按最终名查表)")
            else:
                print(f"  校验文件中未找到文件名 {query_name}")
            return None

        except (requests.RequestException, ValueError) as e:
            print(f"  从URL获取校验和失败: {e}")
            return None

    @staticmethod
    def _is_sha256_hex(s: str) -> bool:
        """是否为合法的 64 位十六进制 SHA256 摘要。"""
        return len(s) == 64 and all(c in '0123456789abcdefABCDEF' for c in s)

    @classmethod
    def _extract_checksum_for(cls, content: str, name: str) -> Optional[str]:
        """在校验和文本中查找 ``name`` 的 SHA256, 找不到返回 None。

        支持两种格式:
          * 标准格式      `<64hex>  <filename>`
          * BSD/PGP 格式  `SHA256 (<filename>) = <64hex>`

        匹配一律**按字段精确比对文件名**, 不用子串包含 —— 否则
        `archlinux-x86_64.iso` 会错误命中 `archlinux-2026.09.01-x86_64.iso`。
        """
        for raw in content.split('\n'):
            line = raw.strip()
            if not line or name not in line:
                # 先用廉价的包含判断粗筛(名字是行的子串才有可能), 再精判
                continue

            # 格式一: <checksum>  <filename>
            parts = line.split()
            if len(parts) >= 2 and cls._is_sha256_hex(parts[0]):
                if parts[-1] == name:          # 文件名字段精确相等
                    return parts[0].lower()
                # 某些清单用 "checksum *filename" 形式, 去掉前导星号再比
                if parts[-1].lstrip('*') == name:
                    return parts[0].lower()

            # 格式二: SHA256 (filename) = checksum
            if line.upper().startswith('SHA256') and '=' in line and '(' in line:
                inside = line[line.index('(') + 1: line.index(')')] if ')' in line else ''
                if inside == name:
                    candidate = line.split('=', 1)[1].strip()
                    if cls._is_sha256_hex(candidate):
                        return candidate.lower()
        return None

    # 指纹只保留十六进制字符, 归一化后应为 40 位(OpenPGP v4 fingerprint)
    _FINGERPRINT_HEX = frozenset("0123456789ABCDEF")

    def _normalize_fingerprints(self, raw: object) -> List[str]:
        """把 gpg_key_fingerprint 字段归一化为 40 位大写十六进制指纹列表。

        兼容三种写法: 未配置(None/空) / 单个字符串 / 字符串数组(发行版轮换密钥
        或多位签名者时很有用)。无法归一化为 40 位的条目会被忽略并告警,
        避免一条误配的指纹拖累整站拒绝下载。
        """
        if not raw:
            return []
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        result: List[str] = []
        for item in items:
            if not isinstance(item, str):
                continue
            fp = "".join(ch for ch in item.upper() if ch in self._FINGERPRINT_HEX)
            if len(fp) == 40:
                result.append(fp)
            else:
                print(f"  ⚠ 忽略非法指纹配置(需 40 位十六进制): {item!r}")
        return result

    @staticmethod
    def _parse_colon_fingerprints(colons: str) -> List[str]:
        """从 gpg --with-colons 输出里提取所有**主密钥**(pub)的指纹。

        只取 pub 段后的 fpr 行: sub 段的 fpr 是子密钥指纹, 不参与锚定比对。
        """
        if isinstance(colons, bytes):  # 兼容未启用 text 模式的 gpg 输出
            colons = colons.decode("utf-8", "replace")
        fps: List[str] = []
        is_primary: bool = False
        for line in colons.splitlines():
            fields = line.split(":")
            if fields[0] in ("pub", "sub"):
                is_primary = fields[0] == "pub"
            elif fields[0] == "fpr" and is_primary and len(fields) > 9:
                fps.append(fields[9].upper())
        return fps

    def _keyring_fingerprints(self, keyring: Path) -> List[str]:
        """读取已缓存 keyring(或任意公钥文件)内主密钥的指纹。

        用 gpg --show-keys 直接读文件: 不依赖 GNUPGHOME 里的密钥库, 也不会改动它。

        v1.5.0: GNUPGHOME 必须显式指到临时目录。`--show-keys` 看起来是"纯读文件",
        实际仍会在 homedir 里创建 pubring.kbx / trustdb.gpg —— read_only 根文件系统
        的容器里直接 `Fatal: ... directory does not exist!`, 且**一行指纹都不输出**。
        后果不是报错, 而是: 缓存 keyring 被判"指纹不匹配" → 每轮下载都删掉重建、
        重新拉公钥; 公钥源恰好不可达时还会降级成只校验 SHA256。全部静默。
        """
        try:
            if not keyring.exists() or keyring.stat().st_size == 0:
                return []
        except OSError:
            return []
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, GNUPGHOME=home)
            r = subprocess.run(
                ["gpg", "--batch", "--no-tty", "--with-colons", "--show-keys", str(keyring)],
                capture_output=True, text=True, errors="replace", env=env)
        if r.returncode != 0:
            return []
        return self._parse_colon_fingerprints(r.stdout)

    def _list_imported_fingerprints(self, env: Dict[str, str]) -> List[str]:
        """在指定 GNUPGHOME 下列出刚导入的主密钥指纹。"""
        r = subprocess.run(["gpg", "--batch", "--with-colons", "--list-keys"],
                           capture_output=True, text=True, errors="replace", env=env)
        if r.returncode != 0:
            return []
        return self._parse_colon_fingerprints(r.stdout)

    def _prepare_keyring(self, keyring: Path, gpg_key_url: str,
                         expected: List[str]) -> str:
        """确保 keyring 存在, 且其中的密钥指纹与预期一致(公钥指纹锚定)。

        这是消除 TOFU(首次信任即信任)风险的关键: 无论公钥来自官网 HTTPS
        还是 keyserver, 指纹不匹配就绝不写入缓存。

        returns:
          "ok"   可继续做签名校验
          "skip" 无公钥地址/下载失败/解析失败(降级, 不阻塞)
          "fail" 指纹不匹配(公钥源被污染 或 硬编码指纹已过时) -> 拒绝下载
        """
        # 确保持久 keyring 目录存在(旧实现把 mkdir 放在 verify_signature, 重构到
        # _prepare_keyring 后丢失, 会导致 write_bytes 抛 FileNotFoundError, 进而被
        # 上层 except 兜底成 skip —— 静默降级, 防护形同虚设)。
        try:
            keyring.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"  创建 keyring 目录失败(降级): {e}")
            return "skip"
        try:
            cached_ok = keyring.exists() and keyring.stat().st_size > 0
        except OSError:
            cached_ok = False

        if cached_ok:
            if not expected:
                return "ok"  # 未配置指纹锚定: 沿用旧行为(只在首次拉取)
            actual = self._keyring_fingerprints(keyring)
            if any(fp in expected for fp in actual):
                return "ok"
            # 迁移路径: 旧缓存是无指纹校验时代写入的, 可能已被投毒, 必须重建
            print(f"  ⚠ 已缓存 keyring 指纹不匹配, 删除并重建: {keyring}")
            print(f"    预期指纹: {', '.join(expected)}")
            print(f"    实际指纹: {', '.join(actual) or '(空)'}")
            try:
                keyring.unlink()
            except OSError as e:
                print(f"    删除旧 keyring 失败: {e}")
                return "fail"

        if not gpg_key_url:
            return "skip"  # 未提供公钥获取地址, 官方无签名可验
        try:
            resp = requests.get(gpg_key_url, timeout=30)
            resp.raise_for_status()
            pubkey = resp.content  # 官方 .gpg 多为二进制, 必须用 content 而非 text
        except Exception as e:  # noqa: BLE001 - 边界: 公钥/签名获取失败一律降级 skip(不阻塞下载), 这是设计契约
            print(f"  获取官方公钥失败(降级): {e}")
            return "skip"
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, GNUPGHOME=home)
            imp = subprocess.run(["gpg", "--batch", "--import"], input=pubkey,
                                 capture_output=True, env=env)
            if imp.returncode != 0:
                # 公钥解析失败(如返回 HTML 错误页): 不阻塞下载
                print("  公钥解析失败(降级): "
                      f"{imp.stderr.decode('utf-8', 'replace')[:200]}")
                return "skip"
            actual = self._list_imported_fingerprints(env)
            matched = [fp for fp in actual if fp in expected] if expected else []
            if expected and not matched:
                # 要么公钥源被污染(安全事件), 要么硬编码指纹已过时。
                # 两者都不应静默降级到 SHA256 —— 那等于让防护失效。
                print("  ⚠ 公钥指纹不匹配, 拒绝下载")
                print(f"    公钥来源: {gpg_key_url}")
                print(f"    预期指纹: {', '.join(expected)}")
                print(f"    实际指纹: {', '.join(actual) or '(空)'}")
                return "fail"  # 关键: 不写入缓存
            if matched:
                # 只导出命中指纹的密钥: keyring 里绝不留未锚定的密钥,
                # 否则同一 keyring 里的"旁密钥"仍能放行伪造签名。
                exp = subprocess.run(["gpg", "--batch", "--export"] + matched,
                                     capture_output=True, env=env)
                data = exp.stdout
            else:
                exp = subprocess.run(["gpg", "--batch", "--export"],
                                     capture_output=True, env=env)
                data = exp.stdout
            if not data:
                print("  公钥导出结果为空(降级)")
                return "skip"
            keyring.write_bytes(data)  # 缓存到持久卷
            if expected:
                print(f"  ✓ 公钥指纹锚定通过: {', '.join(matched)}")
            return "ok"

    @staticmethod
    def _has_embedded_signature(checksum_text: str) -> bool:
        """判断 checksum 文本是否内嵌 clearsigned PGP 签名(如 Fedora CHECKSUM)。

        Fedora 的 CHECKSUM 文件是 cleartext signature 形式(direct signature),
        签名就内嵌在文件自身, 没有独立的 detached .asc/.gpg 签名文件可下载。
        识别标准: 同时出现 PGP signed message 头与 PGP signature 块。
        """
        if not checksum_text:
            return False
        return ("-----BEGIN PGP SIGNED MESSAGE-----" in checksum_text
                and "-----BEGIN PGP SIGNATURE-----" in checksum_text)

    def verify_signature(self, checksum_text: str, sig_url: str, gpg_key_url: str,
                         keyring_dir: Path,
                         expected_fingerprints: Optional[object] = None) -> str:
        """验证 checksum 文件是否由官方私钥签名。

        expected_fingerprints: 发行版条目里配置的 gpg_key_fingerprint
          (字符串或字符串数组)。配置后即启用公钥指纹锚定, 消除 TOFU 风险;
          未配置(None)时保持改动前的行为, 仅打印风险告警。

        returns:
          "pass"  签名验证通过(文件可信, 其内 SHA256 可放心比对 ISO)
          "fail"  签名校验失败/指纹不匹配(应拒绝下载)
          "skip"  官方无公钥/获取失败/无 gpg 环境(降级到 SHA256, 不阻塞)
        """
        # M1 修复(审查 2026-10): 没有 checksum 文本就没有可验的对象。
        # 缺了这道闸, checksum 源 404 后调用方会把 404 的 HTML 正文当 checksum
        # 传进来, 再去配 .gpg 验签, 必然 fail —— 又是一次"不可达被误判成被篡改"。
        if not checksum_text:
            print("  - 无 checksum 文本可验, 跳过 GPG 验签(降级)")
            return "skip"
        try:
            import shutil
            # 导入/导出/指纹提取都依赖 gpg; gpgv 只负责校验签名
            if shutil.which("gpg") is None:
                return "skip"  # 无 gpg 环境, 降级
            use_gpgv = shutil.which("gpgv") is not None
            keyring_dir.mkdir(parents=True, exist_ok=True)
            keyring = keyring_dir / "iso-hub.gpg"
            expected = self._normalize_fingerprints(expected_fingerprints)
            if not expected:
                print("  ⚠ 未配置公钥指纹锚定(gpg_key_fingerprint), 存在 TOFU 首次信任风险")
            # 1. 准备可信 keyring: 指纹锚定 + 旧缓存迁移校验
            state = self._prepare_keyring(keyring, gpg_key_url, expected)
            if state != "ok":
                return state
            # 2. 内嵌 clearsigned 签名(如 Fedora CHECKSUM): 直接对 checksum 文本
            #    单文件验签, 无需独立的 detached 签名文件。gpgv 不支持
            #    cleartext signature(direct signature), 只能用 gpg --verify。
            embedded = self._has_embedded_signature(checksum_text)
            if embedded:
                import tempfile
                with tempfile.TemporaryDirectory() as home:
                    env = dict(os.environ, GNUPGHOME=home)
                    kr = home + "/keyring.gpg"
                    open(kr, "wb").write(keyring.read_bytes())
                    cf = home + "/checksum.txt"
                    open(cf, "w", encoding="utf-8").write(checksum_text)
                    cmd = ["gpg", "--no-default-keyring", "--keyring", kr,
                           "--verify", cf]
                    r = subprocess.run(cmd, capture_output=True, env=env)
                    if r.returncode == 0:
                        return "pass"
                    print("  内嵌签名验证失败: "
                          f"{r.stderr.decode('utf-8', 'replace')[:200]}")
                    return "fail"
            # 3. 取 detached 签名文件(URL 或按惯例推导)
            if not sig_url:
                return "skip"  # 无签名文件可验
            # M1 修复(审查 2026-10): 必须先判 HTTP 状态码, 再判内容是否为空。
            # 镜像对不存在的 .gpg 返回 404 的 HTML 错误页 —— 正文非空, 拦不住下面
            # 的 `if not sig`, 交给 gpg 验签必然失败, 于是"源不可达"被误判成
            # "签名被篡改"(fail), 触发无谓重下并污染 /api/health 账本。
            # 不可达属"无法判定", 只能降级 skip —— 这是 v1.3.17 三态契约的要求:
            # fail 只留给"拿到了材料且明确不符"。
            try:
                sig_resp = requests.get(sig_url, timeout=30)
                sig_resp.raise_for_status()
                sig = sig_resp.content
            except requests.RequestException as e:
                print(f"  获取签名文件失败(降级跳过验签): {e}")
                return "skip"
            if not sig:
                return "skip"
            # 4. 验证 detached 签名: 优先 gpgv(不信任签名者), 否则回退 gpg --verify
            import tempfile
            with tempfile.TemporaryDirectory() as home:
                env = dict(os.environ, GNUPGHOME=home)
                kr = home + "/keyring.gpg"
                open(kr, "wb").write(keyring.read_bytes())
                cf = home + "/checksum.txt"
                sf = home + "/checksum.sig"
                open(cf, "w", encoding="utf-8").write(checksum_text)
                open(sf, "wb").write(sig)
                if use_gpgv:
                    cmd = ["gpgv", "--keyring", kr, sf, cf]
                else:
                    # gpg --verify 需显式禁用默认 keyring 以只信任导入的官方公钥
                    cmd = ["gpg", "--no-default-keyring", "--keyring", kr,
                           "--verify", sf, cf]
                r = subprocess.run(cmd, capture_output=True, env=env)
                if r.returncode == 0:
                    return "pass"
                return "fail"
        except OSError as e:  # gpg 子进程/临时文件/密钥缓存的磁盘错误
            print(f"  GPG 签名验证异常(降级): {e}")
            return "skip"

    def _default_sig_url(self, checksum_url: str) -> str:
        """按上游惯例推导 checksum 文件的签名 URL。

        Ubuntu: SHA256SUMS -> SHA256SUMS.gpg
        Arch:   sha256sums.txt -> sha256sums.txt.sig
        其他:   返回空(交由配置或跳过)
        """
        for suffix in (".gpg", ".sig"):
            if checksum_url.endswith(suffix):
                return checksum_url  # 已是签名文件
        return checksum_url + ".gpg"

    def _head_content_length(self, url: str) -> int:
        """HEAD 探测目标文件总字节数, 用于 Size 校验兜底。失败返回 0。

        v1.2.9 新增: 服务器不给 Content-Length/chunked 时, 过去会导致大小校验被
        整段跳过, 不完整文件直接去算校验和。这里提供一条独立的长度来源作兜底。
        """
        try:
            r = requests.head(url, headers=self.headers, timeout=15, allow_redirects=True)
            if r.status_code < 400:
                return int(r.headers.get("content-length") or 0)
        except (requests.RequestException, ValueError):  # 探测失败按 0(大小未知)处理
            pass
        return 0

    def verify_checksum_smart(self, filepath: Path, checksum_url: Optional[str],
                             stored_checksum: Optional[str],
                             dist: Optional[dict] = None) -> tuple[Optional[bool], str]:
        """智能校验和验证，按优先级进行。

        返回值是**三态**, 调用方必须区分对待(这是 v1.3.17 的核心修复):

          * ``True``  —— 校验通过
          * ``False`` —— **确认不符**: 拿到了官方摘要且比对不一致, 文件确实损坏
          * ``None``  —— **无法判定**: 校验源不可达(404/超时)或清单里没有该文件名。
                         此时"没验过"不等于"验不过", 绝不能据此删文件/重下 ——
                         否则 Proxmox 那种"checksum 模板配错 → 每周期 416 → 死循环"
                         就是典型后果(生产实测: 完整 ISO 与等大 .part 并存, 周周重下)。

        dist: 发行版条目(dict), 含可选的 gpg_verify/signature_url/gpg_key_url/
              gpg_key_fingerprint 字段。配置了 gpg_verify 时, 先验证 checksum 文件的
              GPG 签名(指纹锚定), 再取其 SHA256 比对。
        """
        filename = filepath.name

        # P3: GPG 预检 - 配置了 gpg 验证且可获取 checksum 文本时, 先验签名
        if dist and dist.get("gpg_verify") and checksum_url:
            print("  尝试 GPG 签名验证 checksum 文件…")
            checksum_text = ""
            try:
                # M1 修复(审查 2026-10): 与 verify_signature 同因 —— 404/5xx 返回的
                # HTML 正文不是 checksum, 不能拿去验签。raise_for_status 让"不可达"
                # 走 except 分支, 保持 checksum_text 为空, 后续由 verify_signature
                # 的空文本闸降级为 skip, 而不是报 fail。
                checksum_resp = requests.get(checksum_url, headers=self.headers, timeout=30)
                checksum_resp.raise_for_status()
                checksum_text = checksum_resp.text
            except Exception as e:  # noqa: BLE001 - 边界: GPG 预检是可选增强, 获取失败一律降级到 SHA256 比对
                print(f"  获取 checksum 文本失败(降级): {e}")
            sig_url = dist.get("signature_url") or self._default_sig_url(checksum_url)
            keyring_dir = self.download_dir / "gpg-keyring"  # 缓存到 data 卷
            gpg_status = self.verify_signature(checksum_text, sig_url,
                                               dist.get("gpg_key_url", ""), keyring_dir,
                                               dist.get("gpg_key_fingerprint"))
            if gpg_status == "pass":
                print("  ✓ GPG 签名验证通过: checksum 文件由官方私钥签名, 可信")
            elif gpg_status == "fail":
                print("  ⚠ GPG 校验未通过: checksum 文件可能被篡改, 或公钥指纹不匹配, 拒绝下载")
                return False, "GPG 校验未通过(签名无效或公钥指纹不匹配, checksum 文件不可信)"
            else:
                print("  - GPG 签名验证跳过(官方无公钥/获取失败), 降级到 SHA256")

        # 第一优先级：从checksum_url获取最新校验和
        if checksum_url:
            print(f"  尝试从URL获取最新校验和: {checksum_url}")
            url_checksum = self.get_checksum_from_url(checksum_url, filename)
            if url_checksum:
                print(f"  从URL获取到校验和: {url_checksum}")
                if self.verify_checksum(filepath, url_checksum):
                    return True, f"URL校验和验证通过: {url_checksum}"
                else:
                    print("  URL校验和验证失败(内容与清单声明不一致)")
                    got = self.sha256_of(filepath)
                    return False, (f"内容校验不符: 期望 {url_checksum}, 实际 {got}"
                                   " —— 文件内容与官方清单不一致(下载损坏或镜像同步中)")
            else:
                # v1.3.17: 这里过去直接 return False, 被上层当成"校验失败 → 重新下载"。
                # 但"取不到官方摘要"和"摘要对不上"是两回事: 前者可能是镜像站 404、
                # checksum 模板配错、文件尚未进清单 —— 本地文件本身也许完好无损。
                # 现返回 None(无法判定), 由调用方决定是"当作完整"还是"换源重试",
                # 不再无条件重下。
                got = self.sha256_of(filepath)
                return None, (f"无法判定: 未能在校验源中找到该文件名, 或校验源不可达"
                              f"(本地实际 SHA256={got}, 来源 {checksum_url})")

        # 第二优先级：使用JSON中存储的checksum
        if stored_checksum:
            print(f"  使用存储的校验和: {stored_checksum}")
            if self.verify_checksum(filepath, stored_checksum):
                return True, f"存储校验和验证通过: {stored_checksum}"
            else:
                got = self.sha256_of(filepath)
                return False, (f"内容校验不符: 期望 {stored_checksum}, 实际 {got}")

        # 第三优先级：两个都没有，跳过验证
        if not checksum_url and not stored_checksum:
            print("  警告: 没有可用的校验和信息，跳过验证")
            return True, "跳过校验和验证（无可用信息）"

        return False, "所有校验和验证都失败"
    
    def cleanup_distribution_dir(self, dist_dir: Path, expected_files: List[str]) -> None:
        """清理发行版目录，删除不在JSON中维护的文件"""
        # 安全校验: dist_dir 必须位于 self.download_dir 内
        try:
            dist_dir.resolve().relative_to(self.download_dir.resolve())
        except ValueError:
            print(f"拒绝清理越界目录: {dist_dir}")
            return
        if not dist_dir.exists():
            return
        
        # 获取目录中的所有文件
        existing_files = [f.name for f in dist_dir.iterdir() if f.is_file()]
        
        # 找出需要删除的文件。半成品(.part / .aria2)一律保留:
        # 它们是正在下载或可续传的数据, 既不是"过时 ISO", 也不能当作完整文件处理。
        #
        # M4 补充: .part.src 是 .part 的**同源证据**, 单独把它删掉, 下一轮就会
        # 退化成"无标记 → 照旧续传", 正好把跨源拼接的洞重新打开。所以它必须跟着
        # .part 一起活; 反过来, .part 已经不在的孤立标记要顺手清掉, 否则永久堆积。
        def _keep(fname: str) -> bool:
            low = fname.lower()
            if low.endswith((".part", ".aria2")):
                return True
            if low.endswith(PART_SRC_SUFFIX):
                return (dist_dir / fname[:-len(PART_SRC_SUFFIX)]).exists()
            return fname in expected_files

        files_to_delete = [f for f in existing_files if not _keep(f)]
        
        if files_to_delete:
            print(f"  清理目录 {dist_dir.name}，删除 {len(files_to_delete)} 个过时文件:")
            for file_name in files_to_delete:
                file_path = dist_dir / file_name
                try:
                    file_path.unlink()
                    print(f"    ✓ 删除: {file_name}")
                except OSError as e:
                    print(f"    ✗ 删除失败 {file_name}: {e}")
        else:
            print(f"  目录 {dist_dir.name} 无需清理")
    
    # ------------------------------------------------------------------ 多源候选(M5)
    def candidate_sources(self, dist: dict) -> list:
        """把清单条目展开成有序候选源 [(url, checksum_url), ...]。

        Web 侧(_pick_candidates)一直有多源 failover, CLI 这边过去只认
        download_url 一个 URL —— 主源挂掉 CLI 直接跳过, 没有任何兜底。同一个
        清单在两个入口行为不同, 本身就是缺陷。这里对齐:
          * download_urls 的顺序就是优先级(与 Web 策略 A 一致);
          * pin 指定的源置顶(用户在 UI 上手动选过源);
          * 没有 download_urls 的旧清单退化成单源, 行为与修复前完全一致。
        """
        urls = list(dist.get("download_urls") or [])
        if not urls:
            urls = [dist["download_url"]]
        css = list(dist.get("checksum_urls") or [])
        css += [None] * (len(urls) - len(css))
        pin = (dist.get("pin") or "").strip()
        if pin and pin in urls:
            i = urls.index(pin)
            urls = [urls[i]] + urls[:i] + urls[i + 1:]
            css = [css[i]] + css[:i] + css[i + 1:]
        return list(zip(urls, css))

    # ------------------------------------------------------------- 未验签账本(M5)
    def _unverified_path(self) -> Path:
        return Path(self.download_dir) / "download_unverified.json"

    @staticmethod
    def _ledger_lock(path):
        """复用 web/config_files.py 的跨进程锁; CLI 独立运行时退化为空锁。

        download_linux.py 是**可以单独运行**的上游脚本(不依赖 web/), 所以不能
        在模块级 import config_files。容器里由 app 侧拉起时 web/ 在 sys.path 上,
        这时就用同一把锁, 与 iso_runner / sync_subscriptions 互斥。
        """
        try:
            import config_files  # noqa: PLC0415 - 可选依赖, 见 docstring
        except ImportError:
            return contextlib.nullcontext()
        return config_files.cross_process_lock(path)

    def _record_unverified(self, dist: dict, fname: str, reason: str,
                           state: str = "unverified") -> None:
        """落一条「未验签」记录。

        Web 侧早就在落(iso_runner._record_unverified), CLI 侧过去只打印一行 ⚠
        就当成功了 —— 同一个文件从 CLI 下和从 Web 下, UI 上一个带徽标一个不带。
        键格式与 Web 侧完全一致: "<type>/<distribution>/<fname>"。
        """
        key = "%s/%s/%s" % (dist.get("type", "linux"), dist.get("distribution", ""), fname)
        jf = self._unverified_path()
        try:
            with self._ledger_lock(jf):
                data = {}
                if jf.exists():
                    try:
                        data = json.loads(jf.read_text(encoding="utf-8")) or {}
                    except (OSError, ValueError):
                        data = {}
                data[key] = {
                    "at": int(time.time()),
                    "reason": (reason or "")[:300],
                    "state": state if state in ("unverified", "mismatch") else "unverified",
                }
                tmp = jf.with_name(jf.name + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(jf)
        except (OSError, TypeError, ValueError):  # 记账失败不影响落定本身
            pass

    def _clear_unverified(self, dist: dict, fname: str) -> None:
        """真正校验通过后撤掉「未验签」标记 —— 否则徽标会永远挂着。"""
        key = "%s/%s/%s" % (dist.get("type", "linux"), dist.get("distribution", ""), fname)
        jf = self._unverified_path()
        try:
            with self._ledger_lock(jf):
                if not jf.exists():
                    return
                data = json.loads(jf.read_text(encoding="utf-8")) or {}
                if key in data:
                    data.pop(key, None)
                    tmp = jf.with_name(jf.name + ".tmp")
                    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    tmp.replace(jf)
        except (OSError, TypeError, ValueError):
            pass

    # ------------------------------------------------------------------ 主流程
    def _already_have(self, filepath: Path, target_dist: dict,
                      verify_checksum: bool) -> bool:
        """文件已存在时的处置。返回 True 表示"这个版本不用再下"。

        三种结局与修复前一致: 校验通过 → 保留; 校验源不可达 → 保留(不重下);
        明确不符 → 返回 False 让它重下。额外补两件事(M5②):
        校验通过时撤掉「未验签」标记; 验不了时**落一条**未验签记录。
        """
        print(f"文件已存在: {filepath}")
        if not verify_checksum:
            return False
        success, message = self.verify_checksum_smart(
            filepath,
            target_dist.get("checksum_url"),
            target_dist.get("checksum"),
            dist=target_dist
        )
        if success:
            print(f"✓ {message}")
            # 完整文件已校验通过 -> 同名的 .part 是上一轮中断留下的冗余字节。
            # 不清理会让 UI 长期误报「下载停止(已下载部分)」: 后端 build_distros()
            # 在同名的完整文件与 .part 之间取 mtime 较新者作条目代表, 残留
            # .part 通常更新, 于是"已完整"的文件被判成半成品。
            stale_part = filepath.with_name(filepath.name + PART_SUFFIX)
            try:
                if stale_part.exists():
                    stale_part.unlink()
                    print(f"  清理冗余半成品: {stale_part}")
                forget_part_src(stale_part)
            except OSError as e:  # noqa: BLE001
                print(f"  清理冗余半成品失败(忽略): {e}")
            self._clear_unverified(target_dist, filepath.name)
            return True
        if success is None:
            # v1.3.17: 校验源不可达/清单里没这个文件名 —— "没验过"不等于"验不过"。
            # 旧代码在这里也走 else 分支 → 打印"校验和验证失败，将重新下载",
            # 于是每个周期都把完好的 ISO 重下一遍(Proxmox 就是这么循环了 10 天)。
            # 现在: 保留现有文件不动, 明确告警, 不触发重下。
            print(f"⚠ {message}")
            print("  完整性无法判定(校验源不可用), 保留现有文件, 不重新下载")
            self._record_unverified(target_dist, filepath.name, message)
            return True
        print(f"✗ {message}")
        print("校验和验证失败，将重新下载")
        return False

    def _download_from_url(self, target_dist: dict, url: str, checksum_url,
                           dist_dir: Path, filepath: Path, filename: str,
                           verify_checksum: bool) -> str:
        """从**一个** URL 下载到 .part, 校验通过后原子落定。

        返回:
          "ok"    —— 落定成功
          "next"  —— 这个源不行, 换下一个候选源
          "fatal" —— 与源无关的失败(磁盘空间不足), 换源也没用
        """
        name = target_dist.get("distribution", "")
        # 下载期间写 <最终名>.part, 全部校验通过后才原子改名(见下方 os.replace)。
        # 这样任务被「停止」kill 或网络中断时, 磁盘上留下的是 .part 半成品,
        # 后端能识别为「下载停止」, 而不会被误判为已下载的完整 ISO。
        part_path = filepath.with_name(filepath.name + PART_SUFFIX)

        # M4: 半成品只能续**同一个源**的字节, 换源必须从头下
        prev_src = read_part_src(part_path)
        if part_path.exists() and prev_src and prev_src != url:
            print(f"  ⚠ 半成品来自另一个源, 不跨源续传, 从头下载: {prev_src}")
            discard_part(part_path)

        print(f"开始下载 {name}: {filename}")
        print(f"下载链接: {url}")

        try:
            # 断点续传: 若已有 .part, 带 Range 头请求剩余部分
            _have = part_path.stat().st_size if part_path.exists() else 0
            _headers = dict(self.headers or {})
            if _have:
                _headers["Range"] = f"bytes={_have}-"

            # B6 修复: 显式超时(连接 15s, 读 60s), 避免镜像站半开连接时下载线程永久挂起
            # v1.3.17: 再套一层退避重试, 覆盖瞬时抖动与 429/5xx(不重试 416)
            response = http_get_with_retry(url, headers=_headers,
                                           stream=True, timeout=(15, 60))
            # v1.3.17: 416 = Range 起点越界, 说明 .part 字节数已 >= 远端文件全长。
            # 旧实现把它归进 else 分支(同 200 处理) → 从头重下整个文件, 下完 .part
            # 又是全量 → 下一轮再 416 → 无限循环(Proxmox 实测每天一轮, 10 天未自愈)。
            # 正确语义: .part 已是**完整候选**, 跳过传输直接进校验。
            _already_complete = bool(_have and response.status_code == 416)
            if _already_complete:
                print(f"断点已满({_have/1024/1024:.1f} MiB): 服务器返回 416, "
                      f"{part_path.name} 视为完整候选, 直接进入校验")
                response.close()
                total_size = _have
            # 服务器支持 Range 时返回 206, 此时应追加写入
            elif _have and response.status_code == 206:
                _cr = response.headers.get('content-range', '')
                try:
                    total_size = int(_cr.rsplit('/', 1)[-1]) if '/' in _cr else 0
                except (TypeError, ValueError):
                    total_size = 0
                if not total_size:
                    try:
                        total_size = _have + int(response.headers.get('content-length') or 0)
                    except (TypeError, ValueError):
                        total_size = 0
                print(f"续传: 从 {_have/1024/1024:.1f} MiB 继续")
                _mode = 'ab'
            else:
                if _have and response.status_code == 200:
                    print(f"服务器不支持断点续传, 从头下载(丢弃 {_have/1024/1024:.1f} MiB)")
                _have = 0
                _mode = 'wb'
                # 风险6修复: 镜像站可能返回非法/缺失 content-length, 解析失败按 0 处理(不定长模式)
                try:
                    total_size = int(response.headers.get('content-length') or 0)
                except (TypeError, ValueError):
                    total_size = 0
            if not _already_complete:
                # 416 是"已完成"信号而非错误, 上面已 close, 不能再 raise_for_status
                response.raise_for_status()

            if not _already_complete:
                # v1.5.2: 写盘前确认放得下, 不足则整条跳过(不产生 .part)。
                # 416 时字节已经全在盘上, 无需再判。GET 没给 Content-Length 时
                # 用 HEAD 兜底再判一次(判不了才放行, 与大小校验同一套兜底思路)。
                if not total_size:
                    total_size = self._head_content_length(url)
                _ok_space, _why_space = check_free_space(
                    dist_dir, max(0, total_size - _have))
                if not _ok_space:
                    response.close()
                    print(f"\n✗ 下载中止: {_why_space}")
                    print("  清出空间后重跑即可续传(.part 未被删除)")
                    # 空间不足与源无关: 换下一个镜像照样写不下, 只会把同一个错误重试 N 遍
                    return "fatal"
                if _why_space:
                    print(f"⚠ {_why_space}")

            # 使用tqdm创建进度条(416 时跳过传输, 直接落校验)
            if not _already_complete:
                write_part_src(part_path, url)   # 先记来源, 再落第一个字节
                with open(part_path, _mode) as f:
                    with tqdm(
                        total=total_size,
                        initial=_have,
                        unit='B',
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=f"下载 {filename}",
                        bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]'
                    ) as pbar:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                pbar.update(len(chunk))

            print(f"\n下载完成: {part_path}")

            # 大小校验: 声明了 content-length 就必须一致(截断的流不算完成)。
            # v1.2.9: total_size==0 时过去会**整段跳过**校验, 让不完整文件直接进入
            # 校验和比对 → 必然失败 → 日志表现为"没下载完就开始校验"。现补 HEAD 兜底:
            # 仍拿不到就明确告警"无法核对完整性", 不再静默放行。
            actual_bytes = part_path.stat().st_size
            if not total_size:
                total_size = self._head_content_length(url)
            if total_size and actual_bytes != total_size:
                raise Exception(
                    f"大小不匹配: 期望 {total_size}B, 实际 {actual_bytes}B"
                )
            if not total_size:
                print("⚠ 服务器未提供文件总大小, 无法核对完整性, 直接交由校验和判定")

            if not verify_checksum:
                if filepath.exists():
                    filepath.unlink()
                os.replace(part_path, filepath)
                forget_part_src(part_path)
                return "ok"

            # 智能校验和验证(对 .part 校验, 通过后才改名)
            success, message = self.verify_checksum_smart(
                part_path,
                checksum_url or target_dist.get("checksum_url"),
                target_dist.get("checksum"),
                dist=target_dist
            )
            if success is None:
                # v1.3.17: 校验源不可达/清单无此文件名。长度已与服务器声明一致,
                # 只是拿不到官方摘要 —— 若此时不落定, .part 会一直躺在磁盘上,
                # UI 永远显示「下载停止」(正是本次要根治的症状)。故按"尽最大努力
                # 验证"落定, 但留醒目告警, 便于事后发现源配置问题。
                print(f"⚠ {message}")
                if _already_complete and prev_src != url:
                    # L4: 416 把 .part 直接当完整文件, 但同源性无法证明(旧版本
                    # 留下的半成品没有来源标记)。有校验和时它能证明内容对不对;
                    # 而这里恰恰是"验不了"的分支 —— 再落定成"未验签", 等于把来源
                    # 不明的内容标成"只是没验", 用户会当它是好的。丢弃重下。
                    print("  416 判定为完整, 但半成品无法确认与当前源同源, "
                          "且无校验和可兜底 → 丢弃重下")
                    discard_part(part_path)
                    return "next"
                if total_size and actual_bytes == total_size:
                    print("  长度已与服务器声明一致, 按无法校验处理并落定(请检查 checksum 源配置)")
                    if filepath.exists():
                        filepath.unlink()
                    os.replace(part_path, filepath)
                    forget_part_src(part_path)
                    # M5②: 与 Web 侧一致地落一条「未验签」, 让 UI 能标出来
                    self._record_unverified(target_dist, filename, message)
                    return "ok"
                print("  长度未知或不匹配, 保守保留半成品供下次续传")
                return "next"

            if success:
                print(f"✓ {message}")
                # 校验通过 → 原子改名为最终文件名, 此刻才算真正下载完成
                if filepath.exists():
                    filepath.unlink()
                os.replace(part_path, filepath)
                forget_part_src(part_path)
                self._clear_unverified(target_dist, filename)
                return "ok"

            print(f"✗ {message}")
            # 尺寸与预期一致却校验不过 → 内容确实损坏。必须丢弃: 留着的话下次
            # Range 起点正好等于全长 → 416 → 又被当完整候选 → 永远验不过的死循环。
            # (与 iso_runner 的同名分支保持一致)
            if total_size:
                discard_part(part_path)
                print("  半成品尺寸正确但内容校验不符, 已丢弃(避免坏数据被续传)")
            return "next"

        # 风险5修复: 除网络错误外, 也捕获磁盘/IO 错误(磁盘满/权限不足/文件被占用),
        # 避免下载中断且不清理不完整文件。
        # 半成品保留为 .part(不删): 供后端识别为「下载停止」并支持续传。
        except (requests.exceptions.RequestException, OSError, IOError) as e:
            print(f"\n下载失败: {e}")
            return "next"

    def download_distribution(self, name: str, verify_checksum: bool = True) -> bool:
        """下载指定的发行版(逐个候选源 failover, 见 candidate_sources)。"""
        # 查找匹配的发行版
        matching_dists = []
        for dist in self.distributions["distributions"]:
            if dist["distribution"].lower() == name.lower():
                matching_dists.append(dist)

        if not matching_dists:
            print(f"错误: 找不到匹配的发行版 {name}")
            return False

        print(f"找到 {len(matching_dists)} 个 {name} 发行版，开始下载所有版本...")

        # 准备清理：收集所有应该存在的文件名
        expected_files = []
        for dist in matching_dists:
            filename = os.path.basename(urlparse(dist["download_url"]).path)
            expected_files.append(filename)

        # 下载所有匹配的版本
        success_count = 0
        for i, target_dist in enumerate(matching_dists, 1):
            print(f"\n{'='*60}")
            print(f"下载第 {i}/{len(matching_dists)} 个版本:")

            # 创建下载目录: classified 用 type/distribution, flat 用统一单目录
            dist_dir = _safe_dist_dir(self.download_dir, target_dist.get("type", "linux"), target_dist.get("distribution", ""), flat=_flat_of(self))
            if dist_dir is None:
                print(f"错误: 发行版 {target_dist} 的 type/distribution 不合法, 跳过")
                continue
            dist_dir.mkdir(parents=True, exist_ok=True)

            # 获取文件名: 一律以清单里的主 URL 为准, 保证同名文件在不同镜像下
            # 落到同一个路径(候选源里的文件名可能不同, 不能跟着它变)。
            filename = os.path.basename(urlparse(target_dist["download_url"]).path)
            filepath = dist_dir / filename

            # 文件是否已存在与源无关, 只判一次
            if filepath.exists() and self._already_have(filepath, target_dist, verify_checksum):
                success_count += 1
                continue

            candidates = self.candidate_sources(target_dist)
            if len(candidates) > 1:
                print(f"共 {len(candidates)} 个候选源, 失败自动换源")
            ok = False
            for ci, (url, cs_url) in enumerate(candidates, 1):
                if len(candidates) > 1:
                    print(f"\n候选源 {ci}/{len(candidates)}: {url}")
                status = self._download_from_url(target_dist, url, cs_url, dist_dir,
                                                 filepath, filename, verify_checksum)
                if status == "ok":
                    ok = True
                    break
                if status == "fatal":
                    break
                if ci < len(candidates):
                    print("  → 尝试下一个候选源")
            if ok:
                success_count += 1
            elif len(candidates) > 1:
                print(f"  ✗ {filename} 的所有候选源均失败")

        # 清理发行版目录，删除不在JSON中维护的文件
        if matching_dists:
            dist_dir = _safe_dist_dir(self.download_dir, matching_dists[0].get("type", "linux"), matching_dists[0].get("distribution", ""), flat=_flat_of(self))
            if dist_dir is not None:
                self.cleanup_distribution_dir(dist_dir, expected_files)

        print(f"\n{'='*60}")
        print(f"下载完成！成功下载 {success_count}/{len(matching_dists)} 个版本")
        return success_count > 0

    def sha256_of(self, filepath: Path) -> str:
        """计算文件的 SHA256(小写十六进制)。供校验与诊断日志复用。"""
        sha256_hash = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()

    def verify_checksum(self, filepath: Path, expected_checksum: str) -> bool:
        """验证文件的SHA256校验和。

        v1.3.1: 两侧统一转小写并去空白后比较。旧实现直接 `==`, 一旦清单里
        是大写摘要、或带首尾空白, 就会被误判成"内容不符"。
        """
        try:
            actual_checksum = self.sha256_of(filepath)
            return actual_checksum == (expected_checksum or "").strip().lower()
        except OSError as e:  # 文件读不出按校验不通过处理
            print(f"校验和验证错误: {e}")
            return False

    def download_all(self, verify_checksum: bool = True) -> None:
        """下载所有发行版"""
        print("开始下载所有发行版...")
        
        # 按发行版名称分组
        dist_groups = {}
        for dist in self.distributions["distributions"]:
            dist_name = dist["distribution"]
            if dist_name not in dist_groups:
                dist_groups[dist_name] = []
            dist_groups[dist_name].append(dist)
        
        # 按分组下载，每个发行版只调用一次download_distribution
        for dist_name, dists in dist_groups.items():
            print(f"\n{'='*60}")
            # 风险8修复: 单个发行版下载异常不中断整批, 隔离后继续后续
            try:
                success = self.download_distribution(
                    dist_name, verify_checksum
                )
                if not success:
                    print(f"下载失败: {dist_name}")
            except Exception as e:  # noqa: BLE001 - 边界: 单个发行版的未预期异常不中断整批(风险8)
                print(f"下载 {dist_name} 异常: {e}")
            time.sleep(2)  # 避免请求过于频繁
        
        print(f"\n{'='*60}")
        print("所有下载任务完成")


def main():
    parser = argparse.ArgumentParser(description="Linux发行版下载器")
    parser.add_argument("--list", "-l", action="store_true", help="列出所有发行版")
    parser.add_argument("--download", "-d", nargs=1, metavar=("NAME"),
                       help="下载指定的发行版")
    parser.add_argument("--download-all", "-a", action="store_true", help="下载所有发行版")
    parser.add_argument("--filter-name", help="按名称过滤")
    parser.add_argument("--filter-type", help="按类型过滤 (linux, windows, macos)")
    parser.add_argument("--no-verify", action="store_true", help="跳过校验和验证")
    parser.add_argument("--json-file", default="distributions.json", help="指定JSON文件路径")
    parser.add_argument("--download-dir", help="指定下载目录")
    
    args = parser.parse_args()
    
    # 创建下载器实例
    downloader = LinuxDistributionDownloader(args.json_file, args.download_dir)
    
    # 检查是否有明确的动作参数
    has_explicit_action = args.list or args.download or args.download_all
    
    if args.list:
        downloader.list_distributions(args.filter_name, args.filter_type)
    elif args.download:
        name = args.download[0]
        success = downloader.download_distribution(
            name, verify_checksum=not args.no_verify
        )
        if success:
            print("下载成功!")
        else:
            print("下载失败!")
            sys.exit(1)
    elif args.download_all or not has_explicit_action:
        # 如果指定了--download-all或者没有传任何明确的动作参数，都执行下载所有
        downloader.download_all(verify_checksum=not args.no_verify)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
