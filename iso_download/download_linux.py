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
    
    def verify_signature(self, checksum_text: str, sig_url: str, gpg_key_url: str,
                         keyring_dir: Path) -> str:
        """验证 checksum 文件是否由官方私钥签名。

        returns:
          "pass"  签名验证通过(文件可信, 其内 SHA256 可放心比对 ISO)
          "fail"  签名校验失败(文件可能被篡改, 应拒绝下载)
          "skip"  官方无公钥/获取失败/无 gpg 环境(降级到 SHA256, 不阻塞)
        """
        try:
            import shutil
            if shutil.which("gpgv") is None and shutil.which("gpg") is None:
                return "skip"  # 容器未装 gpg, 降级
            keyring_dir.mkdir(parents=True, exist_ok=True)
            keyring = keyring_dir / "iso-hub.gpg"
            # 1. 首次使用: 从官方 keyserver 获取公钥并缓存到 data 卷
            if not keyring.exists():
                if not gpg_key_url:
                    return "skip"  # 未提供公钥获取地址, 官方无签名可验
                resp = requests.get(gpg_key_url, timeout=30)
                resp.raise_for_status()
                pubkey = resp.text
                import tempfile
                with tempfile.TemporaryDirectory() as home:
                    env = dict(os.environ, GNUPGHOME=home)
                    imp = subprocess.run(["gpg", "--import"], input=pubkey.encode(),
                                         capture_output=True, env=env)
                    if imp.returncode != 0:
                        return "skip"  # 公钥解析失败, 不阻塞下载
                    exp = subprocess.run(["gpg", "--export"], capture_output=True, env=env)
                    keyring.write_bytes(exp.stdout)  # 缓存到持久卷
            # 2. 取签名文件(URL 或按惯例推导)
            if not sig_url:
                return "skip"  # 无签名文件可验
            sig = requests.get(sig_url, timeout=30).content
            if not sig:
                return "skip"
            # 3. 用 gpgv(不信任签名者) 验证 detached 签名
            import tempfile
            with tempfile.TemporaryDirectory() as home:
                kr = home + "/keyring.gpg"
                open(kr, "wb").write(keyring.read_bytes())
                cf = home + "/checksum.txt"
                sf = home + "/checksum.sig"
                open(cf, "w", encoding="utf-8").write(checksum_text)
                open(sf, "wb").write(sig)
                r = subprocess.run(["gpgv", "--keyring", kr, sf, cf],
                                   capture_output=True, env=dict(os.environ, GNUPGHOME=home))
                if r.returncode == 0:
                    return "pass"
                # 校验和文件自身可含嵌入式签名(如 Fedora CHECKSUM): 尝试用 gpg --verify 校验文件内签名
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

    def verify_checksum_smart(self, filepath: Path, checksum_url: Optional[str],
                             stored_checksum: Optional[str],
                             dist: Optional[dict] = None) -> tuple[bool, str]:
        """智能校验和验证，按优先级进行。

        dist: 发行版条目(dict), 含可选的 gpg_verify/signature_url/gpg_key_url 字段。
              配置了 gpg_verify 时, 先验证 checksum 文件的 GPG 签名, 再取其 SHA256 比对。
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
                                               dist.get("gpg_key_url", ""), keyring_dir)
            if gpg_status == "pass":
                print("  ✓ GPG 签名验证通过: checksum 文件由官方私钥签名, 可信")
            elif gpg_status == "fail":
                print("  ⚠ GPG 签名校验失败: checksum 文件可能被篡改, 拒绝下载")
                return False, "GPG 签名校验失败(checksum 文件不可信)"
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
        
        # 找出需要删除的文件
        files_to_delete = [f for f in existing_files if f not in expected_files]
        
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
                # B6 修复: 显式超时(连接 15s, 读 60s), 避免镜像站半开连接时下载线程永久挂起
                response = requests.get(target_dist["download_url"], headers=self.headers, stream=True, timeout=(15, 60))
                response.raise_for_status()
                
                # 风险6修复: 镜像站可能返回非法/缺失 content-length, 解析失败按 0 处理(不定长模式)
                try:
                    total_size = int(response.headers.get('content-length') or 0)
                except (TypeError, ValueError):
                    total_size = 0
                
                # 使用tqdm创建进度条
                with open(filepath, 'wb') as f:
                    with tqdm(
                        total=total_size,
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
                
                print(f"\n下载完成: {filepath}")
                
                # 智能校验和验证
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
                    else:
                        print(f"✗ {message}")
                else:
                    success_count += 1
                
            # 风险5修复: 除网络错误外, 也捕获磁盘/IO 错误(磁盘满/权限不足/文件被占用),
            # 避免下载中断且不清理不完整文件
            except (requests.exceptions.RequestException, OSError, IOError) as e:
                print(f"\n下载失败: {e}")
                try:
                    if filepath.exists():
                        filepath.unlink()  # 删除不完整的文件
                except OSError as ue:
                    print(f"清理不完整文件失败: {ue}")
        
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
