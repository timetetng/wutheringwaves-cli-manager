# core.py
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)

from ww_manager.config import (
    APPID_TO_SERVER,
    DIFF_CACHE_SERVERS,
    RESOURCE_CONFIG_URLS,
    SERVER_CONFIGS,
    SERVER_DIFF_CACHE_DIR_NAME,
    SERVER_DIFF_CACHE_MANIFEST,
    SERVER_DIFF_CACHE_VERSION,
    parse_major_minor,
)
from ww_manager.download import (
    DownloadSpaceError,
    available_disk_space,
    check_space_requirement,
    create_download_progress,
    download_single_file,
    format_bytes,
    space_shortage,
)
from ww_manager.incremental import (
    IncrementalError,
    IncrementalManager,
    _resource_rel_path,
    check_hpatchz_requirements,
)

logger = logging.getLogger("WW_Manager")


# 日志解密
# 文件头前 3 字节每次运行都会变（实测有 00 54 50 / ef bb bf / a5 f1 f5 / 4a 1e 1a 等），
# 所以不能靠固定 magic 判断，只能按解码后的内容判断。
# 明文开头只有两种形态："Log file open, [YYYY..." 或 "[YYYY.MM.DD-..."
_LOG_HEAD = re.compile(rb"^(\xef\xbb\xbf)?(\[20\d\d\.|Log file open)")


def decrypt_client_log(data: bytes) -> bytes:
    if len(data) < 3:
        return data
    out = bytearray()
    for b in data[3:]:
        out.append(b ^ (0xA5 if b % 2 == 1 else 0xEF))
    return bytes(out)


def is_log_encrypted(data: bytes) -> bool:
    if len(data) < 3:
        return False
    if _LOG_HEAD.match(data):  # 头部已是明文
        return False
    # 否则按“跳过前 3 字节 + 奇偶异或”试解，看开头是否像日志
    return bool(_LOG_HEAD.match(decrypt_client_log(data[:64])))


# --- 自定义异常 ---
class WWError(Exception):
    """基础异常类"""

    pass


class NetworkError(WWError):
    """网络相关错误"""

    pass


