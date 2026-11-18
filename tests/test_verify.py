"""统一校验模块：状态判定、摘要缓存复用、分块扫描与分块修复的读盘次数。"""

import hashlib
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ww_manager import verify
from ww_manager.chunk_repair import repair_file
from ww_manager.core import MD5Cache

CHUNK = 1024


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def chunk_list(payload: bytes) -> list:
    return [
        {"start": start, "end": min(start + CHUNK - 1, len(payload) - 1), "md5": md5(payload[start : start + CHUNK])}
        for start in range(0, len(payload), CHUNK)
    ]


class CountingCache:
    """记录命中次数的摘要缓存替身，用来确认没重复读盘。"""

    def __init__(self):
        self.values = {}
        self.computed = []
        self.hits = 0

    def get(self, path: Path, *, on_bytes=None):
        rel = Path(path).as_posix()
        if rel in self.values:
            self.hits += 1
            return self.values[rel]
        self.computed.append(rel)
        return self.values.get(rel)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_digest_helpers_agree_with_hashlib(self):
        data = b"abc" * 1000
        path = self.put("a.bin", data)
        self.assertEqual(verify.file_md5(path), md5(data))
        self.assertEqual(verify.safe_file_md5(path), md5(data))
        self.assertEqual(verify.digest_of(data), md5(data))
        self.assertEqual(verify.range_md5(path, 10, 2000), md5(data[10:2001]))

    def test_safe_file_md5_returns_none_for_missing_file(self):
        self.assertIsNone(verify.safe_file_md5(self.root / "nope.bin"))

    def test_file_matches_checks_size_before_digest(self):
        data = b"payload"
        path = self.put("a.bin", data)
        self.assertTrue(verify.file_matches(path, len(data), md5(data)))
        self.assertTrue(verify.file_matches(path, len(data), None))
        self.assertFalse(verify.file_matches(path, len(data) + 1, md5(data)))
        self.assertFalse(verify.file_matches(path, len(data), md5(b"other")))
        self.assertFalse(verify.file_matches(self.root / "nope.bin", 1, None))

    def test_check_files_reports_each_failure_kind(self):
        good = self.put("good.bin", b"good")
        short = self.put("short.bin", b"short")
        corrupt = self.put("corrupt.bin", b"corrupt")
        missing = self.root / "missing.bin"
        entries = [
            ("good", good, 4, md5(b"good")),
            ("short", short, 99, md5(b"short")),
            ("corrupt", corrupt, 7, md5(b"corrupt!")),
            ("missing", missing, 1, md5(b"x")),
            ("no-md5", good, 4, None),
        ]
        status = verify.check_files(entries)
        self.assertEqual(
            status,
            {"good": "ok", "short": "size", "corrupt": "md5", "missing": "missing", "no-md5": "ok"},
        )

    def test_check_files_uses_cache_and_skips_rehash(self):
        entries = []
        cache = CountingCache()
        for index in range(6):
            data = bytes([index]) * 4096
            path = self.put(f"{index}.bin", data)
            cache.values[path.as_posix()] = md5(data)
            entries.append((f"{index}", path, len(data), md5(data)))
        self.assertEqual(set(verify.check_files(entries, cache=cache).values()), {"ok"})
        self.assertEqual(cache.computed, [])  # 全部命中缓存，一次也没读盘

    def test_check_files_reports_first_seen_digest_through_cache(self):
        data = b"z" * 8192
        path = self.put("a.bin", data)
        cache = CountingCache()
        self.assertEqual(verify.check_files([("a", path, len(data), md5(data))], cache=cache)["a"], "md5")
        self.assertEqual(cache.computed, [path.as_posix()])  # 未命中时确实算了一次

    def test_check_files_parallel_matches_sequential(self):
        entries = []
        for index in range(40):
            data = os.urandom(1024 + index)
            path = self.put(f"d{index % 4}/{index}.bin", data)
            entries.append((str(index), path, len(data), md5(data)))
        expected = {key: "ok" for key, _, _, _ in entries}
        self.assertEqual(verify.check_files(entries, workers=8), expected)
        self.assertEqual(verify.check_files(entries, workers=1), expected)

    def test_check_files_advances_progress_as_each_file_finishes(self):
        """进度必须在每项完成时就推进；等全部算完再统一推进，进度条会整段时间停在 0%。"""
        events = []

        class FakeProgress:
            def add_task(self, description, total=None):
                events.append("add")
                return 0

            def advance(self, task_id, advance=1):
                events.append("advance")

            def update(self, task_id, **kwargs):
                pass

        real_md5 = verify.safe_file_md5

        def spy(path, *args, **kwargs):
            events.append("hash")
            return real_md5(path, *args, **kwargs)

        entries = []
        for index in range(3):
            data = b"x" * (16 + index)
            path = self.put(f"p{index}.bin", data)
            entries.append((str(index), path, len(data), md5(data)))

        with patch.object(verify, "safe_file_md5", spy):
            verify.check_files(entries, progress=FakeProgress(), workers=1)

        self.assertEqual(events, ["add", "hash", "advance", "hash", "advance", "hash", "advance"])

    def test_check_files_advances_inside_one_large_file(self):
        """单个大文件也要在读取过程中推进，而不是只在读完那一刻跳一次。"""
        data = os.urandom(verify.BLOCK_SIZE * 3 + 7)
        path = self.put("big.bin", data)
        advances = []

        class FakeProgress:
            def add_task(self, description, total=None):
                return 0

            def advance(self, task_id, advance=1):
                advances.append(advance)

            def update(self, task_id, **kwargs):
                pass

        with patch.object(verify, "_PROGRESS_FLUSH", verify.BLOCK_SIZE):
            verify.check_files([("big", path, len(data), md5(data))], progress=FakeProgress(), workers=1)

        self.assertGreaterEqual(len(advances), 4)  # 3 个整块各推一次，尾部再补一次
        self.assertEqual(sum(advances), len(data))

    def test_check_files_progress_totals_match_expected_bytes(self):
        """缓存命中、大小不符、文件缺失都不读盘，收尾时必须把差额补上，总推进量恰好等于清单字节。"""
        cache = CountingCache()
        entries = []
        for index in range(4):
            data = bytes([index]) * 2048
            path = self.put(f"c{index}.bin", data)
            cache.values[path.as_posix()] = md5(data)
            entries.append((f"hit{index}", path, len(data), md5(data)))
        short = self.put("short.bin", b"abc")
        entries.append(("short", short, 999, md5(b"abc")))
        entries.append(("gone", self.root / "gone.bin", 512, md5(b"x")))
        expected_total = sum(size for _, _, size, _ in entries)
        advances = []

        class FakeProgress:
            def add_task(self, description, total=None):
                self.total = total
                return 0

            def advance(self, task_id, advance=1):
                advances.append(advance)

            def update(self, task_id, **kwargs):
                pass

        progress = FakeProgress()
        verify.check_files(entries, cache=cache, progress=progress)
        self.assertEqual(progress.total, expected_total)
        self.assertEqual(sum(advances), expected_total)

    def test_chunk_digests_and_scan_agree(self):
        data = bytes(range(256)) * 20  # 5120 字节，正好 5 个 CHUNK
        path = self.put("c.bin", data)
        chunks = [normalize for normalize in _chunks(data)]
        self.assertEqual(verify.chunk_digests(path, chunks), [chunk[2] for chunk in chunks])
        whole, digests = verify.scan(path, chunks)
        self.assertEqual(whole, md5(data))
        self.assertEqual(digests, [chunk[2] for chunk in chunks])

    def test_scan_marks_truncated_tail_as_mismatch(self):
        data = bytes(range(256)) * 20
        chunks = _chunks(data)
        path = self.put("c.bin", data[: CHUNK * 3])  # 尾部两块被截掉
        whole, digests = verify.scan(path, chunks)
        self.assertEqual(whole, md5(data[: CHUNK * 3]))
        self.assertEqual(digests[:3], [chunk[2] for chunk in chunks[:3]])
        self.assertNotEqual(digests[3], chunks[3][2])

    def test_scan_reads_file_once(self):
        data = os.urandom(CHUNK * 8)
        path = self.put("c.bin", data)
        chunks = _chunks(data)
        calls = []
        real_pread = os.pread

        def counting(fd, size, offset):
            piece = real_pread(fd, size, offset)
            calls.append(len(piece))
            return piece

        with patch.object(verify.os, "pread", counting):
            verify.scan(path, chunks)
        self.assertEqual(sum(calls), len(data))  # 整文件 + 分块只走一遍顺序读

    def test_repair_reads_file_twice_not_four_times(self):
        payload = bytes(range(256)) * 24  # 6144 字节，6 个块
        path = self.put("d.bin", payload)
        broken = bytearray(payload)
        broken[CHUNK * 2 + 5] ^= 0xFF  # 第 3 块坏
        path.write_bytes(bytes(broken))

        server = RangeServer(("127.0.0.1", 0), RangeHandler)
        server.payload = payload
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/d.bin"
        scans = []
        real_scan = verify.scan

        def counting_scan(*args, **kwargs):
            scans.append(args[0])
            return real_scan(*args, **kwargs)

        try:
            with patch.object(verify, "scan", counting_scan):
                self.assertTrue(repair_file(url, path, chunk_list(payload), md5(payload)))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(len(scans), 2)  # 修前定位坏块一次、修后复验一次
        self.assertEqual(server.requested, [CHUNK * 2])  # 只重取了那一个坏块

    def test_repair_reports_digest_for_cache_reuse(self):
        payload = bytes(range(256)) * 8
        path = self.put("e.bin", payload[:-1] + b"\x00")
        server = RangeServer(("127.0.0.1", 0), RangeHandler)
        server.payload = payload
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/e.bin"
        seen = []
        try:
            self.assertTrue(
                repair_file(url, path, chunk_list(payload), md5(payload), on_verified=lambda p, d: seen.append(d))
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen, [md5(payload)])  # 复验时算过的摘要直接交回，不用再读


