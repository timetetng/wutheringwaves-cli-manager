# verify.py
"""统一校验模块：MD5 计算、按文件并行核对、按分块比对。

下载侧早已集中在 :mod:`ww_manager.download`，校验侧却散落在 core、incremental、
resource_updates、chunk_repair 与 download 各自维护一份块大小和线程数。这里只留
一份实现：块大小、线程上限、摘要构造方式都在本文件定义，其余模块一律调用它，
所以换块大小或调并行度只改这一处。

MD5 无法切分并行（后一块的输入依赖前一块的状态），能并行的是**多个文件**和
**同一文件的多个分块**。因此本模块提供三种粒度：

* :func:`file_md5` —— 单文件顺序摘要；
* :func:`chunk_digests` —— 同一文件的各分块并行摘要；
* :func:`parallel_map` —— 把「一个文件一项」的核对逻辑丢进同一个线程池。
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# 顺序读块的默认大小；实测 1 MiB~16 MiB 之间差异在 5% 内，4 MiB 对缓存更友好。
BLOCK_SIZE = 4 * 1024 * 1024
# 默认并行度：纯 CPU 场景 8 线程的 md5 能跑到约 4.9 GiB/s（单线程 0.83 GiB/s）。
# 真实游戏目录往往先撞磁盘或压缩文件系统的顶（本机 btrfs+zstd 约 1.6 GiB/s），
# 那时线程越多越互相抢，可试 WWM_VERIFY_WORKERS=4；具体取值以实测为准。
DEFAULT_WORKERS = 8
MAX_WORKERS = 32
WORKERS_ENV = "WWM_VERIFY_WORKERS"
# 块级进度至少挣够这么多字节才动一次进度条：rich 的刷新率是 10 次/秒，更密只是白抢锁。
_PROGRESS_FLUSH = 32 * 1024 * 1024


def worker_count(workers: Optional[int] = None, pending: Optional[int] = None) -> int:
    """解析并行度：显式参数 > 环境变量 > 默认值，且不超过上限与待办数。"""
    if workers is None:
        raw = os.environ.get(WORKERS_ENV, "").strip()
        try:
            workers = int(raw) if raw else DEFAULT_WORKERS
        except ValueError:
            workers = DEFAULT_WORKERS
    if workers <= 0:
        workers = DEFAULT_WORKERS
    workers = min(MAX_WORKERS, int(workers))
    if pending is not None:
        workers = min(workers, max(1, int(pending)))
    return max(1, workers)


def new_digest():
    """构造本模块统一使用的摘要对象（官方清单只有 MD5，此处用于一致性而非安全认证）。"""
    return hashlib.md5(usedforsecurity=False)


def digest_of(data: bytes) -> str:
    """计算一段字节的摘要（分块重取后与 chunkInfos 比对用）。"""
    digest = new_digest()
    digest.update(data)
    return digest.hexdigest()


def file_md5(path: Path, block: int = BLOCK_SIZE, *, on_bytes: Optional[Callable[[int], None]] = None) -> str:
    """顺序计算整个文件的 MD5；读失败时抛 OSError，由调用方决定是否降级。

    on_bytes 给出时每读完一块回报字节数：大文件校验期间进度条可以平滑推进，不再只在读完时跳一次。
    """
    digest = new_digest()
    with open(path, "rb") as stream:
        for piece in iter(lambda: stream.read(block), b""):
            digest.update(piece)
            if on_bytes is not None:
                on_bytes(len(piece))
    return digest.hexdigest()


def safe_file_md5(
    path: Path, block: int = BLOCK_SIZE, *, on_bytes: Optional[Callable[[int], None]] = None
) -> Optional[str]:
    """与 :func:`file_md5` 相同，但读失败返回 None 供「算不出来就别当通过」的调用点使用。"""
    try:
        return file_md5(path, block, on_bytes=on_bytes)
    except (OSError, ValueError):
        return None


def file_matches(path: Path, size: Optional[int] = None, md5: Optional[str] = None) -> bool:
    """先比大小再算摘要；清单没给 md5 时只认大小，文件不存在或读不了即不通过。"""
    try:
        if not path.is_file():
            return False
        if size is not None and path.stat().st_size != size:
            return False
    except OSError:
        return False
    if not md5:
        return True
    return safe_file_md5(path) == md5.lower()


def range_md5(path: Path, start: int, end: int, block: int = BLOCK_SIZE) -> str:
    """按闭区间 [start, end] 计算 MD5；文件比区间短时抛 OSError（视为该分块不可用）。"""
    digest = new_digest()
    offset = start
    remaining = end - start + 1
    if remaining <= 0:
        return digest.hexdigest()
    fd = os.open(path, os.O_RDONLY)
    try:
        while remaining > 0:
            piece = os.pread(fd, min(block, remaining), offset)
            if not piece:
                raise OSError(f"文件短于分块区间: {path} [{start}, {end}]")
            digest.update(piece)
            offset += len(piece)
            remaining -= len(piece)
    finally:
        os.close(fd)
    return digest.hexdigest()


def chunk_digests(
    path: Path, chunks: Sequence[Tuple[int, int, str]], workers: Optional[int] = None
) -> List[Optional[str]]:
    """并行计算各分块 MD5；越界或读不到的分块返回 None。"""
    items = list(chunks)
    if not items:
        return []
    results: List[Optional[str]] = [None] * len(items)

    def one(index: int):
        start, end = items[index][0], items[index][1]
        try:
            return index, range_md5(path, start, end)
        except OSError:
            return index, None

    if len(items) == 1:
        for index, digest in (one(0),):
            results[index] = digest
        return results
    with ThreadPoolExecutor(max_workers=worker_count(workers, len(items))) as pool:
        for index, digest in pool.map(one, range(len(items))):
            results[index] = digest
    return results


def scan(path: Path, chunks: Sequence[Tuple[int, int, str]], workers: Optional[int] = None) -> Tuple[str, List[str]]:
    """一次读盘同时得到整文件 MD5 与各分块 MD5。

    分块修复既要判断「整文件对不对」又要判断「哪个块坏了」，分开算要读两遍盘。
    这里顺着文件读一遍：整文件摘要由读取线程顺序更新，落在同一块里的字节顺手丢给
    线程池更新对应分块的摘要，两套结果只付一次顺序读的代价。
    """
    items = list(chunks)
    whole = new_digest()
    digests = [new_digest() for _ in items]
    locks = [threading.Lock() for _ in items]
    limit = worker_count(workers, max(1, len(items))) * 2

    def feed(index: int, piece: bytes) -> None:
        # 同一分块的多次更新必须串行，否则 hashlib 状态会被并发写坏。
        with locks[index]:
            digests[index].update(piece)

    pool = ThreadPoolExecutor(max_workers=worker_count(workers, len(items))) if items else None
    pending: deque = deque()
    fd = os.open(path, os.O_RDONLY)
    try:
        offset = 0
        while True:
            piece = os.pread(fd, BLOCK_SIZE, offset)
            if not piece:
                break
            whole.update(piece)
            block_start = offset
            offset += len(piece)
            if pool is None:
                continue
            for index, item in enumerate(items):
                chunk_start, chunk_end = item[0], item[1]
                if chunk_end < block_start or chunk_start >= offset:
                    continue
                head = max(chunk_start, block_start) - block_start
                tail = min(chunk_end + 1, offset) - block_start
                pending.append(pool.submit(feed, index, piece[head:tail]))
                if len(pending) > limit:
                    pending.popleft().result()
        for future in pending:
            future.result()
    finally:
        os.close(fd)
        if pool is not None:
            pool.shutdown()
    return whole.hexdigest(), [digest.hexdigest() for digest in digests]


def check_files(
    entries: Iterable[Tuple[object, Path, Optional[int], Optional[str]]],
    *,
    cache: Optional[object] = None,
    progress: Optional[object] = None,
    description: str = "校验文件",
    workers: Optional[int] = None,
) -> Dict[object, str]:
    """并发核对一批文件，返回 ``{标识: 状态}``，状态取 ``ok`` / ``missing`` / ``size`` / ``md5``。

    entries 每项为 ``(标识, Path, 期望大小, 期望 MD5)``；大小或 MD5 为 None 时只按能比的部分判定：
    不是普通文件 → missing，大小不符 → size，MD5 不符 → md5，其余 ok。

    之所以把判定放在这里：core、incremental、resource_updates 原本各写一份同样的「先比大小、
    再算 MD5」循环，且只有 core 是多线程的，其余都是单线程逐个算。「算 MD5」这一件事在全程序
    只应该有这一份实现，线程数、块大小和状态语义才不会再走偏。

    cache 给出时 MD5 走摘要缓存（未命中才现算并回填），不给则每项现算。
    progress 为 rich Progress 时按字节推进，总字节在内部统计，调用方不用再算。
    """
    jobs = [(key, Path(path), size, md5) for key, path, size, md5 in entries]
    if not jobs:
        return {}
    status: Dict[object, str] = {}
    weights = [int(size or 0) for _, _, size, _ in jobs]
    byte_mode = bool(sum(weights))
    task_id = None
    if progress is not None:
        task_id = progress.add_task(f"[cyan]{description}...", total=sum(weights) or len(jobs))
    counters = [{"inline": 0, "pending": 0} for _ in jobs]

    def ticker(index):
        """返回该任务的「读完一块就报字节」回调。

        撑够 _PROGRESS_FLUSH 才动一次进度条：块级推进否则每个 4 MiB 块都要抢一次 rich 的锁，
        八个线程同时算哈希时会白白变成竞争点。rich Progress 自带 RLock，跨线程 advance 是安全的。
        """
        counter = counters[index]

        def tick(count: int) -> None:
            if task_id is None or not count:
                return
            counter["pending"] += count
            if counter["pending"] >= _PROGRESS_FLUSH:
                progress.advance(task_id, counter["pending"])
                counter["inline"] += counter["pending"]
                counter["pending"] = 0

        return tick

    def one(index):
        key, path, size, md5 = jobs[index]
        on_bytes = ticker(index) if task_id is not None and byte_mode else None
        try:
            if not path.is_file():
                return key, "missing", int(size or 0)
            actual = path.stat().st_size
            if size is not None and actual != size:
                return key, "size", int(size)
        except OSError:
            return key, "missing", int(size or 0)
        if md5 is None:
            return key, "ok", int(size or 0)
        expected = md5.lower()
        if cache is not None:
            digest = cache.get(path, on_bytes=on_bytes)
        else:
            digest = safe_file_md5(path, on_bytes=on_bytes)
        return key, ("ok" if digest == expected else "md5"), int(size or 0)

    def note(index, result):
        """一项收尾：先补齐「没被读到」的字节（缓存命中、大小不符、文件缺失），再刷新当前文件名。

        不承这笔差额的话，命中缓存的部分永远不会被推进，进度条最终停在半途。
        """
        key, verdict, expected = result
        status[key] = verdict
        if task_id is None:
            return
        if byte_mode:
            counter = counters[index]
            bypassed = max((expected or 0) - counter["inline"] - counter["pending"], 0)
            delta = counter["pending"] + bypassed
            counter["pending"] = 0
            if delta:
                progress.advance(task_id, delta)
        else:
            progress.advance(task_id)
        progress.update(task_id, description=f"[cyan]{description}: {getattr(key, 'name', key)}[/cyan]")

    # 重活（大文件）先提交：线程池做完一项立刻取下一项，小文件正好填进大文件留下的空档，
    # 比按清单顺序提交少一次「大文件排在最后、其余线程先空转」的等待。
    order = sorted(range(len(jobs)), key=lambda index: -weights[index])
    results = [None] * len(jobs)
    if len(jobs) == 1 or worker_count(workers, len(jobs)) == 1:
        for index in range(len(jobs)):
            results[index] = one(index)
            note(index, results[index])
    else:
        with ThreadPoolExecutor(max_workers=worker_count(workers, len(jobs))) as pool:
            futures = {pool.submit(one, index): index for index in order}
            # 边完成边推进：等 as_completed 全部收完再统一推进，进度条会整段时间停在 0%。
            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()
                note(index, results[index])

    for key, state, _done in results:
        status[key] = state
    return status


def parallel_map(func: Callable, items: Iterable, workers: Optional[int] = None) -> List:
    """保序并行映射；只有一项时不建线程池，异常按原样抛给调用方。"""
    entries = list(items)
    if not entries:
        return []
    if len(entries) == 1 or worker_count(workers, len(entries)) == 1:
        return [func(entry) for entry in entries]
    with ThreadPoolExecutor(max_workers=worker_count(workers, len(entries))) as pool:
        return list(pool.map(func, entries))


__all__ = [
    "BLOCK_SIZE",
    "DEFAULT_WORKERS",
    "check_files",
    "chunk_digests",
    "digest_of",
    "file_matches",
    "file_md5",
    "new_digest",
    "parallel_map",
    "range_md5",
    "safe_file_md5",
    "scan",
    "worker_count",
]
