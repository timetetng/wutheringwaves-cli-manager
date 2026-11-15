"""按官方 chunkInfos（100 MiB 分块 md5）修复本地文件，只重取坏块。

官方增量索引在 ``resource`` 与 ``groupInfos[].dstFiles`` 上都可能带 ``chunkInfos``：
``[{"start": 0, "end": 104857599, "md5": ...}, ...]``，闭区间、首尾相连、覆盖整份文件。
丢失或损坏一个分块时，整文件重下要付几百 MB 到 1 GB，按块重取只需约 100 MB。

这里的修复是**就地**的：调用方只在文件已确认不合格（md5 不符、被截断）时才调用，
因此中断最坏情况仍是这份已损坏的文件，不会额外破坏好文件。
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("WW_Manager")

USER_AGENT = "WW-Manager/2.0"
RANGE_READ_SIZE = 1024 * 1024
RANGE_TIMEOUT = 30

Chunk = Tuple[int, int, str]


def normalize_chunks(chunks: object, total_size: Optional[int] = None) -> Optional[List[Chunk]]:
    """校验 chunkInfos；不连续、越界或缺字段时返回 None（宁可不修，也不按猜测拼接）。"""
    if not isinstance(chunks, list) or not chunks:
        return None
    result: List[Chunk] = []
    expect = 0
    for item in chunks:
        if not isinstance(item, dict):
            return None
        start, end, digest = item.get("start"), item.get("end"), item.get("md5")
        if not isinstance(start, int) or not isinstance(end, int) or not isinstance(digest, str):
            return None
        if isinstance(start, bool) or isinstance(end, bool):
            return None
        if start != expect or end < start or len(digest) != 32:
            return None
        result.append((start, end, digest.lower()))
        expect = end + 1
    if total_size is not None and expect != total_size:
        return None
    return result


def chunk_map(index: object) -> Dict[str, List[Chunk]]:
    """从索引收集 dest → 分块表；只收覆盖整份文件的分块表。"""
    if not isinstance(index, dict):
        return {}
    result: Dict[str, List[Chunk]] = {}

    def take(item: object) -> None:
        if not isinstance(item, dict):
            return
        dest = item.get("dest")
        raw_size = item.get("size")
        if not isinstance(raw_size, int) or isinstance(raw_size, bool):
            return
        size = raw_size
        if not isinstance(dest, str) or not dest or size <= 0:
            return
        chunks = normalize_chunks(item.get("chunkInfos"), size)
        if chunks:
            result[dest] = chunks

    for item in _as_list(index.get("resource")):
        take(item)
    for group in _as_list(index.get("groupInfos")):
        if not isinstance(group, dict):
            continue
        for item in _as_list(group.get("dstFiles")):
            take(item)
    return result


def _as_list(value: object) -> Iterable:
    return value if isinstance(value, list) else []


def file_md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _range_md5(stream, start: int, end: int) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    stream.seek(start)
    remaining = end - start + 1
    while remaining > 0:
        block = stream.read(min(RANGE_READ_SIZE, remaining))
        if not block:
            break
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest()


def bad_chunks(path: Path, chunks: object) -> List[Chunk]:
    """按 chunkInfos 找出本地 md5 不符的分块；分块表不合法时返回空。"""
    normalized = normalize_chunks(chunks)
    if not normalized:
        return []
    return _bad_of(path, normalized)


def _bad_of(path: Path, chunks: List[Chunk]) -> List[Chunk]:
    """返回本地 md5 不符的分块；文件比总分块短时，超出部分也算坏块。"""
    if not path.is_file():
        return []
    size = path.stat().st_size
    bad: List[Chunk] = []
    with path.open("rb") as stream:
        for start, end, digest in chunks:
            if end >= size:
                bad.append((start, end, digest))
            elif _range_md5(stream, start, end) != digest:
                bad.append((start, end, digest))
    return bad


def _fetch_range(url: str, start: int, end: int) -> Optional[bytes]:
    """取一段字节；服务端不支持 Range 或长度不符时返回 None，由调用方整文件重下。"""
    headers = {"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"}
    try:
        with urlopen(Request(url, headers=headers), timeout=RANGE_TIMEOUT) as rsp:
            if rsp.status != 206:
                logger.debug(f"服务端未按 Range 返回（HTTP {rsp.status}），放弃分块修复")
                return None
            content_range = rsp.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {start}-{end}/"):
                logger.debug(f"Content-Range 不符: {content_range!r}，放弃分块修复")
                return None
            expected = end - start + 1
            buffer = bytearray()
            while len(buffer) < expected:
                block = rsp.read(min(RANGE_READ_SIZE, expected - len(buffer)))
                if not block:
                    break
                buffer.extend(block)
            if len(buffer) != expected:
                logger.debug(f"分块 {start}-{end} 长度不足: {len(buffer)}/{expected}")
                return None
            return bytes(buffer)
    except (HTTPError, URLError, OSError) as e:
        logger.debug(f"分块 {start}-{end} 下载失败: {e}")
        return None


def repair_file(
    url: str,
    path: Path,
    chunks: object,
    expected_md5: Optional[str] = None,
    *,
    label: Optional[str] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> bool:
    """就地重取坏块并复验；返回 True 表示文件现在与清单一致。

    调用前文件可以不存在、被截断或内容损坏；任一分块取不到就返回 False，
    由调用方退回整文件下载。整文件名单 md5 优先，没有 md5 时以各分块 md5 为准。
    """
    name = label or path.name
    normalized = normalize_chunks(chunks)
    if not normalized or not path.is_file():
        return False
    total = normalized[-1][1] + 1
    if path.stat().st_size > total:
        logger.debug(f"{name}: 文件比分块总和更长，放弃分块修复")
        return False
    bad = _bad_of(path, normalized)
    if not bad:
        return expected_md5 is None or file_md5(path) == expected_md5.lower()
    # 文件比清单短说明尾部整段缺失，补齐这些块才能让大小恢复。
    repaired_mib = sum(end - start + 1 for start, end, _ in bad) // 1048576
    logger.info(f"{name}: 按分块修复 {len(bad)}/{len(normalized)} 个块（约 {repaired_mib} MiB）")
    try:
        with path.open("r+b" if path.stat().st_size else "w+b") as stream:
            for start, end, digest in bad:
                if cancelled and cancelled():
                    raise KeyboardInterrupt
                payload = _fetch_range(url, start, end)
                if payload is None or hashlib.md5(payload, usedforsecurity=False).hexdigest() != digest:
                    return False
                stream.seek(start)
                stream.write(payload)
            stream.truncate(total)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as e:
        logger.debug(f"{name}: 写入分块失败: {e}")
        return False
    if expected_md5 is not None and file_md5(path) != expected_md5.lower():
        logger.warning(f"{name}: 分块已重取但整文件 MD5 仍不符，将整文件重下")
        return False
    if _bad_of(path, normalized):
        logger.warning(f"{name}: 分块 md5 复验失败，将整文件重下")
        return False
    logger.info(f"{name}: 分块修复完成")
    return True


__all__ = [
    "Chunk",
    "bad_chunks",
    "chunk_map",
    "file_md5",
    "normalize_chunks",
    "repair_file",
]
