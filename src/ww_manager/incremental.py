# incremental.py
"""
增量更新模块：处理 krpdiff 增量包的下载和应用

使用方式与 predownload 类似：
1. ww incremental     - 提前下载增量包（仅在新版本预下载开放期间可用）
2. ww incremental --apply  - 应用已下载的增量包

支持 cn、global、bilibili 三个服务器
"""

import gzip
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("WW_Manager")

HPATCHZ_REPO = "timetetng/wutheringwaves-cli-manager"
HPATCHZ_VERSION = "hpatchz-v4.8.0"
HPATCHZ_FILENAME = "hpatchz.exe"


class IncrementalError(Exception):
    """增量更新相关错误"""

    pass


def check_wine_available() -> bool:
    """检查 wine 是否可用"""
    if platform.system() == "Windows":
        return True
    return shutil.which("wine") is not None


def check_hpatchz_requirements() -> Tuple[bool, str]:
    """检查增量更新所需的环境是否满足"""
    if platform.system() != "Windows":
        wine_path = shutil.which("wine")
        if not wine_path:
            return False, (
                "增量更新需要 wine 环境。\n"
                "请安装 wine: sudo apt install wine (Ubuntu/Debian) 或 sudo pacman -S wine (Arch)\n"
                "或者使用 'ww sync' 进行全量同步。"
            )
        logger.info(f"使用 wine: {wine_path}")
    return True, ""


