# core.py
import gzip
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    Task,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from ww_manager.config import (
    APPID_TO_SERVER,
    DIFF_CACHE_SERVERS,
    SERVER_CONFIGS,
    SERVER_DIFF_CACHE_DIR_NAME,
    SERVER_DIFF_CACHE_MANIFEST,
    SERVER_DIFF_CACHE_VERSION,
    parse_major_minor,
)
from ww_manager.incremental import (
    IncrementalError,
    IncrementalManager,
    _resource_rel_path,
    check_hpatchz_requirements,
)

logger = logging.getLogger("WW_Manager")


# 日志解密
LOG_MAGIC = b"\xa5\xef\xa5"


def decrypt_client_log(data: bytes) -> bytes:
    if len(data) < 3:
        return data
    out = bytearray()
    for b in data[3:]:
        out.append(b ^ (0xA5 if b % 2 == 1 else 0xEF))
    return bytes(out)


def is_log_encrypted(data: bytes) -> bool:
    if data[:3] == LOG_MAGIC:
        return True
    if len(data) > 3 and data[0] == 0:
        dec = decrypt_client_log(data)
        return dec.decode("utf-8", errors="ignore").startswith("Log file open")
    return False


# --- 自定义异常 ---
class WWError(Exception):
    """基础异常类"""

    pass


class NetworkError(WWError):
    """网络相关错误"""

    pass


class ConfigError(WWError):
    """配置或路径错误"""

    pass


# --- MD5 缓存管理器 ---
class MD5Cache:
    def __init__(self, cache_path: Path, game_root: Path):
        self.cache_path = cache_path
        self.game_root = game_root
        self.cache: Dict[str, Dict[str, Any]] = self._load()
        self._updated = False
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Any]:
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save(self) -> None:
        if not self._updated:
            return
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, indent=2)
            logger.debug("MD5 缓存已保存")
        except Exception as e:
            logger.error(f"保存 MD5 缓存失败: {e}")

    def get(self, file_path: Path) -> Optional[str]:
        if not file_path.exists():
            return None

        try:
            rel_path = str(file_path.relative_to(self.game_root)).replace("\\", "/")
        except ValueError:
            rel_path = file_path.name

        mtime = os.path.getmtime(file_path)

        with self._lock:
            if rel_path in self.cache:
                data = self.cache[rel_path]
                if data["mtime"] == mtime:
                    return data["md5"]

        new_md5 = self._calculate_md5(file_path)
        if new_md5:
            with self._lock:
                self.cache[rel_path] = {"mtime": mtime, "md5": new_md5}
                self._updated = True
        return new_md5

    def _calculate_md5(self, file_path: Path) -> Optional[str]:
        logger.debug(f"计算 MD5: {file_path.name}")
        try:
            hash_md5 = hashlib.md5()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096 * 1024), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception as e:
            logger.error(f"计算 MD5 错误 {file_path}: {e}")
            return None

    def clear(self, file_path: Path) -> None:
        try:
            rel_path = str(file_path.relative_to(self.game_root)).replace("\\", "/")
            with self._lock:
                if rel_path in self.cache:
                    del self.cache[rel_path]
                    self._updated = True
        except ValueError:
            pass


# 定义彩虹颜色列表
RAINBOW_COLORS = [
    "bright_red",
    "orange1",
    "gold1",
    "yellow",
    "chartreuse1",
    "green",
    "spring_green1",
    "cyan1",
    "deep_sky_blue1",
    "dodger_blue1",
    "blue",
    "purple",
    "magenta",
    "hot_pink",
    "deep_pink2",
]


class RainbowBarColumn(BarColumn):
    """美化bar"""

    def render(self, task: Task) -> Any:
        # 根据 task.id 计算颜色，确保和文件名颜色一致
        color = RAINBOW_COLORS[task.id % len(RAINBOW_COLORS)]
        self.complete_style = color
        self.finished_style = color
        # 调用父类的 render
        return super().render(task)


