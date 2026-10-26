"""下载与切服菜单的命令分派测试；无网络、真实下载或用户配置写入。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from ww_manager.cli import app
from ww_manager.config import SERVER_CONFIGS
from ww_manager.core import WGameManager
from ww_manager.menu import MenuOption


class CommandMenuTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ww-command-menu-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = Mock(spec=WGameManager)
        self.factory = self.start_patch("ww_manager.cli.WGameManager", return_value=self.manager)
        self.menu = self.start_patch("ww_manager.cli.build_selection_menu")
        self.terminal = self.start_patch("ww_manager.cli._interactive_terminal", return_value=True)
        self.start_patch("ww_manager.cli._auto_install_completion")
        self.start_patch("ww_manager.cli.setup_logging")
        self.start_patch("ww_manager.cli.check_pypi_version_silent")
        self.start_patch(
            "ww_manager.cli.load_app_config",
            return_value={
                "default_path": str(self.root),
                "latest_available_version": "999.0",
            },
        )
        self.save_config = self.start_patch("ww_manager.cli.save_app_config")

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def invoke(self, *args):
        return CliRunner().invoke(app, list(args))

    def test_download_selects_server_then_tier_and_preserves_force(self):
        self.menu.return_value.run.side_effect = [MenuOption("bilibili", "B服"), MenuOption("uhd", "极致")]
        result = self.invoke("download", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.factory.assert_called_once_with(self.root, "bilibili")
        self.manager.download_full.assert_called_once_with(tier="uhd", force_space=True)
        self.assertEqual(self.menu.call_count, 2)
        self.assertIn("1/2", self.menu.call_args_list[0].kwargs["title"])
        self.assertIn("2/2", self.menu.call_args_list[1].kwargs["title"])
        self.assertEqual({o.key for o in self.menu.call_args_list[1].args[0]}, {"sd", "hd", "uhd"})
        for call in self.menu.call_args_list:
            self.assertNotIn("shortcuts", call.kwargs)
        self.save_config.assert_not_called()

    def test_download_cancel_at_either_stage_never_creates_manager(self):
        for choices in ([None], [MenuOption("cn", "官服"), None]):
            with self.subTest(choices=choices):
                self.menu.return_value.run.side_effect = choices
                result = self.invoke("download")
                self.assertEqual(result.exit_code, 0, result.output)
                self.factory.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_menu_eof_or_ctrl_c_is_cancellation(self):
        for exception in (EOFError(), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__):
                self.menu.return_value.run.side_effect = exception
                result = self.invoke("download")
                self.assertEqual(result.exit_code, 0, result.output)
                self.factory.assert_not_called()

    def test_download_server_argument_keeps_hd_default_without_menu(self):
        result = self.invoke("download", "bilibili")
        self.assertEqual(result.exit_code, 0, result.output)
        self.menu.assert_not_called()
        self.manager.download_full.assert_called_once_with(tier="hd", force_space=False)

    def test_download_explicit_tier_never_opens_menu(self):
        result = self.invoke("download", "cn", "SD", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.menu.assert_not_called()
        self.manager.download_full.assert_called_once_with(tier="SD", force_space=True)

    def test_global_menu_blocks_unsupported_tiers(self):
        self.menu.return_value.run.side_effect = [MenuOption("global", "国际服"), None]
        result = self.invoke("download")
        self.assertEqual(result.exit_code, 0, result.output)
        for option in self.menu.call_args_list[1].args[0]:
            if option.key == "hd":
                self.assertIsNone(option.blocked_reason)
            else:
                self.assertIn("国际服", option.blocked_reason or "")
        self.factory.assert_not_called()

    def test_global_menu_labels_untested_and_requires_separate_opt_in(self):
        for flags, allowed in ((["--force"], False), (["--allow-untested"], True)):
            with self.subTest(flags=flags):
                self.menu.return_value.run.side_effect = [MenuOption("global", "国际服"), None]
                result = self.invoke("download", *flags)
                self.assertEqual(result.exit_code, 0, result.output)
                for option in self.menu.call_args_list[-1].args[0]:
                    if option.key != "hd":
                        self.assertIn("未验证", option.description)
                        self.assertEqual(option.blocked_reason is None, allowed)
                self.factory.assert_not_called()

    def test_global_download_opt_in_configures_manager_without_redefining_force(self):
        self.manager.server_type = "global"
        result = self.invoke("download", "global", "UHD", "--allow-untested")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(self.manager.allow_untested)
        self.manager.download_full.assert_called_once_with(tier="UHD", force_space=False)

    def test_first_download_can_choose_menu_for_nonexistent_directory(self):
        target = self.root / "not-created" / "game"
        self.menu.return_value.run.return_value = None
        result = self.invoke("--path", str(target), "download")
        self.assertEqual(result.exit_code, 0, result.output)
        self.menu.assert_called_once()
        self.assertFalse(target.exists())
        self.factory.assert_not_called()

    def test_checkout_highlights_current_server_and_preserves_flags(self):
        (self.root / "launcherDownloadConfig.json").write_text(
            json.dumps(
                {
                    "appId": SERVER_CONFIGS["bilibili"]["appId"],
                }
            ),
            encoding="utf-8",
        )
        self.menu.return_value.run.return_value = MenuOption("cn", "官服")
        result = self.invoke("checkout", "--force", "--force-sync")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.menu.call_args.args[1], "bilibili")
        self.assertEqual(self.menu.call_args.kwargs["current_label"], "当前服务器")
        self.assertNotIn("shortcuts", self.menu.call_args.kwargs)
        self.factory.assert_called_once_with(self.root, "cn")
        self.manager.checkout.assert_called_once_with("cn", force_sync=True, force_space=True)

    def test_checkout_cancel_never_creates_manager(self):
        self.menu.return_value.run.return_value = None
        result = self.invoke("checkout")
        self.assertEqual(result.exit_code, 0, result.output)
        self.factory.assert_not_called()

    def test_checkout_explicit_argument_never_opens_menu(self):
        result = self.invoke("checkout", "bilibili", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.menu.assert_not_called()
        self.manager.checkout.assert_called_once_with("bilibili", force_sync=False, force_space=True)

    def test_noninteractive_missing_arguments_refuse_without_downloading(self):
        self.terminal.return_value = False
        for command in ("download", "checkout"):
            with self.subTest(command=command):
                result = self.invoke(command)
                self.assertNotEqual(result.exit_code, 0, result.output)
                self.assertIn("非交互环境", result.output)
                self.menu.assert_not_called()
                self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
