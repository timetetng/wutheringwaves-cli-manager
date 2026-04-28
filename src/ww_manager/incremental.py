# incremental.py
"""
增量更新模块：处理 krpdiff 增量包的下载和应用
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

HPATCHZ_REPO = "timetetng/WutheringWaves_manager"
HPATCHZ_VERSION = "v4.8.0"
HPATCHZ_FILENAME = "hpatchz.exe"


class IncrementalError(Exception):
    """增量更新相关错误"""

    pass


class EnvironmentError(IncrementalError):
    """环境不满足要求"""

    pass


def check_wine_available() -> bool:
    """检查 wine 是否可用"""
    if platform.system() == "Windows":
        return True
    return shutil.which("wine") is not None


def get_wine_executable() -> Optional[str]:
    """获取 wine 可执行文件路径"""
    if platform.system() == "Windows":
        return None
    return shutil.which("wine")


def check_hpatchz_requirements() -> Tuple[bool, str]:
    """
    检查增量更新所需的环境是否满足

    Returns:
        (is_ok, error_message)
    """
    if platform.system() != "Windows":
        wine_path = get_wine_executable()
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


class IncrementalPatchManager:
    """增量更新管理器"""

    def __init__(self, game_folder: Path, server_type: str, cdn_base: str):
        self.game_folder = game_folder
        self.server_type = server_type
        self.cdn_base = cdn_base.rstrip("/")
        self.cache_dir = game_folder / ".ww_manager_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.hpatchz_path: Optional[Path] = None
        self._index_file: Optional[Dict] = None

    @property
    def index_file(self) -> Optional[Dict]:
        """获取 indexFile.json（懒加载）"""
        if self._index_file is None:
            self._index_file = self._load_index_file()
        return self._index_file

    def _load_index_file(self) -> Optional[Dict]:
        """加载 indexFile.json"""
        return None

    def get_patch_index_url(self, from_version: str, to_version: str) -> Optional[str]:
        """获取增量更新 indexFile 的 URL"""
        return None

    def get_group_infos(self) -> List[Dict]:
        """获取 groupInfos 列表"""
        if not self.index_file:
            return []
        return self.index_file.get("groupInfos", [])

    def download_and_apply_incremental(
        self,
        from_version: str,
        to_version: str,
        progress_callback=None,
    ) -> bool:
        """
        下载并应用增量更新

        Args:
            from_version: 当前版本
            to_version: 目标版本
            progress_callback: 进度回调函数 (group_name, current, total)

        Returns:
            是否成功
        """
        is_ok, error_msg = check_hpatchz_requirements()
        if not is_ok:
            logger.warning(f"环境检查失败: {error_msg}")
            raise EnvironmentError(error_msg)

        patch_index_url = self.get_patch_index_url(from_version, to_version)
        if not patch_index_url:
            logger.error("无法获取增量更新索引URL")
            return False

        logger.info(f"正在获取增量更新信息: {from_version} -> {to_version}")

        index_data = _http_get_json(patch_index_url)
        if not index_data:
            logger.error("获取增量更新索引失败")
            return False

        self._index_file = index_data

        group_infos = self.get_group_infos()
        if not group_infos:
            logger.warning("没有找到增量更新组")
            return False

        logger.info(f"找到 {len(group_infos)} 个增量更新组")

        self.hpatchz_path = get_hpatchz_path(self.cache_dir)

        success_count = 0
        fail_count = 0
        skipped_count = 0

        for i, group in enumerate(group_infos):
            group_name = group.get("dest", f"group_{i}")
            logger.info(f"处理更新组 {i + 1}/{len(group_infos)}: {group_name}")

            try:
                result = self._apply_single_group(group)
                if result == "success":
                    success_count += 1
                elif result == "skipped":
                    skipped_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                logger.error(f"处理组 {group_name} 失败: {e}")
                fail_count += 1

            if progress_callback:
                progress_callback(group_name, i + 1, len(group_infos))

        logger.info(f"增量更新完成: 成功={success_count}, 跳过={skipped_count}, 失败={fail_count}")

        if fail_count > 0:
            return False

        return True

    def _apply_single_group(self, group: Dict) -> str:
        """
        应用单个增量更新组

        Returns:
            "success", "skipped", or "failed"
        """
        krpdiff_name = group.get("dest")
        src_files = group.get("srcFiles", [])
        dst_files = group.get("dstFiles", [])

        if not krpdiff_name or not src_files or not dst_files:
            logger.warning(f"组数据不完整: {krpdiff_name}")
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

        local_md5 = self._calculate_file_md5(src_full_path)
        if local_md5 != src_md5:
            logger.warning(f"源文件 MD5 不匹配: {src_path}\n  期望: {src_md5}\n  实际: {local_md5}\n  跳过此组")
            return "skipped"

        krpdiff_url = f"{self.cdn_base}/resources/{krpdiff_name}"
        krpdiff_path = self.cache_dir / krpdiff_name

        logger.debug(f"下载增量包: {krpdiff_url}")
        if not self._download_file(krpdiff_url, krpdiff_path):
            logger.error(f"下载增量包失败: {krpdiff_name}")
            return "failed"

        temp_dir = tempfile.mkdtemp(prefix="ww_patch_")
        try:
            output_dir = Path(temp_dir) / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            if not self._run_hpatchz(krpdiff_path, output_dir):
                logger.error(f"应用增量包失败: {krpdiff_name}")
                return "failed"

            output_file = output_dir / src_path

            if not output_file.exists():
                logger.error(f"输出文件未生成: {output_file}")
                return "failed"

            output_md5 = self._calculate_file_md5(output_file)
            if output_md5 != dst_md5:
                logger.error(f"输出文件 MD5 不匹配: {src_path}\n  期望: {dst_md5}\n  实际: {output_md5}")
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
            if krpdiff_path.exists():
                krpdiff_path.unlink()

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

    def _run_hpatchz(self, krpdiff_path: Path, output_dir: Path) -> bool:
        """运行 hpatchz 应用增量包"""
        old_dir = Path(tempfile.mkdtemp(prefix="ww_old_"))
        try:
            src_file_info = None
            for group in self.get_group_infos():
                if group.get("dest") == krpdiff_path.name:
                    src_file_info = group["srcFiles"][0]
                    break

            if not src_file_info:
                logger.error(f"无法找到源文件信息 for {krpdiff_path.name}")
                return False

            src_name = os.path.basename(src_file_info["dest"])
            src_full_path = self.game_folder / src_file_info["dest"]

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

    @staticmethod
    def _calculate_file_md5(file_path: Path) -> str:
        """计算文件 MD5"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096 * 1024), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()


