# download.py
"""
通用下载模块：提供带进度条的并行下载支持
"""

import hashlib
import logging
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
    expected_md5: Optional[str] = None,
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
        expected_md5: 可选的清单 MD5，临时文件通过校验后才替换目标

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

    def verified() -> bool:
        if not temp_file.is_file() or temp_file.stat().st_size != expected_size:
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
                if temp_file.exists() and temp_file.stat().st_size >= expected_size:
                    if verified():
                        temp_file.replace(dest)
                        return True
                    temp_file.unlink()

                resume_byte = temp_file.stat().st_size if resume_support and temp_file.exists() else 0
                headers = {"User-Agent": "WW-Manager/2.0"}
                if resume_byte:
                    headers["Range"] = f"bytes={resume_byte}-"
                if progress and task_id is not None:
                    progress.update(task_id, completed=resume_byte)

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
                    mode = "ab" if resume_byte else "wb"
                    with temp_file.open(mode) as stream:
                        for chunk in iter(lambda: rsp.read(256 * 1024), b""):
                            stream.write(chunk)
                            if progress:
                                if task_id is not None:
                                    progress.update(task_id, advance=len(chunk))
                                if overall_task_id is not None:
                                    progress.update(overall_task_id, advance=len(chunk))

                if verified():
                    temp_file.replace(dest)
                    return True
                if temp_file.stat().st_size >= expected_size:
                    temp_file.unlink()
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
    "NetworkError",
    "RAINBOW_COLORS",
]
