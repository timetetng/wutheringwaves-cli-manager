"""帮助文案回归测试；不访问网络、补全脚本或用户配置。"""

import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from ww_manager.cli import app


class CliHelpTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner(env={"COLUMNS": "120", "TERM": "dumb", "NO_COLOR": "1"})
        for mock in (
            patch("ww_manager.cli._auto_install_completion"),
            patch("ww_manager.cli.setup_logging"),
            patch("ww_manager.cli.load_app_config", return_value={}),
            patch("ww_manager.cli.save_app_config"),
            patch("ww_manager.cli.threading.Thread"),
            patch("ww_manager.cli.get_game_path", side_effect=AssertionError("帮助不应访问游戏目录")),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def test_root_help_has_short_command_descriptions(self):
        result = self.runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        for text in (
            "下载客户端", "下载画质资源包", "切换服务器", "查看状态",
            "下载或应用增量更新", "校验并修复文件", "获取抽卡记录链接", "更新工具",
        ):
            self.assertIn(text, result.output)
        for text in ("[初始化]", "Client/Content", "3.7+", "incremental", "不带档位"):
            self.assertNotIn(text, result.output)

    def test_subcommand_help_keeps_menu_and_safety_details(self):
        examples = {
            "download": ("不带参数", "服务器", "画质", "--allow-untested", "--force"),
            "resource": ("按 d", "删除", "确认", "--force-md5", "--remove"),
            "checkout": ("不带参数", "差异缓存", "--force-sync"),
            "predownload": ("--apply", "维护", "--force"),
        }
        for command, expected in examples.items():
            with self.subTest(command=command):
                result = self.runner.invoke(app, [command, "--help"])
                self.assertEqual(result.exit_code, 0, result.output)
                for text in expected:
                    self.assertIn(text, result.output)

    def test_hidden_alias_still_has_help(self):
        result = self.runner.invoke(app, ["incremental", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("predownload", result.output)
        self.assertIn("--apply", result.output)


if __name__ == "__main__":
    unittest.main()
