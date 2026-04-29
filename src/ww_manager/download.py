# download.py
"""
通用下载模块：提供带进度条的并行下载支持
"""

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional
from urllib.request import Request, urlopen

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

logger = logging.getLogger("WW_Manager")


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


def create_download_progress(transient: bool = True) -> Progress:
    """创建下载进度条"""
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        "[progress.percentage]{task.percentage:>3.1f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        expand=True,
        transient=transient,
    )


def download_single_file(
    url: str,
    dest: Path,
    expected_size: int,
    progress: Optional[Progress] = None,
    overall_task_id: Optional[TaskID] = None,
    resume_support: bool = True,
) -> bool:
    """
    下载单个文件，支持断点续传和进度显示

    Args:
        url: 下载URL
        dest: 目标路径
        expected_size: 期望文件大小
        progress: Rich Progress实例
        overall_task_id: 总任务ID（用于更新总进度）
        resume_support: 是否支持断点续传

    Returns:
        是否成功
    """
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
            if resume_support and temp_file.exists():
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
    "NetworkError",
    "RAINBOW_COLORS",
]
