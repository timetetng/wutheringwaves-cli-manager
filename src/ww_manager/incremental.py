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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

import requests
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from ww_manager.download import RAINBOW_COLORS, NetworkError

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


def _get_krpdiff_size(url: str) -> Optional[int]:
    """通过 HEAD 请求获取 krpdiff 文件大小"""
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": "WW-Manager/2.0"})
        with urlopen(req, timeout=10) as rsp:
            cl = rsp.headers.get("Content-Length")
            if cl:
                return int(cl)
    except Exception as e:
        logger.debug(f"HEAD 请求获取文件大小失败 {url}: {e}")
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

    def _get_group_size(self, group: Dict) -> int:
        """获取 group 的增量包大小"""
        if group.get("size"):
            return group["size"]
        dst_files = group.get("dstFiles", [])
        if dst_files:
            return dst_files[0].get("size", 0)
        return 0

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

        pending_groups = []
        skipped_groups = []
        total_size = 0
        pending_size = 0

        for group in group_infos:
            krpdiff_name = group.get("dest")
            if not krpdiff_name:
                continue
            krpdiff_path = download_dir / krpdiff_name
            krpdiff_url = self._build_krpdiff_url(krpdiff_name)

            expected_size = _get_krpdiff_size(krpdiff_url)

            if krpdiff_path.exists() and expected_size:
                local_size = krpdiff_path.stat().st_size
                if local_size == expected_size:
                    logger.debug(f"跳过已完成的增量包: {krpdiff_name}")
                    skipped_groups.append(krpdiff_name)
                    total_size += expected_size
                    continue
                elif local_size < expected_size:
                    logger.info(f"检测到不完整的增量包，将重新下载: {krpdiff_name}")
                    krpdiff_path.unlink()
                else:
                    logger.info(f"文件大小异常，重新下载: {krpdiff_name}")
                    krpdiff_path.unlink()

            pending_groups.append((group, krpdiff_path, krpdiff_url, expected_size or 0))
            if expected_size:
                pending_size += expected_size

        total_size = total_size + pending_size

        if not pending_groups:
            logger.info(f"所有增量包已下载完成 ({len(skipped_groups)}/{len(group_infos)})")
            return True

        already_downloaded_count = len(skipped_groups)
        logger.info(f"待下载: {len(pending_groups)} 个增量包 ({pending_size / 1024 / 1024:.2f} MB)")

        progress = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            "[progress.percentage]{task.percentage:>3.1f}%",
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            expand=True,
            transient=True,
        )

        max_workers = 8

        with progress:
            overall_task = progress.add_task(
                f"增量包 ({already_downloaded_count}/{len(group_infos)} 已下载)",
                total=total_size,
            )

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                for group, krpdiff_path, krpdiff_url, size in pending_groups:
                    krpdiff_name = group.get("dest")
                    future = executor.submit(
                        self._download_file,
                        krpdiff_url,
                        krpdiff_path,
                        size,
                        progress,
                        overall_task,
                    )
                    futures[future] = (krpdiff_name, size)

                failed = []
                for future in as_completed(futures):
                    krpdiff_name, size = futures[future]
                    if future.result():
                        pass
                    else:
                        failed.append(krpdiff_name)

        if failed:
            logger.error(f"下载失败: {failed}")
            return False

        logger.info(f"增量包下载完成: {len(group_infos)}/{len(group_infos)}")
        return True

    def _download_file(
        self,
        url: str,
        dest: Path,
        expected_size: int,
        progress: Optional[Progress] = None,
        overall_task_id: Optional[TaskID] = None,
    ) -> bool:
        """下载单个文件，支持断点续传和大小校验，使用 Rich Progress 显示进度"""
        dest.parent.mkdir(parents=True, exist_ok=True)

        task_id = None
        if progress:
            task_id = progress.add_task(description=dest.name, total=expected_size)
            color = RAINBOW_COLORS[task_id % len(RAINBOW_COLORS)]
            progress.update(task_id, description=f"[{color}]{dest.name}[/{color}]")

        temp_file = dest.with_suffix(dest.suffix + ".temp")
        headers = {"User-Agent": "WW-Manager/2.0"}

        retries = 3
        success = False

        for attempt in range(retries):
            try:
                resume_byte = 0
                if temp_file.exists():
                    resume_byte = temp_file.stat().st_size

                if resume_byte == expected_size:
                    if progress and task_id is not None:
                        progress.update(task_id, completed=expected_size)
                    success = True
                    break

                if resume_byte > 0:
                    headers["Range"] = f"bytes={resume_byte}-"
                    if progress and task_id is not None:
                        progress.update(task_id, completed=resume_byte)

                mode = "ab" if resume_byte > 0 else "wb"

                req = Request(url, headers=headers)
                with urlopen(req, timeout=15) as rsp:
                    if rsp.status not in (200, 206):
                        raise NetworkError(f"HTTP {rsp.status}")

                    with open(temp_file, mode) as f:
                        while True:
                            chunk = rsp.read(1024 * 256)
                            if not chunk:
                                break
                            f.write(chunk)

                            if progress:
                                chunk_len = len(chunk)
                                if task_id is not None:
                                    progress.update(task_id, advance=chunk_len)
                                if overall_task_id is not None:
                                    progress.update(overall_task_id, advance=chunk_len)

                if temp_file.stat().st_size == expected_size:
                    success = True
                    break

            except Exception as e:
                if attempt == retries - 1:
                    if progress:
                        progress.console.log(f"[red]下载失败 {dest.name}: {e}[/red]")
                    return False
                time.sleep(1 + attempt)

        if success:
            shutil.move(temp_file, dest)

        if progress and task_id is not None:
            progress.remove_task(task_id)

        return success

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
        skip_count = 0
        failed_groups = []

        for group in group_infos:
            try:
                result = self._apply_single_group(group, download_dir)
            except Exception as e:
                logger.error(f"应用增量包异常: {e}")
                result = "failed"

            if result == "success":
                success_count += 1
            elif result == "skipped":
                skip_count += 1
            else:
                fail_count += 1
                failed_groups.append(group.get("dest"))

        logger.info(f"增量更新应用完成: 成功={success_count}, 跳过={skip_count}, 失败={fail_count}")

        if fail_count > 0:
            logger.warning(f"失败文件: {failed_groups}")
            logger.warning(f"有 {fail_count} 个增量包应用失败。请运行 'ww sync' 修复或等待游戏修复工具。")
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
        if local_md5 == dst_md5:
            logger.info(f"跳过已更新的文件: {src_path}")
            return "success"
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
                logger.warning(f"hpatchz 应用失败，尝试下载完整文件: {src_path}")
                if not self._download_full_file(dst_file_info, src_full_path):
                    logger.error(f"应用增量包失败: {krpdiff_name}")
                    return "failed"
                return "success"

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

    def _download_full_file(self, dst_info: Dict, dest_path: Path) -> bool:
        """下载完整文件作为 hpatchz 失败的回退方案"""
        dest = dst_info["dest"]
        expected_md5 = dst_info["md5"]
        expected_size = int(dst_info["size"])

        res_base = self._find_resource_base()
        if not res_base:
            logger.error("无法获取资源路径")
            return False

        from urllib.parse import quote

        url = f"{self.cdn_base}/{res_base}/{quote(dest, safe='/:')}"

        logger.info(f"下载完整文件: {dest}")
        return self._download_and_verify(url, dest_path, expected_size, expected_md5)

    def _run_hpatchz(self, krpdiff_path: Path, output_dir: Path, src_path: str) -> bool:
        """运行 hpatchz 应用增量包"""
        if platform.system() == "Windows":
            cmd = [
                str(self.hpatchz_path),
                "-f",
                str(self.game_folder.absolute()) + "/",
                str(krpdiff_path.absolute()),
                str(output_dir.absolute()) + "/",
            ]
        else:
            cmd = [
                "wine",
                str(self.hpatchz_path),
                "-f",
                str(self.game_folder.absolute()) + "/",
                str(krpdiff_path.absolute()),
                str(output_dir.absolute()) + "/",
            ]

        logger.debug(f"Running hpatchz: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            logger.error(f"hpatchz 运行失败: {result.stderr}")
            return False

        return True

    def verify_new_version(self) -> bool:
        """校验新版本文件状态，用于增量更新后的检查"""
        download_dir = self.game_folder / ".incremental_download"
        index_file = download_dir / "indexFile.json"

        if not index_file.exists():
            logger.error("未找到增量更新的 indexFile.json，请先执行 'ww incremental' 下载")
            return False

        try:
            index_data = json.loads(index_file.read_text())
        except Exception:
            logger.error("无法解析 indexFile.json")
            return False

        resources = index_data.get("resource", [])
        if not resources:
            logger.error("indexFile.json 中没有 resource 列表")
            return False

        game_files = [r for r in resources if not r["dest"].endswith(".krpdiff")]
        logger.info(f"正在校验 {len(game_files)} 个新版本文件...")

        from concurrent.futures import ThreadPoolExecutor, as_completed

        from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

        ok_count = 0
        inconsistent_files = []
        missing_files = []

        with Progress(
            TextColumn("[progress.description]{task.description}", justify="left"),
            BarColumn(bar_width=40),
            "[progress.percentage]{task.percentage:>3.1f}%",
            TimeRemainingColumn(),
            expand=True,
            transient=True,
        ) as progress:
            verify_task = progress.add_task("[cyan]校验文件...", total=len(game_files))

            def check_resource(item):
                dest_path = self.game_folder / item["dest"]
                expected_md5 = item["md5"]
                expected_size = int(item["size"])

                status = "ok"
                if not dest_path.exists():
                    status = "missing"
                elif dest_path.stat().st_size != expected_size:
                    status = "size_mismatch"
                else:
                    actual_md5 = _calculate_file_md5(dest_path)
                    if actual_md5 != expected_md5:
                        status = "md5_mismatch"
                return status, item["dest"]

            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(check_resource, item): item for item in game_files}

                for future in as_completed(futures):
                    status, dest = future.result()
                    progress.advance(verify_task)

                    if status == "ok":
                        ok_count += 1
                    elif status == "missing":
                        missing_files.append(dest)
                    else:
                        inconsistent_files.append(dest)

        logger.info(f"校验完成: 正常={ok_count}, 不一致={len(inconsistent_files)}, 缺失={len(missing_files)}")

        if not inconsistent_files and not missing_files:
            logger.info("所有游戏文件状态正常")
            self._update_local_version(index_data)
            return True

        repair_needed = inconsistent_files + missing_files
        logger.warning(f"需要修复的游戏文件: {repair_needed}")

        res_base = self._find_resource_base()
        if not res_base:
            logger.error("无法获取新版本资源路径")
            return False

        from concurrent.futures import ThreadPoolExecutor, as_completed
        from urllib.parse import quote

        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        game_res_map = {r["dest"]: r for r in game_files}
        repair_count = 0
        fail_count = 0
        total_size = sum(int(game_res_map[d]["size"]) for d in repair_needed)

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            "[progress.percentage]{task.percentage:>3.1f}%",
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as progress:
            overall_task = progress.add_task("修复文件", total=total_size)

            def download_repair(dest):
                item = game_res_map.get(dest)
                if not item:
                    return False, dest, 0
                dest_path = self.game_folder / dest
                expected_md5 = item["md5"]
                expected_size = int(item["size"])
                url = f"{self.cdn_base}/{res_base}/{quote(dest, safe='/:')}"

                task_id = progress.add_task(description=f"[cyan]{dest.name[:40]}[/cyan]", total=expected_size)

                try:
                    headers = {"User-Agent": "WW-Manager/2.0"}
                    response = requests.get(url, headers=headers, timeout=60, stream=True)
                    response.raise_for_status()

                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    downloaded = 0
                    with open(dest_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=1024 * 256):
                            f.write(chunk)
                            downloaded += len(chunk)
                            progress.update(task_id, advance=len(chunk))

                    progress.remove_task(task_id)

                    actual_size = dest_path.stat().st_size
                    if actual_size != expected_size:
                        logger.error(f"文件大小不匹配: {dest.name}")
                        dest_path.unlink()
                        return False, dest, expected_size

                    actual_md5 = _calculate_file_md5(dest_path)
                    if actual_md5 != expected_md5:
                        logger.error(f"文件 MD5 不匹配: {dest.name}")
                        dest_path.unlink()
                        return False, dest, expected_size

                    return True, dest, expected_size

                except Exception as e:
                    logger.error(f"下载失败 {dest.name}: {e}")
                    if dest_path.exists():
                        dest_path.unlink()
                    try:
                        progress.remove_task(task_id)
                    except Exception:
                        pass
                    return False, dest, expected_size

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {executor.submit(download_repair, d): d for d in repair_needed}
                for future in as_completed(futures):
                    success, dest, size = future.result()
                    if success:
                        repair_count += 1
                        logger.info(f"修复成功: {dest}")
                    else:
                        fail_count += 1
                        logger.error(f"修复失败: {dest}")
                    progress.update(overall_task, advance=size)

        logger.info(f"修复完成: 成功={repair_count}, 失败={fail_count}")
        return fail_count == 0

    def _find_resource_base(self) -> Optional[str]:
        """从 indexFile.json 获取资源路径前缀"""
        download_dir = self.game_folder / ".incremental_download"
        index_file = download_dir / "indexFile.json"
        if not index_file.exists():
            return None

        try:
            data = json.loads(index_file.read_text())
            resources = data.get("resource", [])
            if resources:
                from_folder = resources[0].get("fromFolder", "")
                if from_folder:
                    return from_folder.rstrip("/")
        except Exception:
            pass

        return "launcher/game/G152/10003/3.3.0/LwvQueHvaDihmfrFKvPkzsBMsZoMxIAD/zip"

    def _download_and_verify(self, url: str, dest: Path, expected_size: int, expected_md5: str) -> bool:
        """下载文件并验证 MD5"""
        try:
            headers = {"User-Agent": "WW-Manager/2.0"}
            response = requests.get(url, headers=headers, timeout=60, stream=True)
            response.raise_for_status()

            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    f.write(chunk)

            actual_size = dest.stat().st_size
            if actual_size != expected_size:
                logger.error(f"文件大小不匹配: {dest.name} (期望 {expected_size}, 实际 {actual_size})")
                dest.unlink()
                return False

            actual_md5 = _calculate_file_md5(dest)
            if actual_md5 != expected_md5:
                logger.error(f"文件 MD5 不匹配: {dest.name}")
                dest.unlink()
                return False

            logger.info(f"修复成功: {dest.name}")
            return True

        except Exception as e:
            logger.error(f"下载失败 {dest.name}: {e}")
            if dest.exists():
                dest.unlink()
            return False

    def _update_local_version(self, version_or_data):
        """更新本地版本，可接收版本字符串或 indexFile.json 数据"""
        if isinstance(version_or_data, dict):
            group_infos = version_or_data.get("groupInfos", [])
            if not group_infos:
                logger.warning("无法获取 groupInfos 信息")
                return
            krpdiff_name = group_infos[0]["dest"]
            parts = krpdiff_name.split("_")
            new_version = parts[1] if len(parts) >= 2 else None
        else:
            new_version = version_or_data

        if not new_version:
            logger.warning("无法获取新版本号")
            return

        cfg_path = self.game_folder / "launcherDownloadConfig.json"
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text())
                data["version"] = new_version
                cfg_path.write_text(json.dumps(data, indent=4))
                logger.info(f"本地版本已更新: {new_version}")
            except Exception as e:
                logger.error(f"更新本地版本失败: {e}")
                logger.error(f"更新本地版本失败: {e}")
