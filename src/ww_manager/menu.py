"""通用 CLI 单选菜单；业务逻辑、数据获取和操作由调用方负责。"""

from dataclasses import dataclass, replace
from typing import Optional, Sequence

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style


@dataclass(frozen=True)
class MenuOption:
    """key 用于业务识别；blocked_reason 非空时允许浏览但拒绝确认。"""

    key: str
    label: str
    description: str = ""
    details: str = ""
    blocked_reason: Optional[str] = None
    description_style: str = ""
    action: str = "select"


@dataclass(frozen=True)
class MenuShortcut:
    """附加动作只返回标识和高亮项，不在菜单内部执行副作用。"""

    key: str
    label: str
    action: str


def build_selection_menu(
    options: Sequence[MenuOption],
    current_key: Optional[str] = None,
    *,
    title: str = "请选择",
    subtitle: str = "",
    current_label: str = "当前默认",
    initial_key: Optional[str] = None,
    shortcuts: Sequence[MenuShortcut] = (),
    input: Optional[Input] = None,
    output: Optional[Output] = None,
) -> Application[Optional[MenuOption]]:
    """返回可运行的菜单应用；.run() 返回选项，取消则返回 None。

    ↑/↓ 移动高亮、回车确认高亮项；Esc/Ctrl-C 取消。
    本组件不联网、不读写配置、不执行所选操作。
    调用方可传入 Input/Output 测试键盘交互，无需真实终端。
    """
    if not options:
        raise ValueError("选项列表不能为空")
    items = tuple(options)
    if len({item.key for item in items}) != len(items):
        raise ValueError("选项 key 不得重复")
    reserved = {"up", "down", "enter", "escape", "c-c"}
    shortcut_keys = [shortcut.key for shortcut in shortcuts]
    if len(set(shortcut_keys)) != len(shortcut_keys) or any(key in reserved for key in shortcut_keys):
        raise ValueError("快捷键不得重复或覆盖导航键")
    focus_key = initial_key if initial_key is not None else current_key
    focused = next((i for i, item in enumerate(items) if item.key == focus_key), 0)
    message = ""
    keys = KeyBindings()

    def render() -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = [("class:title", title + "\n")]
        if subtitle:
            fragments.append(("class:hint", subtitle + "\n"))
        fragments.append(("", "\n"))
        for index, item in enumerate(items):
            focused_style = "class:focused" if index == focused else ""
            pointer = "›" if index == focused else " "
            current = f"  [{current_label}]" if item.key == current_key else ""
            fragments.append((focused_style, f" {pointer} {item.label}"))
            if item.description:
                fragments.append((item.description_style, f"  {item.description}"))
            fragments.append((focused_style, current + "\n"))
            if item.details:
                fragments.append(("class:hint", f"   {item.details}\n"))
        shortcut_hint = "".join(f"  {shortcut.key} {shortcut.label}" for shortcut in shortcuts)
        fragments.extend(
            [
                ("class:hint", f"\n↑/↓ 选择  回车 确认高亮项{shortcut_hint}  Esc 取消\n"),
                ("class:warning", message + "\n"),
            ]
        )
        return fragments

    @keys.add("up")
    def move_up(event: KeyPressEvent) -> None:
        nonlocal focused, message
        focused = (focused - 1) % len(items)
        message = ""

    @keys.add("down")
    def move_down(event: KeyPressEvent) -> None:
        nonlocal focused, message
        focused = (focused + 1) % len(items)
        message = ""

    @keys.add("enter")
    def confirm(event: KeyPressEvent) -> None:
        nonlocal message
        item = items[focused]
        if item.blocked_reason:
            message = f"无法确认：{item.blocked_reason}。请选择其它项或按 Esc 取消。"
            return
        event.app.exit(result=item)

    def bind_shortcut(shortcut: MenuShortcut) -> None:
        @keys.add(shortcut.key)
        def invoke(event: KeyPressEvent) -> None:
            # 空间不足等限制只针对回车操作，不妨碍移除已占空间的资源。
            event.app.exit(result=replace(items[focused], action=shortcut.action))

    for shortcut in shortcuts:
        bind_shortcut(shortcut)

    @keys.add("escape")
    @keys.add("c-c")
    def cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=None)

    return Application(
        layout=Layout(Window(FormattedTextControl(render), always_hide_cursor=True)),
        key_bindings=keys,
        style=Style.from_dict(
            {
                "title": "bold ansicyan",
                "focused": "reverse bold",
                "hint": "ansibrightblack",
                "warning": "ansiyellow",
            }
        ),
        full_screen=False,
        erase_when_done=True,
        input=input,
        output=output,
    )
