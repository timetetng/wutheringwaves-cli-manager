# incremental.py
"""
增量更新模块：处理 krpdiff 增量包的下载和应用

使用方式与 predownload 类似：
1. ww incremental     - 提前下载增量包（仅在新版本预下载开放期间可用）
2. ww incremental --apply  - 应用已下载的增量包

支持 cn、global、bilibili 三个服务器
"""

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen

import requests
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

from ww_manager.download import (
    RAINBOW_COLORS,
    DownloadSpaceError,
    RainbowBarColumn,
    available_disk_space,
    cancel_downloads,
    check_space_requirement,
    create_download_progress,
    download_single_file,
)

logger = logging.getLogger("WW_Manager")

# 应用阶段同时运行的增量包数量：取 4 是因为单组已经是多线程读写，
# 再高只会让 md5 与写盘抢同一批带宽。
APPLY_MAX_WORKERS = 4


HPATCHZ_REPO = "timetetng/wutheringwaves-cli-manager"
HPATCHZ_VERSION = "hpatchz-v4.8.0"
HPATCHZ_FILENAME = "hpatchz.exe"

# 原生补丁器（native/krpatch）：官方 Release 只提供 Linux x86_64 产物。
KRPATCH_VERSION = "krpatch-v0.2.1"
KRPATCH_FILENAME = "krpatch-linux-x86_64"
KRPATCH_BIN_NAME = "krpatch"
KRPATCH_RECORD = "krpatch.version"
_KR_MACHINES = ("x86_64", "amd64")


class ApplySpinnerColumn(SpinnerColumn):
    """只为正在运行的增量包子任务显示 spinner。"""

    def render(self, task):
        if task.fields.get("kind") == "group":
            return super().render(task)
        return Text("")


class ApplyRainbowBarColumn(ProgressColumn):
    """总任务复用下载进度条，子任务只显示状态。"""

    def __init__(self, bar_width=40):
        super().__init__()
        self.bar = RainbowBarColumn(bar_width=bar_width)

    def render(self, task):
        if task.fields.get("kind") == "overall":
            return self.bar.render(task)
        return Text("")


class ApplyPercentageColumn(ProgressColumn):
    """只为总任务显示百分比。"""

    def render(self, task):
        if task.total is None:
            return Text("")
        percentage = task.percentage or 0
        return Text(f"{percentage:>3.1f}%", style="progress.percentage")


class ApplyCountColumn(ProgressColumn):
    """只为总任务显示 (完成数/总数)。"""

    def render(self, task):
        if task.total is None:
            return Text("")
        return Text(f"({int(task.completed)}/{int(task.total)})")


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


def _krpatch_supported() -> bool:
    """官方 Release 只发 Linux x86_64 一个产物。"""
    return platform.system() == "Linux" and platform.machine().lower() in _KR_MACHINES


def find_cached_krpatch(cache_dir: Path) -> Optional[Path]:
    """本地已有的原生补丁器；不联网。"""
    if os.environ.get("WW_KRPATCH"):
        override = Path(os.environ["WW_KRPATCH"]).expanduser()
        if override.is_file():
            return override
        logger.warning(f"WW_KRPATCH 指向的文件不存在: {override}")
    found = shutil.which(KRPATCH_BIN_NAME)
    if found:
        return Path(found)
    candidate = cache_dir / KRPATCH_BIN_NAME
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    except OSError as e:
        logger.debug(f"检查缓存的 krpatch 失败: {e}")
    return None


def get_krpatch_path(cache_dir: Path) -> Optional[Path]:
    """取原生补丁器：已缓存 → 直接用；否则从 Release 下载并校验 sha256。

    拿不到就返回 None，让调用方回退 wine + hpatchz.exe。
    """
    cached = find_cached_krpatch(cache_dir)
    record = cache_dir / KRPATCH_RECORD
    if cached is not None:
        if cached.parent != cache_dir or not record.exists():
            return cached
        if record.read_text(encoding="utf-8").strip() == KRPATCH_VERSION:
            return cached
        logger.info(f"krpatch 版本不同（缓存 {record.read_text().strip()}），尝试更新")

    if not _krpatch_supported():
        return cached

    base = f"https://github.com/{HPATCHZ_REPO}/releases/download/{KRPATCH_VERSION}"
    target = cache_dir / KRPATCH_BIN_NAME
    temp = cache_dir / f"{KRPATCH_BIN_NAME}.download"
    logger.info(f"正在下载 {KRPATCH_VERSION} 原生补丁器...")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with requests.get(f"{base}/{KRPATCH_FILENAME}", timeout=60, stream=True) as rsp:
            rsp.raise_for_status()
            digest = hashlib.sha256()
            with open(temp, "wb") as f:
                for chunk in rsp.iter_content(chunk_size=64 * 1024):
                    f.write(chunk)
                    digest.update(chunk)
        rsp = requests.get(f"{base}/{KRPATCH_FILENAME}.sha256", timeout=30)
        rsp.raise_for_status()
        expected = rsp.text.strip().split()[0].lower()
        if digest.hexdigest() != expected:
            raise IncrementalError(f"sha256 不符（{digest.hexdigest()[:16]}... != {expected[:16]}...）")
        os.chmod(temp, 0o755)
        os.replace(temp, target)
        record.write_text(KRPATCH_VERSION + "\n", encoding="utf-8")
        logger.info(f"krpatch 已就绪: {target}")
        return target
    except Exception as e:
        for path in (temp,):
            if path.exists():
                path.unlink()
        if cached is not None:
            logger.warning(f"krpatch 更新失败（{e}），继续使用已有版本: {cached}")
            return cached
        logger.warning(f"无法获取原生补丁器 krpatch（{e}），将回退 wine + hpatchz.exe")
        return None


def check_patch_applier(cache_dir: Path) -> Tuple[bool, str]:
    """检查有没有可用的补丁应用器（不联网）。"""
    if find_cached_krpatch(cache_dir) is not None:
        return True, ""
    if check_hpatchz_requirements()[0]:
        return True, ""
    return False, (
        "没有可用的增量补丁应用器。\n"
        "原生 krpatch 可自动下载（Linux x86_64，无需 wine）；\n"
        "或用 wine + hpatchz.exe：sudo pacman -S wine / sudo apt install wine。\n"
        "也可以使用 'ww sync' 进行全量同步。"
    )


def _http_get_json(url: str) -> Optional[Any]:
    """下载并解析 JSON"""
    try:
        headers = {"User-Agent": "WW-Manager/2.0"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
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
    hash_md5 = hashlib.md5(usedforsecurity=False)
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096 * 1024), b""):
                hash_md5.update(chunk)
    except OSError as e:
        raise IncrementalError(f"无法校验文件 {file_path}: {e}") from e
    return hash_md5.hexdigest()


def _is_diff_resource(dest: str) -> bool:
    """判断资源路径是否是增量文件"""
    return dest.lower().endswith((".krdiff", ".krpdiff"))


def _resource_rel_path(dest: str) -> Path:
    """将资源 dest 转为相对路径，避免意外的绝对路径拼接"""
    normalized = dest.replace("\\", "/").lstrip("/")
    rel_path = PurePosixPath(normalized)
    if not normalized or ".." in rel_path.parts or (rel_path.parts and rel_path.parts[0].endswith(":")):
        raise IncrementalError(f"非法资源路径: {dest}")
    return Path(*rel_path.parts)


def _backup_path(path: Path) -> Path:
    """获取替换文件时使用的备份路径"""
    return path.with_suffix(path.suffix + ".bak")


def _group_description(dest: Optional[str], stage: Optional[str] = None, color: str = "cyan") -> str:
    label = Path(dest).stem if dest else "unknown"
    if stage:
        return f"[{color}]{label}[/{color}] [dim]{stage}[/dim]"
    return f"[{color}]{label}[/{color}]"


class _GroupActivityTracker:
    """把工作线程上报的进度事件翻译成 Rich 子任务；多个分包共用一份，避免颜色与任务号冲突。"""

    def __init__(self, progress: Progress):
        self.progress = progress
        self.events: Queue = Queue()
        self.active: Dict[str, TaskID] = {}
        self.color_map: Dict[str, int] = {}
        self.counter = 0

    def push(self, kind: str, target: Optional[str], msg: Optional[str] = None) -> None:
        self.events.put((kind, target, msg))

    def _color(self, target: str) -> str:
        return RAINBOW_COLORS[self.color_map.get(target, 0) % len(RAINBOW_COLORS)]

    def drain(self) -> None:
        while True:
            try:
                kind, target, msg = self.events.get_nowait()
            except Empty:
                return
            if kind in ("start", "status"):
                task_id = self.active.get(target)
                if task_id is None:
                    self.color_map[target] = self.counter
                    self.counter += 1
                    color = self._color(target)
                    self.active[target] = self.progress.add_task(
                        _group_description(target, msg, color), total=None, kind="group"
                    )
                else:
                    self.progress.update(task_id, description=_group_description(target, msg, self._color(target)))
            elif kind == "log":
                _emit_progress_log(self.progress, target, msg)
            elif kind == "finish":
                task_id = self.active.pop(target, None)
                self.color_map.pop(target, None)
                if task_id is not None:
                    self.progress.remove_task(task_id)

    def close(self) -> None:
        """兜底清理仍显示的子任务，避免异常路径残留。"""
        self.drain()
        for task_id in list(self.active.values()):
            self.progress.remove_task(task_id)
        self.active.clear()
        self.color_map.clear()


class ApplyStats:
    """一次 apply 的合并统计。多个包共用一份，结束时只输出一次。"""

    def __init__(self) -> None:
        self.packs: List[str] = []
        self.groups = 0
        self.success = 0
        self.skipped = 0
        self.failed = 0
        self.failed_groups: List[str] = []
        self.moved = 0
        self.reused = 0
        self._started = time.monotonic()

    def add(self, label: Optional[str], results: Dict[str, str]) -> None:
        if label and label not in self.packs:
            self.packs.append(label)
        self.groups += len(results)
        self.success += sum(1 for r in results.values() if r == "success")
        self.skipped += sum(1 for r in results.values() if r == "skipped")
        failed = [dest for dest, r in results.items() if r in ("failed", "partial_failed")]
        self.failed += len(failed)
        self.failed_groups += [f"{label}:{dest}" if label else dest for dest in failed]

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def describe(self) -> str:
        scope = " + ".join(self.packs) if self.packs else "增量包"
        if self.groups:
            merged = f"合并 成功={self.success} 跳过={self.skipped} 失败={self.failed}（{self.groups} 组）"
        else:
            merged = "无差分合并组"
        migrated = f"迁移 更新={self.moved} 已是目标版本={self.reused}"
        return f"{scope} 增量完成: {merged}；{migrated}（用时 {self.elapsed:.1f}s）"


