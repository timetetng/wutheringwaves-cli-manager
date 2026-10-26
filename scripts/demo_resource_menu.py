#!/usr/bin/env python3
"""无下载、无联网、无配置写入的菜单原型。

运行：uv run --locked python scripts/demo_resource_menu.py
空间不足场景：uv run --locked python scripts/demo_resource_menu.py --low-space
"""

import argparse
import sys
from typing import Optional, Sequence

from ww_manager.menu import MenuOption, build_selection_menu

GIB = 1024**3
# 所有体积均为展示交互而虚构，不代表真实客户端下载大小。
DEMO_CHOICES = (
    ("sd", "标清", 12 * GIB, 12 * GIB),
    ("hd", "高清", 24 * GIB, 0),
    ("uhd", "极致", 48 * GIB, 48 * GIB),
)


def format_size(size: int) -> str:
    return f"{size / GIB:.1f} GiB"


def demo_options(free_bytes: int):
    return [
        MenuOption(
            key=tier,
            label=f"{tier.upper()} · {name}",
            description="✓ 已安装 · v3.7.1" if tier == "hd" else "× 未安装",
            description_style="fg:ansigreen bold" if tier == "hd" else "fg:ansired bold",
            details=f"完整包 {format_size(total)} · 预计新增 {format_size(required)}",
            blocked_reason=f"空间不足，拒绝下载 {tier.upper()}" if required > free_bytes else None,
        )
        for tier, name, total, required in DEMO_CHOICES
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="档位菜单原型；所有大小和状态均为模拟值，无真实下载")
    parser.add_argument("--low-space", action="store_true", help="模拟仅剩 5 GiB 空间（默认模拟 80 GiB）")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("请在交互终端直接运行此原型，不要通过管道或重定向运行。", file=sys.stderr)
        return 1
    free_bytes = (5 if args.low_space else 80) * GIB
    try:
        choice = build_selection_menu(
            demo_options(free_bytes), "hd",
            title="画质档位 · 交互原型（全部为模拟数据）",
            subtitle=f"可用空间 {format_size(free_bytes)}",
        ).run()
    except (EOFError, KeyboardInterrupt):
        choice = None
    if choice is None:
        print("已取消；未进行任何操作。")
    elif choice.key == "hd":
        print(f"[模拟] 已确认默认档位：{choice.label}（已安装，不重复下载/校验）")
    else:
        print(f"[模拟] 将下载并校验 {choice.label}；{choice.details}")
    print("这是交互原型：未联网、未下载、未修改游戏或配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