# --- 核心管理器 ---
class WGameManager:
    def __init__(self, game_folder: Path, server_type: str):
        if server_type not in SERVER_CONFIGS:
            raise ConfigError(f"无效的服务器类型: {server_type}")

        self.game_folder = game_folder.resolve()
        self.server_type = server_type
        self.config = SERVER_CONFIGS[server_type]

        self.md5_cache = MD5Cache(self.game_folder / "wwm_md5_cache.json", self.game_folder)

        # 服务器差异缓存目录（游戏目录内集中管理）
        self.diff_cache_dir = self.game_folder / SERVER_DIFF_CACHE_DIR_NAME
        self.diff_cache_manifest = self.diff_cache_dir / SERVER_DIFF_CACHE_MANIFEST

        self._launcher_info = None
        self._cdn_node = None
        self._game_index = None

    @property
    def launcher_info(self):
        if not self._launcher_info:
            logger.info(f"正在获取 {self.server_type} 服配置...")
            self._launcher_info = self._http_get_json(self.config["api_url"])
            if not self._launcher_info:
                raise NetworkError("无法获取启动器配置信息")
        return self._launcher_info

    @property
    def cdn_node(self):
        if not self._cdn_node:
            nodes = self.launcher_info["default"].get("cdnList", [])
            valid_nodes = [n for n in nodes if n.get("K1") == 1 and n.get("K2") == 1]
            if not valid_nodes:
                raise NetworkError("没有可用的 CDN 节点")
            best = max(valid_nodes, key=lambda x: x["P"])
            self._cdn_node = best["url"]
            logger.info(f"使用 CDN: {self._cdn_node}")
        return self._cdn_node

    @property
    def game_index(self):
        if not self._game_index:
            uri = self.launcher_info["default"]["config"]["indexFile"]
            url = urljoin(self.cdn_node, uri)
            logger.info("下载文件清单 (Index)...")
            self._game_index = self._http_get_json(url)
            if not self._game_index:
                raise NetworkError("无法下载文件清单")
        return self._game_index

    def _http_get_json(self, url: str) -> Optional[Any]:
        try:
            req = Request(url, headers={"User-Agent": "WW-Manager/2.0", "Accept-Encoding": "gzip"})
            with urlopen(req, timeout=10) as rsp:
                if rsp.status != 200:
                    return None
                data = rsp.read()
                if "gzip" in rsp.headers.get("Content-Encoding", "").lower():
                    data = gzip.decompress(data)
                return json.loads(data)
        except Exception as e:
            logger.error(f"HTTP 请求失败 {url}: {e}")
            return None

    def _download_file(
        self,
        url: str,
        dest: Path,
        expected_size: int,
        progress: Optional[Progress] = None,
        overall_task_id: Optional[TaskID] = None,
    ) -> bool:
        """带重试的单文件下载，使用 Rich Progress"""
        dest.parent.mkdir(parents=True, exist_ok=True)

        task_id = None
        if progress:
            # 注册任务
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
                if temp_file.exists() and temp_file.stat().st_size <= expected_size:
                    resume_byte = temp_file.stat().st_size

                # 如果已完成，更新进度条并跳过
                if resume_byte == expected_size:
                    if progress and task_id is not None:
                        progress.update(task_id, completed=expected_size)
                        if overall_task_id is not None:
                            # 这里不更新总进度，因为会在外部循环控制，或者 update(advance=0)
                            pass
                    success = True
                    break

                if resume_byte > 0:
                    headers["Range"] = f"bytes={resume_byte}-"
                    # 更新子进度条到断点位置
                    if progress and task_id is not None:
                        progress.update(task_id, completed=resume_byte)

                mode = "ab" if resume_byte > 0 else "wb"

                req = Request(url, headers=headers)
                with urlopen(req, timeout=15) as rsp:
                    if rsp.status not in (200, 206):
                        raise NetworkError(f"HTTP {rsp.status}")
                    if resume_byte > 0 and rsp.status == 200:
                        # 服务器忽略 Range 时重新写临时文件，避免追加出损坏文件
                        resume_byte = 0
                        mode = "wb"
                        if progress and task_id is not None:
                            progress.update(task_id, completed=0)

                    with open(temp_file, mode) as f:
                        while True:
                            chunk = rsp.read(1024 * 256)
                            if not chunk:
                                break
                            f.write(chunk)

                            # 更新界面
                            if progress:
                                chunk_len = len(chunk)
                                if task_id is not None:
                                    progress.update(task_id, advance=chunk_len)
                                if overall_task_id is not None:
                                    progress.update(overall_task_id, advance=chunk_len)

                if temp_file.stat().st_size == expected_size:
                    success = True
                    break
                else:
                    # 大小不对，重试
                    pass

            except Exception as e:
                if attempt == retries - 1:
                    # 只有最后一次失败才记录日志，避免进度条乱掉
                    if progress:
                        progress.console.log(f"[red]下载失败 {dest.name}: {e}[/red]")
                    if progress and task_id is not None:
                        progress.remove_task(task_id)
                    return False
                time.sleep(1 + attempt)

        if success:
            shutil.move(temp_file, dest)
            self.md5_cache.clear(dest)

        # 移除子任务，保持界面整洁
        if progress and task_id is not None:
            progress.remove_task(task_id)

        return success

    def _batch_download(self, tasks: List[dict]):
        if not tasks:
            logger.info("没有文件需要下载")
            return

        total_size = sum(t["size"] for t in tasks)
        logger.info(f"准备下载 {len(tasks)} 个文件，总大小: {total_size / 1024 / 1024:.2f} MB")

        max_workers = 8

        progress = Progress(
            TextColumn("{task.description}", justify="right"),
            RainbowBarColumn(bar_width=40),
            "[progress.percentage]{task.percentage:>3.1f}%",
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            expand=True,
            transient=True,  # 进度条完成后自动消失
        )

        with progress:
            overall_task = progress.add_task("Total Download", total=total_size)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = []
                for task in tasks:
                    future = executor.submit(
                        self._download_file,
                        task["url"],
                        task["path"],
                        task["size"],
                        progress,
                        overall_task,
                    )
                    futures.append(future)

                for f in as_completed(futures):
                    if not f.result():
                        progress.console.log("[red]有文件下载失败，请重试 sync[/red]")

    def sync_files(self, force_check_md5=False):
        # 确保获取的是当前版本的配置
        default_info = self.launcher_info["default"]
        res_base = default_info.get("resourcesBasePath") or default_info.get("baseUrl")
        if not res_base:
            raise WWError("无法获取资源路径配置 (resourcesBasePath 和 baseUrl 均为空)")
        res_list = self.game_index["resource"]

        tasks = []

        logger.info("正在校验文件 (可能需要几分钟)...")

        # 引入 rich 进度条和多线程组件
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

        with Progress(
            TextColumn("[progress.description]{task.description}", justify="left"),
            BarColumn(bar_width=40),
            "[progress.percentage]{task.percentage:>3.1f}%",
            TimeRemainingColumn(),
            expand=True,
            transient=True,  # 进度条完成后自动消失
        ) as progress:
            # 添加总校验任务
            verify_task = progress.add_task("[cyan]准备校验...", total=len(res_list))

            # 定义供多线程调用的单个文件校验函数
            def check_file(item):
                try:
                    dest_path = self.game_folder / _resource_rel_path(item["dest"])
                except IncrementalError:
                    return None, item["dest"]
                expected_md5 = item["md5"]
                expected_size = int(item["size"])

                need_download = False
                if not dest_path.exists():
                    need_download = True
                elif force_check_md5:
                    # md5_cache 内部已实现线程安全锁，可安全并发调用
                    if self.md5_cache.get(dest_path) != expected_md5:
                        need_download = True
                elif dest_path.stat().st_size != expected_size:
                    need_download = True

                download_info = None
                if need_download:
                    url = urljoin(self.cdn_node, f"{res_base}/{item['dest']}")
                    download_info = {
                        "url": quote(url, safe=":/"),
                        "path": dest_path,
                        "size": expected_size,
                    }
                return download_info, dest_path.name

            # 开启多线程池并发校验（最大线程数设为 8，兼顾 SSD IO 性能和 CPU 负载）
            max_workers = 8
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # 提交所有任务
                futures = {executor.submit(check_file, item): item for item in res_list}

                # 收集结果并更新进度条
                for future in as_completed(futures):
                    download_info, file_name = future.result()
                    if download_info:
                        tasks.append(download_info)

                    # 在主线程中更新进度条，避免多线程直接操作 UI 导致闪烁
                    progress.update(verify_task, description=f"[cyan]校验中: {file_name}[/cyan]")
                    progress.advance(verify_task)

        if tasks:
            self._batch_download(tasks)
            self.md5_cache.save()
            self._update_local_config()
        else:
            logger.info("所有文件校验通过，无需下载。")

        # Issue #19: 清理已从 manifest 移除的旧文件（如升级后残留的旧 pak）。
        # UE 会挂载 Paks 目录下所有 .pak，过期文件会覆盖新文件导致启动失败。
        self._cleanup_stale_files(res_list)

    def _cleanup_stale_files(self, res_list) -> None:
        """删除磁盘上已从当前 manifest 移除的旧文件（Issue #19）。

        只清理「本工具曾管理过、现已废弃」的文件：
        - 判据 = 文件在 md5 缓存中有记录（说明曾由本工具校验/下载），
          且不在当前 manifest 中；
        - 扫描范围 = manifest 声明过的顶层资源目录（如 Client/、Engine/），
          跳过 `Saved/` 子目录（玩家截图/存档/运行时语音视频资源由游戏自身管理）。

        绝不删除用户自行放置的文件：不在 md5 缓存中的文件一律保留。
        """
        # 1) 当前 manifest 声明的全部相对路径（POSIX 风格）
        expected_dests = set()
        top_dirs = set()
        for item in res_list:
            try:
                rel = _resource_rel_path(item["dest"])
            except IncrementalError:
                continue
            rel_str = rel.as_posix()
            expected_dests.add(rel_str)
            if rel.parent != Path("."):
                top_dirs.add(rel.parts[0])  # 顶层目录名，如 Client / Engine

        if not top_dirs:
            return

        removed, skipped = [], []
        for top in sorted(top_dirs):
            scan_root = self.game_folder / top
            if not scan_root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(scan_root):
                # 跳过隐藏目录与 Saved/ 目录（玩家截图/存档/运行时资源由游戏自身管理）
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "Saved"]
                for name in filenames:
                    if name.startswith(".") or name.endswith((".temp", ".bak", ".tmp")):
                        continue
                    full = Path(dirpath) / name
                    rel_str = full.relative_to(self.game_folder).as_posix()
                    if rel_str in expected_dests:
                        continue
                    # 只清理本工具曾管理过的文件（md5 缓存有记录）
                    if rel_str not in self.md5_cache.cache:
                        skipped.append(rel_str)
                        continue
                    removed.append((full, rel_str))

        for full, rel_str in removed:
            try:
                full.unlink()
                self.md5_cache.clear(full)
                logger.warning(f"已清理已从 manifest 移除的旧文件: {rel_str}")
            except OSError as e:
                logger.warning(f"清理旧文件失败 {rel_str}: {e}")
        if removed:
            logger.info(f"清理完成: 共删除 {len(removed)} 个已废弃文件")
        if skipped:
            logger.warning(
                "以下文件不在当前 manifest 中（可能为运行时资源/用户文件，已保留）: "
                + ", ".join(sorted(skipped)[:10])
                + (f" 等 {len(skipped)} 个" if len(skipped) > 10 else "")
            )

    def download_full(self):
        """下载完整客户端"""
        logger.info(f"准备下载 {self.server_type} 服完整客户端到: {self.game_folder}")

        # 确保游戏根目录存在
        self.game_folder.mkdir(parents=True, exist_ok=True)
        self.sync_files(force_check_md5=False)

        logger.info(f"{self.server_type} 服完整客户端下载完毕！")

    def checkout(self, target_server: str, force_sync: bool = False):
        """切换服务器。

        官服/B服适用"差异缓存"切换：
        - 每次切换都请求两服索引的 md5 清单，算出差异文件集（同名 md5 不同 +
          仅某服存在的文件）。
        - 对每个差异文件，若本地缓存已存在且 md5 匹配目标服索引 -> 直接本地拷贝恢复；
          否则从 CDN 下载并回填缓存（首次/缓存失效必然要走 CDN）。
        - 同时把当前磁盘上的源服版本备份进缓存，保证切回源服时也能本地恢复。
        - 大版本更新（major.minor 变化，如 3.4 -> 3.5）时差异文件必然变化，重置清空缓存。

        国际服(global) 文件集与国服几乎全异，不适用差异缓存，回退到全量校验同步。
        --force-sync 强制走全量 md5 校验同步兜底。
        """
        if target_server not in SERVER_CONFIGS:
            raise ConfigError(f"无效的服务器类型: {target_server}")

        src = self._detect_current_server()
        dst = target_server

        participate = src in DIFF_CACHE_SERVERS and dst in DIFF_CACHE_SERVERS

        # 获取源服与目标服索引（源服用于对比差异与备份，目标服用于恢复/下载）
        src_ctx = self._fetch_server_context(src)
        dst_ctx = self._fetch_server_context(dst)

        # 让 self 指向目标服，供配置写入/后续使用
        self.server_type = dst
        self.config = SERVER_CONFIGS[dst]
        self._launcher_info = dst_ctx["info"]
        self._cdn_node = dst_ctx["cdn"]

        if src == dst:
            logger.info(f"当前已在 {dst} 服，无需切换差异文件")
            if force_sync:
                logger.info("检测到强制同步，执行全量校验同步...")
                self.sync_files(force_check_md5=True)
            self._update_local_config()
            return

        if participate and not force_sync:
            self._checkout_via_cache(src, dst, src_ctx, dst_ctx)
        else:
            label = "非差异缓存适用(国际服)" if not participate else "强制同步"
            logger.info(f"{src} -> {dst}: {label}，执行全量校验同步...")
            self.sync_files(force_check_md5=True)
            self._update_local_config()

    # 差异缓存

    def _detect_current_server(self) -> str:
        """从本地配置读取当前已安装服；读不到则回退 self.server_type。"""
        cfg_file = self.game_folder / "launcherDownloadConfig.json"
        if cfg_file.exists():
            try:
                d = json.loads(cfg_file.read_text(encoding="utf-8"))
                srv = APPID_TO_SERVER.get(d.get("appId"))
                if srv:
                    return srv
            except Exception:
                pass
        return self.server_type

    def _fetch_server_context(self, server: str) -> Dict[str, Any]:
        """获取指定服的索引上下文，独立于 self.server_type。

        返回 {version, cdn, res_base, resource, info}。
        """
        if server not in SERVER_CONFIGS:
            raise ConfigError(f"无效的服务器类型: {server}")
        cfg = SERVER_CONFIGS[server]
        info = self._http_get_json(cfg["api_url"])
        if not info:
            raise NetworkError(f"无法获取 {server} 服启动器配置信息")
        version = info["default"].get("version", "")
        nodes = info["default"].get("cdnList", [])
        valid = [n for n in nodes if n.get("K1") == 1 and n.get("K2") == 1]
        if not valid:
            raise NetworkError(f"{server} 服没有可用的 CDN 节点")
        cdn = max(valid, key=lambda x: x["P"])["url"]
        res_base = info["default"].get("resourcesBasePath") or info["default"].get("baseUrl")
        uri = info["default"]["config"]["indexFile"]
        idx = self._http_get_json(urljoin(cdn, uri))
        if not idx:
            raise NetworkError(f"无法下载 {server} 服文件清单")
        return {
            "version": version,
            "cdn": cdn,
            "res_base": res_base,
            "resource": idx.get("resource", []),
            "info": info,
        }

    @staticmethod
    def _compute_diff_sets(src_res: List[Dict], dst_res: List[Dict]):
        """对比两服文件清单。

        返回 (same, diff_or_new, only_src)：
         - same:        两服同名且 md5 相同（共享文件，无需处理）
         - diff_or_new: 目标服需要的版本文件（同名 md5 不同 或 仅目标服存在）
         - only_src:    仅源服存在（切到目标服后应隔离/移除）
        """
        src_map = {i["dest"]: i for i in src_res}
        dst_map = {i["dest"]: i for i in dst_res}
        same, diff_or_new, only_src = [], [], []
        for d in set(src_map) | set(dst_map):
            s = src_map.get(d)
            t = dst_map.get(d)
            if t is None:
                only_src.append(d)
            elif s is None or s["md5"] != t["md5"]:
                diff_or_new.append(d)
            else:
                same.append(d)
        return same, diff_or_new, only_src

    def _checkout_via_cache(self, src: str, dst: str, src_ctx, dst_ctx) -> None:
        same, diff_or_new, only_src = self._compute_diff_sets(src_ctx["resource"], dst_ctx["resource"])

        # 大版本更新判定：major.minor 变化则重置清空差异缓存
        major = parse_major_minor(dst_ctx["version"])
        manifest = self._load_cache_manifest()
        cached_major = tuple(manifest.get("major_minor") or ())
        if major and cached_major and tuple(major) != cached_major:
            logger.info(
                f"检测到大版本更新 {'.'.join(map(str, cached_major))} -> {'.'.join(map(str, major))}，重置差异缓存..."
            )
            self._reset_diff_cache()
            manifest = self._load_cache_manifest()

        logger.info(f"分析 {src} ↔ {dst} 差异: 共享 {len(same)}，差异 {len(diff_or_new)}，仅{src}存在 {len(only_src)}")

        # 1) 备份源服当前磁盘版本 + 恢复/收集目标服差异文件
        to_download = []  # (dest, size, md5)
        restored = 0
        dst_map = {i["dest"]: i for i in dst_ctx["resource"]}
        for d in diff_or_new:
            item = dst_map[d]
            self._backup_src_to_cache(src, d)
            dst_cache = self._server_cache_path(dst, d)
            if dst_cache.exists() and self._safe_md5(dst_cache) == item["md5"]:
                # 缓存命中：本地恢复
                dest_path = self.game_folder / _resource_rel_path(d)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst_cache, dest_path)
                self.md5_cache.clear(dest_path)
                restored += 1
            else:
                to_download.append((d, item["size"], item["md5"]))

        # 2) 下载缺失/失效的目标服版本并回填缓存
        downloaded = self._download_diff_dst(dst, to_download, dst_ctx)

        # 3) 隔离仅源服存在的文件（备份到缓存后从游戏目录移除）
        for d in only_src:
            self._isolate_src_only(src, d)

        # 4) 记录缓存版本与大版本号
        manifest["version"] = SERVER_DIFF_CACHE_VERSION
        if major:
            manifest["major_minor"] = list(major)
        self._save_cache_manifest(manifest)

        logger.info(f"切换完成: 本地恢复 {restored}，下载 {downloaded}，隔离 {len(only_src)}，共享 {len(same)}")

        self._update_local_config()

    def _load_cache_manifest(self) -> Dict[str, Any]:
        if self.diff_cache_manifest.exists():
            try:
                with open(self.diff_cache_manifest, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"读取差异缓存清单失败，将重建: {e}")
        return {}

    def _save_cache_manifest(self, manifest: Dict[str, Any]) -> None:
        try:
            self.diff_cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self.diff_cache_manifest, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存差异缓存清单失败: {e}")

    def _reset_diff_cache(self) -> None:
        """大版本更新时清空整个差异缓存目录（差异文件集必然变化）。"""
        try:
            if self.diff_cache_dir.exists():
                shutil.rmtree(self.diff_cache_dir, ignore_errors=True)
            logger.info(f"差异缓存已重置: {self.diff_cache_dir}")
        except Exception as e:
            logger.error(f"重置差异缓存失败: {e}")

    def _server_cache_path(self, server: str, dest: str) -> Path:
        """某服某文件的缓存路径：<cache_dir>/<server>/<rel_path>"""
        return self.diff_cache_dir / server / _resource_rel_path(dest)

    def _safe_md5(self, path: Path) -> Optional[str]:
        if not path.exists() or not path.is_file():
            return None
        try:
            with open(path, "rb") as f:
                h = hashlib.md5()
                for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def _backup_src_to_cache(self, src: str, dest: str) -> None:
        """把当前磁盘上的源服版本备份进缓存，供切回源服时秒级恢复。"""
        game_path = self.game_folder / _resource_rel_path(dest)
        if not game_path.is_file():
            return
        cache_path = self._server_cache_path(src, dest)
        if cache_path.is_file():
            return  # 已缓存
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(game_path, cache_path)
        logger.debug(f"已备份源服版本: {dest} -> {cache_path}")

    def _isolate_src_only(self, src: str, dest: str) -> None:
        """仅源服存在的文件：切到目标服后不再需要，备份进缓存并从游戏目录移除。"""
        game_path = self.game_folder / _resource_rel_path(dest)
        if not game_path.is_file():
            return
        cache_path = self._server_cache_path(src, dest)
        if not cache_path.is_file():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(game_path, cache_path)
        os.remove(game_path)
        self.md5_cache.clear(game_path)
        logger.debug(f"已隔离仅{src}存在的文件: {dest}")

    def _download_diff_dst(self, dst: str, to_download, dst_ctx) -> int:
        """下载目标服差异文件到游戏目录并回填缓存。to_download: (dest, size, md5)。"""
        if not to_download:
            return 0
        total = sum(t[1] for t in to_download)
        logger.info(f"准备下载 {len(to_download)} 个差异文件，总大小: {total / 1024 / 1024:.2f} MB")

        tasks = []
        for d, size, _md5 in to_download:
            url = urljoin(dst_ctx["cdn"], f"{dst_ctx['res_base']}/{d}")
            tasks.append(
                {
                    "url": quote(url, safe=":/"),
                    "path": self.game_folder / _resource_rel_path(d),
                    "size": size,
                    "_dest": d,
                }
            )

        self._batch_download(tasks)

        # 回填目标服缓存
        for t in tasks:
            d = t["_dest"]
            cache_path = self._server_cache_path(dst, d)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(t["path"], cache_path)
        return len(tasks)

    def _update_local_config(self):
        # 优先使用 launcher_info 中的版本，如果获取不到则保持原状或报错
        if self._launcher_info:
            v = self.launcher_info["default"]["version"]
            cfg = {"version": v, "appId": self.config["appId"], "group": "default"}
            self.game_folder.mkdir(parents=True, exist_ok=True)
            with open(self.game_folder / "launcherDownloadConfig.json", "w") as f:
                json.dump(cfg, f, indent=4)
            logger.info(f"本地配置已更新: {self.server_type} ({v})")

    def apply_incremental_update(self, dry_run: bool = False) -> bool:
        """旧接口保留，调用新的统一管理器"""
        is_ok, error_msg = check_hpatchz_requirements()
        if not is_ok:
            logger.warning(f"增量更新环境检查失败: {error_msg}")
            return False

        if dry_run:
            logger.info("增量更新环境检查通过")
            return True

        manager = IncrementalManager(
            self.game_folder,
            self.server_type,
            self.launcher_info,
            self.cdn_node,
        )

        try:
            return manager.apply_incremental()
        except IncrementalError as e:
            logger.error(f"增量更新失败: {e}")
            return False

    def download_incremental(self) -> bool:
        """下载增量更新包"""
        manager = IncrementalManager(
            self.game_folder,
            self.server_type,
            self.launcher_info,
            self.cdn_node,
        )
        try:
            return manager.download_incremental()
        except IncrementalError as e:
            logger.error(f"增量包下载失败: {e}")
            return False

    def apply_incremental(self) -> bool:
        """应用已下载的增量更新包"""
        manager = IncrementalManager(
            self.game_folder,
            self.server_type,
            self.launcher_info,
            self.cdn_node,
        )
        try:
            return manager.apply_incremental()
        except IncrementalError as e:
            logger.error(f"增量更新应用失败: {e}")
            return False

    def verify_new_version(self) -> bool:
        """校验新版本文件状态（用于增量更新前检查）"""
        manager = IncrementalManager(
            self.game_folder,
            self.server_type,
            self.launcher_info,
            self.cdn_node,
        )
        try:
            return manager.verify_new_version()
        except IncrementalError as e:
            logger.error(f"校验失败: {e}")
            return False
