#!/usr/bin/env python3
"""档位生命周期与通用下载器回归测试；仅访问本地 HTTP，所有文件位于临时目录。

运行：uv run python -m unittest discover -s tests -p test_resource_tier.py -v
"""

import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ww_manager.cli import app
from ww_manager.config import RESOURCE_CONFIG_URLS
from ww_manager.core import ConfigError, MD5Cache, NetworkError, WGameManager
from ww_manager.download import create_download_progress, download_single_file

GOOD = b"official-resource-content"
DEST = "Client/Content/UHD/test.pak"


def md5(data):
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def item(dest=DEST, data=GOOD):
    return {"dest": dest, "size": len(data), "md5": md5(data)}


class CDNServer(ThreadingHTTPServer):
    resources: list
    requests: list


class LocalCDN(BaseHTTPRequestHandler):
    """支持 Range、忽略 Range、错误 Range、错误数据与 404。"""

    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        assert isinstance(self.server, CDNServer)
        self.server.requests.append((self.path, self.headers.get("Range")))
        if self.path == "/manifest.json":
            payload = json.dumps({"resource": self.server.resources}).encode()
        elif self.path in ("/missing", "/files/Client/Content/UHD/missing.pak"):
            self.send_error(404)
            return
        else:
            payload = b"x" * len(GOOD) if self.path == "/corrupt" else GOOD
        offset = 0
        header = self.headers.get("Range")
        if header and self.path != "/ignore-range":
            offset = int(header.split("=")[1].split("-")[0])
            self.send_response(206)
            start = offset + 1 if self.path == "/wrong-range" else offset
            self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(payload) - offset))
        self.end_headers()
        self.wfile.write(payload[offset:])


class ResourceTierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = CDNServer(("127.0.0.1", 0), LocalCDN)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ww-resource-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server.resources = [item()]
        self.server.requests = []
        self.bundle = {
            "resourcePacks": {"uhd": {"version": "3.8.0", "baseUrl": "files", "indexFile": "manifest.json"}},
            "bundles": {"UHD": {"resourcePacks": ["common", "uhd"]}},
            "cdnList": [{"K1": 1, "K2": 1, "P": 1, "url": self.url}],
        }
        self.manager = WGameManager(self.root, "cn")
        self.bundle_patch = patch.object(self.manager, "get_bundle_config", return_value=self.bundle)
        self.bundle_patch.start()
        self.addCleanup(self.bundle_patch.stop)
        # 失败测试仍走真实 HTTP；只省略三次重试之间的等待。
        sleeper = patch("ww_manager.download.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        # CLI 回调的补全安装、版本检查和用户配置必须隔离，避免离线测试产生全局副作用。
        for name in ("_auto_install_completion", "setup_logging", "check_pypi_version_silent", "save_app_config"):
            mocked = patch(f"ww_manager.cli.{name}")
            mocked.start()
            self.addCleanup(mocked.stop)
        config = patch("ww_manager.cli.load_app_config", return_value={"latest_available_version": "999.0"})
        config.start()
        self.addCleanup(config.stop)

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def read_json(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    def registered(self):
        cfg = self.read_json("launcherDownloadConfig.json")
        self.assertEqual(
            cfg["bundles"]["UHD"],
            {
                "version": "3.8.0",
                "state": "",
                "resourcePacks": ["common", "uhd"],
            },
        )
        self.assertEqual(self.read_json("launcherDownloadConfig/uhd.json"), {"packName": "uhd", "version": "3.8.0"})

    def invoke(self, *args, input=None):
        with (
            patch("ww_manager.cli.get_game_path", return_value=self.root),
            patch("ww_manager.cli.WGameManager", return_value=self.manager),
        ):
            return CliRunner().invoke(app, list(args), input=input)

    def test_download_register_cache_and_repeat(self):
        self.put("launcherDownloadConfig.json", b'{"appId":"10003","custom":42,"bundles":{"SD":{"version":"old"}}}')
        self.manager.download_resource_tier("UHD")
        self.assertEqual((self.root / DEST).read_bytes(), GOOD)
        self.registered()
        cfg = self.read_json("launcherDownloadConfig.json")
        self.assertEqual(cfg["custom"], 42)
        self.assertIn("SD", cfg["bundles"])
        self.assertIn(DEST, self.read_json("wwm_md5_cache_resource_uhd.json"))
        self.server.requests.clear()
        self.manager.download_resource_tier("uhd")
        self.assertEqual(self.server.requests, [("/manifest.json", None)])

    def test_same_size_corruption_repaired_by_default(self):
        self.put(DEST, b"x" * len(GOOD))
        self.manager.download_resource_tier("uhd")
        self.assertEqual((self.root / DEST).read_bytes(), GOOD)
        self.registered()

    def test_corrupt_complete_temp_not_promoted(self):
        self.put(DEST + ".temp", b"x" * len(GOOD))
        self.manager.download_resource_tier("uhd", force_check_md5=True)
        self.assertEqual((self.root / DEST).read_bytes(), GOOD)
        self.assertFalse((self.root / (DEST + ".temp")).exists())

    def test_no_download_still_saves_cache(self):
        self.put(DEST, GOOD)
        self.manager.download_resource_tier("uhd")
        self.assertTrue((self.root / "wwm_md5_cache_resource_uhd.json").exists())
        self.assertEqual(self.server.requests, [("/manifest.json", None)])

    def test_force_md5_ignores_valid_cache(self):
        path = self.put(DEST, GOOD)
        cache = MD5Cache(self.root / "wwm_md5_cache_resource_uhd.json", self.root)
        cache.get(path)
        cache.save()
        with patch.object(MD5Cache, "_calculate_md5", return_value=md5(GOOD)) as calc:
            self.manager.download_resource_tier("uhd", force_check_md5=True)
        calc.assert_called_once_with(path)

    def test_cache_detects_change_with_restored_mtime(self):
        path = self.put(DEST, GOOD)
        cache = MD5Cache(self.root / "cache.json", self.root)
        self.assertEqual(cache.get(path), md5(GOOD))
        stat = path.stat()
        path.write_bytes(b"x" * len(GOOD))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(cache.get(path), md5(GOOD))

    def test_failed_download_cli_nonzero_and_unregisters(self):
        self.put("launcherDownloadConfig.json", b'{"bundles":{"UHD":{"version":"old"},"SD":{"version":"keep"}}}')
        self.put("launcherDownloadConfig/uhd.json", b'{"version":"old"}')
        self.server.resources = [item("Client/Content/UHD/missing.pak")]
        result = self.invoke("resource", "uhd")
        self.assertNotEqual(result.exit_code, 0, result.output)
        self.assertFalse((self.root / "launcherDownloadConfig/uhd.json").exists())
        self.assertNotIn("UHD", self.read_json("launcherDownloadConfig.json")["bundles"])
        self.assertIn("SD", self.read_json("launcherDownloadConfig.json")["bundles"])

    def test_post_download_verification_before_registration(self):
        with patch.object(self.manager, "_batch_download", return_value=0):
            with self.assertRaises(NetworkError):
                self.manager.download_resource_tier("uhd")
        self.assertFalse((self.root / "launcherDownloadConfig/uhd.json").exists())

    def test_stale_managed_only_cleanup(self):
        old = self.put("Client/Content/UHD/old.pak", b"old")
        cache = MD5Cache(self.root / "wwm_md5_cache_resource_uhd.json", self.root)
        cache.get(old)
        cache.save()
        custom = self.put("Client/Content/UHD/user.pak", b"user")
        other = self.put("Client/Content/SD/keep.pak", b"other")
        self.manager.download_resource_tier("uhd")
        self.assertFalse(old.exists())
        self.assertTrue(custom.exists())
        self.assertTrue(other.exists())
        self.assertNotIn("Client/Content/UHD/old.pak", self.read_json("wwm_md5_cache_resource_uhd.json"))

    def test_manifest_errors_do_not_touch_files_or_markers(self):
        old = self.put("Client/Content/UHD/old.pak", GOOD)
        marker = self.put("launcherDownloadConfig/uhd.json", b'{"version":"old"}')
        cases = [
            [],
            [item("../outside")],
            [item("Client/Content/UHD/../escape.pak")],
            [item("/Client/Content/UHD/a.pak")],
            [item("C:\\outside.pak")],
            [item("Client/Content/SD/a.pak")],
            [item(), item()],
            [dict(item(), md5="broken")],
            [dict(item(), size=-1)],
            [item("Client/Content/UHD/a"), item("Client/Content/UHD/a/b")],
        ]
        for resources in cases:
            with self.subTest(resources=resources):
                self.server.resources = resources
                with self.assertRaises(ConfigError):
                    self.manager.download_resource_tier("uhd")
                self.assertTrue(old.exists())
                self.assertEqual(marker.read_bytes(), b'{"version":"old"}')

    def test_windows_separators(self):
        self.server.resources = [item(DEST.replace("/", "\\"))]
        self.manager.download_resource_tier("uhd")
        self.assertEqual((self.root / DEST).read_bytes(), GOOD)

    def test_symlink_rejected_before_download(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "Client/Content").mkdir(parents=True)
        (self.root / "Client/Content/UHD").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.manager.download_resource_tier("uhd")
        self.assertEqual(list(outside.iterdir()), [])

    def test_bad_config_preserved(self):
        cfg = self.put("launcherDownloadConfig.json", b"not-json")
        with self.assertRaises(ConfigError):
            self.manager.download_resource_tier("uhd")
        self.assertEqual(cfg.read_bytes(), b"not-json")
        self.assertFalse((self.root / DEST).exists())

    def test_malformed_bundle_preflight(self):
        for key, value in (("bundles", []), ("resourcePacks", ["uhd"])):
            with self.subTest(key=key):
                bundle = copy.deepcopy(self.bundle)
                bundle[key] = value
                with patch.object(self.manager, "get_bundle_config", return_value=bundle):
                    with self.assertRaises(ConfigError):
                        self.manager.download_resource_tier("uhd")
                self.assertFalse((self.root / DEST).exists())

    def test_remove_confirmation_and_idempotence(self):
        self.manager.download_resource_tier("uhd")
        other = self.put("Client/Content/SD/keep.pak", b"other")
        result = self.invoke("resource", "uhd", "--remove", input="n\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue((self.root / DEST).exists())
        self.server.requests.clear()
        result = self.invoke("resource", "uhd", "--remove", input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse((self.root / "Client/Content/UHD").exists())
        self.assertFalse((self.root / "wwm_md5_cache_resource_uhd.json").exists())
        self.assertFalse((self.root / "launcherDownloadConfig/uhd.json").exists())
        self.assertNotIn("UHD", self.read_json("launcherDownloadConfig.json")["bundles"])
        self.assertTrue(other.exists())
        self.manager.remove_resource_tier("uhd")
        self.assertEqual(self.server.requests, [])

    def test_status_offline_and_unregistered_directory(self):
        self.put(DEST, GOOD)
        with patch.object(self.manager, "_http_get_json", side_effect=AssertionError("状态不应联网")):
            result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("未登记/不完整", result.output)
        self.assertIn("-krqlv=uhd", result.output)

    def test_status_bad_marker_is_incomplete(self):
        self.put(DEST, GOOD)
        self.put("launcherDownloadConfig/uhd.json", b"[]")
        self.put("launcherDownloadConfig.json", b'{"bundles":{"UHD":{"version":"old"}}}')
        states = self.manager.resource_tier_status()
        self.assertEqual(states[-1]["state"], "未登记/不完整")

    def test_base_update_preserves_bundles(self):
        original = {"version": "old", "custom": 42, "bundles": {"UHD": {"version": "old"}}}
        self.put("launcherDownloadConfig.json", json.dumps(original).encode())
        self.manager._launcher_info = {"default": {"version": "new"}}
        self.manager._update_local_config()
        cfg = self.read_json("launcherDownloadConfig.json")
        self.assertEqual(cfg["bundles"], original["bundles"])
        self.assertEqual(cfg["custom"], 42)
        self.assertEqual(cfg["version"], "new")

    def test_base_sync_failure_does_not_update_config(self):
        self.put("launcherDownloadConfig.json", b'{"version":"old"}')
        self.manager._launcher_info = {"default": {"version": "new", "resourcesBasePath": "files"}}
        self.manager._cdn_node = self.url
        self.manager._game_index = {"resource": [item()]}
        with patch.object(self.manager, "_batch_download", return_value=1):
            with self.assertRaises(NetworkError):
                self.manager.sync_files()
        self.assertEqual(self.read_json("launcherDownloadConfig.json")["version"], "old")

    def test_sync_reminds_without_auto_download(self):
        self.put(DEST, GOOD)
        with (
            patch.object(self.manager, "sync_files") as sync,
            patch.object(self.manager, "download_resource_tier") as resource,
            self.assertLogs("WW_Manager", level="WARNING") as logs,
        ):
            result = self.invoke("sync")
        self.assertEqual(result.exit_code, 0, result.output)
        sync.assert_called_once()
        resource.assert_not_called()
        self.assertIn("UHD", "\n".join(logs.output))
        self.assertIn("ww resource <tier>", "\n".join(logs.output))

    def test_cn_and_bilibili_auto_config_without_launcher(self):
        self.assertFalse(list(self.root.rglob("KRApp.conf")))
        for server in ("cn", "bilibili"):
            manager = WGameManager(self.root, server)
            app_id = manager.config["appId"]
            with patch.object(manager, "_http_get_json", return_value=self.bundle) as fetch:
                self.assertEqual(manager.get_bundle_config(), self.bundle)
            fetch.assert_called_once_with(RESOURCE_CONFIG_URLS[0])
            self.assertEqual(manager.config["appId"], app_id)
            self.assertEqual(manager.server_type, server)

    def test_auto_config_tries_other_official_nodes(self):
        manager = WGameManager(self.root, "cn")
        with patch.object(manager, "_http_get_json", side_effect=[None, {}, self.bundle]) as fetch:
            self.assertEqual(manager.get_bundle_config(), self.bundle)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], list(RESOURCE_CONFIG_URLS))

    def test_invalid_auto_config_fails_without_writing_files(self):
        manager = WGameManager(self.root, "cn")
        before = sorted(self.root.rglob("*"))
        with patch.object(manager, "_http_get_json", return_value=[]):
            with self.assertRaisesRegex(NetworkError, "官方画质配置"):
                manager.get_bundle_config()
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_explicit_config_override_does_not_fallback(self):
        manager = WGameManager(self.root, "cn")
        with patch.object(manager, "_http_get_json", return_value=None) as fetch:
            with self.assertRaises(NetworkError):
                manager.get_bundle_config(self.url)
        fetch.assert_called_once_with(self.url)

    def test_global_keeps_hd_and_never_uses_cn_quality_config(self):
        manager = WGameManager(self.root, "global")
        with patch.object(manager, "_http_get_json") as fetch:
            self.assertEqual(manager.current_resource_tier(), "hd")
            with self.assertRaisesRegex(ConfigError, "国际服暂仅支持默认 HD"):
                manager.download_resource_tier("uhd")
        fetch.assert_not_called()

    def test_download_defaults_to_cn_hd(self):
        with (
            patch("ww_manager.cli.WGameManager") as manager,
            patch("ww_manager.cli.get_game_path", return_value=self.root),
        ):
            result = CliRunner().invoke(app, ["download"])
        self.assertEqual(result.exit_code, 0, result.output)
        manager.assert_called_once_with(self.root, "cn")
        manager.return_value.download_full.assert_called_once()

    def test_download_preserves_positional_server_argument(self):
        for server in ("cn", "bilibili", "global"):
            with (
                self.subTest(server=server),
                patch("ww_manager.cli.WGameManager") as manager,
                patch("ww_manager.cli.get_game_path", return_value=self.root),
            ):
                result = CliRunner().invoke(app, ["download", server])
                self.assertEqual(result.exit_code, 0, result.output)
                manager.assert_called_once_with(self.root, server)
                manager.return_value.download_full.assert_called_once()

    def test_same_version_checkout_does_not_warn_tier_is_outdated(self):
        self.manager.download_resource_tier("uhd")
        cfg = self.read_json("launcherDownloadConfig.json")
        cfg["version"] = self.bundle["resourcePacks"]["uhd"]["version"]
        self.manager._write_local_json("launcherDownloadConfig.json", cfg)
        with patch("ww_manager.core.logger.warning") as warning:
            self.manager.remind_resource_tiers()
        warning.assert_not_called()

    def test_download_progress_counts_resume_once(self):
        for endpoint in ("ok", "ignore-range", "wrong-range"):
            with self.subTest(endpoint=endpoint), create_download_progress() as progress:
                path = self.root / "download.pak"
                self.put("download.pak.temp", GOOD[:5])
                task = progress.add_task("Total", total=len(GOOD))
                self.assertTrue(
                    download_single_file(self.url + endpoint, path, len(GOOD), progress, task, expected_md5=md5(GOOD))
                )
                self.assertEqual(progress.tasks[0].completed, len(GOOD))

    def test_remove_symlink_cannot_delete_outside(self):
        outside = self.root / "outside"
        outside.mkdir()
        keep = outside / "keep"
        keep.write_bytes(GOOD)
        (self.root / "Client/Content").mkdir(parents=True)
        (self.root / "Client/Content/UHD").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.manager.remove_resource_tier("uhd")
        self.assertEqual(keep.read_bytes(), GOOD)

    def test_temp_symlink_rejected(self):
        keep = self.put("outside", GOOD)
        (self.root / "Client/Content/UHD").mkdir(parents=True)
        (self.root / (DEST + ".temp")).symlink_to(keep)
        with self.assertRaises(ConfigError):
            self.manager.download_resource_tier("uhd")
        self.assertEqual(keep.read_bytes(), GOOD)

    def test_missing_tier_cli_does_not_download(self):
        result = self.invoke("resource")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.server.requests, [])

    def test_downloader_range_resume(self):
        path = self.root / "download.pak"
        self.put("download.pak.temp", GOOD[:5])
        self.assertTrue(download_single_file(self.url + "ok", path, len(GOOD), expected_md5=md5(GOOD)))
        self.assertEqual(path.read_bytes(), GOOD)
        self.assertIn(("/ok", "bytes=5-"), self.server.requests)

    def test_downloader_ignored_range_restarts(self):
        path = self.root / "download.pak"
        self.put("download.pak.temp", GOOD[:5])
        self.assertTrue(download_single_file(self.url + "ignore-range", path, len(GOOD), expected_md5=md5(GOOD)))
        self.assertEqual(path.read_bytes(), GOOD)

    def test_downloader_wrong_range_never_corrupts_final(self):
        path = self.put("download.pak", b"old")
        self.put("download.pak.temp", GOOD[:5])
        self.assertTrue(download_single_file(self.url + "wrong-range", path, len(GOOD), expected_md5=md5(GOOD)))
        self.assertEqual(path.read_bytes(), GOOD)
        self.assertIn(("/wrong-range", None), self.server.requests)

    def test_downloader_bad_payload_keeps_old_file(self):
        path = self.put("download.pak", b"old")
        self.assertFalse(download_single_file(self.url + "corrupt", path, len(GOOD), expected_md5=md5(GOOD)))
        self.assertEqual(path.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
