#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Linux发行版下载器
用于下载、更新和验证各种Linux发行版的ISO文件
"""

import json
import os
import sys
import hashlib
import subprocess
import requests
import argparse
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse
import time
from tqdm import tqdm


ALLOWED_TYPES = {"linux", "bsd", "windows", "macos"}


def _safe_dist_dir(download_dir: Path, typ: str, name: str) -> Path | None:
    """把 (type, name) 安全拼接为 download_dir 下的路径, 拒绝路径穿越/非法字符。"""
    if not typ or not name or typ not in ALLOWED_TYPES:
        return None
    for comp in (typ, name):
        comp = str(comp)
        if comp != comp.strip() or comp in (".", ".."):
            return None
        if "/" in comp or "\\" in comp:
            return None
    target = (download_dir / typ / name).resolve()
    try:
        target.relative_to(download_dir.resolve())
    except ValueError:
        return None
    return target


class LinuxDistributionDownloader:
    def __init__(self, json_file: str = "distributions.json", download_dir: Optional[str] = None):
        """初始化下载器"""
        self.json_file = json_file

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
        """从校验和URL获取指定文件的校验和"""
        try:
            response = requests.get(checksum_url, headers=self.headers, timeout=30)
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
            
            # 查找对应的校验和
            for line in content_to_parse.split('\n'):
                if filename in line:
                    # 处理标准格式: checksum filename
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        potential_checksum = parts[0]
                        # 验证是否为有效的SHA256校验和（64位十六进制）
                        if len(potential_checksum) == 64 and all(c in '0123456789abcdefABCDEF' for c in potential_checksum):
                            return potential_checksum.lower()
                    
                    # 处理PGP签名格式: SHA256 (filename) = checksum
                    if 'SHA256' in line and filename in line and '=' in line:
                        # 提取等号后面的校验和
                        checksum_part = line.split('=')[1].strip()
                        if len(checksum_part) == 64 and all(c in '0123456789abcdefABCDEF' for c in checksum_part):
                            return checksum_part.lower()
            return None
            
        except Exception as e:
            print(f"  从URL获取校验和失败: {e}")
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

        用 gpg --show-keys 直接读文件: 不依赖 GNUPGHOME, 也不会改动用户 keyring。
        """
        try:
            if not keyring.exists() or keyring.stat().st_size == 0:
                return []
        except OSError:
            return []
        r = subprocess.run(
            ["gpg", "--batch", "--no-tty", "--with-colons", "--show-keys", str(keyring)],
            capture_output=True, text=True, errors="replace")
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
        except Exception as e:  # noqa: BLE001
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
            sig = requests.get(sig_url, timeout=30).content
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
        except Exception as e:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            pass
        return 0

    def verify_checksum_smart(self, filepath: Path, checksum_url: Optional[str],
                             stored_checksum: Optional[str],
                             dist: Optional[dict] = None) -> tuple[bool, str]:
        """智能校验和验证，按优先级进行。

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
                checksum_text = requests.get(checksum_url, headers=self.headers, timeout=30).text
            except Exception as e:  # noqa: BLE001
                print(f"  获取 checksum 文本失败: {e}")
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
                    print("  URL校验和验证失败")
        
        # 第二优先级：使用JSON中存储的checksum
        if stored_checksum:
            print(f"  使用存储的校验和: {stored_checksum}")
            if self.verify_checksum(filepath, stored_checksum):
                return True, f"存储校验和验证通过: {stored_checksum}"
            else:
                print("  存储校验和验证失败")
        
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
        files_to_delete = [
            f for f in existing_files
            if f not in expected_files and not f.lower().endswith((".part", ".aria2"))
        ]
        
        if files_to_delete:
            print(f"  清理目录 {dist_dir.name}，删除 {len(files_to_delete)} 个过时文件:")
            for file_name in files_to_delete:
                file_path = dist_dir / file_name
                try:
                    file_path.unlink()
                    print(f"    ✓ 删除: {file_name}")
                except Exception as e:
                    print(f"    ✗ 删除失败 {file_name}: {e}")
        else:
            print(f"  目录 {dist_dir.name} 无需清理")
    
    def download_distribution(self, name: str, verify_checksum: bool = True) -> bool:
        """下载指定的发行版"""
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
            
            # 创建下载目录，使用 type/distribution 格式
            dist_dir = _safe_dist_dir(self.download_dir, target_dist.get("type", "linux"), target_dist.get("distribution", ""))
            if dist_dir is None:
                print(f"错误: 发行版 {target_dist} 的 type/distribution 不合法, 跳过")
                continue
            dist_dir.mkdir(parents=True, exist_ok=True)
            
            # 获取文件名
            filename = os.path.basename(urlparse(target_dist["download_url"]).path)
            
            filepath = dist_dir / filename
            
            # 检查文件是否已存在
            if filepath.exists():
                print(f"文件已存在: {filepath}")
                if verify_checksum:
                    success, message = self.verify_checksum_smart(
                        filepath, 
                        target_dist.get("checksum_url"), 
                        target_dist.get("checksum"),
                        dist=target_dist
                    )
                    if success:
                        print(f"✓ {message}")
                        success_count += 1
                        continue
                    else:
                        print(f"✗ {message}")
                        print("校验和验证失败，将重新下载")
            
            # 开始下载
            print(f"开始下载 {name}: {filename}")
            print(f"下载链接: {target_dist['download_url']}")
            
            try:
                # 下载期间写 <最终名>.part, 全部校验通过后才原子改名(见下方 os.replace)。
                # 这样任务被「停止」kill 或网络中断时, 磁盘上留下的是 .part 半成品,
                # 后端能识别为「下载停止」, 而不会被误判为已下载的完整 ISO。
                part_path = filepath.with_name(filepath.name + ".part")

                # 断点续传: 若已有 .part, 带 Range 头请求剩余部分
                _have = part_path.stat().st_size if part_path.exists() else 0
                _headers = dict(self.headers or {})
                if _have:
                    _headers["Range"] = f"bytes={_have}-"

                # B6 修复: 显式超时(连接 15s, 读 60s), 避免镜像站半开连接时下载线程永久挂起
                response = requests.get(target_dist["download_url"], headers=_headers,
                                        stream=True, timeout=(15, 60))
                # 服务器支持 Range 时返回 206, 此时应追加写入
                if _have and response.status_code == 206:
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
                response.raise_for_status()

                # 使用tqdm创建进度条
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
                    total_size = self._head_content_length(target_dist["download_url"])
                if total_size and actual_bytes != total_size:
                    raise Exception(
                        f"大小不匹配: 期望 {total_size}B, 实际 {actual_bytes}B"
                    )
                if not total_size:
                    print("⚠ 服务器未提供文件总大小, 无法核对完整性, 直接交由校验和判定")

                # 智能校验和验证(对 .part 校验, 通过后才改名)
                if verify_checksum:
                    success, message = self.verify_checksum_smart(
                        part_path, 
                        target_dist.get("checksum_url"), 
                        target_dist.get("checksum"),
                        dist=target_dist
                    )
                    if success:
                        print(f"✓ {message}")
                        # 校验通过 → 原子改名为最终文件名, 此刻才算真正下载完成
                        if filepath.exists():
                            filepath.unlink()
                        os.replace(part_path, filepath)
                        success_count += 1
                    else:
                        print(f"✗ {message}")
                else:
                    if filepath.exists():
                        filepath.unlink()
                    os.replace(part_path, filepath)
                    success_count += 1
                
            # 风险5修复: 除网络错误外, 也捕获磁盘/IO 错误(磁盘满/权限不足/文件被占用),
            # 避免下载中断且不清理不完整文件。
            # 半成品保留为 .part(不删): 供后端识别为「下载停止」并支持续传。
            except (requests.exceptions.RequestException, OSError, IOError) as e:
                print(f"\n下载失败: {e}")
        
        # 清理发行版目录，删除不在JSON中维护的文件
        if matching_dists:
            dist_dir = _safe_dist_dir(self.download_dir, matching_dists[0].get("type", "linux"), matching_dists[0].get("distribution", ""))
            if dist_dir is not None:
                self.cleanup_distribution_dir(dist_dir, expected_files)
        
        print(f"\n{'='*60}")
        print(f"下载完成！成功下载 {success_count}/{len(matching_dists)} 个版本")
        return success_count > 0
    
    def verify_checksum(self, filepath: Path, expected_checksum: str) -> bool:
        """验证文件的SHA256校验和"""
        try:
            sha256_hash = hashlib.sha256()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(chunk)
            
            actual_checksum = sha256_hash.hexdigest()
            return actual_checksum == expected_checksum
        except Exception as e:
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
            except Exception as e:  # noqa: BLE001
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
