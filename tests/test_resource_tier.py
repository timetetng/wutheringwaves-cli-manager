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
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from ww_manager.cli import app
from ww_manager.config import RESOURCE_CONFIG_URLS
from ww_manager.core import ConfigError, DiskSpaceError, MD5Cache, NetworkError, WGameManager
from ww_manager.menu import MenuOption
from ww_manager.download import create_download_progress, download_single_file
from ww_manager.resource_updates import ResourceUpdater

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
            result = self.invoke("resource")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("未登记/不完整", result.output)
        self.assertIn("UHD · 极致", result.output)

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
        self.manager._game_index = {"resource": [item("Client/Content/Paks/common.pak")]}
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

    def test_download_without_server_requires_interactive_terminal(self):
        with (
            patch("ww_manager.cli.WGameManager") as manager,
            patch("ww_manager.cli.get_game_path", return_value=self.root),
            patch("ww_manager.cli._interactive_terminal", return_value=False),
        ):
            result = CliRunner().invoke(app, ["download"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("非交互环境", result.output)
        manager.assert_not_called()

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
                manager.return_value.download_full.assert_called_once_with(tier="hd", force_space=False)

    def prepare_full_download(self, tier="uhd"):
        self.manager._launcher_info = {"default": {"version": "3.8.0", "resourcesBasePath": "channel"}}
        self.manager._cdn_node = self.url
        self.manager._game_index = {
            "resource": [item("Client/Content/Paks/common.pak"), item("Client/Content/HD/hd.pak")]
        }
        if tier != "hd":
            self.bundle["resourcePacks"]["common"] = {"version": "3.8.0"}
            self.bundle["resourcePacks"][tier] = {"version": "3.8.0", "baseUrl": "files", "indexFile": "manifest.json"}
            self.bundle["bundles"][tier.upper()] = {"resourcePacks": ["common", tier]}
            self.server.resources = [item(f"Client/Content/{tier.upper()}/test.pak")]

    def test_full_download_only_selected_tier_and_sync_remembers(self):
        for tier in ("sd", "uhd"):
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as folder:
                self.manager.game_folder = Path(folder)
                self.manager.md5_cache = MD5Cache(Path(folder) / "wwm_md5_cache.json", Path(folder))
                self.prepare_full_download(tier)
                self.server.requests.clear()
                self.manager.download_full(tier)
                root = Path(folder)
                self.assertEqual((root / "Client/Content/Paks/common.pak").read_bytes(), GOOD)
                self.assertEqual((root / f"Client/Content/{tier.upper()}/test.pak").read_bytes(), GOOD)
                self.assertFalse((root / "Client/Content/HD").exists())
                self.assertEqual(self.manager.current_resource_tier(), tier)
                self.assertNotIn("hd", self.manager._read_resource_state()["versions"])
                self.assertIsNone(self.manager.resource_pack_version("hd"))
                self.assertFalse(any("/HD/" in path for path, _ in self.server.requests))
                self.manager.sync_files(force_check_md5=True)
                self.assertFalse((root / "Client/Content/HD").exists())
                self.assertFalse(any("/HD/" in path for path, _ in self.server.requests))

    def test_default_download_records_hd_without_quality_config(self):
        self.prepare_full_download("hd")
        self.bundle_patch.stop()
        with patch.object(self.manager, "get_bundle_config") as fetch:
            self.manager.download_full()
        fetch.assert_not_called()
        self.assertTrue((self.root / "Client/Content/HD/hd.pak").is_file())
        self.assertEqual(self.manager.current_resource_tier(), "hd")
        self.assertEqual(self.manager.resource_pack_version("hd"), "3.8.0")
        self.assertEqual(self.read_json("launcherDownloadConfig/hd.json"), {"packName": "hd", "version": "3.8.0"})
        states = self.manager.resource_tier_status()
        self.assertEqual(next(s for s in states if s["tier"] == "hd")["state"], "已安装")
        with patch("ww_manager.core.logger.warning") as warning:
            self.manager.remind_resource_tiers()
        warning.assert_not_called()

    def test_hd_bilibili_install_verifies_and_registers(self):
        self.prepare_full_download("hd")
        self.manager.server_type = "bilibili"
        self.manager.config = WGameManager(self.root, "bilibili").config
        self.manager.download_full()
        result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(result.output.strip().splitlines()), 4)
        for text in (str(self.root), "bilibili", "3.8.0", "当前默认档位", "HD · 高清"):
            self.assertIn(text, result.output)
        self.assertNotIn("未校验", result.output)
        self.assertNotIn("SD", result.output)
        self.server.requests.clear()
        self.manager.download_full()
        self.assertEqual(self.server.requests, [])

    def test_base_success_without_valid_files_does_not_register(self):
        self.prepare_full_download("hd")
        with patch.object(self.manager, "_batch_download", return_value=0):
            with self.assertRaisesRegex(NetworkError, "基础资源校验失败"):
                self.manager.download_full()
        self.assertFalse((self.root / "launcherDownloadConfig/hd.json").exists())
        self.assertFalse((self.root / "launcherDownloadConfig.json").exists())
        self.assertIn("hd", self.manager._read_resource_state()["incomplete"])

    def test_verified_resource_listing_is_not_unverified(self):
        self.manager.download_resource_tier("uhd")
        result = self.invoke("resource")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("已安装", result.output)
        self.assertNotIn("未校验", result.output)
        self.assertIn("SD · 标清", result.output)
        self.assertIn("HD · 高清", result.output)
        self.assertIn("UHD · 极致", result.output)

    def test_status_without_install_or_selection(self):
        result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("未知", result.output)
        self.assertIn("HD · 高清", result.output)
        self.manager._write_local_json("wwm_resource_state.json", {"active_tier": None, "versions": {}})
        result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("未选择", result.output)

    def test_bilibili_download_uses_channel_common_and_shared_quality(self):
        self.prepare_full_download()
        self.manager.server_type = "bilibili"
        self.manager.config = WGameManager(self.root, "bilibili").config
        self.manager.download_full("uhd")
        self.assertIn(("/channel/Client/Content/Paks/common.pak", None), self.server.requests)
        self.assertIn(("/files/Client/Content/UHD/test.pak", None), self.server.requests)
        self.assertEqual(self.read_json("launcherDownloadConfig.json")["appId"], self.manager.config["appId"])
        self.assertEqual(self.manager._read_resource_state()["common_server"], "bilibili")

    def test_install_version_mismatch_has_no_file_side_effects(self):
        self.prepare_full_download()
        self.bundle["resourcePacks"]["common"]["version"] = "3.7.1"
        with self.assertRaisesRegex(ConfigError, "版本不一致"):
            self.manager.download_full("uhd")
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.server.requests, [("/manifest.json", None)])

    def test_failed_install_does_not_select_or_register_target(self):
        self.prepare_full_download()
        self.server.resources = [item("Client/Content/UHD/missing.pak")]
        with self.assertRaises(NetworkError):
            self.manager.download_full("uhd")
        self.assertEqual(self.manager.current_resource_tier(), "hd")
        self.assertFalse((self.root / "launcherDownloadConfig.json").exists())
        self.assertFalse((self.root / "launcherDownloadConfig/uhd.json").exists())
        self.assertIn("uhd", self.manager._read_resource_state()["incomplete"])
        self.assertNotIn("hd", self.manager._read_resource_state()["versions"])
        self.server.resources = [item()]
        self.manager.download_full("uhd")
        self.assertEqual(self.manager.current_resource_tier(), "uhd")
        self.assertEqual(self.manager._read_resource_state()["incomplete"], [])

    def test_uhd_sync_preserves_previously_managed_hd(self):
        self.prepare_full_download()
        hd = self.put("Client/Content/HD/old.pak", GOOD)
        self.manager.md5_cache.get(hd)
        self.manager.download_full("uhd")
        self.assertEqual(hd.read_bytes(), GOOD)
        self.manager.sync_files()
        self.assertEqual(hd.read_bytes(), GOOD)

    def test_corrupt_base_same_size_is_repaired(self):
        self.prepare_full_download("hd")
        self.put("Client/Content/Paks/common.pak", b"x" * len(GOOD))
        self.manager.download_full()
        self.assertEqual((self.root / "Client/Content/Paks/common.pak").read_bytes(), GOOD)

    def test_global_non_hd_and_unknown_tier_fail_before_network(self):
        manager = WGameManager(self.root, "global")
        with patch.object(manager, "_http_get_json") as fetch:
            for tier in ("sd", "uhd", "invalid"):
                with self.subTest(tier=tier), self.assertRaises(ConfigError):
                    manager.download_full(tier)
        fetch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_incremental_plan_uses_tier_remembered_by_initial_install(self):
        self.prepare_full_download()
        self.manager.download_full("uhd")
        pack = self.bundle["resourcePacks"]["uhd"]
        pack["version"] = "3.9.0"
        pack["patchConfig"] = [{"version": "3.8.0", "indexFile": "manifest.json"}]
        plan = ResourceUpdater(self.manager)._create_plan(None)
        self.assertEqual(plan["tier"], "uhd")
        self.assertEqual(set(plan["packs"]), {"common", "uhd"})
        self.assertEqual(plan["packs"]["uhd"]["from_version"], "3.8.0")
        self.assertEqual(plan["packs"]["uhd"]["to_version"], "3.9.0")

    def test_bad_base_manifest_rejected_before_writes(self):
        self.prepare_full_download()
        invalid = [
            [item("Client/../escape.pak")],
            [item("/absolute.pak")],
            [item("Client/Content")],
            [item("WWM_resource_state.json")],
            [item("launcherDownloadConfig/uhd.json")],
            [item("Client/a.pak"), item("client/A.pak")],
            [item("Client/a"), item("Client/a/b")],
            [{**item("Client/a.pak"), "md5": "invalid"}],
        ]
        for resources in invalid:
            with self.subTest(resources=resources):
                self.manager._game_index = {"resource": resources}
                with self.assertRaises(ConfigError):
                    self.manager.download_full("uhd")
                self.assertEqual(list(self.root.iterdir()), [])

    def test_global_hd_stays_on_own_channel(self):
        self.prepare_full_download("hd")
        self.manager.server_type = "global"
        self.manager.config = WGameManager(self.root, "global").config
        with patch.object(self.manager, "get_bundle_config") as quality:
            self.manager.download_full("hd")
        quality.assert_not_called()
        self.assertTrue((self.root / "Client/Content/HD/hd.pak").is_file())
        self.assertEqual(self.read_json("launcherDownloadConfig.json")["appId"], self.manager.config["appId"])

    def test_base_failure_preserves_previous_selection(self):
        self.prepare_full_download()
        state = {"active_tier": "sd", "versions": {"common": "3.7.1", "sd": "3.7.1"}, "incomplete": []}
        self.manager._write_local_json("wwm_resource_state.json", state)
        with patch.object(self.manager, "_batch_download", return_value=1):
            with self.assertRaises(NetworkError):
                self.manager.download_full("uhd")
        self.assertEqual(self.manager.current_resource_tier(), "sd")
        self.assertNotIn("uhd", self.manager._read_resource_state()["versions"])
        self.assertFalse((self.root / "launcherDownloadConfig.json").exists())

    def test_channel_metadata_update_with_removed_active_tier(self):
        self.prepare_full_download()
        self.manager._write_local_json(
            "wwm_resource_state.json", {"active_tier": None, "versions": {}, "incomplete": []}
        )
        self.manager._update_local_config()
        self.assertIsNone(self.manager._read_resource_state()["active_tier"])
        self.assertNotIn("hd", self.manager._read_resource_state()["versions"])

    def test_download_cli_passes_explicit_tier(self):
        for tier in ("hd", "sd", "uhd"):
            with self.subTest(tier=tier), patch.object(self.manager, "download_full") as download:
                result = self.invoke("download", "cn", tier)
                self.assertEqual(result.exit_code, 0, result.output)
                download.assert_called_once_with(tier=tier, force_space=False)

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
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("安装情况", result.output)
        self.assertEqual(self.server.requests, [])

    def test_resource_action_options_require_tier(self):
        for option in ("--remove", "--force-md5", "--config-url"):
            args = ["resource", option]
            if option == "--config-url":
                args.append("https://example.invalid/config.json")
            result = self.invoke(*args)
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("须指定档位", result.output)
        self.assertEqual(self.server.requests, [])

    def test_disk_space_uses_nearest_existing_target_parent(self):
        manager = WGameManager(self.root / "new" / "game", "cn")
        with patch("ww_manager.core.shutil.disk_usage", return_value=SimpleNamespace(free=123)) as usage:
            self.assertEqual(manager.available_disk_space(), 123)
        usage.assert_called_once_with(self.root)
        self.assertFalse((self.root / "new").exists())

    def test_disk_space_can_read_real_temporary_filesystem(self):
        free = self.manager.available_disk_space()
        self.assertIsInstance(free, int)
        assert free is not None
        self.assertGreaterEqual(free, 0)

    def test_resource_space_refusal_preserves_existing_files_and_records(self):
        old = self.put(DEST, b"old")
        cfg = self.put("launcherDownloadConfig.json", b'{"bundles":{"UHD":{"version":"old"}}}')
        marker = self.put("launcherDownloadConfig/uhd.json", b'{"packName":"uhd","version":"old"}')
        with patch.object(self.manager, "available_disk_space", return_value=0):
            result = self.invoke("resource", "uhd")
        self.assertNotEqual(result.exit_code, 0, result.output)
        for text in ("空间不足", "差额", "--force"):
            self.assertIn(text, result.output)
        self.assertEqual(old.read_bytes(), b"old")
        self.assertEqual(cfg.read_bytes(), b'{"bundles":{"UHD":{"version":"old"}}}')
        self.assertEqual(marker.read_bytes(), b'{"packName":"uhd","version":"old"}')
        self.assertFalse((self.root / "wwm_resource_state.json").exists())
        self.assertEqual(self.server.requests, [("/manifest.json", None)])

    def test_resource_force_bypasses_space_only(self):
        with patch.object(self.manager, "available_disk_space", return_value=0):
            result = self.invoke("resource", "uhd", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual((self.root / DEST).read_bytes(), GOOD)
        self.registered()

    def test_resource_force_does_not_bypass_post_download_verification(self):
        with (
            patch.object(self.manager, "available_disk_space", return_value=0),
            patch.object(self.manager, "_batch_download", return_value=0),
        ):
            result = self.invoke("resource", "uhd", "--force")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("档位文件校验失败", result.output)
        self.assertFalse((self.root / "launcherDownloadConfig/uhd.json").exists())

    def test_space_exactly_equal_to_pending_files_is_accepted(self):
        self.server.resources = [item(), item("Client/Content/UHD/second.pak")]
        self.put(DEST, GOOD)
        with patch.object(self.manager, "available_disk_space", return_value=len(GOOD)):
            self.manager.download_resource_tier("uhd")
        self.assertEqual((self.root / "Client/Content/UHD/second.pak").read_bytes(), GOOD)
        self.assertNotIn(("/files/" + DEST, None), self.server.requests)

    def test_installed_explicit_verification_needs_no_free_space(self):
        self.manager.download_resource_tier("uhd")
        self.server.requests.clear()
        with patch.object(self.manager, "available_disk_space", return_value=0):
            self.manager.download_resource_tier("uhd")
        self.assertEqual(self.server.requests, [("/manifest.json", None)])

    def test_full_download_checks_combined_size_before_any_payload(self):
        self.prepare_full_download("uhd")
        with patch.object(self.manager, "available_disk_space", return_value=len(GOOD)):
            with self.assertRaises(DiskSpaceError):
                self.manager.download_full("uhd")
        self.assertEqual(self.server.requests, [("/manifest.json", None)])
        self.assertFalse((self.root / "Client").exists())
        self.assertFalse((self.root / "wwm_resource_state.json").exists())

    def test_full_hd_download_checks_space(self):
        self.prepare_full_download("hd")
        with patch.object(self.manager, "available_disk_space", return_value=len(GOOD)):
            result = self.invoke("download", "bilibili")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("空间不足", result.output)
        self.assertEqual(self.server.requests, [])
        self.assertFalse((self.root / "Client").exists())

    def test_full_download_force_propagates_to_combined_install(self):
        self.prepare_full_download("uhd")
        with patch.object(self.manager, "available_disk_space", return_value=0):
            result = self.invoke("download", "bilibili", "uhd", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual((self.root / DEST).read_bytes(), GOOD)
        self.registered()

    def test_unknown_disk_space_warns_without_claiming_sufficient(self):
        with (
            patch("ww_manager.core.shutil.disk_usage", side_effect=OSError("unavailable")),
            patch("ww_manager.core.logger.warning") as warning,
        ):
            self.manager.download_resource_tier("uhd")
        self.assertTrue(any("无法保证空间充足" in str(call) for call in warning.call_args_list))
        self.registered()

    def test_menu_preview_uses_official_sizes_without_writing(self):
        self.server.resources = [dict(item(), size="24")]
        rows = self.manager.resource_menu_data()
        uhd = next(row for row in rows if row["tier"] == "uhd")
        self.assertEqual(uhd["total_size"], 24)
        self.assertEqual(uhd["download_size"], 24)
        self.assertIsNone(uhd["error"])
        self.assertFalse(uhd["installed"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.server.requests, [("/manifest.json", None)])

    def test_menu_select_installed_changes_default_without_hash_or_download(self):
        self.manager.download_resource_tier("uhd")
        state = self.manager._read_resource_state()
        state["active_tier"] = "hd"
        self.manager._write_local_json("wwm_resource_state.json", state)
        self.server.requests.clear()
        prepared = self.manager._prepare_resource_tier("uhd")
        self.server.requests.clear()
        with patch.object(MD5Cache, "get", side_effect=AssertionError("不得重复校验")):
            self.manager.select_resource_tier("uhd", prepared)
        self.assertEqual(self.manager.current_resource_tier(), "uhd")
        self.assertEqual(self.server.requests, [])

    def test_menu_outdated_tier_is_not_treated_as_ready(self):
        self.manager.download_resource_tier("uhd")
        self.bundle["resourcePacks"]["uhd"]["version"] = "3.9.0"
        row = next(row for row in self.manager.resource_menu_data() if row["tier"] == "uhd")
        self.assertFalse(row["installed"])
        self.manager.select_resource_tier("uhd", row["prepared"])
        self.assertEqual(self.manager.resource_pack_version("uhd"), "3.9.0")

    def test_menu_cancel_is_read_only(self):
        with (
            patch("ww_manager.cli._interactive_terminal", return_value=True),
            patch("ww_manager.cli.build_selection_menu") as menu,
        ):
            menu.return_value.run.return_value = None
            result = self.invoke("resource")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(list(self.root.iterdir()), [])
        options = menu.call_args.args[0]
        uhd = next(option for option in options if option.key == "uhd")
        self.assertIn("完整包", uhd.details)
        self.assertIn("本次下载", uhd.details)
        self.assertEqual(uhd.description_style, "fg:ansired bold")

    def test_menu_select_uninstalled_downloads_and_registers(self):
        with (
            patch("ww_manager.cli._interactive_terminal", return_value=True),
            patch("ww_manager.cli.build_selection_menu") as menu,
        ):
            menu.return_value.run.return_value = MenuOption("uhd", "UHD")
            result = self.invoke("resource")
        self.assertEqual(result.exit_code, 0, result.output)
        self.registered()
        self.assertEqual(self.manager.current_resource_tier(), "uhd")

    def test_menu_force_unblocks_space_but_not_unavailable_tiers(self):
        with (
            patch("ww_manager.cli._interactive_terminal", return_value=True),
            patch.object(self.manager, "available_disk_space", return_value=0),
            patch("ww_manager.cli.build_selection_menu") as menu,
        ):
            menu.return_value.run.return_value = None
            result = self.invoke("resource")
            self.assertEqual(result.exit_code, 0, result.output)
            options = menu.call_args.args[0]
            self.assertIn("空间不足", next(o for o in options if o.key == "uhd").blocked_reason or "")
            result = self.invoke("resource", "--force")
            self.assertEqual(result.exit_code, 0, result.output)
            options = menu.call_args.args[0]
            self.assertIsNone(next(o for o in options if o.key == "uhd").blocked_reason)
            self.assertIsNotNone(next(o for o in options if o.key == "sd").blocked_reason)

    def test_menu_force_passes_through_to_real_installer(self):
        with (
            patch("ww_manager.cli._interactive_terminal", return_value=True),
            patch.object(self.manager, "available_disk_space", return_value=0),
            patch("ww_manager.cli.build_selection_menu") as menu,
        ):
            menu.return_value.run.return_value = MenuOption("uhd", "UHD")
            result = self.invoke("resource", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.registered()

    def test_menu_remove_defaults_to_cancel_even_with_force(self):
        self.manager.download_resource_tier("uhd")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with (
            patch("ww_manager.cli._interactive_terminal", return_value=True),
            patch("ww_manager.cli.build_selection_menu") as menu,
        ):
            menu.return_value.run.side_effect = [MenuOption("uhd", "极致", action="remove"), None]
            result = self.invoke("resource", "--force", input="\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(str(self.root / "Client/Content/UHD"), result.output)
        self.assertIn("自行放置的文件", result.output)
        self.assertIn("[y/N]", result.output)
        self.assertEqual(menu.call_count, 2)
        self.assertEqual(menu.call_args.kwargs["initial_key"], "uhd")
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_menu_remove_confirmed_removes_only_highlighted_tier_and_refreshes(self):
        self.manager.download_resource_tier("uhd")
        self.put("Client/Content/UHD/custom-note.txt", b"custom")
        hd = self.put("Client/Content/HD/keep.pak", b"keep-hd")
        common = self.put("Client/Content/Paks/common.pak", b"keep-common")
        with (
            patch("ww_manager.cli._interactive_terminal", return_value=True),
            patch("ww_manager.cli.build_selection_menu") as menu,
        ):
            menu.return_value.run.side_effect = [MenuOption("uhd", "极致", action="remove"), None]
            result = self.invoke("resource", input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse((self.root / "Client/Content/UHD").exists())
        self.assertFalse((self.root / "launcherDownloadConfig/uhd.json").exists())
        self.assertIsNone(self.manager._read_resource_state()["active_tier"])
        self.assertEqual(hd.read_bytes(), b"keep-hd")
        self.assertEqual(common.read_bytes(), b"keep-common")
        self.assertEqual(menu.call_count, 2)
        self.assertIsNone(menu.call_args.args[1])
        uhd = next(option for option in menu.call_args.args[0] if option.key == "uhd")
        self.assertIn("未安装", uhd.description)

    def test_menu_can_remove_local_tier_when_its_remote_manifest_is_unavailable(self):
        self.manager.download_resource_tier("uhd")
        self.bundle["resourcePacks"].clear()
        with (
            patch("ww_manager.cli._interactive_terminal", return_value=True),
            patch("ww_manager.cli.build_selection_menu") as menu,
        ):
            menu.return_value.run.side_effect = [MenuOption("uhd", "极致", action="remove"), None]
            result = self.invoke("resource", input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse((self.root / "Client/Content/UHD").exists())

    def test_menu_rechecks_space_at_actual_install(self):
        prepared = self.manager._prepare_resource_tier("uhd")
        with patch.object(self.manager, "available_disk_space", return_value=0):
            with self.assertRaises(DiskSpaceError):
                self.manager.select_resource_tier("uhd", prepared)
        self.assertFalse((self.root / "Client").exists())

    def test_unknown_download_size_warns_but_known_shortage_still_refuses(self):
        with (
            patch.object(self.manager, "available_disk_space", return_value=0),
            patch("ww_manager.core.logger.warning") as warning,
        ):
            self.manager._check_download_space([{"size": None}])
            self.assertTrue(any("大小未知" in str(call) for call in warning.call_args_list))
            with self.assertRaises(DiskSpaceError):
                self.manager._check_download_space([{"size": 1}, {"size": None}])

    def test_zero_byte_file_is_not_reported_as_unknown_size(self):
        with (
            patch.object(self.manager, "available_disk_space", return_value=0),
            patch("ww_manager.core.logger.warning") as warning,
        ):
            self.manager._check_download_space([{"size": 0}])
        warning.assert_not_called()

    def test_sync_refuses_space_and_force_can_repair(self):
        self.prepare_full_download("hd")
        with patch.object(self.manager, "available_disk_space", return_value=0):
            result = self.invoke("sync")
            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("空间不足", result.output)
            self.assertEqual(self.server.requests, [])
            result = self.invoke("sync", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.manager.resource_pack_version("hd"), "3.8.0")

    def test_sync_new_passes_force_to_repair(self):
        with patch.object(self.manager, "verify_new_version", return_value=True) as verify:
            result = self.invoke("sync", "--new", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        verify.assert_called_once_with(force_space=True)

    def checkout_contexts(self):
        self.put(DEST, b"old")
        self.put("launcherDownloadConfig.json", b'{"appId":"10003","version":"3.8.0"}')
        source = {"resource": [item(data=b"old")], "version": "3.8.0"}
        target = {
            "resource": [item()], "version": "3.8.0", "cdn": self.url, "res_base": "files",
            "info": {"default": {"version": "3.8.0"}},
        }
        return {"cn": source, "bilibili": target}

    def test_checkout_refuses_before_backups_or_game_changes(self):
        contexts = self.checkout_contexts()
        before = (self.root / "launcherDownloadConfig.json").read_bytes()
        with (
            patch.object(self.manager, "_fetch_server_context", side_effect=contexts.__getitem__),
            patch.object(self.manager, "available_disk_space", return_value=0),
        ):
            result = self.invoke("checkout", "bilibili")
        self.assertNotEqual(result.exit_code, 0, result.output)
        self.assertIn("空间不足", result.output)
        self.assertEqual((self.root / DEST).read_bytes(), b"old")
        self.assertEqual((self.root / "launcherDownloadConfig.json").read_bytes(), before)
        self.assertFalse(self.manager.diff_cache_dir.exists())
        self.assertEqual(self.server.requests, [])

    def test_checkout_force_downloads_diff_and_verifies(self):
        contexts = self.checkout_contexts()
        with (
            patch.object(self.manager, "_fetch_server_context", side_effect=contexts.__getitem__),
            patch.object(self.manager, "available_disk_space", return_value=0),
        ):
            result = self.invoke("checkout", "bilibili", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual((self.root / DEST).read_bytes(), GOOD)
        self.assertEqual(self.read_json("launcherDownloadConfig.json")["appId"], self.manager.config["appId"])

    def test_checkout_cache_only_does_not_require_download_space(self):
        contexts = self.checkout_contexts()
        cached = self.manager._server_cache_path("bilibili", DEST)
        cached.parent.mkdir(parents=True)
        cached.write_bytes(GOOD)
        with (
            patch.object(self.manager, "_fetch_server_context", side_effect=contexts.__getitem__),
            patch.object(self.manager, "available_disk_space", return_value=0),
        ):
            self.manager.checkout("bilibili")
        self.assertEqual((self.root / DEST).read_bytes(), GOOD)
        self.assertEqual(self.server.requests, [])

    def test_checkout_space_refusal_preserves_cache_due_for_reset(self):
        contexts = self.checkout_contexts()
        cached = self.manager._server_cache_path("bilibili", DEST)
        cached.parent.mkdir(parents=True)
        cached.write_bytes(GOOD)
        self.manager._save_cache_manifest({"major_minor": [3, 7]})
        with (
            patch.object(self.manager, "_fetch_server_context", side_effect=contexts.__getitem__),
            patch.object(self.manager, "available_disk_space", return_value=0),
        ):
            with self.assertRaises(DiskSpaceError):
                self.manager.checkout("bilibili")
        self.assertEqual(cached.read_bytes(), GOOD)
        self.assertEqual(self.manager._load_cache_manifest()["major_minor"], [3, 7])
        self.assertEqual((self.root / DEST).read_bytes(), b"old")

    def test_checkout_force_sync_propagates_space_bypass(self):
        contexts = self.checkout_contexts()
        with (
            patch.object(self.manager, "_fetch_server_context", side_effect=contexts.__getitem__),
            patch.object(self.manager, "sync_files") as sync,
        ):
            result = self.invoke("checkout", "bilibili", "--force-sync", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        sync.assert_called_once_with(force_check_md5=True, force_space=True)

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