def get_hpatchz_path(cache_dir: Path) -> Path:
    """获取或下载 hpatchz.exe"""
    hpatchz_path = cache_dir / "hpatchz.exe"

    if hpatchz_path.exists():
        logger.debug(f"使用缓存的 hpatchz.exe: {hpatchz_path}")
        return hpatchz_path

    logger.info("正在下载 hpatchz.exe...")

    download_url = f"https://github.com/{HPATCHZ_REPO}/releases/download/{HPATCHZ_VERSION}/{HPATCHZ_FILENAME}"

    try:
        response = requests.get(download_url, timeout=30, stream=True)
        response.raise_for_status()

        hpatchz_path.parent.mkdir(parents=True, exist_ok=True)
        with open(hpatchz_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        os.chmod(hpatchz_path, 0o755)
        logger.info(f"hpatchz.exe 已下载到: {hpatchz_path}")
        return hpatchz_path

    except Exception as e:
        if hpatchz_path.exists():
            hpatchz_path.unlink()
        raise IncrementalError(f"下载 hpatchz.exe 失败: {e}")


def _http_get_json(url: str) -> Optional[Any]:
    """下载并解析 JSON"""
    try:
        headers = {"User-Agent": "WW-Manager/2.0", "Accept-Encoding": "gzip"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.content
        if "gzip" in response.headers.get("Content-Encoding", "").lower():
            data = gzip.decompress(data)

        return json.loads(data)
    except Exception as e:
        logger.error(f"HTTP 请求失败 {url}: {e}")
        return None


def _calculate_file_md5(file_path: Path) -> str:
    """计算文件 MD5"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096 * 1024), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


class IncrementalManager:
    """统一的增量更新管理器，支持 cn、global、bilibili"""

    def __init__(self, game_folder: Path, server_type: str, api_data: Dict, cdn_base: str):
        self.game_folder = game_folder
        self.server_type = server_type
        self.cdn_base = cdn_base.rstrip("/")
        self.api_data = api_data
        self.cache_dir = game_folder / ".ww_manager_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.hpatchz_path: Optional[Path] = None
        self._index_file: Optional[Dict] = None

    @property
    def predownload_config(self) -> Dict:
        """获取预下载配置"""
        return self.api_data.get("predownload", {}).get("config", {})

    @property
    def index_file(self) -> Optional[Dict]:
        """获取 indexFile.json（懒加载）"""
        if self._index_file is None:
            self._index_file = self._load_index_file()
        return self._index_file

    def _load_index_file(self) -> Optional[Dict]:
        """加载 indexFile.json"""
        patch_info = self._find_matching_patch()
        if not patch_info:
            return None

        index_url = self._build_index_url(patch_info)
        if not index_url:
            return None

        return _http_get_json(index_url)

    def _find_matching_patch(self) -> Optional[Dict]:
        """从 patchConfig 中找到匹配当前版本的增量配置"""
        patch_configs = self.predownload_config.get("patchConfig", [])
        if not patch_configs:
            return None

        current_version = self.get_current_version()
        if not current_version:
            return None

        for patch in patch_configs:
            if patch.get("version") == current_version:
                return patch

        return None

    def _build_index_url(self, patch_info: Dict) -> Optional[str]:
        """构建 indexFile URL"""
        index_file_path = patch_info.get("indexFile")
        if not index_file_path:
            return None
        return f"{self.cdn_base}/{index_file_path.lstrip('/')}"

    def _build_krpdiff_url(self, krpdiff_name: str) -> str:
        """构建 krpdiff 下载 URL"""
        patch_info = self._find_matching_patch()
        if not patch_info:
            return f"{self.cdn_base}/resources/{krpdiff_name}"

        base_url = patch_info.get("baseUrl", "")
        return f"{self.cdn_base}/{base_url.lstrip('/')}{krpdiff_name}"

    def get_current_version(self) -> Optional[str]:
        """获取当前版本"""
        cfg_file = self.game_folder / "launcherDownloadConfig.json"
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text())
                return data.get("version")
            except Exception:
                pass
        return None

    def get_target_version(self) -> str:
        """获取目标版本"""
        return self.predownload_config.get("version", "")

    def get_group_infos(self) -> List[Dict]:
        """获取 groupInfos 列表"""
        if not self.index_file:
            return []
        return self.index_file.get("groupInfos", [])

    def get_index_url(self) -> Optional[str]:
        """获取增量更新 indexFile 的 URL"""
        patch_info = self._find_matching_patch()
        if not patch_info:
            return None
        return self._build_index_url(patch_info)

    def download_incremental(self) -> bool:
        """下载增量更新包"""
        is_ok, error_msg = check_hpatchz_requirements()
        if not is_ok:
            logger.warning(f"环境检查失败: {error_msg}")
            return False

        current_version = self.get_current_version()
        if not current_version:
            logger.error("无法获取当前版本")
            return False

        index_url = self.get_index_url()
        if not index_url:
            logger.error("无法获取增量更新索引URL（可能预下载尚未开放）")
            return False

        logger.info(f"正在获取增量更新信息: {current_version}")

        index_data = _http_get_json(index_url)
        if not index_data:
            logger.error("获取增量更新索引失败")
            return False

        self._index_file = index_data

        group_infos = self.get_group_infos()
        if not group_infos:
            logger.warning("没有找到增量更新组")
            return False

        logger.info(f"找到 {len(group_infos)} 个增量更新组")

        download_dir = self.game_folder / ".incremental_download"
        download_dir.mkdir(parents=True, exist_ok=True)

        (download_dir / "indexFile.json").write_text(json.dumps(index_data, indent=2))

        version_info = {
            "from_version": current_version,
            "to_version": self.get_target_version(),
            "server": self.server_type,
        }
        with open(download_dir / "version_info.json", "w", encoding="utf-8") as f:
            json.dump(version_info, f, indent=4)

        downloaded = 0
        failed = 0

        for i, group in enumerate(group_infos):
            krpdiff_name = group.get("dest")
            if not krpdiff_name:
                continue

            krpdiff_url = self._build_krpdiff_url(krpdiff_name)
            krpdiff_path = download_dir / krpdiff_name

            if krpdiff_path.exists():
                logger.debug(f"跳过已下载: {krpdiff_name}")
                downloaded += 1
                continue

            logger.info(f"下载增量包 {i + 1}/{len(group_infos)}: {krpdiff_name}")

            if self._download_file(krpdiff_url, krpdiff_path):
                downloaded += 1
            else:
                logger.error(f"下载失败: {krpdiff_name}")
                failed += 1

        logger.info(f"增量包下载完成: 成功={downloaded}, 失败={failed}")
        return failed == 0

    def _download_file(self, url: str, dest: Path) -> bool:
        """下载单个文件"""
        try:
            headers = {"User-Agent": "WW-Manager/2.0"}
            response = requests.get(url, headers=headers, timeout=60, stream=True)
            response.raise_for_status()

            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"下载失败 {url}: {e}")
            if dest.exists():
                dest.unlink()
            return False

    def apply_incremental(self) -> bool:
        """应用已下载的增量更新包"""
        is_ok, error_msg = check_hpatchz_requirements()
        if not is_ok:
            logger.warning(f"环境检查失败: {error_msg}")
            raise IncrementalError(error_msg)

        download_dir = self.game_folder / ".incremental_download"
        version_file = download_dir / "version_info.json"

        if not download_dir.exists() or not version_file.exists():
            logger.error("未找到增量更新包，请先执行 'ww incremental' 下载")
            return False

        try:
            version_info = json.loads(version_file.read_text())
        except Exception:
            logger.error("增量更新版本信息损坏")
            return False

        if version_info.get("server") != self.server_type:
            logger.error(f"服务器不匹配: 预期 {version_info.get('server')}, 实际 {self.server_type}")
            return False

        logger.info(f"开始应用增量更新: {version_info.get('from_version')} -> {version_info.get('to_version')}")

        self.hpatchz_path = get_hpatchz_path(self.cache_dir)

        index_file = download_dir / "indexFile.json"
        if not index_file.exists():
            logger.error("无法找到 indexFile.json")
            return False

        try:
            self._index_file = json.loads(index_file.read_text())
        except Exception:
            logger.error("无法解析 indexFile.json")
            return False

        group_infos = self.get_group_infos()
        if not group_infos:
            logger.warning("没有增量更新组")
            return False

        success_count = 0
        fail_count = 0

        for group in group_infos:
            result = self._apply_single_group(group, download_dir)
            if result == "success":
                success_count += 1
            elif result == "failed":
                fail_count += 1

        logger.info(f"增量更新应用完成: 成功={success_count}, 失败={fail_count}")

        if fail_count > 0:
            return False

        shutil.rmtree(download_dir)
        logger.info("已清理增量下载目录")

        self._update_local_version(version_info.get("to_version"))

        return True

    def _apply_single_group(self, group: Dict, download_dir: Path) -> str:
        """应用单个增量更新组"""
        krpdiff_name = group.get("dest")
        src_files = group.get("srcFiles", [])
        dst_files = group.get("dstFiles", [])

        if not krpdiff_name or not src_files or not dst_files:
            return "failed"

        src_file_info = src_files[0]
        dst_file_info = dst_files[0]

        src_path = src_file_info["dest"]
        src_md5 = src_file_info["md5"]
        dst_md5 = dst_file_info["md5"]

        src_full_path = self.game_folder / src_path

        if not src_full_path.exists():
            logger.warning(f"源文件不存在: {src_path}")
            return "failed"

        local_md5 = _calculate_file_md5(src_full_path)
        if local_md5 != src_md5:
            logger.warning(f"源文件 MD5 不匹配: {src_path} (期望 {src_md5}, 实际 {local_md5})")
            return "failed"

        krpdiff_path = download_dir / krpdiff_name
        if not krpdiff_path.exists():
            logger.error(f"增量包不存在: {krpdiff_name}")
            return "failed"

        temp_dir = tempfile.mkdtemp(prefix="ww_patch_")
        try:
            output_dir = Path(temp_dir) / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            if not self._run_hpatchz(krpdiff_path, output_dir, src_path):
                logger.error(f"应用增量包失败: {krpdiff_name}")
                return "failed"

            output_file = output_dir / src_path

            if not output_file.exists():
                logger.error(f"输出文件未生成: {output_file}")
                return "failed"

            output_md5 = _calculate_file_md5(output_file)
            if output_md5 != dst_md5:
                logger.error(f"输出文件 MD5 不匹配: {src_path} (期望 {dst_md5}, 实际 {output_md5})")
                return "failed"

            backup_path = src_full_path.with_suffix(src_full_path.suffix + ".bak")
            if src_full_path.exists():
                shutil.move(src_full_path, backup_path)

            output_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(output_file, src_full_path)

            logger.info(f"成功更新: {src_path} -> {output_md5}")

            if backup_path.exists():
                backup_path.unlink()

            return "success"

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _run_hpatchz(self, krpdiff_path: Path, output_dir: Path, src_path: str) -> bool:
        """运行 hpatchz 应用增量包"""
        old_dir = Path(tempfile.mkdtemp(prefix="ww_old_"))
        try:
            src_name = os.path.basename(src_path)
            src_full_path = self.game_folder / src_path

            old_file = old_dir / src_name
            shutil.copy2(src_full_path, old_file)

            if platform.system() == "Windows":
                cmd = [
                    str(self.hpatchz_path),
                    "-m",
                    str(old_dir.absolute()) + "/",
                    str(krpdiff_path.absolute()),
                    str(output_dir.absolute()) + "/",
                ]
            else:
                cmd = [
                    "wine",
                    str(self.hpatchz_path),
                    "-m",
                    str(old_dir.absolute()) + "/",
                    str(krpdiff_path.absolute()),
                    str(output_dir.absolute()) + "/",
                ]

            logger.debug(f"Running hpatchz: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

            if result.returncode != 0:
                logger.error(f"hpatchz 运行失败: {result.stderr}")
                return False

            return True

        finally:
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)

    def _update_local_version(self, version: str):
        """更新本地版本"""
        cfg_path = self.game_folder / "launcherDownloadConfig.json"
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text())
                data["version"] = version
                cfg_path.write_text(json.dumps(data, indent=4))
                logger.info(f"本地版本已更新: {version}")
            except Exception as e:
                logger.error(f"更新本地版本失败: {e}")