def report_apply(stats: ApplyStats) -> None:
    """整次 apply 只打印一份统计；common 与画质包合并计数。"""
    logger.info(stats.describe())
    if stats.failed:
        logger.warning(f"失败组: {stats.failed_groups}")
        logger.warning(f"有 {stats.failed} 个增量包应用存在失败。请运行 'ww sync' 修复。")
        return
    logger.info("增量更新应用完成。建议运行 'ww sync --new' 做最终校验。")


def create_apply_progress() -> Progress:
    """apply 阶段的统一进度条；多个包共用同一个实例。"""
    return Progress(
        ApplySpinnerColumn("dots"),
        TextColumn("{task.description}"),
        ApplyRainbowBarColumn(bar_width=40),
        ApplyPercentageColumn(),
        ApplyCountColumn(),
        TimeElapsedColumn(),
        transient=False,
        expand=True,
    )


def _format_progress_log(level: str, msg: str) -> str:
    """生成 Rich Progress 中手动输出日志的统一格式。"""
    return f"{time.strftime('%H:%M:%S')} [{level}] {msg}"


def _emit_progress_log(progress: Optional[Progress], level: str, msg: str) -> None:
    """在 Rich Progress 上方输出带时间和级别的日志；无 Progress 时回退到 logging。"""
    style_map = {"INFO": "dim", "WARNING": "yellow", "ERROR": "red"}
    if progress:
        progress.console.print(_format_progress_log(level, msg), style=style_map.get(level))
        return

    if level == "ERROR":
        logger.error(msg)
    elif level == "WARNING":
        logger.warning(msg)
    else:
        logger.info(msg)


def _safe_replace_file(src: Path, dest: Path, error_callback: Optional[Callable[[str], None]] = None) -> bool:
    """用已校验文件替换目标文件，失败时尽量恢复原文件"""
    backup = _backup_path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            backup.unlink()
        if dest.exists():
            shutil.move(dest, backup)
        shutil.move(src, dest)
        if backup.exists():
            backup.unlink()
        return True
    except Exception as e:
        message = f"替换文件失败: {dest} ({e})"
        if error_callback:
            error_callback(message)
        else:
            logger.error(message)
        try:
            if dest.exists() and backup.exists():
                dest.unlink()
            if backup.exists() and not dest.exists():
                shutil.move(backup, dest)
        except Exception as restore_error:
            message = f"恢复备份失败: {dest} ({restore_error})"
            if error_callback:
                error_callback(message)
            else:
                logger.error(message)
        return False


