"""chunkInfos 分块修复：只重取坏块，取不到就交回整文件下载。"""

import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ww_manager.chunk_repair import bad_chunks, chunk_map, normalize_chunks, repair_file
from ww_manager.download import download_single_file

CHUNK = 1024
PAYLOAD = bytes(range(256)) * 12  # 3072 字节，正好 3 个 CHUNK


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def chunk_list(payload: bytes) -> list:
    return [
        {"start": start, "end": min(start + CHUNK - 1, len(payload) - 1), "md5": md5(payload[start : start + CHUNK])}
        for start in range(0, len(payload), CHUNK)
    ]


class RangeServer(ThreadingHTTPServer):
    payload: bytes
    ignore_range: bool
    requested: list


class RangeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        server.requested.append(self.headers.get("Range"))
        payload = server.payload
        header = self.headers.get("Range")
        if header and not server.ignore_range:
            start_s, end_s = header.removeprefix("bytes=").split("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(payload) - 1
            body = payload[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        else:
            body = payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ChunkRepairTests(unittest.TestCase):
    def setUp(self):
        self.server = RangeServer(("127.0.0.1", 0), RangeHandler)
        self.server.payload = PAYLOAD
        self.server.ignore_range = False
        self.server.requested = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/f.bin"
        temporary = tempfile.TemporaryDirectory(prefix="ww-chunk-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.chunks = chunk_list(PAYLOAD)

    def corrupt(self, path: Path, offset: int):
        data = bytearray(path.read_bytes())
        data[offset] ^= 0xFF
        path.write_bytes(bytes(data))

    def test_normalize_rejects_gaps_overlaps_and_size_mismatch(self):
        self.assertEqual(len(normalize_chunks(self.chunks)), 3)
        gap = [self.chunks[0], {**self.chunks[2]}]
        self.assertIsNone(normalize_chunks(gap))
        overlap = [self.chunks[0], {**self.chunks[1], "start": 512}]
        self.assertIsNone(normalize_chunks(overlap))
        self.assertIsNone(normalize_chunks(self.chunks, total_size=len(PAYLOAD) + 1))
        self.assertIsNone(normalize_chunks([{"start": 0, "end": 9, "md5": "xyz"}]))
        self.assertIsNone(normalize_chunks([]))
        self.assertIsNone(normalize_chunks(None))

    def test_chunk_map_collects_resource_and_group_dst_files(self):
        index = {
            "resource": [{"dest": "a.krpdiff", "size": len(PAYLOAD), "chunkInfos": self.chunks}],
            "groupInfos": [
                {
                    "dest": "a.krpdiff",
                    "dstFiles": [
                        {"dest": "Client/Content/Paks/x.pak", "size": len(PAYLOAD), "chunkInfos": self.chunks}
                    ],
                }
            ],
        }
        collected = chunk_map(index)
        self.assertEqual(set(collected), {"a.krpdiff", "Client/Content/Paks/x.pak"})
        # 分块表没覆盖整份文件时不参与修复，避免按猜测拼接。
        index["resource"].append({"dest": "b.pak", "size": len(PAYLOAD) * 2, "chunkInfos": self.chunks})
        self.assertNotIn("b.pak", chunk_map(index))

    def test_bad_chunks_detects_corruption_and_truncation(self):
        path = self.root / "f.bin"
        path.write_bytes(PAYLOAD)
        self.assertEqual(bad_chunks(path, self.chunks), [])
        self.corrupt(path, CHUNK + 5)
        self.assertEqual([c[0] for c in bad_chunks(path, self.chunks)], [CHUNK])
        path.write_bytes(PAYLOAD[: CHUNK * 2 + 10])
        self.assertEqual([c[0] for c in bad_chunks(path, self.chunks)], [CHUNK * 2])

    def test_repair_only_fetches_bad_chunk(self):
        path = self.root / "f.bin"
        path.write_bytes(PAYLOAD)
        self.corrupt(path, CHUNK + 7)
        self.assertTrue(repair_file(self.url, path, self.chunks, md5(PAYLOAD)))
        self.assertEqual(path.read_bytes(), PAYLOAD)
        ranges = [r for r in self.server.requested if r]
        self.assertEqual(ranges, [f"bytes={CHUNK}-{CHUNK * 2 - 1}"])

    def test_repair_restores_truncated_tail(self):
        path = self.root / "f.bin"
        path.write_bytes(PAYLOAD[: CHUNK + 10])
        self.assertTrue(repair_file(self.url, path, self.chunks, md5(PAYLOAD)))
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_repair_gives_up_when_server_ignores_range(self):
        self.server.ignore_range = True
        path = self.root / "f.bin"
        path.write_bytes(PAYLOAD)
        self.corrupt(path, CHUNK + 7)
        self.assertFalse(repair_file(self.url, path, self.chunks, md5(PAYLOAD)))
        # 放弃修复时不动文件，交给调用方整文件重下。
        self.assertNotEqual(path.read_bytes(), PAYLOAD)

    def test_repair_rejects_whole_file_md5_mismatch(self):
        path = self.root / "f.bin"
        path.write_bytes(PAYLOAD)
        # 分块 md5 全对，但整文件名单不同：说明分块表与清单不是同一版本，不能认为修好了。
        self.assertFalse(repair_file(self.url, path, self.chunks, "0" * 32))
        self.assertEqual(self.server.requested, [])

    def test_download_single_file_repairs_existing_target(self):
        dest = self.root / "f.bin"
        dest.write_bytes(PAYLOAD)
        self.corrupt(dest, CHUNK * 2 + 3)
        self.assertTrue(
            download_single_file(
                self.url,
                dest,
                len(PAYLOAD),
                expected_md5=md5(PAYLOAD),
                chunk_infos=self.chunks,
            )
        )
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        self.assertEqual([r for r in self.server.requested if r], [f"bytes={CHUNK * 2}-{len(PAYLOAD) - 1}"])
        self.assertFalse(dest.with_suffix(".bin.temp").exists())

    def test_download_falls_back_to_full_file_when_repair_fails(self):
        self.server.ignore_range = True
        dest = self.root / "f.bin"
        dest.write_bytes(PAYLOAD)
        self.corrupt(dest, CHUNK)
        with patch("ww_manager.download.probe_range_support", return_value=None):
            self.assertTrue(
                download_single_file(
                    self.url,
                    dest,
                    len(PAYLOAD),
                    expected_md5=md5(PAYLOAD),
                    chunk_infos=self.chunks,
                )
            )
        self.assertEqual(dest.read_bytes(), PAYLOAD)


if __name__ == "__main__":
    unittest.main()
