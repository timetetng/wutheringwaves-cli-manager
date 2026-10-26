"""统一菜单演示的安全边界：仅内存状态，不调用真实游戏操作。"""

import io
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.demo_cli_menus import GIB, DemoManager, main
from ww_manager.core import WGameManager, WWError
from ww_manager.menu import MenuOption


class TerminalBuffer(io.StringIO):
    def isatty(self):
        return True


class DemoMenuTests(unittest.TestCase):
    def test_demo_resource_actions_only_change_memory(self):
        with tempfile.TemporaryDirectory(prefix="ww-demo-memory-") as temporary:
            root = Path(temporary)
            manager = DemoManager(root, "cn", 5 * GIB)
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(WWError):
                    manager.select_resource_tier("uhd", {})
                self.assertEqual(manager.installed, {"hd"})
                manager.select_resource_tier("uhd", {}, force_space=True)
                manager.remove_resource_tier("hd")
                self.assertEqual(manager.current, "uhd")
                manager.remove_resource_tier("uhd")
                self.assertIsNone(manager.current)
            self.assertEqual(list(root.iterdir()), [])

    def run_demo(self, command, choices, *, low_space=False, force=False, confirm=True):
        output = TerminalBuffer()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch("sys.stdin.isatty", return_value=True))
            menu = stack.enter_context(patch("ww_manager.cli.build_selection_menu"))
            menu.return_value.run.side_effect = choices
            stack.enter_context(patch("ww_manager.cli.typer.confirm", return_value=confirm))
            # 任何误接入真实业务方法都会使测试失败。
            for method in ("download_full", "checkout", "select_resource_tier", "remove_resource_tier"):
                stack.enter_context(patch.object(WGameManager, method, side_effect=AssertionError("禁止真实操作")))
            args = ["--command", command]
            if low_space:
                args.append("--low-space")
            if force:
                args.append("--force")
            result = main(args)
        return result, output.getvalue(), menu

    def test_resource_demo_delete_refreshes_memory_state(self):
        code, output, menu = self.run_demo("resource", [MenuOption("hd", "高清", action="remove"), None])
        self.assertEqual(code, 0, output)
        self.assertIn("仅改变内存状态", output)
        hd = next(option for option in menu.call_args.args[0] if option.key == "hd")
        self.assertIn("未安装", hd.description)
        self.assertIsNone(menu.call_args.args[1])

    def test_resource_demo_cancelled_delete_preserves_default(self):
        code, output, menu = self.run_demo(
            "resource", [MenuOption("hd", "高清", action="remove"), None], confirm=False,
        )
        self.assertEqual(code, 0, output)
        hd = next(option for option in menu.call_args.args[0] if option.key == "hd")
        self.assertIn("已安装", hd.description)
        self.assertEqual(menu.call_args.args[1], "hd")

    def test_download_demo_uses_both_real_menu_stages(self):
        code, output, menu = self.run_demo(
            "download", [MenuOption("bilibili", "B服"), MenuOption("sd", "标清")],
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(menu.call_count, 2)
        self.assertIn("将下载 bilibili 服 / SD", output)

    def test_download_demo_low_space_has_failure_exit_code(self):
        code, output, _ = self.run_demo(
            "download", [MenuOption("bilibili", "B服"), MenuOption("uhd", "极致")], low_space=True,
        )
        self.assertEqual(code, 1, output)
        self.assertIn("空间不足", output)
        self.assertIn("模拟结束", output)

    def test_download_demo_force_only_simulates_bypass(self):
        code, output, _ = self.run_demo(
            "download", [MenuOption("bilibili", "B服"), MenuOption("uhd", "极致")],
            low_space=True, force=True,
        )
        self.assertEqual(code, 0, output)
        self.assertIn("--force 已绕过", output)

    def test_checkout_demo_uses_real_server_menu(self):
        code, output, menu = self.run_demo("checkout", [MenuOption("cn", "官服")])
        self.assertEqual(code, 0, output)
        self.assertEqual(menu.call_args.args[1], "bilibili")
        self.assertIn("将从 bilibili 切换到 cn", output)


if __name__ == "__main__":
    unittest.main()