class IncrementalManager:
    """统一的增量更新管理器，支持 cn、global、bilibili"""

    def __init__(
        self,
        game_folder: Path,
        server_type: str,
        api_data: Dict,
        cdn_base: str,
        pack_name: Optional[str] = None,
        *,
        force_space: bool = False,
        space_check: Optional[Callable[[List[dict], bool], None]] = None,
    ):
        self.game_folder = game_folder
        self.server_type = server_type
        self.cdn_base = cdn_base.rstrip("/")
        self.api_data = api_data
        self.force_space = force_space
        self._space_check = space_check
        self.cache_dir = game_folder / ".ww_manager_cache"
        self.download_dir = game_folder / ".incremental_download"
        if pack_name is not None:
            if pack_name not in ("common", "hd", "sd", "uhd"):
                raise IncrementalError(f"非法资源包名称: {pack_name}")
            self.cache_dir = self.cache_dir / "packs" / pack_name
            self.download_dir = self.download_dir / "packs" / pack_name
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.hpatchz_path: Optional[Path] = None
        # "krpatch"（原生）或 "hpatchz"（wine）；由 apply_into 决定。
        self.krpatch_path: Optional[Path] = None
        self.applier: Optional[str] = None
        self._index_file: Optional[Dict] = None
        self._verified_staged_patch_dests: set[str] = set()
        self._verified_staged_patch_lock = threading.Lock()

    @property
    def default_config(self) -> Dict:
        """获取默认配置（始终可用）"""
        return (self.api_data.get("default") or {}).get("config", {})

    @property
    def predownload_config(self) -> Dict:
        """获取预下载配置（仅在预下载开放期间存在）。

        真实 3.8 渠道 index 把 ``predownload`` 写成显式 null，
        不能靠 ``get(key, {})`` 的默认值兜底。
        """
        return (self.api_data.get("predownload") or {}).get("config", {})

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

    def _get_all_patch_configs(self) -> List[Dict]:
        """获取所有 patchConfig，合并 predownload 和 default 两个来源"""
        configs = []
        predownload_configs = self.predownload_config.get("patchConfig", [])
        if predownload_configs:
            configs.extend(predownload_configs)
            logger.debug(
                f"从 predownload.config 获取 {len(predownload_configs)} 个 patchConfig: "
                f"{[p.get('version') for p in predownload_configs[:5]]}..."
            )
        default_configs = self.default_config.get("patchConfig", [])
        if default_configs:
            # 避免重复（以 predownload 的为准）
            predownload_versions = {p.get("version") for p in predownload_configs}
            new_from_default = [p for p in default_configs if p.get("version") not in predownload_versions]
            configs.extend(new_from_default)
            logger.debug(
                f"从 default.config 获取 {len(default_configs)} 个 patchConfig，去重后新增 {len(new_from_default)} 个"
            )
        return configs

    def _find_matching_patch(self) -> Optional[Dict]:
        """从 patchConfig 中找到匹配当前版本的增量配置（合并 predownload 和 default 来源）"""
        patch_configs = self._get_all_patch_configs()
        if not patch_configs:
            logger.debug("patchConfig 为空（predownload 和 default 均无 patchConfig）")
            return None

        current_version = self.get_current_version()
        if not current_version:
            logger.debug("无法获取当前游戏版本（launcherDownloadConfig.json 不存在或无效）")
            return None

        for patch in patch_configs:
            if patch.get("version") == current_version:
                logger.debug(
                    f"匹配到增量配置: version={current_version}, "
                    f"indexFile={patch.get('indexFile', 'N/A')[:80]}, "
                    f"baseUrl={patch.get('baseUrl', 'N/A')}, "
                    f"size={patch.get('size', 0) / 1024 / 1024:.0f}MB"
                )
                return patch

        available = [p.get("version") for p in patch_configs]
        logger.debug(
            f"当前版本 {current_version} 不在 {len(available)} 个 patchConfig 中，"
            f"可用版本范围: {available[0]} ~ {available[-1]}"
        )
        return None

    def _build_index_url(self, patch_info: Dict) -> Optional[str]:
        """构建 indexFile URL"""
        index_file_path = patch_info.get("indexFile")
        if not index_file_path:
            logger.debug("patch_info 中缺少 indexFile 字段")
            return None
        url = f"{self.cdn_base}/{index_file_path.lstrip('/')}"
        logger.debug(f"indexFile URL: {url}")
        return url

    def _build_krpdiff_url(self, krpdiff_name: str) -> str:
        """构建 krpdiff 下载 URL"""
        patch_info = self._find_matching_patch()
        if patch_info:
            base_url = patch_info.get("baseUrl", "")
            if base_url:
                url = f"{self.cdn_base}/{base_url.strip('/')}/{quote(krpdiff_name, safe='/:')}"
                logger.debug(f"krpdiff URL (patch): {url}")
                return url

        # 回退到 default config 的 baseUrl
        default_base = self.default_config.get("baseUrl", "")
        if default_base:
            url = f"{self.cdn_base}/{default_base.strip('/')}/{quote(krpdiff_name, safe='/:')}"
            logger.debug(f"krpdiff URL (default): {url}")
            return url
        logger.debug(f"krpdiff URL (fallback): {self.cdn_base}/resources/{krpdiff_name}")
        return f"{self.cdn_base}/resources/{quote(krpdiff_name, safe='/:')}"

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
        """获取目标版本（优先 predownload，回退 default）"""
        return self.predownload_config.get("version") or self.default_config.get("version", "")

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
        """输出总量，用作“大组先跑”的排序键。

        真实 index 的 groupInfos 没有 size 字段，只看 dstFiles[0] 的话，多文件组的
        首个文件往往只有 0.1 MiB，1–2 GiB 的组会被排到最后，收尾时只剩它单独在跑。
        """
        if group.get("size"):
            return int(group["size"])
        for key in ("dstFiles", "srcFiles"):
            total = sum(int(f.get("size") or 0) for f in group.get(key) or [])
            if total:
                return total
        return 0

    def _get_resource_base_for_item(self, item: Dict, fallback_base: Optional[str] = None) -> Optional[str]:
        """获取单个资源文件的下载路径前缀，优先使用资源自己的 fromFolder"""
        item_base = item.get("fromFolder")
        if item_base:
            return item_base.rstrip("/")
        if fallback_base:
            return fallback_base.rstrip("/")
        return self._find_resource_base()

    def _build_resource_url(self, item: Dict, fallback_base: Optional[str] = None) -> Optional[str]:
        """构建完整资源文件下载 URL"""
        res_base = self._get_resource_base_for_item(item, fallback_base)
        if not res_base:
            return None
        return f"{self.cdn_base}/{res_base}/{quote(item['dest'], safe='/:')}"

    def _full_resource_base(self) -> Optional[str]:
        """完整资源文件所在的全量目录。

        3.8 的 index 里补丁 baseUrl 是 `<pack>/<ver>/resources`（只有 krpdiff），
        完整文件在 `<ver>/zip/`；旧 schema 两者的 baseUrl 同构，所以以前没暴露。
        """
        base = self.default_config.get("baseUrl")
        if base:
            return base.rstrip("/")
        for path in (self.download_dir / "indexFile.json", self.cache_dir / "indexFile.json"):
            if not path.exists():
                continue
            try:
                resources = json.loads(path.read_text()).get("resource", [])
            except Exception:
                continue
            if resources:
                from_folder = resources[0].get("fromFolder", "")
                if from_folder:
                    return from_folder.rstrip("/")
        return self._find_resource_base()

    def _get_staged_complete_path(self, download_dir: Path, dest: str) -> Path:
        """获取完整资源在增量下载目录中的缓存路径"""
        return download_dir / "resources" / _resource_rel_path(dest)

    def _get_staged_patch_path(self, download_dir: Path, dest: str) -> Path:
        """获取增量包输出或回退下载文件的缓存路径"""
        return download_dir / "krpdiff_temp" / _resource_rel_path(dest)

    def _get_fallback_download_path(self, dest: str) -> Path:
        """获取回退完整下载使用的临时缓存路径"""
        return self.cache_dir / "full_download_temp" / _resource_rel_path(dest)

    def _get_repair_download_path(self, dest: str) -> Path:
        """获取校验修复下载使用的临时缓存路径"""
        return self.cache_dir / "repair_download_temp" / _resource_rel_path(dest)

    def _is_file_verified(self, path: Path, expected_size: int, expected_md5: str) -> bool:
        """检查本地文件是否符合预期大小和 MD5"""
        if not path.exists() or path.stat().st_size != expected_size:
            return False
        return _calculate_file_md5(path) == expected_md5

    def _mark_staged_patch_verified(self, dest: str) -> None:
        """记录本次 apply 过程中已经完成 MD5 校验的增量输出缓存。"""
        with self._verified_staged_patch_lock:
            self._verified_staged_patch_dests.add(dest)

    def _is_staged_patch_verified_in_this_run(self, dest: str, staged_path: Path, expected_size: int) -> bool:
        """判断 staged patch 是否已在本次 apply 中校验过，可避免迁移阶段重复读大文件算 MD5。"""
        with self._verified_staged_patch_lock:
            verified = dest in self._verified_staged_patch_dests
        return verified and staged_path.exists() and staged_path.stat().st_size == expected_size

    def check_download_space(self, tasks: List[dict]) -> None:
        if not tasks:
            return
        if self._space_check is not None:
            self._space_check(tasks, self.force_space)
            return
        try:
            check_space_requirement(tasks, available_disk_space(self.game_folder), self.force_space)
        except DownloadSpaceError as e:
            raise IncrementalError(str(e)) from e

    def pending_download_tasks(self) -> List[dict]:
        """只读预检待传输资源；供多包合计检查和国际服下载复用。"""
        if self._index_file is None:
            url = self.get_index_url()
            self._index_file = _http_get_json(url) if url else None
        if not self._index_file:
            raise IncrementalError("无法获取增量更新索引，未开始下载")
        resources = self._index_file.get("resource", [])
        by_dest = {item["dest"]: item for item in resources if item.get("dest")}
        tasks = []
        for group in self.get_group_infos():
            dest = group.get("dest")
            if not dest:
                continue
            item = by_dest.get(dest, {})
            size = int(item.get("size") or 0)
            if not size:
                size = _get_krpdiff_size(self._build_krpdiff_url(dest)) or 0
            path = self.download_dir / _resource_rel_path(dest)
            md5 = item.get("md5")
            if path.is_file() and (not size or path.stat().st_size == size):
                if md5 and _calculate_file_md5(path) == md5:
                    continue
                if size and not md5:
                    continue
            tasks.append({"path": path, "size": size or None})
        for item in resources:
            if _is_diff_resource(item["dest"]):
                continue
            size, md5 = int(item["size"]), item["md5"]
            game_path = self.game_folder / _resource_rel_path(item["dest"])
            staged = self._get_staged_complete_path(self.download_dir, item["dest"])
            if not self._is_file_verified(game_path, size, md5) and not self._is_file_verified(staged, size, md5):
                tasks.append({"path": staged, "size": size})
        return tasks

    def plan_download(self) -> Optional[Dict[str, Any]]:
        """检查本地缓存、登记下载记录并返回待下载作业；不启动任何下载线程。

        作业清单可以交给 DownloadBatch，把 common 与画质包合并成一条进度条下载。
        返回 None 表示索引或缓存检查不通过，调用方不应继续下载。
        """
        current_version = self.get_current_version()
        if not current_version:
            logger.error("无法获取当前版本")
            return None

        index_url = self.get_index_url()
        if not index_url:
            logger.error(
                "无法获取增量更新索引URL。可能原因：\n"
                "  1. 当前版本不在 patchConfig 中（版本太旧或太新）\n"
                "  2. 网络连接异常\n"
                "  请先运行 'ww sync' 更新到最新版本后再试。"
            )
            return None

        logger.info(f"正在获取增量更新信息: {current_version}")

        index_data = self._index_file if self._index_file is not None else _http_get_json(index_url)
        if not index_data:
            logger.error("获取增量更新索引失败")
            return None

        self._index_file = index_data

        group_infos = self.get_group_infos()
        # 分包热修复允许空清单，也允许只有完整文件或删除项而没有二进制差分组。
        logger.debug(f"找到 {len(group_infos)} 个增量更新组")

        # 在写下载记录、清理失效缓存或启动任意下载线程前检查整批资源。
        self.check_download_space(self.pending_download_tasks())
        download_dir = self.download_dir
        download_dir.mkdir(parents=True, exist_ok=True)

        version_info = {
            "from_version": current_version,
            "to_version": self.get_target_version(),
            "server": self.server_type,
        }
        try:
            (download_dir / "indexFile.json").write_text(json.dumps(index_data, indent=2), encoding="utf-8")
            (download_dir / "version_info.json").write_text(json.dumps(version_info, indent=4), encoding="utf-8")
        except OSError as e:
            logger.error(f"无法保存增量下载记录: {e}")
            return None

        resources = index_data.get("resource", [])
        resource_by_dest = {r["dest"]: r for r in resources if r.get("dest")}
        complete_files = [r for r in resources if not _is_diff_resource(r["dest"])]
        res_base = self._full_resource_base()
        if not res_base:
            logger.debug("无法确定全量资源目录，将逐个使用资源的 fromFolder")

        logger.debug("正在检查本地增量更新资源缓存...")

        pending_groups = []
        skipped_groups = []
        total_size = 0
        pending_size = 0

        for group in group_infos:
            krpdiff_name = group.get("dest")
            if not krpdiff_name:
                continue
            krpdiff_path = download_dir / _resource_rel_path(krpdiff_name)
            krpdiff_url = self._build_krpdiff_url(krpdiff_name)
            krpdiff_info = resource_by_dest.get(krpdiff_name, {})
            expected_size = int(krpdiff_info.get("size") or 0)
            expected_md5 = krpdiff_info.get("md5")
            if not expected_size:
                expected_size = _get_krpdiff_size(krpdiff_url) or 0

            if krpdiff_path.exists() and expected_size:
                local_size = krpdiff_path.stat().st_size
                if local_size == expected_size and (
                    not expected_md5 or _calculate_file_md5(krpdiff_path) == expected_md5
                ):
                    logger.debug(f"跳过已完成的增量包: {krpdiff_name}")
                    skipped_groups.append(krpdiff_name)
                    continue
                elif local_size < expected_size:
                    logger.info(f"检测到不完整的增量包，将重新下载: {krpdiff_name}")
                    krpdiff_path.unlink()
                else:
                    logger.info(f"文件大小或 MD5 异常，重新下载: {krpdiff_name}")
                    krpdiff_path.unlink()
            elif krpdiff_path.exists() and expected_md5:
                if _calculate_file_md5(krpdiff_path) == expected_md5:
                    logger.debug(f"跳过已完成的增量包: {krpdiff_name}")
                    skipped_groups.append(krpdiff_name)
                    continue
                logger.info(f"增量包 MD5 异常，重新下载: {krpdiff_name}")
                krpdiff_path.unlink()

            pending_groups.append((group, krpdiff_path, krpdiff_url, expected_size, expected_md5))
            if expected_size:
                resume_size = 0
                temp_path = krpdiff_path.with_suffix(krpdiff_path.suffix + ".temp")
                if temp_path.exists():
                    temp_size = temp_path.stat().st_size
                    if temp_size < expected_size:
                        resume_size = temp_size
                    elif temp_size == expected_size and (
                        not expected_md5 or _calculate_file_md5(temp_path) == expected_md5
                    ):
                        resume_size = temp_size
                    else:
                        logger.info(f"临时增量包大小或 MD5 异常，将重新下载: {krpdiff_name}")
                        temp_path.unlink()
                pending_size += expected_size - resume_size

        total_size = sum(item[3] for item in pending_groups)

        complete_tasks = []
        complete_ready_count = 0
        complete_total_count = len(complete_files)
        if complete_files:
            for item in complete_files:
                expected_size = int(item["size"])
                expected_md5 = item["md5"]
                game_path = self.game_folder / _resource_rel_path(item["dest"])
                staged_path = self._get_staged_complete_path(download_dir, item["dest"])

                if self._is_file_verified(game_path, expected_size, expected_md5):
                    complete_ready_count += 1
                    continue
                if self._is_file_verified(staged_path, expected_size, expected_md5):
                    complete_ready_count += 1
                    continue
                if staged_path.exists():
                    logger.info(f"缓存完整文件校验失败，将重新下载: {item['dest']}")
                    staged_path.unlink()

                url = self._build_resource_url(item, res_base)
                if not url:
                    logger.error(f"无法构建完整文件下载 URL: {item['dest']}")
                    return None
                complete_tasks.append(
                    {
                        "url": url,
                        "path": staged_path,
                        "size": expected_size,
                        "dest": item["dest"],
                        "md5": expected_md5,
                    }
                )
            if complete_tasks:
                complete_total = sum(t["size"] for t in complete_tasks)
                total_size += complete_total

        return {
            "patch_jobs": pending_groups,
            "complete_jobs": complete_tasks,
            "patch_total": len(group_infos),
            "patch_skipped": len(skipped_groups),
            "complete_total": complete_total_count,
            "complete_ready": complete_ready_count,
            "pending_patch_bytes": pending_size,
            "pending_complete_bytes": sum(t["size"] for t in complete_tasks),
            "total_bytes": total_size,
        }

    def download_incremental(self) -> bool:
        """单个分包自成一批下载（旧调用方与测试仍用这个入口）。"""
        plan = self.plan_download()
        if plan is None:
            return False
        return DownloadBatch([self], [plan]).run()

    def _download_file(
        self,
        url: str,
        dest: Path,
        expected_size: int,
        expected_md5: Optional[str] = None,
        progress: Optional[Progress] = None,
        overall_task_id: Optional[TaskID] = None,
    ) -> bool:
        """复用通用下载器；expected_size <= 0 表示大小未知。"""
        self.check_download_space([{"path": dest, "size": expected_size or None}])
        return download_single_file(url, dest, expected_size, progress, overall_task_id, expected_md5=expected_md5)

    def _apply_staged_complete_files(
        self,
        download_dir: Path,
        *,
        progress: Optional[Progress] = None,
        stats: Optional["ApplyStats"] = None,
        label: Optional[str] = None,
    ) -> bool:
        """将下载阶段缓存的完整资源文件迁移到游戏目录；传入 progress 时并入同一个进度条。"""
        if progress is None:
            with create_apply_progress() as own_progress:
                return self._apply_staged_complete_files(
                    download_dir, progress=own_progress, stats=stats or ApplyStats(), label=label
                )
        stats = stats or ApplyStats()
        tag = f"[{label}] " if label else ""
        resources = (self.index_file or {}).get("resource", [])
        complete_files = [r for r in resources if not _is_diff_resource(r["dest"])]
        if not complete_files:
            return True

        moved_count = 0
        skipped_count = 0
        failed = []

        logger.info(f"{tag}开始迁移并校验完整资源文件: 共 {len(complete_files)} 个")
        task_id = progress.add_task(f"[bold cyan]{tag}迁移完整资源", total=len(complete_files))

        try:
            for item in complete_files:
                dest = item["dest"]
                progress.update(task_id, description=f"[bold cyan]{tag}迁移完整资源: {Path(dest).name[:60]}")

                expected_size = int(item["size"])
                expected_md5 = item["md5"]
                game_path = self.game_folder / _resource_rel_path(dest)
                staged_path = self._get_staged_complete_path(download_dir, dest)

                try:
                    if self._is_file_verified(staged_path, expected_size, expected_md5):
                        if _safe_replace_file(
                            staged_path,
                            game_path,
                            lambda msg: _emit_progress_log(progress, "ERROR", msg),
                        ):
                            moved_count += 1
                        else:
                            failed.append(dest)
                        continue

                    if self._is_file_verified(game_path, expected_size, expected_md5):
                        skipped_count += 1
                        continue

                    _emit_progress_log(progress, "ERROR", f"完整文件缓存缺失或校验失败: {dest}")
                    failed.append(dest)
                finally:
                    progress.advance(task_id)
        finally:
            progress.remove_task(task_id)

        if failed:
            logger.error(f"{tag}完整文件迁移失败: {failed}")
            return False

        stats.moved += moved_count
        stats.reused += skipped_count
        logger.debug(f"{tag}完整文件迁移完成: 更新={moved_count}, 已是目标版本={skipped_count}")
        return True

    def _apply_delete_files(self, delete_files: List[str]) -> int:
        """按清单删除补丁。

        indexFile.json 的 deleteFiles 字段明确列出应删除的旧资源
        残留会导致 UE 挂载覆盖新文件、热更卡死
        """
        removed = 0
        failed = 0
        for dest in delete_files or []:
            try:
                rel = _resource_rel_path(dest)
            except IncrementalError:
                logger.debug(f"跳过非法 deleteFiles 路径: {dest}")
                continue
            full = self.game_folder / rel
            if not full.exists():
                logger.debug(f"deleteFiles 目标不存在，跳过: {dest}")
                continue
            if full.is_dir():
                logger.debug(f"跳过 deleteFiles 目录: {dest}")
                continue
            try:
                full.unlink()
                removed += 1
            except OSError as e:
                failed += 1
                logger.debug(f"删除废弃文件失败 {dest}: {e}")
        if removed:
            logger.warning(f"已清理 {removed} 个废弃补丁")
        if failed:
            logger.warning(f"{failed} 个废弃补丁删除失败")
        return removed

    def _apply_staged_patch_files(
        self,
        download_dir: Path,
        *,
        progress: Optional[Progress] = None,
        stats: Optional["ApplyStats"] = None,
        label: Optional[str] = None,
    ) -> bool:
        """将已校验的增量输出文件统一迁移到游戏目录；传入 progress 时并入同一个进度条。"""
        if progress is None:
            with create_apply_progress() as own_progress:
                return self._apply_staged_patch_files(
                    download_dir, progress=own_progress, stats=stats or ApplyStats(), label=label
                )
        stats = stats or ApplyStats()
        tag = f"[{label}] " if label else ""
        resources = (self.index_file or {}).get("resource", [])
        complete_dests = {r["dest"] for r in resources if r.get("dest") and not _is_diff_resource(r["dest"])}

        patch_targets = []
        for group in self.get_group_infos():
            for dst_info in group.get("dstFiles", []):
                dest = dst_info["dest"]
                if dest in complete_dests:
                    # 官方逻辑中普通资源文件优先，patch 输出不覆盖这些完整资源。
                    continue
                patch_targets.append(dst_info)

        moved_count = 0
        skipped_count = 0
        failed = []

        if not patch_targets:
            logger.info(f"{tag}没有需要迁移的增量输出文件")
            return True

        logger.info(f"{tag}开始覆盖并校验增量输出文件: 共 {len(patch_targets)} 个")
        task_id = progress.add_task(f"[bold cyan]{tag}覆盖并校验增量输出", total=len(patch_targets))

        try:
            for dst_info in patch_targets:
                dest = dst_info["dest"]
                progress.update(task_id, description=f"[bold cyan]{tag}覆盖/校验: {Path(dest).name[:60]}")

                expected_size = int(dst_info["size"])
                expected_md5 = dst_info["md5"]
                game_path = self.game_folder / _resource_rel_path(dest)
                staged_path = self._get_staged_patch_path(download_dir, dest)

                try:
                    if self._is_staged_patch_verified_in_this_run(dest, staged_path, expected_size):
                        if _safe_replace_file(
                            staged_path,
                            game_path,
                            lambda msg: _emit_progress_log(progress, "ERROR", msg),
                        ):
                            moved_count += 1
                        else:
                            failed.append(dest)
                        continue

                    if self._is_file_verified(staged_path, expected_size, expected_md5):
                        if _safe_replace_file(
                            staged_path,
                            game_path,
                            lambda msg: _emit_progress_log(progress, "ERROR", msg),
                        ):
                            moved_count += 1
                        else:
                            failed.append(dest)
                        continue

                    if self._is_file_verified(game_path, expected_size, expected_md5):
                        skipped_count += 1
                        continue

                    _emit_progress_log(progress, "ERROR", f"增量输出缓存缺失或校验失败: {dest}")
                    failed.append(dest)
                finally:
                    progress.advance(task_id)
        finally:
            progress.remove_task(task_id)

        if failed:
            logger.error(f"{tag}增量输出迁移失败: {failed}")
            return False

        stats.moved += moved_count
        stats.reused += skipped_count
        logger.debug(f"{tag}增量输出迁移完成: 更新={moved_count}, 已是目标版本={skipped_count}")
        return True

    def apply_incremental(self) -> bool:
        """应用已下载的增量更新包；单包入口，自带进度条与一份统计。"""
        stats = ApplyStats()
        progress = create_apply_progress()
        with progress:
            ok = self.apply_into(progress, stats)
        report_apply(stats)
        return ok

    def prepare_apply(self, label: Optional[str] = None) -> Optional[List[Dict]]:
        """检查下载缓存与索引、选好补丁器，返回按输出总量降序排列的 group 列表。

        返回 None 表示本包当前不能应用（缺缓存、版本或服务器不符）；此时不应写任何游戏文件。
        """
        tag = f"[{label}] " if label else ""
        logger.info(f"{tag}正在准备应用增量更新...")
        download_dir = self.download_dir
        version_file = download_dir / "version_info.json"

        if not download_dir.exists() or not version_file.exists():
            logger.error(f"{tag}未找到增量更新包，请先执行 'ww incremental' 下载")
            return None

        try:
            version_info = json.loads(version_file.read_text())
        except Exception:
            logger.error(f"{tag}增量更新版本信息损坏")
            return None

        if version_info.get("server") != self.server_type:
            logger.error(f"{tag}服务器不匹配: 预期 {version_info.get('server')}, 实际 {self.server_type}")
            return None

        logger.info(f"{tag}开始应用增量更新: {version_info.get('from_version')} -> {version_info.get('to_version')}")
        index_file = download_dir / "indexFile.json"
        if not index_file.exists():
            logger.error(f"{tag}无法找到 indexFile.json")
            return None

        try:
            self._index_file = json.loads(index_file.read_text())
        except Exception:
            logger.error(f"{tag}无法解析 indexFile.json")
            return None

        self._apply_version_info = version_info
        group_infos = self.get_group_infos()
        if group_infos:
            self.applier = self._select_applier()
            if self.applier == "krpatch":
                logger.info(f"{tag}使用原生补丁器 krpatch: {self.krpatch_path}")
            elif self.applier == "hpatchz":
                is_ok, error_msg = check_hpatchz_requirements()
                if not is_ok:
                    raise IncrementalError(error_msg)
                logger.info(f"{tag}正在准备 hpatchz 工具...")
                self.hpatchz_path = get_hpatchz_path(self.cache_dir)
            else:
                raise IncrementalError(check_patch_applier(self.cache_dir)[1])

        # 大文件优先：按 group 输出总量降序，让最大的先启动。
        return sorted(group_infos, key=lambda g: self._get_group_size(g), reverse=True)

    def run_apply_jobs(
        self,
        jobs: List[tuple],
        progress: Progress,
        tracker: "_GroupActivityTracker",
        overall_task: Optional[TaskID] = None,
        tag: str = "",
    ) -> Dict[int, Dict[str, str]]:
        """在共享进度条上应用 (engine, group) 作业，返回按引擎聚合的结果。

        作业列表可以同时包含 common 与画质包：排序由调用方负责，这里只调度。
        """
        results: Dict[int, Dict[str, str]] = {}
        if not jobs:
            return results
        max_workers = max(1, min(APPLY_MAX_WORKERS, len(jobs)))
        own_task = overall_task is None
        if own_task:
            overall_task = progress.add_task(f"{tag}[bold cyan]应用增量包", total=len(jobs), kind="overall")

        def apply_wrapper(engine: "IncrementalManager", group: Dict) -> tuple:
            dest = group.get("dest")

            def emit(kind: str, msg: Optional[str] = None):
                tracker.push(kind, dest, msg)

            def emit_log(level: str, msg: str):
                tracker.push("log", level, msg)

            def update_status(msg: str):
                emit("status", msg)

            def log_info(_msg):
                # 普通成功日志不在 Rich 进度条期间输出，避免刷屏。
                pass

            def log_warning(msg):
                emit_log("WARNING", msg)

            def log_error(msg):
                emit_log("ERROR", msg)

            emit("start", "开始处理")
            try:
                group_dest, result = engine._apply_single_group(
                    group,
                    engine.download_dir,
                    log_info,
                    log_warning,
                    log_error,
                    update_status,
                )
                return engine, group_dest, result
            except Exception as e:
                log_error(f"应用增量包异常: {e}")
                return engine, dest, "failed"
            finally:
                emit("finish")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            pending = {executor.submit(apply_wrapper, engine, group): (engine, group) for engine, group in jobs}
            while pending:
                done, pending_futures = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                pending = pending_futures
                tracker.drain()

                for future in done:
                    engine, dest, result = future.result()
                    results.setdefault(id(engine), {})[dest] = result
                    progress.update(overall_task, advance=1)

                tracker.drain()

        if own_task:
            progress.remove_task(overall_task)
        return results

    def migrate_outputs(self, progress: Progress, stats: "ApplyStats", label: Optional[str] = None) -> bool:
        """迁移已校验的输出、删除废弃补丁、登记版本并清理下载目录。"""
        tag = f"[{label}] " if label else ""
        download_dir = self.download_dir
        index_file = download_dir / "indexFile.json"

        if not self._apply_staged_patch_files(download_dir, progress=progress, stats=stats, label=label):
            logger.warning(f"{tag}增量输出迁移失败，请运行 'ww sync' 修复。")
            return False

        if not self._apply_staged_complete_files(download_dir, progress=progress, stats=stats, label=label):
            logger.warning(f"{tag}完整文件迁移失败，请运行 'ww sync' 修复。")
            return False

        delete_files = (self.index_file or {}).get("deleteFiles", [])
        if delete_files:
            self._apply_delete_files(delete_files)

        logger.debug(f"{tag}正在保存索引并清理临时目录...")
        saved_index = self.cache_dir / "indexFile.json"
        if index_file.exists():
            shutil.copy2(index_file, saved_index)
            logger.debug(f"{tag}已保存索引到缓存")

        # 先提交分包断点；登记失败时保留下载目录，允许继续校验和应用。
        self._update_local_version((self._apply_version_info or {}).get("to_version"))
        try:
            shutil.rmtree(download_dir)
            logger.debug(f"{tag}已清理增量下载目录")
        except OSError as e:
            # 已完成迁移和版本登记；仅临时清理失败不应伪装成更新失败。
            logger.warning(f"{tag}增量更新已完成，但临时下载目录清理失败: {e}")

        self._cleanup_cache_dir()
        logger.debug(f"{tag}已清理缓存临时目录")

        return True

    def apply_into(self, progress: Progress, stats: "ApplyStats", label: Optional[str] = None) -> bool:
        """单包应用入口：自带总进度任务，结束后统计累加进 stats。"""
        tag = f"[{label}] " if label else ""
        group_infos = self.prepare_apply(label)
        if group_infos is None:
            return False

        max_workers = max(1, min(APPLY_MAX_WORKERS, len(group_infos)))
        logger.info(
            f"{tag}开始合并 {len(group_infos)} 个增量包，并发 {max_workers} 个；"
            "部分大文件可能耗时数分钟，请勿中断进程。"
        )

        tracker = _GroupActivityTracker(progress)
        task_id = progress.add_task(f"{tag}[bold cyan]应用增量包", total=len(group_infos), kind="overall")
        results = self.run_apply_jobs([(self, group) for group in group_infos], progress, tracker, task_id, tag)
        tracker.close()
        progress.remove_task(task_id)

        stats.add(label, results.get(id(self), {}))
        if stats.failed:
            return False
        return self.migrate_outputs(progress, stats, label)

    def _apply_single_group(
        self,
        group: Dict,
        download_dir: Path,
        log_info,
        log_warning,
        log_error,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> tuple:
        """应用单个增量更新组"""
        krpdiff_name = group.get("dest")
        src_files = group.get("srcFiles", [])
        dst_files = group.get("dstFiles", [])

        def set_status(msg: str):
            if status_callback:
                status_callback(msg)

        set_status("检查资源信息")
        if not krpdiff_name or not src_files or not dst_files:
            return krpdiff_name, "failed"

        # 检查是否所有 dst 文件都已经是最新版本（跳过已完成的分组）
        set_status("检查目标文件")
        dst_by_md5 = {d["md5"]: d for d in dst_files}
        all_dst_done = True
        for dst_info in dst_files:
            dst_full = self.game_folder / _resource_rel_path(dst_info["dest"])
            if not dst_full.exists() or dst_full.stat().st_size != int(dst_info["size"]):
                all_dst_done = False
                break
        if all_dst_done:
            # 大小匹配，抽样第一个文件做 MD5 确认，避免在跳过路径上重复校验所有 dstFiles。
            set_status("校验目标文件")
            first_dst_full = self.game_folder / _resource_rel_path(dst_files[0]["dest"])
            if _calculate_file_md5(first_dst_full) == dst_files[0]["md5"]:
                log_info(f"所有文件已为目标版本，跳过 {len(dst_files)} 个文件")
                return krpdiff_name, "skipped"

        # 上次 apply 中断时输出可能已经在 staging 里；校验通过就直接复用，避免重跑大补丁
        staged_outputs = [(d, self._get_staged_patch_path(download_dir, d["dest"])) for d in dst_files]
        if all(p.exists() and p.stat().st_size == int(d["size"]) for d, p in staged_outputs):
            set_status("校验已暂存的输出")
            if all(self._is_file_verified(p, int(d["size"]), d["md5"]) for d, p in staged_outputs):
                for d, _ in staged_outputs:
                    self._mark_staged_patch_verified(d["dest"])
                log_info(f"复用上次已校验的增量输出: {len(dst_files)} 个文件")
                return krpdiff_name, "success"

        # 检查第一个源文件的状态（决定运行 hpatchz 还是回退下载）
        set_status("检查源文件")
        first_src = src_files[0]
        first_src_path = first_src["dest"]
        first_src_md5 = first_src["md5"]
        first_src_full = self.game_folder / _resource_rel_path(first_src_path)
        backup_path = _backup_path(first_src_full)

        # 只计算一次 MD5，缓存复用
        first_local_md5 = None
        if first_src_full.exists():
            first_local_md5 = _calculate_file_md5(first_src_full)
        elif backup_path.exists():
            first_local_md5 = _calculate_file_md5(backup_path)

        if first_local_md5 is None:
            # 源文件不存在，回退下载所有 dst 文件
            set_status(f"源文件缺失，回退下载 {len(dst_files)} 个文件")
            logger.debug(f"源文件不存在: {first_src_path}, 回退下载 {len(dst_files)} 个文件")
            failed_count = self._fallback_download_group(dst_files, download_dir, log_info, log_error, set_status)
            return krpdiff_name, "failed" if failed_count > 0 else "success"

        # 检查 MD5：必须是 src 版本或已经是 dst 版本
        if first_local_md5 in dst_by_md5:
            # 已经是目标版本，但其他 dst 文件可能缺失（多文件 group 场景）
            # 仍需运行 hpatchz 来生成其他输出文件
            pass
        elif first_local_md5 != first_src_md5:
            # 源文件与索引记录的旧版本不同（例如另一渠道的原装文件），整组回退为完整下载，不中断整次更新。
            set_status(f"源文件校验失败，回退下载 {len(dst_files)} 个文件")
            log_warning(
                f"源文件 MD5 不匹配: {first_src_path} "
                f"(期望 {first_src_md5[:16]}... 或 dst MD5, 实际 {first_local_md5[:16]}...)，"
                f"回退全量下载 {len(dst_files)} 个文件"
            )
            failed_count = self._fallback_download_group(dst_files, download_dir, log_info, log_error, set_status)
            return krpdiff_name, "failed" if failed_count > 0 else "success"

        # 从备份恢复
        if backup_path.exists() and not first_src_full.exists():
            set_status("恢复中断文件")
            try:
                shutil.move(backup_path, first_src_full)
            except OSError as e:
                log_error(f"无法恢复中断的更新 {first_src_path}: {e}")
                return krpdiff_name, "failed"
            log_info(f"恢复中断的更新: {first_src_path}")
            first_local_md5 = _calculate_file_md5(first_src_full)

        set_status("检查增量包")
        krpdiff_path = download_dir / _resource_rel_path(krpdiff_name)
        if not krpdiff_path.exists():
            log_error(f"增量包不存在: {krpdiff_name}")
            return krpdiff_name, "failed"

        patch_temp_dir = self.cache_dir / "patch_temp"
        patch_temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            with tempfile.TemporaryDirectory(prefix="ww_patch_", dir=patch_temp_dir) as temp_dir:
                set_status("准备临时目录")
                output_dir = Path(temp_dir) / "output"
                output_dir.mkdir(parents=True, exist_ok=True)

                if self.applier == "krpatch" and self.krpatch_path is not None:
                    source_error = self._ensure_patch_sources(src_files)
                    if source_error:
                        set_status(f"源文件不可用，回退下载 {len(dst_files)} 个文件")
                        log_warning(f"{source_error}，回退全量下载 {len(dst_files)} 个文件")
                        failed_count = self._fallback_download_group(
                            dst_files, download_dir, log_info, log_error, set_status
                        )
                        return krpdiff_name, "failed" if failed_count > 0 else "success"

                    # krpatch 直接读游戏目录、直接写临时输出目录（不再 copy 源文件），
                    # 用官方 index 的 md5 作为 --expect 真值，同时在二进制内逐文件比对。
                    verified_dests, needs_fallback, patch_error = self._run_krpatch(
                        krpdiff_path, output_dir, dst_files, status_callback=set_status
                    )
                    if patch_error:
                        set_status(f"krpatch 失败，回退下载 {len(dst_files)} 个文件")
                        log_warning(
                            f"krpatch 应用失败，回退到完整下载: {first_src_path} 等 "
                            f"{len(dst_files)} 个文件（{patch_error}）"
                        )
                        failed_count = self._fallback_download_group(
                            dst_files, download_dir, log_info, log_error, set_status
                        )
                        return krpdiff_name, "failed" if failed_count > 0 else "success"
                else:
                    verified_dests, needs_fallback = set(), set()
                    if not self._run_hpatchz(
                        krpdiff_path,
                        output_dir,
                        src_files,
                        status_callback=set_status,
                        error_callback=log_error,
                    ):
                        set_status(f"hpatchz 失败，回退下载 {len(dst_files)} 个文件")
                        log_warning(f"hpatchz 应用失败，回退到完整下载: {first_src_path} 等 {len(dst_files)} 个文件")
                        failed_count = self._fallback_download_group(
                            dst_files, download_dir, log_info, log_error, set_status
                        )
                        return krpdiff_name, "failed" if failed_count > 0 else "success"

                # 输出已就位，缓存到 staging，最后统一迁移到游戏目录
                set_status("缓存输出文件")
                fail_count = 0
                for index, dst_info in enumerate(dst_files, 1):
                    set_status(f"缓存输出 {index}/{len(dst_files)}")
                    dst_rel_path = dst_info["dest"]
                    output_file = output_dir / _resource_rel_path(dst_rel_path)
                    staged_path = self._get_staged_patch_path(download_dir, dst_rel_path)
                    dest_key = self._krpatch_key(dst_rel_path)

                    if dest_key in needs_fallback:
                        set_status(f"输出与官方校验不一致，回退下载 {index}/{len(dst_files)}")
                        log_warning(f"输出文件与官方 md5 不一致: {dst_rel_path}, 回退下载")
                        if not self._download_full_file_to_stage(dst_info, staged_path, log_error):
                            fail_count += 1
                            log_error(f"回退下载也失败: {dst_rel_path}")
                        continue

                    if not output_file.exists():
                        set_status(f"输出缺失，回退下载 {index}/{len(dst_files)}")
                        log_warning(f"输出文件未生成: {dst_rel_path}, 回退下载")
                        if not self._download_full_file_to_stage(dst_info, staged_path, log_error):
                            fail_count += 1
                            log_error(f"回退下载也失败: {dst_rel_path}")
                        continue

                    # krpatch 已用 --expect 对过官方 md5，不再重复读一遍大文件。
                    if dest_key not in verified_dests:
                        set_status(f"校验输出 {index}/{len(dst_files)}")
                        if not self._is_file_verified(output_file, int(dst_info["size"]), dst_info["md5"]):
                            set_status(f"输出校验失败，回退下载 {index}/{len(dst_files)}")
                            log_warning(f"输出文件校验失败: {dst_rel_path}, 回退下载")
                            if not self._download_full_file_to_stage(dst_info, staged_path, log_error):
                                fail_count += 1
                                log_error(f"回退下载也失败: {dst_rel_path}")
                            continue

                    staged_path.parent.mkdir(parents=True, exist_ok=True)
                    if staged_path.exists():
                        staged_path.unlink()
                    shutil.move(output_file, staged_path)
                    self._mark_staged_patch_verified(dst_rel_path)

                if fail_count > 0:
                    return krpdiff_name, "partial_failed"

                return krpdiff_name, "success"

        finally:
            pass

    def _fallback_download_group(
        self,
        dst_files: List[Dict],
        download_dir: Path,
        log_info,
        log_error,
        set_status: Callable[[str], None],
    ) -> int:
        """整组回退为完整文件下载，返回失败个数。"""
        failed_count = 0
        for index, dst_info in enumerate(dst_files, 1):
            set_status(f"回退下载 {index}/{len(dst_files)}")
            staged_path = self._get_staged_patch_path(download_dir, dst_info["dest"])
            if not self._download_full_file_to_stage(dst_info, staged_path, log_error):
                failed_count += 1
                log_error(f"下载失败: {dst_info['dest']}")
            else:
                log_info(f"已缓存完整文件: {dst_info['dest']}")
        return failed_count

    def _download_full_file_to_stage(
        self,
        dst_info: Dict,
        staged_path: Path,
        error_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """下载完整文件到指定缓存路径，不直接修改游戏目录"""
        dest = dst_info["dest"]
        expected_md5 = dst_info["md5"]
        expected_size = int(dst_info["size"])

        if self._is_file_verified(staged_path, expected_size, expected_md5):
            self._mark_staged_patch_verified(dest)
            return True
        self.check_download_space([{"path": staged_path, "size": expected_size}])
        if staged_path.exists():
            staged_path.unlink()

        url = self._build_resource_url(dst_info, fallback_base=self._full_resource_base())
        if not url:
            message = "无法获取资源路径"
            if error_callback:
                error_callback(message)
            else:
                logger.error(message)
            return False

        logger.debug(f"下载完整文件到缓存: {dest}")
        success = self._download_and_verify(
            url,
            staged_path,
            expected_size,
            expected_md5,
            error_callback=error_callback,
            suppress_success_log=True,
        )
        if success:
            self._mark_staged_patch_verified(dest)
        return success

    def _download_full_file(self, dst_info: Dict, dest_path: Path) -> bool:
        """下载完整文件作为 hpatchz 失败的回退方案，先写入缓存再替换游戏文件"""
        dest = dst_info["dest"]
        expected_md5 = dst_info["md5"]
        expected_size = int(dst_info["size"])

        if self._is_file_verified(dest_path, expected_size, expected_md5):
            logger.info(f"完整文件已是目标版本: {dest}")
            return True

        url = self._build_resource_url(dst_info)
        if not url:
            logger.error("无法获取资源路径")
            return False

        staged_path = self._get_fallback_download_path(dest)
        if not self._is_file_verified(staged_path, expected_size, expected_md5):
            self.check_download_space([{"path": staged_path, "size": expected_size}])
        if staged_path.exists() and not self._is_file_verified(staged_path, expected_size, expected_md5):
            staged_path.unlink()

        logger.info(f"下载完整文件: {dest}")
        if not self._is_file_verified(staged_path, expected_size, expected_md5):
            if not self._download_and_verify(url, staged_path, expected_size, expected_md5):
                return False

        return _safe_replace_file(staged_path, dest_path)

    KRPATCH_MIN_TIMEOUT = 600.0
    # 保守下限：本机实测 19.6 GiB 分组（含 md5）约 34 s，即 ~570 MiB/s。
    KRPATCH_MIN_MB_PER_S = 20.0

    def _select_applier(self) -> Optional[str]:
        """选补丁应用器：优先原生 krpatch，否则回退 wine + hpatchz。

        ``WW_APPLIER=hpatchz`` 可强制旧路径（调试/对照用）。
        """
        forced = (os.environ.get("WW_APPLIER") or "").strip().lower()
        if forced in ("hpatchz", "wine"):
            return "hpatchz" if check_hpatchz_requirements()[0] else None
        if forced not in ("", "auto", "krpatch"):
            logger.warning(f"未知的 WW_APPLIER={forced}，按 auto 处理")

        krpatch = get_krpatch_path(self.cache_dir)
        if krpatch is not None:
            self.krpatch_path = krpatch
            return "krpatch"
        if forced == "krpatch":
            return None
        return "hpatchz" if check_hpatchz_requirements()[0] else None

    def _krpatch_key(self, dest: str) -> str:
        """统一容器/清单路径的比较键（都走 '/' 分隔的相对路径）。"""
        return _resource_rel_path(dest).as_posix()

    def _ensure_patch_sources(self, src_files: List[Dict]) -> Optional[str]:
        """krpatch 直接读游戏目录，动手前要确认真实源文件都在。

        返回 None 表示就绪；否则返回不能继续的原因（调用方整组回退全量下载）。
        单文件分组的 .bak 恢复已在上面做过，这里补齐多文件分组的剩馀源文件。
        """
        for src in src_files:
            rel_path = _resource_rel_path(src["dest"])
            path = self.game_folder / rel_path
            if path.exists():
                continue
            backup = _backup_path(path)
            if not backup.exists():
                return f"源文件缺失: {src['dest']}"
            try:
                shutil.move(backup, path)
            except OSError as e:
                return f"无法恢复中断的更新 {src['dest']}: {e}"
            logger.info(f"恢复中断的更新: {src['dest']}")
        return None

    def _krpatch_timeout(self, dst_files: List[Dict]) -> float:
        """按输出字节数给 krpatch 超时；环境变量 WW_KRPATCH_TIMEOUT 可覆盖。"""
        override = os.environ.get("WW_KRPATCH_TIMEOUT")
        if override:
            try:
                # 显式覆盖按原意生效（调试/慢盘用），只挡掉非正数
                return max(1.0, float(override))
            except ValueError:
                logger.warning(f"WW_KRPATCH_TIMEOUT 不是数字: {override}，已忽略")
        total = sum(int(item.get("size") or 0) for item in dst_files)
        return max(self.KRPATCH_MIN_TIMEOUT, total / (self.KRPATCH_MIN_MB_PER_S * (1 << 20)))

    def _run_krpatch(
        self,
        krpdiff_path: Path,
        output_dir: Path,
        dst_files: List[Dict],
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[set, set, Optional[str]]:
        """用原生 krpatch 应用一个分组。

        返回 ``(verified_dests, needs_fallback, error)``：
        ``verified_dests`` 是已经在二进制内部与官方 md5 对过的输出（不必再读一遍）；
        ``needs_fallback`` 是输出对不上或缺失、要回退完整下载的文件；
        ``error`` 非空表示整个分组不可用（旧文件缺失、补丁损坏、超时……）。
        """
        all_dests = {self._krpatch_key(item["dest"]) for item in dst_files}

        def set_status(msg: str):
            if status_callback:
                status_callback(msg)

        if self.krpatch_path is None:
            return set(), all_dests, "未配置 krpatch 路径"

        patch_temp_dir = self.cache_dir / "patch_temp"
        patch_temp_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(krpdiff_path).stem
        expect_path = patch_temp_dir / f"expect_{stem}.tsv"
        result_path = patch_temp_dir / f"result_{stem}.json"

        try:
            lines = []
            for item in dst_files:
                dest = str(item["dest"]).replace("\\", "/")
                lines.append(f"{dest}\t{int(item['size'])}\t{item['md5']}\n")
            expect_path.write_text("".join(lines), encoding="utf-8")
        except (OSError, KeyError, ValueError) as e:
            return set(), all_dests, f"无法写入期望值表: {e}"

        cmd = [
            str(self.krpatch_path),
            "apply",
            "--old-root",
            str(self.game_folder),
            "--out-root",
            str(output_dir),
            "--workers",
            "1",
            "--expect",
            str(expect_path),
            "--json",
            str(result_path),
            str(krpdiff_path),
        ]
        logger.debug(f"Running krpatch: {' '.join(cmd)}")
        set_status("应用差分")
        timeout = self._krpatch_timeout(dst_files)
        tail: List[str] = []
        progress_state: List[int] = []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            return set(), all_dests, f"无法启动 krpatch: {e}"

        def drain_stderr(stream) -> None:
            # 进度行用 \r 刷新，按块读再取最后一个百分比，不依赖行边界
            buf = ""
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                buf = (buf + chunk)[-4096:]
                matches = re.findall(r"(\d+(?:\.\d+)?)%", buf)
                if matches:
                    pct = int(float(matches[-1]))
                    if not progress_state or progress_state[-1] != pct:
                        progress_state.append(pct)
                tail.append(chunk)
                if len(tail) > 16:
                    del tail[:-16]

        reader = threading.Thread(target=drain_stderr, args=(proc.stderr,), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        shown = -1
        try:
            while True:
                try:
                    proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if progress_state and progress_state[-1] != shown:
                        shown = progress_state[-1]
                        set_status(f"应用差分 {shown}%")
                    if time.monotonic() > deadline:
                        proc.kill()
                        proc.wait(timeout=10)
                        return (
                            set(),
                            all_dests,
                            f"krpatch 超时（{int(timeout)}s），可用 WW_KRPATCH_TIMEOUT 调大",
                        )
        finally:
            reader.join(timeout=5)
            if proc.stderr is not None:
                proc.stderr.close()
            stderr_text = "".join(tail)
            try:
                expect_path.unlink()
            except OSError:
                pass

        set_status("检查 krpatch 结果")
        data = None
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            finally:
                try:
                    result_path.unlink()
                except OSError:
                    pass

        if proc.returncode not in (0, 3) or not isinstance(data, dict):
            detail = ""
            if isinstance(data, dict):
                groups = data.get("groups") or []
                first = groups[0] if groups else {}
                detail = str(first.get("error") or "")
            if not detail:
                detail = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else ""
            return set(), all_dests, f"退出码 {proc.returncode}{'：' + detail if detail else ''}"

        files: Dict[str, Dict] = {}
        for group in data.get("groups") or []:
            for item in group.get("files") or []:
                try:
                    files[self._krpatch_key(item["path"])] = item
                except (IncrementalError, KeyError):
                    continue

        verified: set = set()
        needs_fallback: set = set()
        for item in dst_files:
            key = self._krpatch_key(item["dest"])
            entry = files.get(key)
            if entry is None:
                needs_fallback.add(key)
            elif entry.get("matched") is True:
                verified.add(key)
            elif entry.get("matched") is False:
                needs_fallback.add(key)
            # matched 为 None（期望值表没覆盖到）时留给 Python 侧自己校验
        if needs_fallback:
            logger.debug(f"krpatch 输出待回退: {sorted(needs_fallback)}")
        return verified, needs_fallback, None

    def _run_hpatchz(
        self,
        krpdiff_path: Path,
        output_dir: Path,
        src_files: List[Dict],
        temp_olddir: Optional[Path] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        error_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """运行 hpatchz 应用增量包

        Args:
            krpdiff_path: 增量包路径
            output_dir: 输出目录
            src_files: 源文件列表（包含 dest 和 md5）
            temp_olddir: 预创建的临时源文件目录
            status_callback: 状态回调，用于刷新当前增量包子任务
            error_callback: 错误日志回调，用于在 Rich Progress 上方统一输出
        """

        def set_status(msg: str):
            if status_callback:
                status_callback(msg)

        def emit_error(msg: str):
            if error_callback:
                error_callback(msg)
            else:
                logger.error(msg)

        if temp_olddir is None:
            set_status("准备 hpatchz 源文件")
            # 为当前 group 创建临时源文件目录
            patch_temp_dir = self.cache_dir / "patch_temp"
            patch_temp_dir.mkdir(parents=True, exist_ok=True)
            temp_olddir = patch_temp_dir / f"olddir_{Path(krpdiff_path).stem}"
            try:
                if temp_olddir.exists():
                    shutil.rmtree(temp_olddir)
                temp_olddir.mkdir(parents=True)
            except OSError as e:
                emit_error(f"无法准备 hpatchz 源文件目录: {e}")
                return False

            # 复制所有源文件到临时目录
            for index, src_file in enumerate(src_files, 1):
                set_status(f"复制源文件 {index}/{len(src_files)}")
                src_rel_path = _resource_rel_path(src_file["dest"])
                src_full_path = self.game_folder / src_rel_path
                dst_full_path = temp_olddir / src_rel_path

                if src_full_path.exists():
                    dst_full_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_full_path, dst_full_path)

        if platform.system() == "Windows":
            cmd = [
                str(self.hpatchz_path),
                "-f",
                str(temp_olddir.absolute()) + "/",
                str(krpdiff_path.absolute()),
                str(output_dir.absolute()) + "/",
            ]
        else:
            cmd = [
                "wine",
                str(self.hpatchz_path),
                "-f",
                str(temp_olddir.absolute()) + "/",
                str(krpdiff_path.absolute()),
                str(output_dir.absolute()) + "/",
            ]

        set_status("运行 hpatchz")
        logger.debug(f"Running hpatchz: {' '.join(cmd)}")
        timeout = self._hpatchz_timeout(krpdiff_path)
        result = None
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                emit_error(
                    f"hpatchz 超时 ({int(timeout)}s，补丁 {krpdiff_path.stat().st_size / (1 << 20):.0f}MB)，"
                    f"可用环境变量 WW_HPATCHZ_TIMEOUT 调大"
                )
                return False
            if result.returncode == 0:
                break
            stderr = result.stderr or ""
            # 多个 hpatchz 同时冷启动时会抢 wineserver，重试即可
            if attempt < 3 and ("wine server" in stderr or "cannot connect to it" in stderr):
                emit_error(f"wine server 未就绪，{attempt * 3}s 后重试（{attempt}/3）")
                time.sleep(attempt * 3)
                continue
            break

        set_status("检查 hpatchz 结果")
        if result is None:
            emit_error("hpatchz 未能启动")
            return False
        if result.returncode != 0:
            emit_error(f"hpatchz 运行失败: {result.stderr}")
            return False

        return True

    HPATCHZ_MIN_TIMEOUT = 600.0
    HPATCHZ_TIMEOUT_PER_MB = 4.0
    HPATCHZ_MAX_TIMEOUT = 6 * 3600.0

    def _hpatchz_timeout(self, krpdiff_path: Path) -> float:
        """按补丁体积给 hpatchz 超时；wine 下大补丁可能跑几十分钟。"""
        override = os.environ.get("WW_HPATCHZ_TIMEOUT")
        if override:
            try:
                return max(60.0, float(override))
            except ValueError:
                logger.warning(f"WW_HPATCHZ_TIMEOUT 不是数字: {override}，已忽略")
        try:
            size_mb = krpdiff_path.stat().st_size / (1 << 20)
        except OSError:
            size_mb = 0.0
        return min(
            self.HPATCHZ_MAX_TIMEOUT,
            max(self.HPATCHZ_MIN_TIMEOUT, size_mb * self.HPATCHZ_TIMEOUT_PER_MB),
        )

    def _get_cached_target_version(self, download_dir: Path) -> Optional[str]:
        """从增量下载元信息或 API 配置获取目标版本"""
        version_file = download_dir / "version_info.json"
        if version_file.exists():
            try:
                target_version = json.loads(version_file.read_text()).get("to_version")
                if target_version:
                    return target_version
            except Exception:
                pass
        return self.get_target_version() or None

    def verify_new_version(self) -> bool:
        """校验新版本文件状态，用于增量更新后的检查"""
        download_dir = self.download_dir
        index_file = download_dir / "indexFile.json"
        cache_index_file = self.cache_dir / "indexFile.json"

        if not index_file.exists() and not cache_index_file.exists():
            logger.error("未找到增量更新的 indexFile.json，请先执行 'ww incremental' 下载")
            return False

        index_file = index_file if index_file.exists() else cache_index_file

        try:
            index_data = json.loads(index_file.read_text())
        except Exception:
            logger.error("无法解析 indexFile.json")
            return False

        resources = index_data.get("resource", [])
        group_infos = index_data.get("groupInfos", [])
        expected_files = {}

        for group in group_infos:
            for item in group.get("dstFiles", []):
                dest = item.get("dest")
                if dest and item.get("md5") and item.get("size") is not None and not _is_diff_resource(dest):
                    expected_files[dest] = item

        for item in resources:
            dest = item.get("dest")
            if dest and item.get("md5") and item.get("size") is not None and not _is_diff_resource(dest):
                # resource 中的完整资源优先，贴近 BugFix File 使用 Resource File 的处理。
                expected_files[dest] = item

        game_files = list(expected_files.values())
        if not game_files:
            logger.error("indexFile.json 中没有可校验的新版本游戏文件")
            return False

        logger.info(f"正在校验 {len(game_files)} 个新版本文件...")

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
                dest_path = self.game_folder / _resource_rel_path(item["dest"])
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

        delete_files = index_data.get("deleteFiles", [])
        target_version = self._get_cached_target_version(download_dir)

        if not inconsistent_files and not missing_files:
            if delete_files:
                self._apply_delete_files(delete_files)
            logger.info("所有游戏文件状态正常")
            self._update_local_version(target_version or index_data)
            return True

        repair_needed = inconsistent_files + missing_files
        logger.warning(f"需要修复的游戏文件: {repair_needed}")

        res_base = self._full_resource_base()
        if not res_base:
            logger.warning("无法获取全局新版本资源路径，将尝试使用单个资源的 fromFolder")

        game_res_map = {r["dest"]: r for r in game_files}
        repair_count = 0
        fail_count = 0
        total_size = sum(int(game_res_map[d]["size"]) for d in repair_needed)
        pending = []
        for dest in repair_needed:
            item = game_res_map[dest]
            staged = self._get_repair_download_path(dest)
            if not self._is_file_verified(staged, int(item["size"]), item["md5"]):
                pending.append({"path": staged, "size": int(item["size"])})
        self.check_download_space(pending)
        if delete_files:
            self._apply_delete_files(delete_files)

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
                final_path = self.game_folder / _resource_rel_path(dest)
                staged_path = self._get_repair_download_path(dest)
                expected_md5 = item["md5"]
                expected_size = int(item["size"])
                file_name = Path(dest).name

                if self._is_file_verified(final_path, expected_size, expected_md5):
                    return True, dest, expected_size

                if not self._is_file_verified(staged_path, expected_size, expected_md5):
                    self.check_download_space([{"path": staged_path, "size": expected_size}])
                    if staged_path.exists():
                        staged_path.unlink()

                url = self._build_resource_url(item, res_base)
                if not url:
                    _emit_progress_log(progress, "ERROR", f"无法构建修复文件下载 URL: {dest}")
                    return False, dest, expected_size

                try:
                    if not self._is_file_verified(staged_path, expected_size, expected_md5):
                        if not download_single_file(
                            url,
                            staged_path,
                            expected_size,
                            progress,
                            expected_md5=expected_md5,
                        ):
                            return False, dest, expected_size

                    if not self._is_file_verified(staged_path, expected_size, expected_md5):
                        _emit_progress_log(progress, "ERROR", f"修复文件校验失败: {dest}")
                        if staged_path.exists():
                            staged_path.unlink()
                        return False, dest, expected_size

                    if not _safe_replace_file(
                        staged_path,
                        final_path,
                        lambda msg: _emit_progress_log(progress, "ERROR", msg),
                    ):
                        return False, dest, expected_size

                    return True, dest, expected_size

                except Exception as e:
                    _emit_progress_log(progress, "ERROR", f"下载失败 {file_name}: {e}")
                    if staged_path.exists():
                        staged_path.unlink()
                    return False, dest, expected_size

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {executor.submit(download_repair, d): d for d in repair_needed}
                for future in as_completed(futures):
                    success, dest, size = future.result()
                    if success:
                        repair_count += 1
                        _emit_progress_log(progress, "INFO", f"修复成功: {dest}")
                    else:
                        fail_count += 1
                        _emit_progress_log(progress, "ERROR", f"修复失败: {dest}")
                    progress.update(overall_task, advance=size)

        logger.info(f"修复完成: 成功={repair_count}, 失败={fail_count}")
        if fail_count == 0:
            self._update_local_version(target_version or index_data)
            return True
        return False

    def _cleanup_cache_dir(self) -> None:
        """清理缓存中的临时目录（patch_temp、回退下载、修复下载等）"""
        temp_subdirs = ["patch_temp", "full_download_temp", "repair_download_temp"]
        cleaned = 0
        for name in temp_subdirs:
            subdir = self.cache_dir / name
            if subdir.exists():
                try:
                    shutil.rmtree(subdir)
                    cleaned += 1
                except OSError as e:
                    logger.warning(f"无法清理临时目录 {subdir}: {e}")
        if cleaned:
            logger.debug(f"已清理 {cleaned} 个缓存临时目录")

    def _find_resource_base(self) -> Optional[str]:
        """从 indexFile.json 或 API 配置获取资源路径前缀"""
        download_dir = self.download_dir
        index_file = download_dir / "indexFile.json"
        cache_index_file = self.cache_dir / "indexFile.json"

        if index_file.exists() or cache_index_file.exists():
            if not index_file.exists():
                index_file = cache_index_file
            try:
                data = json.loads(index_file.read_text())
                resources = data.get("resource", [])
                if resources:
                    from_folder = resources[0].get("fromFolder", "")
                    if from_folder:
                        return from_folder.rstrip("/")
            except Exception:
                pass

        patch_info = self._find_matching_patch()
        if patch_info:
            base_url = patch_info.get("baseUrl", "")
            if base_url:
                return base_url.rstrip("/")

        default_base = self.default_config.get("baseUrl", "")
        if default_base:
            return default_base.rstrip("/")

        return None

    def _download_and_verify_complete(
        self,
        url: str,
        dest: Path,
        expected_size: int,
        expected_md5: str,
        progress: Optional[Progress] = None,
        overall_task_id: Optional[TaskID] = None,
    ) -> bool:
        """下载完整文件并验证 MD5；复用通用下载器（大文件自动分段）。"""
        self.check_download_space([{"path": dest, "size": expected_size}])
        if download_single_file(url, dest, expected_size, progress, overall_task_id, expected_md5=expected_md5):
            return True
        _emit_progress_log(progress, "ERROR", f"下载失败 {dest.name}")
        return False

    def _download_and_verify(
        self,
        url: str,
        dest: Path,
        expected_size: int,
        expected_md5: str,
        error_callback: Optional[Callable[[str], None]] = None,
        suppress_success_log: bool = False,
    ) -> bool:
        """下载文件并验证 MD5；复用通用下载器（大文件自动分段）。"""
        self.check_download_space([{"path": dest, "size": expected_size}])

        def emit_error(msg: str):
            if error_callback:
                error_callback(msg)
            else:
                logger.error(msg)

        if not download_single_file(url, dest, expected_size, expected_md5=expected_md5):
            emit_error(f"下载失败 {dest.name}")
            return False
        if not suppress_success_log:
            logger.info(f"修复成功: {dest.name}")
        return True

    def _update_local_version(self, version_or_data):
        """更新本地版本，可接收版本字符串或 indexFile.json 数据"""
        if isinstance(version_or_data, dict):
            new_version = version_or_data.get("version") or version_or_data.get("to_version")
            if not new_version:
                group_infos = version_or_data.get("groupInfos", [])
                if not group_infos:
                    logger.warning("无法获取 groupInfos 信息")
                    return
                krpdiff_name = Path(group_infos[0]["dest"]).stem
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


class DownloadBatch:
    """一次命令里的全部待下载资源：多个分包共用一条进度条与一个线程池。

    以前 common 与画质包各自跑一轮检查和下载，界面上会出现两段列表、两条进度条，
    分组之间还有一次吞吐掉底。合并后统一按作业大小降序提交，大包先起飞。
    """

    MAX_WORKERS = 8

    def __init__(self, engines: List["IncrementalManager"], plans: List[Dict[str, Any]]):
        if len(engines) != len(plans):
            raise ValueError("engines 与 plans 数量不一致")
        self.pairs = list(zip(engines, plans))
        self.patch_total = sum(p["patch_total"] for p in plans)
        self.patch_done = sum(p["patch_skipped"] for p in plans)
        self.complete_total = sum(p["complete_total"] for p in plans)
        self.complete_done = sum(p["complete_ready"] for p in plans)
        self.total_bytes = sum(p["total_bytes"] for p in plans)
        self.failed_patches: List[str] = []
        self.failed_complete_files: List[str] = []

    def has_pending(self) -> bool:
        return any(p["patch_jobs"] or p["complete_jobs"] for _engine, p in self.pairs)

    def describe(self) -> str:
        parts = [f"增量包 {self.patch_done}/{self.patch_total}"]
        if self.complete_total:
            parts.append(f"完整文件 {self.complete_done}/{self.complete_total}")
        return f"下载资源 ({'，'.join(parts)})"

    def _jobs(self) -> List[tuple]:
        jobs = []
        for engine, plan in self.pairs:
            for payload in plan["patch_jobs"]:
                jobs.append((engine, "patch", payload[3] or 0, payload))
            for task in plan["complete_jobs"]:
                jobs.append((engine, "complete", task.get("size") or 0, task))
        # 大作业先跑：否则最后只剩最大的几个在收尾。
        jobs.sort(key=lambda job: job[2], reverse=True)
        return jobs

    def run(self) -> bool:
        if not self.has_pending():
            logger.info("无需下载，所有增量更新资源已在本地就绪")
            return True

        n_patch = sum(len(p["patch_jobs"]) for _engine, p in self.pairs)
        n_complete = sum(len(p["complete_jobs"]) for _engine, p in self.pairs)
        if n_patch:
            size = sum(p["pending_patch_bytes"] for _engine, p in self.pairs) / 1024 / 1024
            logger.info(f"待下载: {n_patch} 个增量包 ({size:.2f} MB)")
        if n_complete:
            size = sum(p["pending_complete_bytes"] for _engine, p in self.pairs) / 1024 / 1024
            logger.info(f"待下载完整文件: {n_complete} 个 ({size:.2f} MB)")

        progress = create_download_progress()
        executor = ThreadPoolExecutor(max_workers=self.MAX_WORKERS)
        try:
            with progress:
                overall_task = progress.add_task(self.describe(), total=self.total_bytes or None)
                futures = {}
                for engine, kind, _size, payload in self._jobs():
                    if kind == "patch":
                        group, path, url, size, md5 = payload
                        future = executor.submit(engine._download_file, url, path, size, md5, progress, overall_task)
                        futures[future] = ("patch", group.get("dest"))
                    else:
                        future = executor.submit(
                            engine._download_and_verify_complete,
                            payload["url"],
                            payload["path"],
                            payload["size"],
                            payload["md5"],
                            progress,
                            overall_task,
                        )
                        futures[future] = ("complete", payload["dest"])

                try:
                    for future in as_completed(futures):
                        kind, dest = futures[future]
                        try:
                            ok = bool(future.result())
                        except Exception as e:
                            _emit_progress_log(progress, "ERROR", f"下载任务异常 {dest}: {e}")
                            ok = False
                        if ok:
                            if kind == "patch":
                                self.patch_done += 1
                            else:
                                self.complete_done += 1
                        elif kind == "patch":
                            self.failed_patches.append(dest)
                        else:
                            self.failed_complete_files.append(dest)
                        progress.update(overall_task, description=self.describe())
                except KeyboardInterrupt:
                    # 先让在飞的下载停手，否则 shutdown(wait=True) 会把它们跑完。
                    cancel_downloads()
                    raise
        finally:
            executor.shutdown(wait=True)

        if self.failed_patches:
            logger.error(f"增量包下载失败: {self.failed_patches}")
        if self.failed_complete_files:
            logger.error(f"完整文件下载失败: {self.failed_complete_files}")
        if self.failed_patches or self.failed_complete_files:
            return False

        if self.complete_total:
            logger.info(
                f"下载完成: 增量包 {self.patch_done}/{self.patch_total}, "
                f"完整文件 {self.complete_done}/{self.complete_total}"
            )
        else:
            logger.info(f"增量包下载完成: {self.patch_done}/{self.patch_total}")
        return True
