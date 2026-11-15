#!/usr/bin/env python3
"""下载路径集成测试：全量 / 资源档位 / 切服差异 / 增量包 / 修包回退。

目的是确认每个业务入口都真的复用统一分段下载器并且能落地文件，
而不只是下载器单函数可测。全部流量指向本地 HTTP 服务器，不访问外网。
"""

import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ww_manager import download
from ww_manager.core import WGameManager
from ww_manager.incremental import IncrementalManager

BIG = os.urandom(2 * 1024 * 1024)
SMALL = b"small-payload" * 64
BLOCK = 256 * 1024


def md5(data):
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class CDNServer(ThreadingHTTPServer):
    files: dict
    requests: list


class CDNHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        assert isinstance(self.server, CDNServer)
        header = self.headers.get("Range")
        self.server.requests.append((self.path, header))
        body = self.server.files.get(self.path.lstrip("/"))
        if body is None:
            self.send_error(404)
            return
        if header:
            start, end = header.split("=")[1].split("-")
            start = int(start)
            end = int(end) if end else len(body) - 1
            end = min(end, len(body) - 1)
            chunk = body[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DownloadPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = CDNServer(("127.0.0.1", 0), CDNHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ww-paths-")
        self.addCleanup(self.tmp.cleanup)
        self.game = Path(self.tmp.name) / "game"
        self.game.mkdir()
        self.server.files = {}
        self.server.requests = []
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

    # --- 辅助 ---

    def _manager(self) -> WGameManager:
        manager = WGameManager(self.game, "cn")
        manager._launcher_info = {
            "default": {
                "version": "9.9.9",
                "resourcesBasePath": f"{self.base}/files",
                "cdnList": [{"K1": 1, "K2": 1, "P": 1, "url": self.base}],
            }
        }
        manager._cdn_node = self.base
        manager._game_index = None
        return manager

    def _incremental(self) -> IncrementalManager:
        return IncrementalManager(
            self.game,
            "cn",
            {"default": {"config": {"baseUrl": "files"}}},
            self.base,
        )

    def _requested(self):
        return {path.lstrip("/") for path, _ in self.server.requests}

    def _ranged(self):
        return [(path, header) for path, header in self.server.requests if header]

    # --- 全量下载：ww download / ww sync ---

    def test_full_download_writes_verified_files(self):
        pak, readme = "Client/Content/Paks/pak0.pak", "Client/Content/Paks/readme.bin"
        self.server.files = {f"files/{pak}": BIG, f"files/{readme}": SMALL}
        manager = self._manager()
        manager._game_index = {
            "resource": [
                {"dest": pak, "size": len(BIG), "md5": md5(BIG)},
                {"dest": readme, "size": len(SMALL), "md5": md5(SMALL)},
            ]
        }
        # 已正确的文件不应重新下载
        intact = self.game / readme
        intact.parent.mkdir(parents=True, exist_ok=True)
        intact.write_bytes(SMALL)

        manager.sync_files(tier="hd")

        self.assertEqual((self.game / pak).read_bytes(), BIG)
        self.assertEqual(intact.read_bytes(), SMALL)
        self.assertIn(f"files/{pak}", self._requested())
        self.assertNotIn(f"files/{readme}", self._requested(), "已校验通过的文件不应下载")
        self.assertTrue(self._ranged(), "大文件应走分段 Range 请求")
        self.assertFalse(Path(str(self.game / pak) + ".temp").exists())
        self.assertFalse(Path(str(self.game / pak) + ".temp.segments").exists())

    def test_full_download_rejects_bad_payload(self):
        pak = "Client/Content/Paks/pak1.pak"
        self.server.files = {f"files/{pak}": BIG}
        manager = self._manager()
        manager._game_index = {"resource": [{"dest": pak, "size": len(BIG), "md5": "0" * 32}]}
        with self.assertRaises(Exception):
            manager.sync_files(tier="hd")
        self.assertFalse((self.game / pak).exists(), "MD5 不符不得落正式文件")

    # --- 资源档位下载：ww resource sd/hd/uhd ---

    def test_resource_tier_download(self):
        dest = "Client/Content/HD/pak7.pak"
        self.server.files = {f"files/hd/pak7.pak": BIG}
        prepared = {
            "bundle": {"bundles": {"HD": {"resourcePacks": ["common", "hd"]}}},
            "version": "9.9.9",
            "cdn": self.base,
            "base": "files",
            "resources": [{"dest": dest, "source": "hd/pak7.pak", "size": len(BIG), "md5": md5(BIG)}],
        }
        manager = self._manager()
        manager._install_resource_tier("hd", prepared)
        self.assertEqual((self.game / dest).read_bytes(), BIG)
        self.assertIn("files/hd/pak7.pak", self._requested())

    # --- 切服差异下载：ww checkout ---

    def test_checkout_diff_download(self):
        dest = "Client/Content/Paks/pak9.pak"
        self.server.files = {f"files/{dest}": BIG}
        manager = self._manager()
        downloaded = manager._download_diff_dst(
            "kr",
            [(dest, len(BIG), md5(BIG))],
            {"cdn": self.base, "res_base": "files"},
        )
        self.assertEqual(downloaded, 1)
        self.assertEqual((self.game / dest).read_bytes(), BIG)

    # --- 增量包与完整文件下载：ww update ---

    def test_incremental_patch_and_complete_download(self):
        self.server.files = {"files/p.pak": BIG}
        inc = self._incremental()
        url = f"{self.base}/files/p.pak"

        krpdiff = self.game / "patch.krpdiff"
        self.assertTrue(inc._download_file(url, krpdiff, len(BIG), md5(BIG)))
        self.assertEqual(krpdiff.read_bytes(), BIG)

        complete = self.game / "complete.pak"
        self.assertTrue(inc._download_and_verify_complete(url, complete, len(BIG), md5(BIG)))
        self.assertEqual(complete.read_bytes(), BIG)

        bad = self.game / "bad.pak"
        self.assertFalse(inc._download_and_verify_complete(url, bad, len(BIG), "0" * 32))
        self.assertFalse(bad.exists(), "校验失败的完整文件不得留在正式路径")

    def test_incremental_orchestration_end_to_end(self):
        """ww incremental 编排层：增量包与完整文件同批下载到缓存目录。"""
        patch_name = "Client/Content/Paks/group0.krpdiff"
        complete_dest = "Client/Content/Paks/full.pak"
        self.server.files = {
            f"files/patch/{patch_name}": BIG,
            f"files/{complete_dest}": SMALL,
        }
        inc = IncrementalManager(
            self.game,
            "cn",
            {
                # 真实 3.8 渠道 index 的 predownload 是显式 null，不是缺字段
                "predownload": None,
                "default": {
                    "config": {
                        "version": "1.1.0",
                        "baseUrl": "files",
                        "patchConfig": [
                            {"version": "1.0.0", "indexFile": "patch/index.json", "baseUrl": "files/patch"}
                        ],
                    }
                }
            },
            self.base,
        )
        inc._index_file = {
            "groupInfos": [{"dest": patch_name}],
            "resource": [
                {"dest": patch_name, "size": len(BIG), "md5": md5(BIG)},
                {"dest": complete_dest, "size": len(SMALL), "md5": md5(SMALL), "fromFolder": "files"},
            ],
        }
        (self.game / "launcherDownloadConfig.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")

        self.assertTrue(inc.download_incremental())
        self.assertEqual((inc.download_dir / patch_name).read_bytes(), BIG)
        staged = inc._get_staged_complete_path(inc.download_dir, complete_dest)
        self.assertEqual(staged.read_bytes(), SMALL)
        self.assertIn(f"files/patch/{patch_name}", self._requested())
        self.assertIn(f"files/{complete_dest}", self._requested())

    # --- 修包 / hpatchz 失败回退 ---

    def test_repair_and_fallback_download(self):
        dest = "Client/Content/Paks/pakR.pak"
        self.server.files = {f"files/{dest}": BIG}
        inc = self._incremental()

        damaged = self.game / dest
        damaged.parent.mkdir(parents=True, exist_ok=True)
        damaged.write_bytes(b"damaged")
        self.assertTrue(inc._download_and_verify(f"{self.base}/files/{dest}", damaged, len(BIG), md5(BIG)))
        self.assertEqual(damaged.read_bytes(), BIG)

        fallback_dest = "Client/Content/Paks/pakF.pak"
        self.server.files[f"files/{fallback_dest}"] = BIG
        info = {"dest": fallback_dest, "size": len(BIG), "md5": md5(BIG)}
        staged = inc._get_staged_patch_path(inc.download_dir, fallback_dest)
        self.assertTrue(inc._download_full_file_to_stage(info, staged))
        self.assertEqual(staged.read_bytes(), BIG)
        self.assertFalse((self.game / fallback_dest).exists(), "缓存下载不应提前改动游戏目录")


if __name__ == "__main__":
    unittest.main()
