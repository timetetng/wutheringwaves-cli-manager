#!/usr/bin/env python3
"""通用下载器回归测试：分段并行、Range 回退、粗粒度续传。仅访问本地 HTTP。"""

import hashlib
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ww_manager import download
from ww_manager.download import download_single_file

PAYLOAD = os.urandom(4 * 1024 * 1024)
BLOCK = 256 * 1024


def md5(data):
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class RangeServer(ThreadingHTTPServer):
    payload: bytes
    requests: list
    allow_range: bool
    fail_ranges: set


class RangeHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        assert isinstance(self.server, RangeServer)
        header = self.headers.get("Range")
        self.server.requests.append((self.path, header))
        payload = self.server.payload
        if self.path == "/missing":
            self.send_error(404)
            return
        if header and self.server.allow_range:
            start, end = header.split("=")[1].split("-")
            start = int(start)
            end = int(end) if end else len(payload) - 1
            if start in self.server.fail_ranges:
                self.send_error(500)
                return
            end = min(end, len(payload) - 1)
            body = payload[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class CountingSemaphore:
    """带计数的连接预算，用来断言「按块申请」而不是按文件一次性预占。"""

    def __init__(self, value):
        self._sem = threading.BoundedSemaphore(value)
        self._lock = threading.Lock()
        self.held = 0
        self.max_held = 0
        self.acquires = 0

    def acquire(self):
        self._sem.acquire()
        with self._lock:
            self.held += 1
            self.acquires += 1
            self.max_held = max(self.max_held, self.held)

    def release(self):
        with self._lock:
            self.held -= 1
        self._sem.release()


class DownloadSegmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = RangeServer(("127.0.0.1", 0), RangeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/file"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ww-download-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server.payload = PAYLOAD
        self.server.requests = []
        self.server.allow_range = True
        self.server.fail_ranges = set()
        for name, value in (
            ("SEGMENT_THRESHOLD", BLOCK),
            ("SEGMENTS", 4),
            ("MAX_CONNECTIONS", 8),
            ("SEGMENT_BLOCK_SIZE", BLOCK),
        ):
            patcher = patch.object(download, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(download, "_connection_slots", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch("ww_manager.download.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_segmented_download_matches_md5(self):
        dest = self.root / "big.pak"
        self.assertTrue(download_single_file(self.url, dest, len(PAYLOAD), expected_md5=md5(PAYLOAD)))
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        self.assertFalse((self.root / "big.pak.temp").exists())
        self.assertFalse((self.root / "big.pak.temp.segments").exists())
        ranges = [r for _, r in self.server.requests if r]
        self.assertGreater(len(ranges), 1, "大文件应使用多个 Range 请求")

    def test_ignored_range_falls_back_to_single_connection(self):
        dest = self.root / "fallback.pak"
        self.server.allow_range = False
        self.assertTrue(download_single_file(self.url, dest, len(PAYLOAD), expected_md5=md5(PAYLOAD)))
        self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_wrong_md5_keeps_no_dest(self):
        dest = self.root / "bad.pak"
        self.assertFalse(download_single_file(self.url, dest, len(PAYLOAD), expected_md5="0" * 32))
        self.assertFalse(dest.exists())
        self.assertFalse(Path(str(dest) + ".temp").exists())

    def test_small_file_skips_segmentation(self):
        dest = self.root / "small.pak"
        data = PAYLOAD[: BLOCK // 2]
        self.server.payload = data
        self.assertTrue(download_single_file(self.url, dest, len(data), expected_md5=md5(data)))
        ranges = [r for _, r in self.server.requests if r]
        self.assertEqual(ranges, [])

    def test_resume_only_requests_missing_blocks(self):
        dest = self.root / "resume.pak"
        failing_block = 2
        self.server.fail_ranges = {failing_block * BLOCK}
        self.assertFalse(download_single_file(self.url, dest, len(PAYLOAD), expected_md5=md5(PAYLOAD)))
        temp = Path(str(dest) + ".temp")
        state = Path(str(temp) + ".segments")
        self.assertTrue(state.is_file())
        done = download._load_segment_done(state, len(PAYLOAD), BLOCK)
        self.assertNotIn(failing_block, done)

        self.server.requests = []
        self.server.fail_ranges = set()
        self.assertTrue(download_single_file(self.url, dest, len(PAYLOAD), expected_md5=md5(PAYLOAD)))
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        requested_blocks = {
            int(r.split("=")[1].split("-")[0]) // BLOCK for _, r in self.server.requests if r and r != "bytes=0-0"
        }
        self.assertIn(failing_block, requested_blocks)
        self.assertFalse(requested_blocks & done, "已完成的块不应重新请求")
        self.assertFalse(state.exists())

    def test_connection_slots_are_acquired_per_block(self):
        """连接预算必须按块申请；按文件一次性预占会让批量下载退化成串行。"""
        blocks = len(PAYLOAD) // BLOCK
        semaphore = CountingSemaphore(4)
        with patch.object(download, "_connection_semaphore", lambda: semaphore):
            dest = self.root / "per-block.pak"
            self.assertTrue(download_single_file(self.url, dest, len(PAYLOAD), expected_md5=md5(PAYLOAD)))
        self.assertEqual(semaphore.acquires, blocks)
        self.assertLessEqual(semaphore.max_held, 4)
        self.assertEqual(semaphore.held, 0, "结束后必须全部归还连接预算")

    def test_batch_download_shares_connection_budget(self):
        """多文件并行时总连接数不超预算，且每个文件都能完成。"""
        semaphore = CountingSemaphore(2)
        tasks = []
        for index in range(2):
            tasks.append({"url": self.url, "path": self.root / f"batch{index}.pak", "size": len(PAYLOAD)})
        with patch.object(download, "_connection_semaphore", lambda: semaphore):
            failed, names = download.batch_download(tasks, progress=None, max_workers=2)
        self.assertEqual(failed, 0, names)
        self.assertLessEqual(semaphore.max_held, 2)
        self.assertEqual(semaphore.held, 0)
        for task in tasks:
            self.assertEqual(task["path"].read_bytes(), PAYLOAD)

    def test_resume_restarts_when_state_size_mismatch(self):
        dest = self.root / "mismatch.pak"
        temp = Path(str(dest) + ".temp")
        temp.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("wb") as stream:
            stream.truncate(len(PAYLOAD))
        Path(str(temp) + ".segments").write_text("size 123\nblock 456\n0\n1\n2\n", encoding="utf-8")
        self.assertTrue(download_single_file(self.url, dest, len(PAYLOAD), expected_md5=md5(PAYLOAD)))
        self.assertEqual(dest.read_bytes(), PAYLOAD)


class SlowServer(ThreadingHTTPServer):
    payload: bytes
    chunk_delay: float


class SlowHandler(BaseHTTPRequestHandler):
    """分块慢速发送，用来验证 Ctrl-C 能不能及时停下。"""

    chunk = 64 * 1024

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        assert isinstance(self.server, SlowServer)
        payload = self.server.payload
        header = self.headers.get("Range")
        if header:
            start, end = header.split("=")[1].split("-")
            start = int(start)
            end = int(end) if end else len(payload) - 1
            end = min(end, len(payload) - 1)
            body = payload[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        else:
            body = payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for offset in range(0, len(body), self.chunk):
                self.wfile.write(body[offset : offset + self.chunk])
                self.wfile.flush()
                time.sleep(self.server.chunk_delay)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端按了 Ctrl-C：服务端写不进去是预期结果。
            return


class CancellationTests(unittest.TestCase):
    """Ctrl-C 必须及时中断下载，并保留续传现场。"""

    PAYLOAD_SIZE = 8 * 1024 * 1024

    @classmethod
    def setUpClass(cls):
        cls.server = SlowServer(("127.0.0.1", 0), SlowHandler)
        cls.server.payload = os.urandom(cls.PAYLOAD_SIZE)
        cls.server.chunk_delay = 0.05
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/slow"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ww-cancel-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("SEGMENTS", 4), ("MAX_CONNECTIONS", 4), ("SEGMENT_BLOCK_SIZE", 256 * 1024)):
            patcher = patch.object(download, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(download, "SEGMENT_THRESHOLD", 256 * 1024)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(download, "_connection_slots", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cancel_stops_segmented_download_and_keeps_state(self):
        dest = self.root / "cancel.pak"
        errors = []

        def run():
            try:
                download_single_file(self.url, dest, self.PAYLOAD_SIZE)
            except BaseException as exc:  # noqa: BLE001 - 测试需要区分异常类型
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(0.6)
        cancelled_at = time.monotonic()
        download.cancel_downloads()
        worker.join(timeout=5)
        elapsed = time.monotonic() - cancelled_at

        self.assertFalse(worker.is_alive(), "取消后下载线程必须退出")
        self.assertLess(elapsed, 2.0, f"取消响应过慢: {elapsed:.2f}s")
        self.assertTrue(errors and isinstance(errors[0], KeyboardInterrupt), f"应抛 KeyboardInterrupt，实际: {errors}")
        self.assertFalse(dest.exists(), "未完成的下载不得落正式文件")
        self.assertTrue(Path(str(dest) + ".temp").exists(), "临时文件应保留供续传")
        self.assertTrue(Path(str(dest) + ".temp.segments").exists(), "续传状态应保留")

    def test_cancel_flag_cleared_for_next_download(self):
        download.cancel_downloads()
        dest = self.root / "after-cancel.pak"
        self.server.chunk_delay = 0
        self.addCleanup(setattr, self.server, "chunk_delay", 0.05)
        self.assertTrue(download_single_file(self.url, dest, self.PAYLOAD_SIZE))
        self.assertEqual(dest.stat().st_size, self.PAYLOAD_SIZE)


if __name__ == "__main__":
    unittest.main()
