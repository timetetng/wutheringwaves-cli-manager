# download.py
"""
通用下载模块：单文件下载（大文件自动多连接分段）与批量并行下载
"""

import hashlib
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional
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
from rich.progress_bar import ProgressBar

logger = logging.getLogger("WW_Manager")

USER_AGENT = "WW-Manager/2.0"

# 大文件分段下载参数，与 aria2 默认习惯对齐（-x16 -s16）；可用环境变量覆盖。
DEFAULT_SEGMENTS = 16
DEFAULT_MAX_CONNECTIONS = 16
DEFAULT_SEGMENT_THRESHOLD = 32 * 1024 * 1024
SEGMENT_BLOCK_SIZE = 16 * 1024 * 1024
READ_SIZE = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"环境变量 {name}={raw!r} 不是整数，使用默认值 {default}")
        return default
    if value <= 0:
        logger.warning(f"环境变量 {name}={value} 必须为正整数，使用默认值 {default}")
        return default
    return value


SEGMENTS = _env_int("WW_DL_SEGMENTS", DEFAULT_SEGMENTS)
MAX_CONNECTIONS = _env_int("WW_DL_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS)
SEGMENT_THRESHOLD = _env_int("WW_DL_SEGMENT_THRESHOLD", DEFAULT_SEGMENT_THRESHOLD)

_connection_slots: Optional[threading.BoundedSemaphore] = None
_connection_slots_lock = threading.Lock()


def _connection_semaphore() -> threading.BoundedSemaphore:
    """全局连接预算，避免批量下载时 文件数 × 分段数 把连接数撑爆。"""
    global _connection_slots
    with _connection_slots_lock:
        if _connection_slots is None:
            _connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        return _connection_slots


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


class NetworkError(Exception):
    """网络相关错误"""

    pass


class DownloadSpaceError(Exception):
    """下载前已确认目标文件系统空间不足。"""


def available_disk_space(target: Path) -> Optional[int]:
    try:
        while not target.exists() and target != target.parent:
            target = target.parent
        return shutil.disk_usage(target).free
    except OSError as e:
        logger.warning(f"无法读取目标磁盘可用空间，无法保证空间充足: {e}")
        return None


def format_bytes(size: int) -> str:
    return f"{size / 1024**3:.2f} GiB ({size:,} 字节)"


def space_shortage(required: int, free: Optional[int]) -> Optional[str]:
    if free is None or required <= free:
        return None
    return (
        f"空间不足：需下载 {format_bytes(required)}，可用 {format_bytes(free)}，"
        f"差额 {format_bytes(required - free)}；可使用 --force 仅绕过空间检查"
    )


def check_space_requirement(tasks: List[dict], free: Optional[int], force_space: bool = False) -> None:
    """按待下载文件清单求和，不扣续传、不计解压与预留；未知大小只警告。"""
    if any(task.get("size") is None for task in tasks):
        logger.warning("部分下载文件大小未知，仅检查已知大小，无法保证空间充足")
    required = sum(int(task.get("size") or 0) for task in tasks)
    shortage = space_shortage(required, free)
    if shortage:
        if not force_space:
            raise DownloadSpaceError(shortage)
        logger.warning(f"{shortage}；已通过 --force 强制继续，文件校验仍然有效")


class RainbowBarColumn(BarColumn):
    """进度条与文件名使用相同的任务颜色。"""

    def render(self, task: Task) -> ProgressBar:
        color = RAINBOW_COLORS[task.id % len(RAINBOW_COLORS)]
        self.complete_style = color
        self.finished_style = color
        return super().render(task)


def create_download_progress(transient: bool = True) -> Progress:
    """创建下载进度条"""
    return Progress(
        TextColumn("{task.description}", justify="right"),
        RainbowBarColumn(bar_width=40),
        "[progress.percentage]{task.percentage:>3.1f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        expand=True,
        transient=transient,
    )


# --- 分段下载 ---


def probe_range_support(url: str, timeout: float = 15) -> Optional[int]:
    """探测 CDN 是否返回 206 与总大小；不支持分段时返回 None。"""
    request = Request(url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 206:
                return None
            match = re.match(r"bytes 0-0/(\d+)", response.headers.get("Content-Range", "") or "")
            if match:
                return int(match.group(1))
    except Exception as e:
        logger.debug(f"分段探测失败 {url}: {e}")
    return None


def _segment_state_path(temp_file: Path) -> Path:
    return temp_file.with_name(temp_file.name + ".segments")


def _segment_header(size: int, block: int) -> str:
    return f"size {size}\nblock {block}\n"


def _load_segment_done(state_path: Path, size: int, block: int) -> Optional[set]:
    """读取已完成块；头部不匹配视为无有效状态。"""
    try:
        lines = state_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    if lines[0].strip() != f"size {size}" or lines[1].strip() != f"block {block}":
        return None
    done = set()
    for line in lines[2:]:
        try:
            done.add(int(line))
        except ValueError:
            continue
    return done


def clear_segment_state(temp_file: Path) -> None:
    _segment_state_path(temp_file).unlink(missing_ok=True)


def _prepare_segment_state(temp_file: Path, expected_size: int, block: int) -> set:
    """准备临时文件与状态；粗粒度续传：只记录已完成块号。"""
    state_path = _segment_state_path(temp_file)
    done = _load_segment_done(state_path, expected_size, block) if temp_file.is_file() else None
    if done is None:
        temp_file.unlink(missing_ok=True)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(_segment_header(expected_size, block), encoding="utf-8")
        with temp_file.open("wb") as stream:
            stream.truncate(expected_size)
        return set()
    with temp_file.open("r+b") as stream:
        if os.fstat(stream.fileno()).st_size != expected_size:
            stream.truncate(expected_size)
    return done


def _download_block(
    url: str,
    temp_file: Path,
    start: int,
    end: int,
    on_bytes,
    timeout: float = 30,
) -> None:
    """下载 [start, end] 一个块，写入临时文件对应偏移；每线程使用独立句柄。"""
    headers = {"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"}
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        if response.status != 206:
            raise NetworkError(f"分段请求未返回 206: HTTP {response.status}")
        content_range = response.headers.get("Content-Range", "")
        if not content_range.startswith(f"bytes {start}-"):
            raise NetworkError(f"无效的分段 Content-Range: {content_range}")
        remaining = end - start + 1
        with temp_file.open("r+b") as stream:
            stream.seek(start)
            while remaining > 0:
                chunk = response.read(min(READ_SIZE, remaining))
                if not chunk:
                    raise NetworkError("分段连接提前结束")
                stream.write(chunk)
                remaining -= len(chunk)
                on_bytes(len(chunk))


def _download_segmented(
    url: str,
    temp_file: Path,
    expected_size: int,
    report,
    block: Optional[int] = None,
    timeout: float = 30,
) -> bool:
    """多连接分段下载同一临时文件；失败时保留状态供下次续传。"""
    if expected_size <= 0:
        return False
    if block is None:
        block = SEGMENT_BLOCK_SIZE
    total_blocks = (expected_size + block - 1) // block
    done = _prepare_segment_state(temp_file, expected_size, block)
    pending = [index for index in range(total_blocks) if index not in done]
    if not pending:
        return True

    state_path = _segment_state_path(temp_file)
    queue = deque(pending)
    lock = threading.Lock()
    errors: List[Exception] = []
    # 连接预算按「块」而不是按「文件」申请：批量下载时先启动的文件不会独呑预算，
    # 后启动的文件也不会退化成单连接。线程数只设上限，拿不到预算就排队。
    semaphore = _connection_semaphore()
    threads_count = max(1, min(SEGMENTS, MAX_CONNECTIONS, len(pending)))
    state_stream = state_path.open("a", encoding="utf-8")
    try:

        def worker() -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    index = queue.popleft()
                start = index * block
                end = min(start + block, expected_size) - 1
                for attempt in range(3):
                    sent = [0]

                    def on_bytes(count: int, sent=sent) -> None:
                        sent[0] += count
                        report(count)

                    semaphore.acquire()
                    try:
                        _download_block(url, temp_file, start, end, on_bytes, timeout=timeout)
                        failure = None
                    except Exception as exc:
                        failure = exc
                    finally:
                        # 退避期间不能攥着连接预算，否则失败块会拖死其他文件。
                        semaphore.release()
                    if failure is None:
                        break
                    report(-sent[0])
                    if attempt == 2:
                        with lock:
                            if len(errors) < 5:
                                errors.append(failure)
                        return
                    time.sleep(1 + attempt)
                with lock:
                    state_stream.write(f"{index}\n")
                    state_stream.flush()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        state_stream.close()

    if errors:
        logger.debug(f"分段下载未完成 {temp_file.name}: {errors[0]}")
        return False
    return True


def download_single_file(
    url: str,
    dest: Path,
    expected_size: int,
    progress: Optional[Progress] = None,
    overall_task_id: Optional[TaskID] = None,
    resume_support: bool = True,
    expected_md5: Optional[str] = None,
) -> bool:
    """
    下载单个文件，支持断点续传和进度显示

    大文件（>= SEGMENT_THRESHOLD）在 CDN 支持 Range 时自动分段多连接下载；
    分段状态记录在 <temp>.segments，粗粒度续传（只补未完成的块）。

    Args:
        url: 下载URL
        dest: 目标路径
        expected_size: 期望文件大小
        progress: Rich Progress实例
        overall_task_id: 总任务ID（用于更新总进度）
        resume_support: 是否支持断点续传
        expected_md5: 可选的清单 MD5，临时文件通过校验后才替换目标

    Returns:
        是否成功
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    task_id = None
    if progress:
        task_id = progress.add_task(description=dest.name, total=expected_size if expected_size > 0 else None)
        color = RAINBOW_COLORS[task_id % len(RAINBOW_COLORS)]
        progress.update(task_id, description=f"[{color}]{dest.name}[/{color}]")

    temp_file = dest.with_suffix(dest.suffix + ".temp")
    reported = 0
    progress_lock = threading.Lock()

    def update_progress(completed: int) -> None:
        nonlocal reported
        if progress:
            with progress_lock:
                delta = completed - reported
                if task_id is not None:
                    progress.update(task_id, completed=completed)
                if overall_task_id is not None and delta:
                    progress.update(overall_task_id, advance=delta)
                reported = completed

    def report_bytes(delta: int) -> None:
        nonlocal reported
        if progress:
            with progress_lock:
                if task_id is not None:
                    progress.update(task_id, advance=delta)
                if overall_task_id is not None:
                    progress.update(overall_task_id, advance=delta)
                reported += delta

    def verified() -> bool:
        if not temp_file.is_file():
            return False
        # expected_size <= 0 表示大小未知：下载完即视为成功。
        if expected_size <= 0:
            return True
        if temp_file.stat().st_size != expected_size:
            return False
        if expected_md5 is None:
            return True
        # 官方清单仅提供 MD5，此处用于资源一致性而非安全认证。
        digest = hashlib.md5(usedforsecurity=False)
        with temp_file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected_md5.lower()

    try:
        for attempt in range(3):
            try:
                if dest.is_symlink() or temp_file.is_symlink():
                    raise NetworkError("拒绝通过软链接写入下载文件")
                if expected_size < 0:
                    raise NetworkError("文件大小不能为负数")
                if expected_size > 0 and temp_file.exists() and temp_file.stat().st_size >= expected_size:
                    if verified():
                        clear_segment_state(temp_file)
                        temp_file.replace(dest)
                        update_progress(expected_size)
                        return True
                    # 分段状态还在时保留文件和状态，交给分段下载按块续传。
                    if not _segment_state_path(temp_file).exists():
                        temp_file.unlink()
                        clear_segment_state(temp_file)

                if resume_support and expected_size >= SEGMENT_THRESHOLD:
                    remote_size = probe_range_support(url)
                    if remote_size == expected_size:
                        update_progress(0)
                        if not _download_segmented(url, temp_file, expected_size, report_bytes):
                            logger.debug(f"分段下载未完成，保留临时文件以便续传: {dest.name}")
                            raise NetworkError(f"分段下载未完成: {dest.name}")
                        if verified():
                            clear_segment_state(temp_file)
                            temp_file.replace(dest)
                            update_progress(expected_size)
                            return True
                        clear_segment_state(temp_file)
                        temp_file.unlink(missing_ok=True)
                        update_progress(0)
                        raise NetworkError(f"文件大小或 MD5 校验失败: {dest.name}")

                resume_byte = temp_file.stat().st_size if resume_support and temp_file.exists() else 0
                headers = {"User-Agent": USER_AGENT}
                if resume_byte:
                    headers["Range"] = f"bytes={resume_byte}-"
                update_progress(resume_byte)

                req = Request(url, headers=headers)
                with urlopen(req, timeout=15) as rsp:
                    if rsp.status not in (200, 206):
                        raise NetworkError(f"HTTP {rsp.status}")
                    if rsp.status == 206:
                        content_range = rsp.headers.get("Content-Range", "")
                        if not content_range.startswith(f"bytes {resume_byte}-"):
                            temp_file.unlink(missing_ok=True)
                            raise NetworkError(f"无效的 Content-Range: {content_range}")
                    else:
                        # CDN 忽略 Range 时从头覆盖，不能把完整响应追加到断点之后。
                        resume_byte = 0
                    update_progress(resume_byte)
                    mode = "ab" if resume_byte else "wb"
                    with temp_file.open(mode) as stream:
                        for chunk in iter(lambda: rsp.read(READ_SIZE), b""):
                            stream.write(chunk)
                            resume_byte += len(chunk)
                            if expected_size > 0:
                                update_progress(min(resume_byte, expected_size))
                            else:
                                update_progress(resume_byte)

                if verified():
                    clear_segment_state(temp_file)
                    temp_file.replace(dest)
                    return True
                if expected_size > 0 and temp_file.stat().st_size >= expected_size:
                    temp_file.unlink()
                    clear_segment_state(temp_file)
                    update_progress(0)
                raise NetworkError(f"文件大小或 MD5 校验失败: {dest.name}")
            except Exception as e:
                if attempt == 2:
                    logger.error(f"下载失败 {dest.name}: {e}")
                    return False
                time.sleep(1 + attempt)
    finally:
        if progress and task_id is not None:
            progress.remove_task(task_id)
    return False


def batch_download(
    tasks: List[dict],
    progress: Optional[Progress] = None,
    max_workers: int = 8,
) -> tuple:
    """
    并行下载任务列表

    Args:
        tasks: [{"url": ..., "path": ..., "size": ...}, ...]
        progress: 可选的Progress实例
        max_workers: 最大线程数

    Returns:
        (failed_count, failed_names)
    """
    if not tasks:
        logger.info("没有文件需要下载")
        return 0, []

    total_size = sum(t["size"] for t in tasks)

    own_progress = progress is None
    if own_progress:
        progress = create_download_progress(transient=True)

    with progress:
        overall_task = progress.add_task("Total Download", total=total_size)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for task in tasks:
                future = executor.submit(
                    download_single_file,
                    task["url"],
                    task["path"],
                    task["size"],
                    progress,
                    overall_task,
                    expected_md5=task.get("md5"),
                )
                futures[future] = task.get("name", task["path"].name)

            failed = []
            for future in as_completed(futures):
                name = futures[future]
                if not future.result():
                    failed.append(name)

    return len(failed), failed


__all__ = [
    "create_download_progress",
    "download_single_file",
    "batch_download",
    "probe_range_support",
    "clear_segment_state",
    "available_disk_space",
    "format_bytes",
    "space_shortage",
    "check_space_requirement",
    "DownloadSpaceError",
    "NetworkError",
    "RAINBOW_COLORS",
    "RainbowBarColumn",
]
