"""按官方 chunkInfos（100 MiB 分块 md5）修复本地文件，只重取坏块。

官方增量索引在 ``resource`` 与 ``groupInfos[].dstFiles`` 上都可能带 ``chunkInfos``：
``[{"start": 0, "end": 104857599, "md5": ...}, ...]``，闭区间、首尾相连、覆盖整份文件。
丢失或损坏一个分块时，整文件重下要付几百 MB 到 1 GB，按块重取只需约 100 MB。

这里的修复是**就地**的：调用方只在文件已确认不合格（md5 不符、被截断）时才调用，
因此中断最坏情况仍是这份已损坏的文件，不会额外破坏好文件。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import verify

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
    """整文件 MD5；分块修复的所有摘要计算都走 :mod:`ww_manager.verify`。"""
    return verify.file_md5(path)


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
    digests = verify.chunk_digests(path, chunks)
    return [chunk for chunk, digest in zip(chunks, digests) if digest != chunk[2]]


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
    on_verified: Optional[Callable[[Path, str], None]] = None,
) -> bool:
    """就地重取坏块并复验；返回 True 表示文件现在与清单一致。

    调用前文件可以不存在、被截断或内容损坏；任一分块取不到就返回 False，
    由调用方退回整文件下载。整文件名单 md5 优先，没有 md5 时以各分块 md5 为准。
    on_verified 会在修复好的摘要算完时回调 (path, md5)，供缓存直接复用这份结果。
    """
    name = label or path.name
    normalized = normalize_chunks(chunks)
    if not normalized or not path.is_file():
        return False
    total = normalized[-1][1] + 1
    if path.stat().st_size > total:
        logger.debug(f"{name}: 文件比分块总和更长，放弃分块修复")
        return False
    # 一次读取同时得到整文件与各分块摘要，省掉「先按块比对、再算整文件」的第二遍盘。
    whole_md5, digests = verify.scan(path, normalized)
    bad = [chunk for chunk, digest in zip(normalized, digests) if digest != chunk[2]]
    if not bad:
        # 分块全对但整文件名单不同，说明分块表与清单不是同一版，不能认为修好了。
        return expected_md5 is None or whole_md5 == expected_md5.lower()
    # 文件比清单短说明尾部整段缺失，补齐这些块才能让大小恢复。
    repaired_mib = sum(end - start + 1 for start, end, _ in bad) // 1048576
    logger.info(f"{name}: 按分块修复 {len(bad)}/{len(normalized)} 个块（约 {repaired_mib} MiB）")
    try:
        with path.open("r+b" if path.stat().st_size else "w+b") as stream:
            for start, end, digest in bad:
                if cancelled and cancelled():
                    raise KeyboardInterrupt
                payload = _fetch_range(url, start, end)
                if payload is None or verify.digest_of(payload) != digest:
                    return False
                stream.seek(start)
                stream.write(payload)
            stream.truncate(total)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as e:
        logger.debug(f"{name}: 写入分块失败: {e}")
        return False
    whole_md5, digests = verify.scan(path, normalized)
    if expected_md5 is not None:
        if whole_md5 != expected_md5.lower():
            logger.warning(f"{name}: 分块已重取但整文件 MD5 仍不符，将整文件重下")
            return False
    elif any(digest != chunk[2] for chunk, digest in zip(normalized, digests)):
        logger.warning(f"{name}: 分块 md5 复验失败，将整文件重下")
        return False
    logger.info(f"{name}: 分块修复完成")
    if on_verified is not None and expected_md5 is not None:
        on_verified(path, whole_md5)
    return True


__all__ = [
    "Chunk",
    "bad_chunks",
    "chunk_map",
    "file_md5",
    "normalize_chunks",
    "repair_file",
]
