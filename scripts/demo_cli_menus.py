#!/usr/bin/env python3
"""所有菜单的安全模拟：真实 CLI/键盘交互，纯内存游戏状态。

uv run --locked python scripts/demo_cli_menus.py --command resource
uv run --locked python scripts/demo_cli_menus.py --command download
uv run --locked python scripts/demo_cli_menus.py --command checkout

不联网、不下载、不删除文件、不写游戏或用户配置。体积和状态均为虚构值。
"""

import argparse
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Optional, Sequence
from unittest.mock import patch

import typer

from ww_manager.cli import app
from ww_manager.core import WWError
from ww_manager.download import format_bytes, space_shortage

GIB = 1024**3
SIZES = {"sd": 12 * GIB, "hd": 24 * GIB, "uhd": 48 * GIB}
NAMES = {"sd": "标清", "hd": "高清", "uhd": "极致"}
VERSION = "3.7.1"


class DemoManager:
    """不继承真实管理器，避免意外调用真实下载或文件操作。"""

    def __init__(self, path: Path, server: str, free_bytes: int):
        self.game_folder = path
        self.server_type = server
        self.free_bytes = free_bytes
        self.installed = {"hd"}
        self.current: Optional[str] = "hd"

    def available_disk_space(self):
        return self.free_bytes

    space_shortage = staticmethod(space_shortage)

    def _read_resource_state(self):
        return {"active_tier": self.current}

    def resource_menu_data(self):
        return [
            {
                "tier": tier, "state": "已安装" if tier in self.installed else "未安装",
                "version": VERSION if tier in self.installed else None,
                "installed": tier in self.installed, "error": None,
                "total_size": size, "download_size": 0 if tier in self.installed else size,
                "prepared": {"version": VERSION},
            }
            for tier, size in SIZES.items()
        ]

    def check_space(self, required: int, force_space: bool):
        shortage = space_shortage(required, self.free_bytes)
        if shortage and not force_space:
            raise WWError(shortage)
        if shortage:
            typer.secho("[模拟] --force 已绕过空间不足；仍未执行任何真实下载。", fg="yellow")

    def select_resource_tier(self, tier, prepared, *, force_space=False):
        if tier in self.installed:
            typer.echo(f"[模拟] 已选择 {tier.upper()} · {NAMES[tier]}，不重复下载或校验。")
        else:
            self.check_space(SIZES[tier], force_space)
            typer.echo(f"[模拟] 将下载并校验 {tier.upper()} · {NAMES[tier]}，然后设为默认档位。")
            self.installed.add(tier)
        self.current = tier

    def remove_resource_tier(self, tier):
        self.installed.discard(tier)
        if self.current == tier:
            self.current = None
        typer.echo(f"[模拟] 已移除 {tier.upper()}；仅改变内存状态，未删除磁盘文件。")

    def download_full(self, tier="hd", force_space=False):
        tier = tier.lower()
        # 演示首次完整安装：公共文件 24 GiB + 所选档位，不复用模拟已安装的 HD。
        required = 24 * GIB + SIZES[tier]
        self.check_space(required, force_space)
        typer.echo(
            f"[模拟] 将下载 {self.server_type} 服 / {tier.upper()} · {NAMES[tier]}，"
            f"公共文件与档位合计 {format_bytes(required)}。"
        )

    def checkout(self, server, *, force_sync=False, force_space=False):
        if server == "bilibili":
            typer.echo("[模拟] 当前已是 bilibili 服，无需切换。")
            return
        self.check_space(8 * GIB, force_space)
        typer.echo(f"[模拟] 将从 bilibili 切换到 {server}，差异文件 8 GiB。")

    def remind_resource_tiers(self):
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", choices=("resource", "download", "checkout"), default="resource")
    parser.add_argument("--low-space", action="store_true", help="模拟仅剩 5 GiB（默认 80 GiB）")
    parser.add_argument("--force", action="store_true", help="演示绕过空间检查；仍然不会真实下载")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("请在交互终端直接运行，不要通过管道或重定向运行。", file=sys.stderr)
        return 1
    free_bytes = (5 if args.low_space else 80) * GIB
    typer.secho("[模拟] 全部状态和体积均为虚构值；不联网、不下载、不删除文件、不写用户配置。", fg="yellow")
    initial_state = "模拟首次完整安装" if args.command == "download" else "默认已安装 HD，当前服务器 bilibili"
    typer.echo(f"模拟剩余空间 {free_bytes / GIB:.0f} GiB；{initial_state}。")
    if args.command == "resource":
        typer.echo("资源菜单可按 d 测试删除确认；确认后仅修改内存状态并刷新菜单。")
    with tempfile.TemporaryDirectory(prefix="ww-menu-demo-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        for name in (
            "_auto_install_completion", "setup_logging", "check_pypi_version_silent", "save_app_config",
        ):
            stack.enter_context(patch(f"ww_manager.cli.{name}"))
        stack.enter_context(patch("ww_manager.cli.load_app_config", return_value={
            "default_path": str(root), "latest_available_version": "999.0",
        }))
        stack.enter_context(patch(
            "ww_manager.cli._local_server", return_value=None if args.command == "download" else "bilibili",
        ))
        stack.enter_context(patch("ww_manager.cli.WGameManager", side_effect=lambda path, server: DemoManager(
            path, server, free_bytes,
        )))
        command = [args.command] + (["--force"] if args.force else [])
        try:
            result = app(args=command, prog_name="ww", standalone_mode=False)
            return result if isinstance(result, int) else 0
        except typer.Exit as error:
            return error.exit_code
        finally:
            typer.echo("[模拟结束] 没有进行真实游戏操作；再次运行会恢复初始模拟状态。")


if __name__ == "__main__":
    raise SystemExit(main())