class DiskSpaceError(WWError):
    """下载前确认目标文件系统空间不足。"""


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
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
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

        stat = file_path.stat()
        mtime = stat.st_mtime

        with self._lock:
            if rel_path in self.cache:
                data = self.cache[rel_path]
                if (
                    isinstance(data, dict)
                    and data.get("mtime_ns") == stat.st_mtime_ns
                    and data.get("ctime_ns") == stat.st_ctime_ns
                    and data.get("size") == stat.st_size
                ):
                    return data.get("md5")

        new_md5 = self._calculate_md5(file_path)
        if new_md5:
            with self._lock:
                self.cache[rel_path] = {
                    "mtime": mtime,
                    "mtime_ns": stat.st_mtime_ns,
                    "ctime_ns": stat.st_ctime_ns,
                    "size": stat.st_size,
                    "md5": new_md5,
                }
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
        expected_md5: Optional[str] = None,
    ) -> bool:
        """复用通用下载器；校验成功后才覆盖正式文件。"""
        success = download_single_file(url, dest, expected_size, progress, overall_task_id, expected_md5=expected_md5)
        if success:
            self.md5_cache.clear(dest)
        return success

    def _batch_download(self, tasks: List[dict]) -> int:
        """批量下载，返回失败的文件数 (0 = 全部成功)"""
        if not tasks:
            logger.info("没有文件需要下载")
            return 0

        total_size = sum(t["size"] for t in tasks)
        logger.info(f"准备下载 {len(tasks)} 个文件，总大小: {total_size / 1024 / 1024:.2f} MB")

        max_workers = 8

        progress = create_download_progress()

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
                        expected_md5=task.get("md5"),
                    )
                    futures.append(future)

                failed = 0
                for f in as_completed(futures):
                    if not f.result():
                        failed += 1
                        logger.debug("文件下载失败，详情见下载器错误")
        return failed

    def sync_files(self, force_check_md5=False, tier: Optional[str] = None, force_space: bool = False):
        tier = self._validate_tier(tier) if tier is not None else self.current_resource_tier()
        if self.server_type == "global" and tier != "hd":
            raise ConfigError("国际服暂仅支持 HD 档位")
        # 确保获取的是当前版本的配置
        default_info = self.launcher_info["default"]
        res_base = default_info.get("resourcesBasePath") or default_info.get("baseUrl")
        if not res_base:
            raise WWError("无法获取资源路径配置 (resourcesBasePath 和 baseUrl 均为空)")
        prepared = self._prepare_resource_tier(tier) if tier != "hd" else None
        if prepared:
            common = prepared["bundle"]["resourcePacks"].get("common", {})
            version = default_info["version"]
            if not isinstance(common, dict) or common.get("version") != version or prepared["version"] != version:
                raise ConfigError("渠道基础包与画质包版本不一致，暂不能组合安装，请稍后重试")
        res_list = self._base_manifest(self.game_index["resource"], tier)
        self._read_local_config()
        self._read_resource_state()

        tasks = []

        logger.info("正在校验文件 (可能需要几分钟)...")

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
                except IncrementalError as e:
                    raise ConfigError(f"非法基础资源路径: {item['dest']}") from e
                expected_md5 = item["md5"]
                expected_size = int(item["size"])

                if force_check_md5:
                    self.md5_cache.clear(dest_path)
                need_download = (
                    not dest_path.is_file()
                    or dest_path.stat().st_size != expected_size
                    or self.md5_cache.get(dest_path) != expected_md5
                )

                download_info = None
                if need_download:
                    url = urljoin(self.cdn_node, f"{res_base}/{item['dest']}")
                    download_info = {
                        "url": quote(url, safe=":/"),
                        "path": dest_path,
                        "size": expected_size,
                        "md5": expected_md5,
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

        # 组合安装在写入基础资源前检查公共文件与所选档位的合计需求。
        tier_tasks = self._resource_download_tasks(tier, prepared, force_check_md5) if prepared else []
        self._check_download_space(tasks + tier_tasks, force_space)
        if tasks:
            for pack in ("common", "hd") if tier == "hd" else ("common",):
                self._record_resource_pack(
                    pack, self.resource_pack_version(pack) or default_info["version"], incomplete=True
                )
            if self._batch_download(tasks):
                raise NetworkError("基础资源下载失败，未更新版本记录；请重跑下载命令或 ww sync")
        else:
            logger.info("所有文件校验通过，无需下载。")
        # 下载器返回成功并不等于正式文件一定完整，登记前再次核对。
        for task in tasks:
            self.md5_cache.clear(task["path"])
        for item in res_list:
            path = self._managed_path(item["dest"])
            if (
                not path.is_file()
                or path.stat().st_size != item["size"]
                or self.md5_cache.get(path) != item["md5"]
            ):
                self.md5_cache.save()
                raise NetworkError(f"基础资源校验失败: {item['dest']}；请重跑下载命令或 ww sync")
        self.md5_cache.save()
        if prepared:
            try:
                self._install_resource_tier(tier, prepared, force_check_md5, force_space=force_space)
            except NetworkError as e:
                raise NetworkError(f"组合安装未完成，请重跑原 download/sync 命令续传: {e}") from e
        if tier == "hd" and any(
            tuple(p.lower() for p in Path(item["dest"]).parts[:3]) == ("client", "content", "hd")
            for item in res_list
        ):
            # HD 已包含在渠道全量清单中，无需再拉取独立画质配置。
            self._register_installed_tier(
                tier, default_info["version"], {"bundles": {"HD": {"resourcePacks": ["common", "hd"]}}}
            )
        self._update_local_config(tier=tier)
        state = self._read_resource_state()
        state["active_tier"] = tier
        self._write_local_json("wwm_resource_state.json", state)

        # Issue #19: 清理已从 manifest 移除的旧文件（如升级后残留的旧 pak）。
        # UE 会挂载 Paks 目录下所有 .pak，过期文件会覆盖新文件导致启动失败。
        self._cleanup_stale_files(res_list, tier=tier)

    def _base_manifest(self, resources: Any, tier: str) -> List[dict]:
        """保留当前渠道的公共文件；非 HD 安装不带入全量清单中的 HD。"""
        if not isinstance(resources, list) or not resources:
            raise ConfigError("基础资源清单为空或格式错误")
        result = []
        seen = set()
        for item in resources:
            try:
                dest = item["dest"]
                if not isinstance(dest, str) or dest.startswith(("/", "\\")):
                    raise ValueError("dest 必须为相对路径")
                rel = _resource_rel_path(dest)
                parts = tuple(p.lower() for p in rel.parts)
                if parts in (("client",), ("client", "content")):
                    raise ValueError("不能将资源目录作为文件写入")
                if parts[0].startswith((".", "wwm_", "launcherdownloadconfig")):
                    raise ValueError("基础资源不能覆盖管理记录")
                if len(parts) >= 3 and parts[:2] == ("client", "content") and parts[2] in self.RESOURCE_TIERS:
                    if tier != "hd" or parts[2] != "hd":
                        continue
                size = int(item["size"])
                md5 = item["md5"]
                key = rel.as_posix().lower()
                if size < 0 or not isinstance(md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", md5):
                    raise ValueError("文件大小或 MD5 非法")
                if key in seen or any(":" in p or "\x00" in p for p in rel.parts):
                    raise ValueError("重复或非法路径")
                path = self._managed_path(rel.as_posix())
                self._managed_path(rel.as_posix() + ".temp")
                if path.exists() and not path.is_file():
                    raise ValueError("目标不是普通文件")
                seen.add(key)
                result.append({**item, "dest": rel.as_posix(), "size": size, "md5": md5.lower()})
            except (KeyError, TypeError, ValueError, IncrementalError) as e:
                raise ConfigError(f"非法基础资源清单项 {item!r}: {e}") from e
        if not result:
            raise ConfigError("基础资源清单未包含公共文件")
        if any(parent.as_posix().lower() in seen for i in result for parent in Path(i["dest"]).parents):
            raise ConfigError("基础资源清单包含文件/目录路径冲突")
        return result

    # --- 3.7+ 资源档位 (HD/SD/UHD) 基础包下载 ---

    RESOURCE_TIERS = ("hd", "sd", "uhd")

    def get_bundle_config(self, config_url: Optional[str] = None) -> Dict[str, Any]:
        """自动获取官服/B服共用的画质配置，不依赖本地启动器。"""
        if self.server_type == "global":
            raise ConfigError("国际服暂仅支持默认 HD，请使用 ww download global 或 ww sync")
        if config_url is not None and (not isinstance(config_url, str) or not config_url):
            raise ConfigError("资源档位配置地址为空")
        logger.info("正在获取资源档位配置...")
        for url in (config_url,) if config_url else RESOURCE_CONFIG_URLS:
            data = self._http_get_json(url)
            if (
                isinstance(data, dict)
                and isinstance(data.get("resourcePacks"), dict)
                and data["resourcePacks"]
                and isinstance(data.get("cdnList"), list)
                and data["cdnList"]
            ):
                return data
        raise NetworkError("无法获取有效的官方画质配置，请检查网络或更新工具后重试")

    def _validate_tier(self, tier: str) -> str:
        tier = tier.lower()
        if tier not in self.RESOURCE_TIERS:
            raise ConfigError(f"无效的资源档位: {tier} (可选: {', '.join(self.RESOURCE_TIERS)})")
        return tier

    def _managed_path(self, relative: str) -> Path:
        """拒绝路径穿越和软链接，避免下载、清理或登记写到管理范围之外。"""
        try:
            rel = _resource_rel_path(relative)
        except IncrementalError as e:
            raise ConfigError(str(e)) from e
        path = self.game_folder
        for part in rel.parts:
            path = path / part
            if path.is_symlink():
                raise ConfigError(f"拒绝操作软链接: {path}")
        return path

    def _read_local_config(self) -> Dict[str, Any]:
        path = self._managed_path("launcherDownloadConfig.json")
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("bundles", {}), dict):
                raise ValueError("配置或 bundles 不是 JSON 对象")
            return data
        except (OSError, ValueError) as e:
            raise ConfigError(f"无法读取本地配置，拒绝覆盖: {e}") from e

    def _write_local_json(self, relative: str, data: Dict[str, Any]) -> None:
        """原子替换单个配置文件，失败时保留旧文件。"""
        import tempfile

        path = self._managed_path(relative)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, indent=4, ensure_ascii=False)
            temporary.replace(path)
        except OSError as e:
            raise ConfigError(f"写入配置失败 {path}: {e}") from e
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def _read_resource_state(self) -> Dict[str, Any]:
        """档位选择由工具显式维护，不从目录或游戏启动参数推断。"""
        path = self._managed_path("wwm_resource_state.json")
        if not path.exists():
            return {"active_tier": "hd", "versions": {}, "incomplete": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("active_tier") not in (*self.RESOURCE_TIERS, None):
                raise ValueError("当前档位非法")
            if not isinstance(data.get("versions"), dict) or not isinstance(data.get("incomplete", []), list):
                raise ValueError("分包版本记录非法")
            if any(not isinstance(v, str) or not v for v in data["versions"].values()):
                raise ValueError("分包版本必须为非空字符串")
            return data
        except (OSError, ValueError) as e:
            raise ConfigError(f"无法读取档位选择记录，拒绝猜测当前档位: {e}") from e

    def current_resource_tier(self) -> str:
        if self.server_type == "global":
            return "hd"
        tier = self._read_resource_state()["active_tier"]
        if tier is None:
            raise ConfigError("当前档位已被移除，请先运行 ww resource <tier> 选择档位")
        return tier

    def resource_pack_version(self, pack: str) -> Optional[str]:
        state = self._read_resource_state()
        versions = state["versions"]
        if pack in versions and (pack != "common" or state.get("common_server") == self.server_type):
            return versions[pack]
        cfg = self._read_local_config()
        if pack == "common" or (pack == "hd" and state["active_tier"] == "hd"):
            # 旧安装默认 HD；显式选择其它档位后不能假定 HD 也已安装。
            return cfg.get("version")
        record = cfg.get("bundles", {}).get(pack.upper(), {})
        marker = self._managed_path(f"launcherDownloadConfig/{pack}.json")
        try:
            data = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
            if isinstance(data, dict) and isinstance(record, dict):
                if data.get("packName") == pack and data.get("version") == record.get("version"):
                    return record.get("version")
        except (OSError, ValueError):
            pass
        return None

    def _record_resource_pack(self, pack: str, version: str, incomplete: bool = False) -> None:
        state = self._read_resource_state()
        state["versions"][pack] = version
        if pack == "common":
            state["common_server"] = self.server_type
        pending = set(state.get("incomplete", []))
        if incomplete:
            pending.add(pack)
        else:
            pending.discard(pack)
        state["incomplete"] = sorted(pending)
        self._write_local_json("wwm_resource_state.json", state)

    def _tier_manifest(self, tier: str, resources: Any) -> List[dict]:
        """先验证整份清单，再允许创建文件、清理旧包或登记安装。"""
        if not isinstance(resources, list) or not resources:
            raise ConfigError("档位清单为空或格式错误")
        result = []
        seen = set()
        for item in resources:
            try:
                dest = item["dest"]
                if not isinstance(dest, str) or dest.startswith(("/", "\\")):
                    raise ValueError("dest 必须为相对路径")
                rel = _resource_rel_path(dest)
                if len(rel.parts) < 4 or tuple(p.lower() for p in rel.parts[:3]) != ("client", "content", tier):
                    raise ValueError("文件不在当前档位目录中")
                # Windows 清单可包含反斜杠，磁盘目录统一为 Client/Content/<TIER>。
                canonical = Path("Client", "Content", tier.upper(), *rel.parts[3:]).as_posix()
                if canonical.lower() in seen:
                    raise ValueError("存在重复文件路径")
                if any(":" in p or "\x00" in p for p in rel.parts):
                    raise ValueError("非法路径字符")
                size = int(item["size"])
                md5 = item["md5"]
                if size < 0 or not isinstance(md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", md5):
                    raise ValueError("文件大小或 MD5 非法")
                path = self._managed_path(canonical)
                self._managed_path(canonical + ".temp")
                if path.exists() and not path.is_file():
                    raise ValueError("目标不是普通文件")
                result.append({**item, "dest": canonical, "source": rel.as_posix(), "size": size, "md5": md5.lower()})
                seen.add(canonical.lower())
            except (KeyError, TypeError, ValueError, IncrementalError) as e:
                raise ConfigError(f"非法档位清单项 {item!r}: {e}") from e
        # 不允许同时将一个路径当作文件和目录。
        paths = {i["dest"].lower() for i in result}
        if any(parent.as_posix().lower() in paths for i in result for parent in Path(i["dest"]).parents):
            raise ConfigError("档位清单包含文件/目录路径冲突")
        return result

    def download_resource_tier(
        self,
        tier: str,
        config_url: Optional[str] = None,
        force_check_md5: bool = False,
        force_space: bool = False,
    ) -> None:
        """下载并验证档位基础包；force_space 只绕过空间检查。"""
        tier = self._validate_tier(tier)
        prepared = self._prepare_resource_tier(tier, config_url)
        self._install_resource_tier(tier, prepared, force_check_md5, force_space=force_space)

    def available_disk_space(self) -> Optional[int]:
        """读取目标目录所在文件系统；未建目录时检查最近的现有父目录。"""
        return available_disk_space(self.game_folder)

    @staticmethod
    def format_bytes(size: int) -> str:
        return format_bytes(size)

    def space_shortage(self, required: int, free: Optional[int]) -> Optional[str]:
        return space_shortage(required, free)

    def _check_download_space(self, tasks: List[dict], force_space: bool = False) -> None:
        if not tasks:
            return
        try:
            check_space_requirement(tasks, self.available_disk_space(), force_space)
        except DownloadSpaceError as e:
            raise DiskSpaceError(str(e)) from e

    def _resource_download_tasks(
        self, tier: str, prepared: dict, force_check_md5: bool = False, *, cache: Optional[MD5Cache] = None,
    ) -> List[dict]:
        if cache is None:
            cache = MD5Cache(self._managed_path(f"wwm_md5_cache_resource_{tier}.json"), self.game_folder)
        tasks = []
        for item in prepared["resources"]:
            path = self._managed_path(item["dest"])
            if force_check_md5:
                cache.clear(path)
            valid = path.is_file() and path.stat().st_size == item["size"] and cache.get(path) == item["md5"]
            if not valid:
                source = f"{prepared['base'].strip('/')}/{item['source']}"
                tasks.append({
                    "url": quote(urljoin(prepared["cdn"], source), safe=":/"),
                    "path": path, "size": item["size"], "md5": item["md5"],
                })
        return tasks

    def _tier_is_current(self, tier: str, version: str) -> bool:
        state = next(s for s in self.resource_tier_status() if s["tier"] == tier)
        return state["state"] == "已安装" and state["version"] == version

    def resource_menu_data(self) -> List[dict]:
        """只读获取官方清单与本地情况；不保存哈希缓存或改变安装记录。"""
        bundle = self.get_bundle_config()
        logger.info("正在读取各档位清单与本地文件，首次可能需要一些时间...")
        states = {s["tier"]: s for s in self.resource_tier_status()}
        rows = []
        for tier in ("sd", "hd", "uhd"):
            state = states[tier]
            try:
                prepared = self._prepare_resource_tier(tier, bundle=bundle)
                installed = self._tier_is_current(tier, prepared["version"])
                tasks = [] if installed else self._resource_download_tasks(tier, prepared)
                rows.append({
                    **state, "installed": installed, "prepared": prepared,
                    "total_size": sum(i["size"] for i in prepared["resources"]),
                    "download_size": sum(t["size"] for t in tasks), "error": None,
                })
            except (WWError, OSError) as e:
                rows.append({**state, "installed": False, "prepared": None, "error": str(e)})
        return rows

    def select_resource_tier(self, tier: str, prepared: dict, force_space: bool = False) -> None:
        """菜单确认：同版本完整登记仅切换默认；其它情况校验、修复后选定。"""
        tier = self._validate_tier(tier)
        if self._tier_is_current(tier, prepared["version"]):
            state = self._read_resource_state()
            state["active_tier"] = tier
            self._write_local_json("wwm_resource_state.json", state)
            logger.info(f"已选定 {tier.upper()} 默认档位（未重复校验）；游戏仍需以 -krqlv={tier} 启动")
        else:
            self._install_resource_tier(tier, prepared, force_space=force_space)

    def _prepare_resource_tier(
        self, tier: str, config_url: Optional[str] = None, *, bundle: Optional[dict] = None,
    ) -> dict:
        """只读预检，组合安装须在公共文件下载前确认画质清单有效。"""
        self._read_resource_state()
        bundle = self.get_bundle_config(config_url) if bundle is None else bundle
        if not isinstance(bundle, dict):
            raise ConfigError("资源档位配置必须为 JSON 对象")
        packs = bundle.get("resourcePacks") or {}
        if not isinstance(packs, dict):
            raise ConfigError("resourcePacks 必须为 JSON 对象")
        bundles = bundle.get("bundles", {})
        if not isinstance(bundles, dict) or not isinstance(bundles.get(tier.upper(), {}), dict):
            raise ConfigError("bundles 档位登记配置错误")
        if tier not in packs:
            raise ConfigError(f"当前渠道不提供 {tier} 档位包，可用: {', '.join(packs) or '无'}")
        pack = packs[tier]
        if not isinstance(pack, dict):
            raise ConfigError("档位配置必须为 JSON 对象")
        try:
            version = pack["version"]
            base = pack["baseUrl"]
            index = pack["indexFile"]
            if not all(isinstance(v, str) and v for v in (version, base, index)):
                raise ValueError("档位版本、资源路径或清单地址为空")
            nodes = [n for n in bundle.get("cdnList", []) if n.get("K1") == 1 and n.get("K2") == 1]
            if not nodes:
                raise ValueError("没有可用的 CDN 节点")
            cdn = max(nodes, key=lambda n: n["P"])["url"]
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"档位配置错误: {e}") from e
        manifest = self._http_get_json(urljoin(cdn, index))
        if not isinstance(manifest, dict):
            raise NetworkError("无法下载资源档位清单")
        resources = self._tier_manifest(tier, manifest.get("resource"))
        self._read_local_config()
        self._managed_path(f"launcherDownloadConfig/{tier}.json")
        self._managed_path(f"wwm_md5_cache_resource_{tier}.json")
        return {"bundle": bundle, "version": version, "resources": resources, "cdn": cdn, "base": base}

    def _install_resource_tier(
        self, tier: str, prepared: dict, force_check_md5: bool = False, *, force_space: bool = False,
    ) -> None:
        bundle, version = prepared["bundle"], prepared["version"]
        resources = prepared["resources"]
        cfg = self._read_local_config()
        cache_path = self._managed_path(f"wwm_md5_cache_resource_{tier}.json")
        cache = MD5Cache(cache_path, self.game_folder)
        logger.info(f"正在校验 {tier.upper()} v{version}: {len(resources)} 个文件")
        tasks = self._resource_download_tasks(tier, prepared, force_check_md5, cache=cache)
        self._check_download_space(tasks, force_space)
        self.game_folder.mkdir(parents=True, exist_ok=True)
        if tasks:
            # 正式文件即将改变，旧安装标记不能继续宣称该档位完整。
            self._record_resource_pack(tier, self.resource_pack_version(tier) or version, incomplete=True)
            self._unregister_installed_tier(tier, cfg)
            failed = self._batch_download(tasks)
            for task in tasks:
                cache.clear(task["path"])
            if failed:
                cache.save()
                raise NetworkError(f"{failed} 个文件下载失败，{tier} 档位未完成；请重跑 ww resource {tier} 续传")

        # 对所有正式文件校验，确保下载器结果与清单一致后才清理、登记。
        for item in resources:
            path = self._managed_path(item["dest"])
            if not path.is_file() or path.stat().st_size != item["size"] or cache.get(path) != item["md5"]:
                self._unregister_installed_tier(tier, cfg)
                cache.save()
                raise NetworkError(f"档位文件校验失败: {item['dest']}")
        self._cleanup_tier_stale_files(tier, resources, cache)
        cache.save()
        self._register_installed_tier(tier, version, bundle)
        self._record_resource_pack(tier, version)
        state = self._read_resource_state()
        state["active_tier"] = tier
        self._write_local_json("wwm_resource_state.json", state)
        logger.info(f"{tier.upper()} 档位已验证并设为工具当前档位。游戏仍需以 -krqlv={tier} 启动。")

    def _cleanup_tier_stale_files(self, tier: str, resources: List[dict], cache: MD5Cache) -> None:
        """仅删除独立缓存里曾管理过的旧资源，保留用户自行放置的文件。"""
        expected = {i["dest"] for i in resources}
        prefix = f"Client/Content/{tier.upper()}/"
        for relative in list(cache.cache):
            if relative in expected or not relative.startswith(prefix):
                continue
            path = self._managed_path(relative)
            try:
                if path.is_file():
                    path.unlink()
                    logger.debug(f"已清理档位旧文件: {relative}")
                cache.clear(path)
            except OSError as e:
                raise WWError(f"清理档位旧文件失败 {relative}: {e}") from e

    def _register_installed_tier(self, tier: str, version: str, bundle: Dict[str, Any]) -> None:
        """全部校验成功后更新两处官方启动器记录，保留其它配置。"""
        cfg = self._read_local_config()
        packs = bundle.get("bundles", {}).get(tier.upper(), {}).get("resourcePacks") or [tier]
        cfg.setdefault("bundles", {})[tier.upper()] = {
            "version": version,
            "state": "",
            "resourcePacks": packs,
        }
        self._write_local_json(f"launcherDownloadConfig/{tier}.json", {"packName": tier, "version": version})
        self._write_local_json("launcherDownloadConfig.json", cfg)

    def _unregister_installed_tier(self, tier: str, cfg: Dict[str, Any]) -> None:
        """使残缺档位不再保留有效安装标记，不影响其它档位和基础版本。"""
        marker = self._managed_path(f"launcherDownloadConfig/{tier}.json")
        try:
            marker.unlink(missing_ok=True)
        except OSError as e:
            raise ConfigError(f"移除档位版本标记失败: {e}") from e
        if tier.upper() in cfg.get("bundles", {}):
            del cfg["bundles"][tier.upper()]
            self._write_local_json("launcherDownloadConfig.json", cfg)

    def resource_tier_status(self) -> List[dict]:
        """只读取本地登记与目录，不联网，也不把目录存在等同于完整安装。"""
        cfg = self._read_local_config()
        selection = self._read_resource_state()
        states = []
        for tier in self.RESOURCE_TIERS:
            record = cfg.get("bundles", {}).get(tier.upper(), {})
            marker = self._managed_path(f"launcherDownloadConfig/{tier}.json")
            directory = self._managed_path(f"Client/Content/{tier.upper()}")
            try:
                data = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            version = record.get("version", "") if isinstance(record, dict) else ""
            registered = bool(version and data.get("version") == version and data.get("packName") == tier)
            present = directory.is_dir() and any(directory.iterdir())
            state = (
                "已安装" if registered and present else ("未登记/不完整" if present or version else "未安装")
            )
            if tier in selection.get("incomplete", []):
                state = "更新未完成"
            states.append(
                {
                    "tier": tier,
                    "version": version,
                    "state": state,
                    "present": present,
                    "active": selection["active_tier"] == tier,
                }
            )
        return states

    def remove_resource_tier(self, tier: str) -> None:
        """删除用户明确选择的整个档位目录、独立缓存及两处安装记录。"""
        tier = self._validate_tier(tier)
        selection = self._read_resource_state()
        selection["versions"].pop(tier, None)
        selection["incomplete"] = [p for p in selection.get("incomplete", []) if p != tier]
        if selection["active_tier"] == tier:
            selection["active_tier"] = None
        self._write_local_json("wwm_resource_state.json", selection)
        cfg = self._read_local_config()
        directory = self._managed_path(f"Client/Content/{tier.upper()}")
        cache = self._managed_path(f"wwm_md5_cache_resource_{tier}.json")
        self._managed_path(f"launcherDownloadConfig/{tier}.json")
        # 先撤销完整安装标记，即使删除中途失败也不谎报完整。
        self._unregister_installed_tier(tier, cfg)
        try:
            if directory.exists():
                shutil.rmtree(directory)
            cache.unlink(missing_ok=True)
        except OSError as e:
            raise WWError(f"删除 {tier.upper()} 档位失败: {e}") from e
        logger.info(f"已移除 {tier.upper()} 档位；其它档位与基础资源未修改")

    def remind_resource_tiers(self) -> None:
        """仅提醒版本落后或更新中断的档位，同版本切服不重复警告。"""
        version = self._read_local_config().get("version")
        outdated = [
            state["tier"]
            for state in self.resource_tier_status()
            if (state["present"] or state["version"])
            and (state["version"] != version or state["state"] == "更新未完成")
        ]
        if outdated:
            logger.warning(
                f"档位 {', '.join(t.upper() for t in outdated)} 未确认与基础版本 {version} 同步；"
                "请用 ww resource 查看，ww resource <tier> 校验修复"
            )

    def _cleanup_stale_files(self, res_list, tier: str = "hd") -> None:
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

        removed = []
        for top in sorted(top_dirs):
            scan_root = self.game_folder / top
            if not scan_root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(scan_root):
                # 跳过隐藏目录与 Saved/ 目录（玩家截图/存档/运行时资源由游戏自身管理）
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "Saved"]
                relative = Path(dirpath).relative_to(self.game_folder)
                if tuple(p.lower() for p in relative.parts) == ("client", "content"):
                    # 未选中的画质包仍属于有效安装，不能当作过期公共文件清理。
                    dirnames[:] = [
                        d
                        for d in dirnames
                        if d.lower() not in self.RESOURCE_TIERS or (tier == "hd" and d.lower() == "hd")
                    ]
                for name in filenames:
                    if name.startswith(".") or name.endswith((".temp", ".bak", ".tmp")):
                        continue
                    full = Path(dirpath) / name
                    rel_str = full.relative_to(self.game_folder).as_posix()
                    if rel_str in expected_dests:
                        continue
                    # 只清理本工具曾管理过的文件（md5 缓存有记录），
                    # 不在缓存中的文件（运行时资源/用户文件）直接跳过
                    if rel_str in self.md5_cache.cache:
                        removed.append((full, rel_str))

        for full, rel_str in removed:
            try:
                full.unlink()
                self.md5_cache.clear(full)
                logger.debug(f"已清理已从 manifest 移除的旧文件: {rel_str}")
            except OSError as e:
                logger.warning(f"清理旧文件失败 {rel_str}: {e}")
        if removed:
            logger.info(f"清理完成: 共删除 {len(removed)} 个已废弃文件")

    def download_full(self, tier: str = "hd", force_space: bool = False):
        """按显式档位初始化，成功后记住选择供后续同步使用。"""
        tier = self._validate_tier(tier)
        logger.info(f"准备下载 {self.server_type} 服 {tier.upper()} 客户端到: {self.game_folder}")
        self.sync_files(force_check_md5=False, tier=tier, force_space=force_space)

        logger.info(f"{self.server_type} 服完整客户端下载完毕！")

    def checkout(self, target_server: str, force_sync: bool = False, force_space: bool = False):
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
                self.sync_files(force_check_md5=True, force_space=force_space)
            self._update_local_config()
            return

        if participate and not force_sync:
            self._checkout_via_cache(src, dst, src_ctx, dst_ctx, force_space=force_space)
        else:
            label = "非差异缓存适用(国际服)" if not participate else "强制同步"
            logger.info(f"{src} -> {dst}: {label}，执行全量校验同步...")
            self.sync_files(force_check_md5=True, force_space=force_space)
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

    def _checkout_via_cache(self, src: str, dst: str, src_ctx, dst_ctx, *, force_space: bool = False) -> None:
        same, diff_or_new, only_src = self._compute_diff_sets(src_ctx["resource"], dst_ctx["resource"])

        # 大版本更新判定：major.minor 变化则重置清空差异缓存
        major = parse_major_minor(dst_ctx["version"])
        manifest = self._load_cache_manifest()
        cached_major = tuple(manifest.get("major_minor") or ())
        reset_cache = bool(major and cached_major and tuple(major) != cached_major)
        dst_map = {item["dest"]: item for item in dst_ctx["resource"]}
        pending = []
        for dest in diff_or_new:
            item = dst_map[dest]
            cache = self._server_cache_path(dst, dest)
            if reset_cache or self._safe_md5(cache) != item["md5"]:
                pending.append({"path": self.game_folder / _resource_rel_path(dest), "size": int(item["size"])})
        # 在清缓存、备份或恢复第一个游戏文件之前拒绝明显不足的下载。
        self._check_download_space(pending, force_space)
        if reset_cache and major is not None:
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
        downloaded = self._download_diff_dst(dst, to_download, dst_ctx, force_space=force_space)

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
        try:
            os.remove(game_path)
        except OSError as e:
            raise WWError(f"隔离源服文件失败 {game_path}: {e}") from e
        self.md5_cache.clear(game_path)
        logger.debug(f"已隔离仅{src}存在的文件: {dest}")

    def _download_diff_dst(self, dst: str, to_download, dst_ctx, *, force_space: bool = False) -> int:
        """下载目标服差异文件到游戏目录并回填缓存。to_download: (dest, size, md5)。"""
        if not to_download:
            return 0
        total = sum(t[1] for t in to_download)
        logger.debug(f"待下载差异文件: {len(to_download)} 个，{total / 1024 / 1024:.2f} MiB")

        tasks = []
        for d, size, _md5 in to_download:
            url = urljoin(dst_ctx["cdn"], f"{dst_ctx['res_base']}/{d}")
            tasks.append(
                {
                    "url": quote(url, safe=":/"),
                    "path": self.game_folder / _resource_rel_path(d),
                    "size": size,
                    "md5": _md5,
                    "_dest": d,
                }
            )

        self._check_download_space(tasks, force_space)
        if self._batch_download(tasks):
            raise NetworkError("差异资源下载失败，未登记服务器切换；请重试")

        # 回填目标服缓存
        for t in tasks:
            d = t["_dest"]
            cache_path = self._server_cache_path(dst, d)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(t["path"], cache_path)
        return len(tasks)

    def _update_local_config(self, tier: Optional[str] = None):
        # 优先使用 launcher_info 中的版本，如果获取不到则保持原状或报错
        if self._launcher_info:
            v = self.launcher_info["default"]["version"]
            # 只更新基础版本字段，保留档位登记和用户自定义字段。
            cfg = self._read_local_config()
            cfg.update({"version": v, "appId": self.config["appId"], "group": "default"})
            self._write_local_json("launcherDownloadConfig.json", cfg)
            self._record_resource_pack("common", v)
            if (tier or self._read_resource_state()["active_tier"]) == "hd":
                self._record_resource_pack("hd", v)
            logger.info(f"本地配置已更新: {self.server_type} ({v})")

    def apply_incremental_update(self, dry_run: bool = False, force_space: bool = False) -> bool:
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
            force_space=force_space,
            space_check=self._check_download_space,
        )

        try:
            return manager.apply_incremental()
        except IncrementalError as e:
            logger.error(f"增量更新失败: {e}")
            return False

    def download_incremental(self, config_url: Optional[str] = None, force_space: bool = False) -> bool:
        """统一下载 common + 当前档位，未开放的分包渠道不降级到 HD。"""
        from .resource_updates import ResourceUpdater

        updater = ResourceUpdater(self)
        if self.server_type != "global" or updater.has_pending_plan():
            return updater.download(config_url, force_space=force_space)
        logger.info("国际服使用默认 HD 基础增量")
        manager = IncrementalManager(
            self.game_folder,
            self.server_type,
            self.launcher_info,
            self.cdn_node,
            force_space=force_space,
            space_check=self._check_download_space,
        )
        try:
            return manager.download_incremental()
        except IncrementalError as e:
            logger.error(f"增量包下载失败: {e}")
            return False

    def apply_incremental(self, force_space: bool = False) -> bool:
        """应用已固定的分包更新计划；回退下载也检查空间。"""
        from .resource_updates import ResourceUpdater

        updater = ResourceUpdater(self)
        if updater.has_pending_plan():
            return updater.apply(force_space=force_space)
        if self.current_resource_tier() != "hd":
            raise ConfigError("未找到当前档位的分包更新计划，请先运行 ww incremental")
        manager = IncrementalManager(
            self.game_folder,
            self.server_type,
            self.launcher_info,
            self.cdn_node,
            force_space=force_space,
            space_check=self._check_download_space,
        )
        try:
            success = manager.apply_incremental()
            if success:
                version = manager.get_current_version()
                if version:
                    self._record_resource_pack("common", version)
                    self._record_resource_pack("hd", version)
            return success
        except IncrementalError as e:
            logger.error(f"增量更新应用失败: {e}")
            return False

    def verify_new_version(self, force_space: bool = False) -> bool:
        """校验并修复已应用更新的文件，不混用其它档位索引。"""
        from .resource_updates import HISTORY, ResourceUpdater

        updater = ResourceUpdater(self)
        if updater.has_pending_plan() or self._managed_path(HISTORY).is_file():
            return updater.verify(force_space=force_space)
        if self.current_resource_tier() != "hd":
            raise ConfigError("没有所选档位的分包更新记录，请使用 ww resource <tier> 全量校验")
        manager = IncrementalManager(
            self.game_folder,
            self.server_type,
            self.launcher_info,
            self.cdn_node,
            force_space=force_space,
            space_check=self._check_download_space,
        )
        try:
            return manager.verify_new_version()
        except IncrementalError as e:
            logger.error(f"校验失败: {e}")
            return False
