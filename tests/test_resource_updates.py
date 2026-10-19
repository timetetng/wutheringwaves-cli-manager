"""分包更新编排回归；所有索引/文件走本地 HTTP，不接触真实游戏或线上渠道。"""

import copy
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ww_manager.cli import app
from ww_manager.core import ConfigError, NetworkError, WGameManager, WWError
from ww_manager.resource_updates import HISTORY, PLAN, PackIncrementalManager, ResourceUpdater

OLD = b"old-release-data"
NEW = b"new-release-resource-content"
SOURCE = "3.7.1"
TARGET = "3.8.0"


def file_item(dest, payload=NEW):
    return {"dest": dest, "size": len(payload), "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest()}


def destination(pack):
    return "Client/Content/Paks/common.pak" if pack == "common" else f"Client/Content/{pack.upper()}/data.pak"


class TestServer(ThreadingHTTPServer):
    payloads: dict
    requested: list


class CDN(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        assert isinstance(self.server, TestServer)
        self.server.requested.append(self.path)
        payload = self.server.payloads.get(self.path)
        if payload is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ResourceUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = TestServer(("127.0.0.1", 0), CDN)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ww-pack-update-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server.payloads = {}
        self.server.requested = []
        self.owner = WGameManager(self.root, "cn")
        self.owner._write_local_json(
            "launcherDownloadConfig.json",
            {
                "appId": "10003",
                "version": SOURCE,
                "custom": 42,
            },
        )
        self.bundle = {"resourcePacks": {}, "cdnList": [{"K1": 1, "K2": 1, "P": 1, "url": self.url}]}
        for name in ("common", "hd", "sd", "uhd"):
            self.bundle["resourcePacks"][name] = {
                "version": TARGET,
                "baseUrl": "files",
                "indexFile": f"full/{name}.json",
                "patchConfig": [{"version": SOURCE, "indexFile": f"patch/{name}.json", "baseUrl": "files"}],
            }
            self.set_index(name, {"resource": [file_item(destination(name))], "deleteFiles": []})
            self.serve_json(f"/full/{name}.json", {"resource": [file_item(destination(name))]})
            self.server.payloads[f"/files/{destination(name)}"] = NEW
            self.put(destination(name), OLD)
        self.choose("uhd")
        mocked = patch.object(self.owner, "get_bundle_config", side_effect=lambda *_: copy.deepcopy(self.bundle))
        mocked.start()
        self.addCleanup(mocked.stop)
        sleeper = patch("ww_manager.incremental.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.updater = ResourceUpdater(self.owner)

    def put(self, relative, payload):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def serve_json(self, url, data):
        self.server.payloads[url] = json.dumps(data).encode()

    def set_index(self, pack, index):
        self.serve_json(f"/patch/{pack}.json", index)

    def read_json(self, relative):
        return json.loads((self.root / relative).read_text())

    def choose(self, tier):
        self.owner._register_installed_tier(tier, SOURCE, {})
        self.owner._write_local_json(
            "wwm_resource_state.json",
            {
                "active_tier": tier,
                "versions": {"common": SOURCE, tier: SOURCE},
                "common_server": "cn",
                "incomplete": [],
            },
        )

    def test_current_versions_with_no_predownload_create_no_plan(self):
        self.bundle["predownload"] = None
        for config in self.bundle["resourcePacks"].values():
            config["version"] = SOURCE
        with self.assertRaisesRegex(ConfigError, "没有可用分包更新"):
            self.updater.download()
        self.assertFalse((self.root / PLAN).exists())

    def test_unknown_predownload_format_is_not_guessed(self):
        self.bundle["predownload"] = {"future_format": True}
        with self.assertRaisesRegex(ConfigError, "尚未适配验证"):
            self.updater.download()
        self.assertFalse((self.root / PLAN).exists())
        self.assertEqual((self.root / destination("common")).read_bytes(), OLD)

    def test_without_launcher_incremental_routes_to_auto_config(self):
        with patch("ww_manager.resource_updates.ResourceUpdater.download", return_value=True) as download:
            self.assertTrue(self.owner.download_incremental())
        download.assert_called_once_with(None)

    def test_global_incremental_keeps_original_hd_engine(self):
        self.owner.server_type = "global"
        self.owner._launcher_info = {"default": {"version": TARGET}}
        self.owner._cdn_node = self.url
        with (
            patch("ww_manager.resource_updates.ResourceUpdater.download") as packs,
            patch("ww_manager.core.IncrementalManager") as legacy,
            patch("ww_manager.core.check_hpatchz_requirements", return_value=(True, "ok")),
        ):
            legacy.return_value.download_incremental.return_value = True
            self.assertTrue(self.owner.download_incremental())
        packs.assert_not_called()
        legacy.return_value.download_incremental.assert_called_once()

    def test_three_tiers_download_and_apply_without_hd_dependency(self):
        for tier in ("uhd", "sd", "hd"):
            with self.subTest(tier=tier):
                # 每轮独立源状态；此前已应用档位保留，不应参与新计划。
                self.choose(tier)
                self.owner._write_local_json(
                    "launcherDownloadConfig.json",
                    {
                        "appId": "10003",
                        "version": SOURCE,
                        "custom": 42,
                    },
                )
                for pack in ("common", "hd", "sd", "uhd"):
                    self.put(destination(pack), OLD)
                self.server.requested = []
                self.assertTrue(self.owner.download_incremental(config_url="local-fixture"))
                plan = self.read_json(PLAN)
                self.assertEqual(set(plan["packs"]), {"common", tier})
                self.assertEqual((self.root / destination(tier)).read_bytes(), OLD)
                self.assertEqual(self.read_json("launcherDownloadConfig.json")["version"], SOURCE)
                self.assertTrue((self.root / f".incremental_download/packs/{tier}/indexFile.json").is_file())
                self.assertTrue(self.owner.apply_incremental())
                self.assertEqual(self.read_json("launcherDownloadConfig.json")["version"], TARGET)
                self.assertEqual(self.read_json("launcherDownloadConfig.json")["custom"], 42)
                for pack in ("common", "hd", "sd", "uhd"):
                    self.assertEqual(
                        (self.root / destination(pack)).read_bytes(), NEW if pack in ("common", tier) else OLD
                    )
                self.assertFalse((self.root / PLAN).exists())
                self.assertTrue((self.root / HISTORY).is_file())
                self.assertEqual(self.owner.resource_pack_version(tier), TARGET)
                self.assertTrue(self.owner.verify_new_version())
                if tier != "hd":
                    self.assertFalse(any("/HD/" in u or "/patch/hd.json" in u for u in self.server.requested))

    def test_missing_tier_patch_does_not_download_or_fallback(self):
        self.bundle["resourcePacks"]["uhd"]["patchConfig"] = []
        with self.assertRaisesRegex(ConfigError, "暂无"):
            self.updater.download()
        self.assertFalse((self.root / PLAN).exists())
        self.assertFalse(any(u.startswith("/files/") for u in self.server.requested))
        self.assertEqual((self.root / destination("common")).read_bytes(), OLD)

    def test_missing_common_patch_rejects_entire_plan(self):
        self.bundle["resourcePacks"]["common"]["patchConfig"] = []
        with self.assertRaises(ConfigError):
            self.updater.download()
        self.assertEqual(self.server.requested, [])

    def test_same_version_pack_skips_only_itself(self):
        self.bundle["resourcePacks"]["uhd"]["version"] = SOURCE
        self.assertTrue(self.updater.download())
        self.assertTrue(self.updater.apply())
        self.assertEqual((self.root / destination("uhd")).read_bytes(), OLD)
        self.assertEqual(self.owner.resource_pack_version("common"), TARGET)
        self.assertEqual(self.owner.resource_pack_version("uhd"), SOURCE)
        self.assertFalse(any("/patch/uhd.json" in u for u in self.server.requested))

    def test_empty_tier_update_is_valid_without_wine(self):
        self.set_index("uhd", {"resource": [], "deleteFiles": []})
        with patch(
            "ww_manager.incremental.check_hpatchz_requirements", side_effect=AssertionError("无差分不需要 wine")
        ):
            self.assertTrue(self.updater.download())
            self.assertTrue(self.updater.apply())
        self.assertEqual(self.owner.resource_pack_version("uhd"), TARGET)
        self.assertEqual((self.root / destination("uhd")).read_bytes(), OLD)
        self.assertTrue(self.updater.verify())

    def test_download_failure_keeps_game_and_versions_and_not_ready(self):
        del self.server.payloads[f"/files/{destination('uhd')}"]
        with self.assertRaises(NetworkError):
            self.updater.download()
        self.assertFalse(self.read_json(PLAN)["ready"])
        for pack in ("common", "uhd"):
            self.assertEqual((self.root / destination(pack)).read_bytes(), OLD)
            self.assertEqual(self.owner.resource_pack_version(pack), SOURCE)
        with self.assertRaisesRegex(ConfigError, "尚未全部下载"):
            self.updater.apply()
        self.server.payloads[f"/files/{destination('uhd')}"] = NEW
        self.assertTrue(self.updater.download())
        self.assertTrue(self.updater.apply())

    def test_second_pack_failure_resumes_without_reapplying_common(self):
        self.assertTrue(self.updater.download())
        original = PackIncrementalManager.apply_incremental
        calls = []

        def fail_tier(engine):
            calls.append(engine.pack_name)
            return original(engine) if engine.pack_name == "common" else False

        with patch.object(PackIncrementalManager, "apply_incremental", autospec=True, side_effect=fail_tier):
            with self.assertRaisesRegex(WWError, "主版本未升级"):
                self.updater.apply()
        self.assertEqual(calls, ["common", "uhd"])
        self.assertEqual(self.owner.resource_pack_version("common"), TARGET)
        self.assertEqual(self.owner.resource_pack_version("uhd"), SOURCE)
        self.assertEqual(self.read_json("launcherDownloadConfig.json")["version"], SOURCE)
        self.assertIn("uhd", self.owner._read_resource_state()["incomplete"])
        self.assertEqual(
            next(s for s in self.owner.resource_tier_status() if s["tier"] == "uhd")["state"], "更新未完成"
        )
        self.assertTrue(self.updater.apply())
        self.assertEqual(self.read_json("launcherDownloadConfig.json")["version"], TARGET)
        self.assertFalse(self.owner._read_resource_state()["incomplete"])

    def test_corrupt_tier_cache_prevents_first_pack_modification(self):
        self.assertTrue(self.updater.download())
        self.put(f".incremental_download/packs/uhd/resources/{destination('uhd')}", b"x" * len(NEW))
        with self.assertRaisesRegex(ConfigError, "缓存缺失或损坏"):
            self.updater.apply()
        self.assertEqual((self.root / destination("common")).read_bytes(), OLD)
        self.assertTrue(self.updater.download())
        self.assertTrue(self.updater.apply())

    def test_cached_index_tampering_rejected_before_common(self):
        self.assertTrue(self.updater.download())
        self.put(".incremental_download/packs/uhd/indexFile.json", b'{"resource":[]}')
        with self.assertRaisesRegex(ConfigError, "缓存版本或索引"):
            self.updater.apply()
        self.assertEqual((self.root / destination("common")).read_bytes(), OLD)

    def test_server_or_selection_change_rejects_plan(self):
        self.assertTrue(self.updater.download())
        self.owner.server_type = "bilibili"
        with self.assertRaisesRegex(ConfigError, "渠道或当前档位"):
            self.updater.apply()
        self.owner.server_type = "cn"
        state = self.owner._read_resource_state()
        state["active_tier"] = "sd"
        self.owner._write_local_json("wwm_resource_state.json", state)
        with self.assertRaisesRegex(ConfigError, "渠道或当前档位"):
            self.updater.apply()

    def test_online_target_changes_do_not_change_downloaded_plan(self):
        self.assertTrue(self.updater.download())
        self.bundle["resourcePacks"]["uhd"]["version"] = "3.9.0"
        with patch.object(self.owner, "get_bundle_config", side_effect=AssertionError("应用不查线上配置")):
            self.assertTrue(self.updater.apply())
        self.assertEqual(self.owner.resource_pack_version("uhd"), TARGET)

    def test_source_version_change_rejects_plan(self):
        self.assertTrue(self.updater.download())
        self.owner._record_resource_pack("uhd", "3.6.0")
        with self.assertRaisesRegex(ConfigError, "源版本已改变"):
            self.updater.apply()

    def test_wrong_pack_paths_rejected_before_file_download(self):
        for dest in ("Client/Content/HD/evil.pak", "Client/Content/UHD/../evil.pak", "/tmp/evil.pak"):
            with self.subTest(dest=dest):
                self.set_index("uhd", {"resource": [file_item(dest)]})
                with self.assertRaises((ConfigError, WWError)):
                    self.updater.download()
                self.assertFalse((self.root / PLAN).exists())
        self.assertFalse(any(u.startswith("/files/") for u in self.server.requested))

    def test_common_cannot_delete_tier_files(self):
        self.set_index("common", {"deleteFiles": [destination("uhd")]})
        with self.assertRaisesRegex(ConfigError, "common 清单越界"):
            self.updater.download()

    def test_group_output_cannot_escape_tier(self):
        self.set_index(
            "uhd",
            {
                "resource": [],
                "groupInfos": [
                    {
                        "dest": "one.krpdiff",
                        "srcFiles": [file_item(destination("uhd"), OLD)],
                        "dstFiles": [file_item(destination("hd"))],
                    }
                ],
            },
        )
        with self.assertRaisesRegex(ConfigError, "清单越界"):
            self.updater.download()

    def test_symlink_cache_rejected_before_game_change(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        (self.root / ".incremental_download").symlink_to(temporary.name, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, "软链接"):
            self.updater.download()

    def test_symlink_metadata_rejected_without_overwriting_external_file(self):
        sentinel = self.put("outside.txt", b"keep-external-data")
        metadata = self.root / ".incremental_download/packs/common/indexFile.json"
        metadata.parent.mkdir(parents=True)
        metadata.symlink_to(sentinel)
        with self.assertRaisesRegex(ConfigError, "软链接"):
            self.updater.download()
        self.assertEqual(sentinel.read_bytes(), b"keep-external-data")

    def test_diff_without_verified_transport_record_is_rejected(self):
        self.set_index(
            "uhd",
            {
                "resource": [],
                "groupInfos": [
                    {
                        "dest": "unverified.krpdiff",
                        "srcFiles": [file_item(destination("uhd"), OLD)],
                        "dstFiles": [file_item(destination("uhd"))],
                    }
                ],
            },
        )
        with self.assertRaisesRegex(ConfigError, "缺少带大小和 MD5"):
            self.updater.download()
        self.assertFalse((self.root / PLAN).exists())

    def test_cleanup_failure_after_committing_pack_is_warning_only(self):
        import shutil

        self.assertTrue(self.updater.download())
        original = shutil.rmtree

        def fail_download_cleanup(path, *args, **kwargs):
            if ".incremental_download" in Path(path).parts:
                raise PermissionError("模拟临时目录被占用")
            return original(path, *args, **kwargs)

        with patch("ww_manager.incremental.shutil.rmtree", side_effect=fail_download_cleanup):
            self.assertTrue(self.updater.apply())
        self.assertEqual(self.read_json("launcherDownloadConfig.json")["version"], TARGET)
        self.assertFalse((self.root / PLAN).exists())
        self.assertTrue((self.root / ".incremental_download/packs/uhd").is_dir())

    def test_resource_sets_selection_and_remove_does_not_guess_another(self):
        self.owner.download_resource_tier("sd")
        self.assertEqual(self.owner.current_resource_tier(), "sd")
        self.assertEqual(self.owner.resource_pack_version("sd"), TARGET)
        self.owner.remove_resource_tier("uhd")
        self.assertEqual(self.owner.current_resource_tier(), "sd")
        self.owner.remove_resource_tier("sd")
        with self.assertRaisesRegex(ConfigError, "当前档位已被移除"):
            self.owner.current_resource_tier()
        self.assertTrue((self.root / destination("hd")).is_file())

    def test_legacy_state_defaults_hd_but_does_not_guess_installed_uhd(self):
        (self.root / "wwm_resource_state.json").unlink()
        self.assertEqual(self.owner.current_resource_tier(), "hd")

    def test_pending_verify_cannot_promote_version(self):
        self.assertTrue(self.updater.download())
        with self.assertRaisesRegex(ConfigError, "尚未应用"):
            self.owner.verify_new_version()
        self.assertEqual(self.owner.resource_pack_version("uhd"), SOURCE)

    def test_downgrade_rejected(self):
        self.bundle["resourcePacks"]["uhd"]["version"] = "3.6.0"
        with self.assertRaisesRegex(ConfigError, "拒绝降级"):
            self.updater.download()

    def test_group_uses_original_engine_with_separate_versions(self):
        diff_dest = "update.krpdiff"
        self.server.payloads[f"/files/{diff_dest}"] = b"test-diff-transport"
        self.set_index(
            "uhd",
            {
                "resource": [file_item(diff_dest, b"test-diff-transport")],
                "groupInfos": [
                    {
                        "dest": diff_dest,
                        "srcFiles": [file_item(destination("uhd"), OLD)],
                        "dstFiles": [file_item(destination("uhd"))],
                    }
                ],
            },
        )
        self.assertTrue(self.updater.download())
        calls = []

        def simulate_patch(engine, krpdiff, output, sources, **kwargs):
            # 只替代外部 hpatchz 调用；源校验、输出校验和迁移均执行原引擎。
            calls.append((engine.pack_name, krpdiff.read_bytes(), sources))
            target = output / destination("uhd")
            target.parent.mkdir(parents=True)
            target.write_bytes(NEW)
            return True

        with (
            patch("ww_manager.incremental.check_hpatchz_requirements", return_value=(True, "")),
            patch("ww_manager.incremental.get_hpatchz_path", return_value=self.root / "fixture-hpatchz"),
            patch.object(PackIncrementalManager, "_run_hpatchz", autospec=True, side_effect=simulate_patch),
        ):
            self.assertTrue(self.updater.apply())
        self.assertEqual(calls[0][0:2], ("uhd", b"test-diff-transport"))
        self.assertEqual((self.root / destination("uhd")).read_bytes(), NEW)
        self.assertEqual((self.root / destination("hd")).read_bytes(), OLD)

    def prepare_bilibili(self):
        self.owner.server_type = "bilibili"
        state = self.owner._read_resource_state()
        state["common_server"] = "bilibili"
        self.owner._write_local_json("wwm_resource_state.json", state)
        self.owner._launcher_info = {"predownload": {"version": TARGET, "config": {"indexFile": "b-target.json"}}}
        self.owner._cdn_node = self.url
        self.serve_json(
            "/b-target.json", {"resource": [file_item(destination("common")), file_item(destination("hd"))]}
        )

    def test_bilibili_common_requires_matching_channel_target(self):
        self.prepare_bilibili()
        self.assertTrue(self.updater.download())
        self.assertTrue(self.updater.apply())
        self.assertEqual((self.root / destination("common")).read_bytes(), NEW)
        self.assertEqual((self.root / destination("hd")).read_bytes(), OLD)

    def test_bilibili_common_rejects_official_channel_exclusive_file(self):
        self.prepare_bilibili()
        self.set_index("common", {"resource": [file_item("Client/official-only.dll")]})
        with self.assertRaisesRegex(ConfigError, "与 B 服目标清单不符"):
            self.updater.download()
        self.assertEqual((self.root / destination("common")).read_bytes(), OLD)
        self.assertFalse((self.root / PLAN).exists())

    def test_bilibili_common_rejects_deleting_required_channel_file(self):
        self.prepare_bilibili()
        self.set_index("common", {"deleteFiles": [destination("common")]})
        with self.assertRaisesRegex(ConfigError, "仍需要"):
            self.updater.download()

    def test_bilibili_no_target_manifest_does_not_fallback(self):
        self.prepare_bilibili()
        self.owner._launcher_info = {"default": {"version": SOURCE}}
        with self.assertRaisesRegex(ConfigError, "目标清单尚未开放"):
            self.updater.download()

    def test_group_output_cannot_be_deleted_by_same_patch(self):
        dest = destination("uhd")
        self.set_index(
            "uhd",
            {
                "resource": [file_item("patch.krpdiff")],
                "groupInfos": [
                    {"dest": "patch.krpdiff", "srcFiles": [file_item(dest, OLD)], "dstFiles": [file_item(dest)]}
                ],
                "deleteFiles": [dest],
            },
        )
        with self.assertRaisesRegex(ConfigError, "同时写入并删除"):
            self.updater.download()

    def test_old_history_cannot_repair_a_different_version(self):
        self.assertTrue(self.updater.download())
        self.assertTrue(self.updater.apply())
        self.owner._record_resource_pack("uhd", SOURCE)
        with self.assertRaisesRegex(ConfigError, "拒绝使用旧记录"):
            self.updater.verify()

    def test_incremental_cli_routes_config_and_does_not_request_resource_after_apply(self):
        for name in ("_auto_install_completion", "setup_logging", "check_pypi_version_silent", "save_app_config"):
            mocked = patch(f"ww_manager.cli.{name}")
            mocked.start()
            self.addCleanup(mocked.stop)
        with (
            patch("ww_manager.cli.load_app_config", return_value={"latest_available_version": "999.0"}),
            patch("ww_manager.cli.get_game_path", return_value=self.root),
            patch("ww_manager.cli.WGameManager", return_value=self.owner),
        ):
            result = CliRunner().invoke(app, ["incremental", "--config-url", "fixture"])
            self.assertEqual(result.exit_code, 0, result.output)
            result = CliRunner().invoke(app, ["incremental", "--apply"], input="y\n")
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("需独立校验", result.output)
            self.assertEqual(self.owner.resource_pack_version("uhd"), TARGET)


if __name__ == "__main__":
    unittest.main()