class OfficialIncrementalManager(IncrementalPatchManager):
    """官方服增量更新管理器"""

    def __init__(self, game_folder: Path, server_type: str, api_data: Dict, cdn_base: str):
        super().__init__(game_folder, server_type, cdn_base)
        self.api_data = api_data
        self._predownload_config: Optional[Dict] = None

    @property
    def predownload_config(self) -> Optional[Dict]:
        """获取预下载配置"""
        if self._predownload_config is None:
            self._predownload_config = self.api_data.get("predownload", {}).get("config", {})
        return self._predownload_config

    def _load_index_file(self) -> Optional[Dict]:
        """加载官方服的 indexFile.json"""
        patch_info = self._find_matching_patch()
        if not patch_info:
            return None

        index_url = self._build_index_url(patch_info)
        if not index_url:
            return None

        return _http_get_json(index_url)

    def _find_matching_patch(self) -> Optional[Dict]:
        """找到匹配当前版本的增量配置"""
        patch_configs = self.predownload_config.get("patchConfig", [])
        if not patch_configs:
            return None

        current_version = self._get_current_version()
        if not current_version:
            return None

        for patch in patch_configs:
            if patch.get("version") == current_version:
                return patch

        return None

    def _get_current_version(self) -> Optional[str]:
        """获取当前版本"""
        cfg_file = self.game_folder / "launcherDownloadConfig.json"
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text())
                return data.get("version")
            except Exception:
                pass
        return None

    def _build_index_url(self, patch_info: Dict) -> Optional[str]:
        """构建 indexFile URL"""
        index_file_path = patch_info.get("indexFile")
        if not index_file_path:
            return None

        return f"{self.cdn_base}/{index_file_path.lstrip('/')}"

    def get_patch_index_url(self, from_version: str, to_version: str) -> Optional[str]:
        """获取增量更新 indexFile 的 URL"""
        patch_configs = self.predownload_config.get("patchConfig", [])
        for patch in patch_configs:
            if patch.get("version") == from_version:
                index_file = patch.get("indexFile")
                if index_file:
                    return f"{self.cdn_base}/{index_file.lstrip('/')}"
        return None


class BilibiliIncrementalManager(IncrementalPatchManager):
    """B站服增量更新管理器"""

    def __init__(self, game_folder: Path, server_type: str, api_data: Dict, cdn_base: str):
        super().__init__(game_folder, server_type, cdn_base)
        self.api_data = api_data
        self._index_url: Optional[str] = None

    def _load_index_file(self) -> Optional[Dict]:
        """加载B站服的 indexFile.json"""
        if not self._index_url:
            return None
        return _http_get_json(self._index_url)

    def set_index_url(self, url: str):
        """设置 indexFile URL"""
        self._index_url = url
        self._index_file = None

    def get_patch_index_url(self, from_version: str, to_version: str) -> Optional[str]:
        """获取增量更新 indexFile 的 URL"""
        return self._index_url
