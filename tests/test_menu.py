"""菜单键盘交互测试：模拟终端输入，不访问游戏目录或网络。"""

import asyncio
import unittest
from typing import Optional

from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import DummyOutput

from ww_manager.menu import MenuOption, MenuShortcut, build_selection_menu

GIB = 1024**3
CHOICES = (
    ("sd", "标清", 12 * GIB),
    ("hd", "高清", 0),
    ("uhd", "极致", 48 * GIB),
)


class ResourceMenuTests(unittest.TestCase):
    def run_keys(
        self, keys, *, free_bytes=80 * GIB, current_tier: Optional[str] = "hd",
        shortcuts=(), initial_key=None,
    ):
        options = [
            MenuOption(
                key, name, "✓ 已安装" if key == "hd" else "× 未安装",
                "完整包 · 预计新增",
                "空间不足" if required > free_bytes else None,
                description_style="fg:ansigreen bold" if key == "hd" else "fg:ansired bold",
            )
            for key, name, required in CHOICES
        ]
        with create_pipe_input() as pipe:
            app = build_selection_menu(
                options, current_tier, title="模拟数据", shortcuts=shortcuts, initial_key=initial_key,
                input=pipe, output=DummyOutput(),
            )
            snapshots = []

            def capture(event):
                window = app.layout.container
                assert isinstance(window, Window)
                control = window.content
                assert isinstance(control, FormattedTextControl)
                fragments = to_formatted_text(control.text)
                self.assertIn(("fg:ansigreen bold", "  ✓ 已安装"), fragments)
                self.assertIn(("fg:ansired bold", "  × 未安装"), fragments)
                snapshots.append("".join(fragment[1] for fragment in fragments))

            async def feed_keys():
                # 分批输入，让确认被拒绝后的提示实际渲染，再继续后续操作。
                for key in keys:
                    pipe.send_text(key)
                    await asyncio.sleep(0.01)

            def start_input():
                app.create_background_task(feed_keys())

            app.after_render += capture
            result = app.run(pre_run=start_input)
        selected = None if result is None else (
            result.key if result.action == "select" else f"{result.action}:{result.key}"
        )
        return selected, "\n".join(snapshots)

    def test_enter_confirms_current_default(self):
        result, screen = self.run_keys("\r")
        self.assertEqual(result, "hd")
        for text in ("[当前默认]", "标清", "高清", "极致", "完整包", "预计新增", "模拟数据"):
            self.assertIn(text, screen)
        for text in ("空格", "●", "○"):
            self.assertNotIn(text, screen)
        self.assertIn(
            "   标清  × 未安装\n   完整包 · 预计新增\n"
            " › 高清  ✓ 已安装  [当前默认]\n   完整包 · 预计新增\n"
            "   极致  × 未安装", screen,
        )

    def test_selection_keys(self):
        cases = (
            ("down_enter", "\x1b[B\r", "uhd"),
            ("up_enter", "\x1b[A\r", "sd"),
            ("wrap_up", "\x1b[A\x1b[A\r", "uhd"),
            ("wrap_down", "\x1b[B\x1b[B\r", "sd"),
            ("ctrl_c", "\x03", None),
            ("escape", "\x1b", None),
        )
        for name, keys, expected in cases:
            with self.subTest(name=name):
                result, _ = self.run_keys(keys)
                self.assertEqual(result, expected)

    def test_low_space_does_not_warn_before_confirmation(self):
        result, screen = self.run_keys("\x03", free_bytes=5 * GIB)
        self.assertIsNone(result)
        self.assertNotIn("空间不足", screen)

    def test_low_space_refuses_and_allows_selecting_installed(self):
        result, screen = self.run_keys("\x1b[B\r\x1b[A\r", free_bytes=5 * GIB)
        self.assertEqual(result, "hd")
        self.assertIn("空间不足", screen)

    def test_low_space_refuses_and_stays_open_until_cancelled(self):
        result, _ = self.run_keys("\x1b[B\r\x03", free_bytes=5 * GIB)
        self.assertIsNone(result)

    def test_exact_space_is_accepted(self):
        result, _ = self.run_keys("\x1b[A\r", free_bytes=12 * GIB)
        self.assertEqual(result, "sd")

    def test_zero_space_can_select_installed(self):
        result, _ = self.run_keys("\r", free_bytes=0)
        self.assertEqual(result, "hd")

    def test_unknown_default_focuses_first(self):
        result, _ = self.run_keys("\r", current_tier=None)
        self.assertEqual(result, "sd")

    def test_delete_shortcut_returns_highlighted_item_even_if_download_blocked(self):
        result, screen = self.run_keys(
            "\x1b[Bd", free_bytes=0,
            shortcuts=[MenuShortcut("d", "删除高亮档位", "remove")],
        )
        self.assertEqual(result, "remove:uhd")
        self.assertIn("d 删除高亮档位", screen)
        self.assertNotIn("无法确认", screen)

    def test_delete_is_disabled_unless_explicitly_enabled(self):
        result, screen = self.run_keys("d\x03")
        self.assertIsNone(result)
        self.assertNotIn("删除", screen)

    def test_initial_focus_can_differ_from_current_default(self):
        result, screen = self.run_keys("\r", initial_key="uhd")
        self.assertEqual(result, "uhd")
        self.assertIn("高清  ✓ 已安装  [当前默认]", screen)
        self.assertIn(" › 极致", screen)

    def test_shortcut_bindings_keep_their_own_actions(self):
        for key, action in (("d", "remove"), ("e", "export")):
            with self.subTest(key=key):
                result, _ = self.run_keys(key, shortcuts=[
                    MenuShortcut("d", "删除", "remove"), MenuShortcut("e", "导出", "export"),
                ])
                self.assertEqual(result, f"{action}:hd")

    def test_conflicting_shortcuts_are_rejected(self):
        for shortcuts in (
            [MenuShortcut("enter", "删除", "remove")],
            [MenuShortcut("d", "删除", "remove"), MenuShortcut("d", "导出", "export")],
        ):
            with self.subTest(shortcuts=shortcuts):
                with self.assertRaisesRegex(ValueError, "快捷键"):
                    build_selection_menu([MenuOption("hd", "HD")], shortcuts=shortcuts)

    def test_empty_choices_rejected(self):
        with self.assertRaisesRegex(ValueError, "选项列表不能为空"):
            build_selection_menu([])

    def test_duplicate_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, "选项 key 不得重复"):
            build_selection_menu([MenuOption("a", "A"), MenuOption("a", "B")])


if __name__ == "__main__":
    unittest.main()
