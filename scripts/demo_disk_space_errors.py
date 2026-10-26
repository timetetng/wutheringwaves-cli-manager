#!/usr/bin/env python3
"""预览各命令的空间不足输出；不联网、不下载、不写用户配置。

运行：uv run --locked python scripts/demo_disk_space_errors.py
单项：uv run --locked python scripts/demo_disk_space_errors.py --command sync-new

真实 CLI + 真实空间检查，业务入口使用模拟管理器。所有体积均为虚构值；
仅展示命令层的错误处理，不复现校验、补丁线程和进度条的完整日志。
"""

import argparse
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple
from unittest.mock import Mock, patch

from rich.console import Console
from rich.text import Text
from typer.testing import CliRunner

from ww_manager.cli import app
from ww_manager.core import WGameManager

GIB = 1024**3
FREE_BYTES = 5 * GIB


@dataclass(frozen=True)
class Scenario:
    args: Tuple[str, ...]
    required: int
    description: str


SCENARIOS = {
    "download": Scenario(("download", "bilibili"), 80 * GIB, "首次完整安装"),
    "resource": Scenario(("resource", "UHD"), 48 * GIB, "下载画质档位"),
    "sync": Scenario(("sync",), 12 * GIB, "全量校验后修复"),
    "sync-new": Scenario(("sync", "--new"), 8 * GIB, "新版本校验后修复"),
    "checkout": Scenario(("checkout", "bilibili"), 8 * GIB, "切服差异资源"),
    "predownload": Scenario(("predownload",), 12 * GIB, "分包预下载"),
    "incremental": Scenario(("incremental",), 12 * GIB, "分包增量下载"),
    "apply": Scenario(("incremental", "--apply"), 8 * GIB, "应用阶段回退下载的命令层错误"),
}


def preview(scenario: Scenario, directory: Path) -> str:
    """模拟业务入口遇到空间不足，调用真实 CLI 输出错误。"""
    manager = Mock(spec=WGameManager)
    manager.available_disk_space.return_value = FREE_BYTES

    def reject(*args, **kwargs):
        WGameManager._check_download_space(manager, [{"size": scenario.required}])
        raise AssertionError("演示场景应当因空间不足退出")

    for method in (
        "download_full",
        "download_resource_tier",
        "sync_files",
        "verify_new_version",
        "checkout",
        "download_incremental",
        "apply_incremental",
    ):
        getattr(manager, method).side_effect = reject

    with ExitStack() as stack:
        for name in (
            "_auto_install_completion",
            "setup_logging",
            "check_pypi_version_silent",
            "save_app_config",
        ):
            stack.enter_context(patch(f"ww_manager.cli.{name}"))
        stack.enter_context(
            patch(
                "ww_manager.cli.load_app_config",
                return_value={
                    "default_path": str(directory),
                    "latest_available_version": "999.0",
                },
            )
        )
        stack.enter_context(patch("ww_manager.cli.WGameManager", return_value=manager))
        result = CliRunner().invoke(app, list(scenario.args), input="y\n", color=True)
    if result.exit_code != 1 or "空间不足" not in result.output:
        raise RuntimeError(f"演示未按预期触发空间拒绝: {result.output}") from result.exception
    return result.output


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", choices=SCENARIOS, help="仅展示指定场景；默认依次展示全部")
    parser.add_argument("--plain", action="store_true", help="关闭颜色，便于复制输出")
    args = parser.parse_args(argv)
    console = Console(color_system=None if args.plain else "auto", highlight=False)
    console.print("[模拟] 可用空间统一为 5 GiB，所有所需大小均为虚构值。")
    console.print("真实 CLI 错误处理；省略业务阶段日志。未联网、未下载，不写游戏或用户配置。\n")
    names = [args.command] if args.command else SCENARIOS
    # 只创建空临时目录满足 CLI 路径检查；不使用真实游戏路径。
    with tempfile.TemporaryDirectory(prefix="ww-space-preview-") as temporary:
        for name in names:
            scenario = SCENARIOS[name]
            console.print(f"── {scenario.description} ──", style="bold cyan")
            console.print("$ ww " + " ".join(scenario.args), style="bold")
            console.print(Text.from_ansi(preview(scenario, Path(temporary))))
            console.print("[退出码 1；未开始该次下载]\n", style="dim")
    return 0


if __name__ == "__main__":
    sys.exit(main())