def _chunks(data: bytes) -> list:
    return [
        (start, min(start + CHUNK - 1, len(data) - 1), md5(data[start : start + CHUNK]))
        for start in range(0, len(data), CHUNK)
    ]


class RangeServer(ThreadingHTTPServer):
    payload: bytes
    requested: list


class RangeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        server.requested = getattr(server, "requested", [])
        start, end = 0, len(server.payload) - 1
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            start_text, _, end_text = header[6:].partition("-")
            start = int(start_text or 0)
            end = int(end_text) if end_text else end
            server.requested.append(start)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(server.payload)}")
        else:
            server.requested.append(0)
            self.send_response(200)
        body = server.payload[start : end + 1]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MD5CacheTests(unittest.TestCase):
    """MD5Cache.set 把刚算过的摘要写进缓存，get 再取时不该碰盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = MD5Cache(self.root / "cache.json", self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_then_get_hits_without_rehashing(self):
        data = b"seeded"
        path = self.root / "a.bin"
        path.write_bytes(data)
        self.cache.set(path, md5(data).upper())
        with patch("ww_manager.core.safe_file_md5", side_effect=AssertionError("不应重算")):
            self.assertEqual(self.cache.get(path), md5(data))

    def test_touched_file_misses_cache(self):
        data = b"seeded"
        path = self.root / "a.bin"
        path.write_bytes(data)
        self.cache.set(path, md5(data))
        path.write_bytes(data + b"!")
        self.assertNotEqual(self.cache.get(path), md5(data))


if __name__ == "__main__":
    unittest.main()
