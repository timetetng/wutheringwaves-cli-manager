import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

import typer
from prompt_toolkit.application import Application
from rich.console import Console
from rich.table import Table
from typing_extensions import Annotated

from ww_manager.config import APPID_TO_SERVER, load_app_config, save_app_config
from ww_manager.core import WGameManager, WWError, decrypt_client_log, is_log_encrypted
from ww_manager.menu import MenuOption, MenuShortcut, build_selection_menu

__version__ = metadata.version("ww-manager")

TIER_NAMES = {"sd": "流畅", "hd": "高清", "uhd": "极致"}
UNTESTED_HELP = "允许国际服复用国服画质清单（未验证，不保证兼容）"


def parse_version(version_str: str) -> tuple:
    """版本号"""
    return tuple(map(int, re.findall(r"\d+", str(version_str))))


def check_pypi_version_silent():
    """后台线程执行：检测 PyPI 版本并保存到配置"""
    try:
        url = "https://pypi.org/pypi/ww-manager/json"
        req = Request(url, headers={"User-Agent": "WW-Manager-Update-Checker"})
        with urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            latest_version = data["info"]["version"]

            latest_v = parse_version(latest_version)
            current_v = parse_version(__version__)

            config = load_app_config()
            if latest_v > current_v:
                config["latest_available_version"] = latest_version
                save_app_config(config)
            else:
                if "latest_available_version" in config:
                    del config["latest_available_version"]
                    save_app_config(config)
    except Exception:
        pass


def get_help_text_with_version():
    """生成帮助文本"""
    help_text = f"WutheringWaves CLI Manager (v{__version__})"
    config = load_app_config()
    latest = config.get("latest_available_version")
    if latest:
        try:
            if parse_version(latest) > parse_version(__version__):
                help_text += f" [bold yellow](最新版本 {latest} 可用！)[/bold yellow]"
        except Exception:
            pass
    return help_text


# --- 初始化 Typer ---

app = typer.Typer(
    help=get_help_text_with_version(),
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
)
logger = logging.getLogger("WW_Manager")


class ServerType(str, Enum):
    cn = "cn"
    global_server = "global"
    bilibili = "bilibili"


def version_callback(value: bool):
    """处理 --version 参数"""
    if value:
        typer.echo(f"ww-manager version: {__version__}")
        raise typer.Exit()


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler()],
    )


def get_game_path(ctx: typer.Context) -> Path:
    p = ctx.obj.get("game_path")
    if not p:
        typer.secho("错误: 未设置游戏路径。请使用 --path 指定。", fg="red")
        raise typer.Exit(1)
    if not p.exists():
        if ctx.invoked_subcommand != "download" and ctx.info_name != "download":
            typer.secho(f"错误: 路径不存在 {p}", fg="red")
            raise typer.Exit(1)
    return p


# --- Commands ---


def _auto_install_completion() -> None:
    """首次运行自动安装当前 shell 的补全脚本, 幂等; 设 _WW_NO_AUTO_COMPLETION=1 禁用"""
    if os.environ.get("_WW_NO_AUTO_COMPLETION"):
        return
    shell = Path(os.environ.get("SHELL", "")).name
    if shell not in {"bash", "zsh", "fish"}:
        return
    dest = {
        "bash": Path.home() / ".bash_completions" / "ww.sh",
        "zsh": Path.home() / ".zfunc" / "_ww",
        "fish": Path.home() / ".config" / "fish" / "completions" / "ww.fish",
    }[shell]
    if dest.exists():
        return
    try:
        from typer._completion_shared import install

        os.environ["_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION"] = "1"
        install(shell=shell, prog_name="ww", complete_var="_WW_COMPLETE")
        if sys.stdin.isatty():
            typer.echo(f"[ww] 已自动安装 {shell} 补全脚本, 重启终端生效")
    except Exception:
        pass


@app.callback()
def main(
    ctx: typer.Context,
    path: Annotated[Optional[Path], typer.Option("--path", "-p", help="游戏安装目录")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="显示详细调试日志")] = False,
    version: Annotated[
        Optional[bool], typer.Option("--version", callback=version_callback, is_eager=True, help="显示工具版本号")
    ] = None,
):
    """
    鸣潮 (Wuthering Waves) CLI 管理器
    """
    _auto_install_completion()
    setup_logging(verbose)
    config = load_app_config()
    _ = version
    last_check_version = config.get("latest_available_version")

    try:
        should_check = not last_check_version or parse_version(last_check_version) <= parse_version(__version__)
    except Exception:
        should_check = True

    if should_check:
        threading.Thread(target=check_pypi_version_silent, daemon=True).start()

    default_path = config.get("default_path")
    final_path = path if path else (Path(default_path) if default_path else None)

    if path and str(path.resolve()) != default_path:
        config["default_path"] = str(path.resolve())
        save_app_config(config)
        logger.info(f"默认路径已更新为: {path.resolve()}")

    ctx.ensure_object(dict)
    ctx.obj["game_path"] = final_path


def _is_ww_manager_repo(path: Path) -> bool:
    """检查目录是否为 ww-manager 源码仓库，避免在其它 git 仓库误拉代码"""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "wutheringwaves-cli-manager" in result.stdout
    except Exception:
        return False


@app.command()
def update():
    """更新工具"""
    typer.echo("开始检测更新环境...")
    current_dir = Path.cwd()
    script_phys_dir = Path(__file__).resolve().parent.parent

    repo_path = None
    if (current_dir / ".git").exists() and _is_ww_manager_repo(current_dir):
        repo_path = current_dir
    elif (script_phys_dir / ".git").exists() and _is_ww_manager_repo(script_phys_dir):
        repo_path = script_phys_dir

    # 1. 源码
    if repo_path:
        typer.secho(f"检测到源码仓库: {repo_path}", fg="cyan")
        try:
            typer.echo("正在拉取最新代码...")
            subprocess.run(["git", "-C", str(repo_path), "pull"], check=True)
            typer.echo("正在通过 uv 重新安装...")
            subprocess.run(["uv", "tool", "install", str(repo_path), "--force"], check=True)
            typer.secho("源码更新成功！", fg="green")
            return
        except subprocess.CalledProcessError as e:
            typer.secho(f"源码更新失败: {e}", fg="red")
            return

    # 2. AUR
    if shutil.which("pacman"):
        check_aur = subprocess.run(["pacman", "-Qq", "ww-manager"], capture_output=True, text=True)
        if check_aur.returncode == 0:
            typer.secho("检测到通过 AUR 安装，尝试刷新索引并更新...", fg="cyan")
            if shutil.which("yay"):
                try:
                    # 使用 -Sy 强制刷新索引
                    subprocess.run(["yay", "-Sy", "ww-manager", "--noconfirm", "--needed"], check=True)
                    typer.secho("AUR 更新指令执行完成。", fg="green")
                    return
                except subprocess.CalledProcessError as e:
                    typer.secho(f"yay 更新失败: {e}", fg="red")
                    return
            else:
                typer.secho("未找到 yay，请手动执行 'yay -Syu ww-manager' 更新。", fg="yellow")
                return

    # 3. PyPI
    if shutil.which("uv"):
        typer.echo("正在通过 uv 检查更新...")
        try:
            subprocess.run(["uv", "tool", "upgrade", "ww-manager"], check=True)
            typer.secho("uv 更新指令已执行。", fg="green")
        except subprocess.CalledProcessError as e:
            typer.secho(f"uv 更新失败: {e}。\n请手动执行 'uv tool upgrade ww-manager'", fg="red")
    else:
        typer.secho("无法识别安装环境，请手动执行更新。", fg="yellow")


@app.command()
def status(ctx: typer.Context):
    """查看状态"""
    path = get_game_path(ctx)
    try:
        cfg_file = path / "launcherDownloadConfig.json"
        data = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("本地配置不是 JSON 对象")
        app_id = str(data.get("appId", ""))
        server = APPID_TO_SERVER.get(app_id, "cn")
        tier = WGameManager(path, server)._read_resource_state()["active_tier"]
        table = Table.grid(padding=(0, 2))
        table.add_column(style="cyan", no_wrap=True)
        table.add_column(overflow="fold")
        table.add_row("目录", str(path))
        table.add_row("服务器", APPID_TO_SERVER.get(app_id, "未知"))
        table.add_row("游戏版本", str(data.get("version", "未知")))
        table.add_row("当前默认档位", f"{tier.upper()} · {TIER_NAMES[tier]}" if tier else "未选择")
        tool_version = __version__
        latest = load_app_config().get("latest_available_version")
        if isinstance(latest, str) and parse_version(latest) > parse_version(__version__):
            tool_version += f" → {latest}（可更新：ww update）"
        table.add_row("工具版本", tool_version)
        Console().print(table, markup=False)
    except (WWError, OSError, ValueError) as e:
        typer.secho(f"读取状态失败: {e}", fg="red")
        raise typer.Exit(1)


@app.command()
def sync(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option("--force", help="仅绕过下载空间检查，不跳过文件校验")] = False,
    check_new: Annotated[bool, typer.Option("--new", help="校验并修复新版本文件状态（用于增量更新应用后）")] = False,
    allow_untested: Annotated[bool, typer.Option("--allow-untested", help=UNTESTED_HELP)] = False,
):
    """校验并修复文件

    按当前默认档位校验公共资源与画质包，修复缺失或损坏的文件。
    """
    path = get_game_path(ctx)
    cfg_file = path / "launcherDownloadConfig.json"
    server = "cn"
    if cfg_file.exists():
        try:
            d = json.loads(cfg_file.read_text(encoding="utf-8"))
            server = APPID_TO_SERVER.get(d.get("appId"), "cn")
        except Exception:
            pass
    try:
        mgr = WGameManager(path, server)
        _configure_untested(mgr, allow_untested)
        if check_new:
            success = mgr.verify_new_version(force_space=force)
            if not success:
                typer.secho("校验发现问题，请根据提示处理", fg="yellow")
                raise typer.Exit(1)
        else:
            mgr.sync_files(force_check_md5=True, force_space=force)
        mgr.remind_resource_tiers()
    except WWError as e:
        typer.secho(f"执行失败: {e}", fg="red")
        raise typer.Exit(1)


def _configure_untested(manager: WGameManager, allowed: bool) -> None:
    manager.allow_untested = allowed
    if allowed and manager.server_type == "global":
        logger.warning("国际服将复用国服画质清单（未验证）；MD5 仅确认文件与清单一致，不代表游戏兼容")


def _run_menu(application: Application[Optional[MenuOption]]) -> Optional[MenuOption]:
    try:
        return application.run()
    except (EOFError, KeyboardInterrupt):
        return None


def _local_server(path: Path) -> Optional[str]:
    try:
        data = json.loads((path / "launcherDownloadConfig.json").read_text(encoding="utf-8"))
        return APPID_TO_SERVER.get(str(data.get("appId", ""))) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _select_server(path: Path, *, title: str) -> Optional[ServerType]:
    names = {"cn": "官服", "bilibili": "哔哩哔哩服", "global": "国际服"}
    selected = _run_menu(
        build_selection_menu(
            [MenuOption(key, f"{key} · {name}") for key, name in names.items()],
            _local_server(path),
            title=title,
            current_label="当前服务器",
        )
    )
    return ServerType(selected.key) if selected is not None else None


def _confirm_resource_removal(manager: WGameManager, tier: str) -> bool:
    directory = manager.game_folder / "Client" / "Content" / tier.upper()
    try:
        return typer.confirm(
            f"确定删除 {tier.upper()} · {TIER_NAMES[tier]} 档位吗？\n目录：{directory}\n"
            "将删除该目录内所有文件（包括自行放置的文件）、该档位缓存及安装登记。\n"
            "若为当前默认档位，也将清除默认选择；其它档位与公共资源保留。",
            default=False,
        )
    except (typer.Abort, EOFError, KeyboardInterrupt):
        return False


@app.command()
def download(
    ctx: typer.Context,
    server: Annotated[Optional[ServerType], typer.Argument(help="目标服务器；省略进入服务器与档位菜单")] = None,
    tier: Annotated[str, typer.Argument(help="画质档位: hd / sd / uhd，默认 HD")] = "hd",
    allow_untested: Annotated[bool, typer.Option("--allow-untested", help=UNTESTED_HELP)] = False,
    force: Annotated[bool, typer.Option("--force", help="仅绕过磁盘空间不足检查，不跳过文件校验")] = False,
):
    """下载客户端

    不带参数时，依次选择服务器与画质档位；也可直接指定，如 ww download bilibili HD。
    成功后记住默认档位。游戏启动参数需自行设置。
    """
    path = get_game_path(ctx)
    try:
        if server is None:
            if not _interactive_terminal():
                raise WWError("非交互环境请指定服务器和档位，例如 ww download bilibili HD")
            server = _select_server(path, title="下载客户端 · 1/2 选择服务器")
            if server is None:
                return
            choice = _run_menu(
                build_selection_menu(
                    [
                        MenuOption(
                            key,
                            f"{key.upper()} · {name}",
                            description="未验证 · 国服清单"
                            if server == ServerType.global_server and key != "hd"
                            else "",
                            description_style="fg:ansiyellow"
                            if server == ServerType.global_server and key != "hd"
                            else "",
                            blocked_reason="国际服其他档位未验证；如接受复用国服清单的风险，请加 --allow-untested"
                            if server == ServerType.global_server and key != "hd" and not allow_untested
                            else None,
                        )
                        for key, name in TIER_NAMES.items()
                    ],
                    "hd",
                    title=f"下载客户端 · 2/2 选择资源档位 · {server.value}",
                    current_label="默认下载",
                )
            )
            if choice is None:
                return
            tier = choice.key
        mgr = WGameManager(path, server.value)
        _configure_untested(mgr, allow_untested)
        mgr.download_full(tier=tier, force_space=force)
        mgr.remind_resource_tiers()
    except (WWError, OSError) as e:
        typer.secho(f"下载失败: {e}", fg="red")
        raise typer.Exit(1)


def run_resource_menu(manager: WGameManager, *, force_space: bool = False) -> None:
    """展示档位；删除动作经额外确认后执行并刷新菜单。"""
    focused_key = None
    while True:
        rows = manager.resource_menu_data()
        free = manager.available_disk_space()
        options = []
        for row in rows:
            tier = row["tier"]
            if row["error"]:
                description = "× 未验证 · 国服清单" if manager.server_type == "global" else "× 暂不可用"
                details = "大小未知（无法获取有效清单）"
                blocked = row["error"]
            else:
                version = row["prepared"]["version"]
                description = f"✓ 已安装 · v{version}" if row["installed"] else f"× {row['state']} · 目标 v{version}"
                if row["state"] == "已安装" and not row["installed"]:
                    description = f"× 需更新 · v{row['version']} → v{version}"
                details = (
                    f"完整包 {row['total_size'] / 1024**3:.2f} GiB · 本次下载 {row['download_size'] / 1024**3:.2f} GiB"
                )
                blocked = None if force_space else manager.space_shortage(row["download_size"], free)
            if manager.server_type == "global" and "未验证" not in description:
                description += " · 未验证（国服清单）"
            options.append(
                MenuOption(
                    key=tier,
                    label=f"{tier.upper()} · {TIER_NAMES[tier]}",
                    description=description,
                    details=details,
                    blocked_reason=blocked,
                    description_style="fg:ansigreen bold" if row["installed"] else "fg:ansired bold",
                )
            )
        space = f"{free / 1024**3:.2f} GiB" if free is not None else "未知（无法检查）"
        subtitle = f"可用空间 {space} · 不计解压与预留空间"
        if force_space:
            subtitle += " · --force 已绕过空间拦截"
        current = manager._read_resource_state()["active_tier"]
        selected = _run_menu(
            build_selection_menu(
                options,
                current,
                title="画质档位",
                subtitle=subtitle,
                initial_key=focused_key,
                shortcuts=[MenuShortcut("d", "删除高亮档位", "remove")],
            )
        )
        if selected is None:
            return
        if selected.action == "remove":
            focused_key = selected.key
            if _confirm_resource_removal(manager, selected.key):
                manager.remove_resource_tier(selected.key)
            continue
        row = next(row for row in rows if row["tier"] == selected.key)
        manager.select_resource_tier(selected.key, row["prepared"], force_space=force_space)
        return


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


@app.command()
def resource(
    ctx: typer.Context,
    tier: Annotated[
        Optional[str], typer.Argument(help="资源档位: sd 流畅 / hd 高清 / uhd 极致；省略打开选择菜单")
    ] = None,
    config_url: Annotated[
        Optional[str],
        typer.Option("--config-url", help="覆盖自动获取的官方画质配置地址（通常无需指定）"),
    ] = None,
    allow_untested: Annotated[bool, typer.Option("--allow-untested", help=UNTESTED_HELP)] = False,
    force_md5: Annotated[bool, typer.Option("--force-md5", help="忽略 MD5 缓存，重新计算所有文件哈希")] = False,
    remove: Annotated[bool, typer.Option("--remove", help="确认后删除该档位目录、缓存和安装登记")] = False,
    force: Annotated[bool, typer.Option("--force", help="仅绕过磁盘空间不足检查，不跳过文件校验")] = False,
):
    """下载画质资源包

    不带档位时进入菜单；非交互终端仅展示本地安装记录。
    菜单中已安装且版本匹配的档位直接选定，不重复校验；按 d 可删除高亮档位。
    指定档位后校验并修复，游戏需以 -krqlv=<tier> 启动。
    --remove 或菜单删除均需确认，会删除档位目录内全部文件、缓存与安装记录。
    """
    path = get_game_path(ctx)
    cfg_file = path / "launcherDownloadConfig.json"
    server = "cn"
    if cfg_file.exists():
        try:
            d = json.loads(cfg_file.read_text(encoding="utf-8"))
            server = APPID_TO_SERVER.get(d.get("appId"), "cn")
        except Exception:
            pass
    try:
        mgr = WGameManager(path, server)
        _configure_untested(mgr, allow_untested)
        if tier is None:
            if remove or force_md5 or config_url is not None:
                raise WWError("使用操作选项时须指定档位，例如 ww resource hd --force-md5")
            if _interactive_terminal():
                run_resource_menu(mgr, force_space=force)
                return
            table = Table(box=None, padding=(0, 2))
            for heading in ("档位", "安装情况", "版本", "默认"):
                table.add_column(heading)
            for state in mgr.resource_tier_status():
                name = state["tier"]
                table.add_row(
                    f"{name.upper()} · {TIER_NAMES[name]}" + (" · 国服清单未验证" if server == "global" else ""),
                    state["state"],
                    state["version"] or "—",
                    "●" if state["active"] else "",
                )
            console = Console()
            console.print(table, markup=False)
            console.print("本地安装记录；校验并修复：ww resource <tier>", style="dim", markup=False)
        elif remove:
            tier = mgr._validate_tier(tier)
            if not _confirm_resource_removal(mgr, tier):
                raise typer.Abort()
            mgr.remove_resource_tier(tier)
        else:
            mgr.download_resource_tier(
                tier,
                config_url=config_url,
                force_check_md5=force_md5,
                force_space=force,
            )
    except (WWError, OSError) as e:
        typer.secho(f"执行失败: {e}", fg="red")
        raise typer.Exit(1)


@app.command()
def checkout(
    ctx: typer.Context,
    server: Annotated[Optional[ServerType], typer.Argument(help="目标服务器；省略进入选择菜单")] = None,
    force_sync: Annotated[bool, typer.Option("--force-sync", help="切换后强制同步")] = False,
    force: Annotated[bool, typer.Option("--force", help="仅绕过下载空间检查，不跳过文件校验")] = False,
):
    """切换服务器

    不带参数时选择目标服务器；也可直接指定，如 ww checkout bilibili。
    官服与 B 服优先复用差异缓存，国际服使用全量校验同步。
    """
    path = get_game_path(ctx)
    try:
        if server is None:
            if not _interactive_terminal():
                raise WWError("非交互环境请指定服务器，例如 ww checkout bilibili")
            server = _select_server(path, title="切换服务器")
            if server is None:
                return
        mgr = WGameManager(path, server.value)
        mgr.checkout(server.value, force_sync=force_sync, force_space=force)
        mgr.remind_resource_tiers()
    except WWError as e:
        typer.secho(f"切换失败: {e}", fg="red")
        raise typer.Exit(1)


def _run_incremental(
    ctx: typer.Context,
    action: Optional[str],
    apply_flag: bool,
    config_url: Optional[str] = None,
    force_space: bool = False,
):
    """增量更新核心逻辑（下载增量包 / 应用增量更新）。

    predownload 与 incremental 两个命令共用此实现：
    通过预设通道/新版本通道下载增量包，并在维护后应用合并补丁。
    """
    path = get_game_path(ctx)
    cfg_file = path / "launcherDownloadConfig.json"
    server = "cn"
    if cfg_file.exists():
        try:
            d = json.loads(cfg_file.read_text(encoding="utf-8"))
            server = APPID_TO_SERVER.get(d.get("appId"), "cn")
        except Exception:
            pass

    try:
        mgr = WGameManager(path, server)
        is_apply = (action == "apply") or apply_flag

        if action and action != "apply":
            typer.secho(f"未知的参数: {action}。直接运行进行下载，应用更新请使用 'apply'。", fg="red")
            raise typer.Exit(1)

        if is_apply:
            typer.confirm(
                "确定要应用增量更新吗？\n这将会修改现有的游戏文件，请确保游戏目前已维护或更新完毕。", abort=True
            )
            success = mgr.apply_incremental(force_space=force_space)
            if success:
                typer.secho("增量更新应用成功！", fg="green")
            else:
                typer.secho("增量更新应用失败", fg="red")
                raise typer.Exit(1)
        else:
            success = mgr.download_incremental(config_url=config_url, force_space=force_space)
            if success:
                typer.secho("增量更新资源下载完成！之后可使用 'apply' 命令应用更新。", fg="green")
            else:
                typer.secho("增量更新资源下载失败", fg="red")
                raise typer.Exit(1)

    except WWError as e:
        typer.secho(f"增量更新操作失败: {e}", fg="red")
        raise typer.Exit(1)


@app.command()
def predownload(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option("--force", help="仅绕过下载空间检查，不跳过文件校验")] = False,
    action: Annotated[Optional[str], typer.Argument(help="输入 'apply' 以应用已下载的增量包")] = None,
    apply_flag: Annotated[bool, typer.Option("--apply", help="应用已下载的增量包")] = False,
    config_url: Annotated[
        Optional[str], typer.Option("--config-url", help="覆盖自动获取的分包配置地址（通常无需指定）")
    ] = None,
):
    """下载或应用增量更新

    直接运行下载增量包，维护后用 ww predownload --apply 应用更新。
    新版本开启后也可下载可用的增量更新。incremental 为兼容别名。
    """
    _run_incremental(ctx, action, apply_flag, config_url, force_space=force)


@app.command(hidden=True)
def incremental(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option("--force", help="仅绕过下载空间检查，不跳过文件校验")] = False,
    action: Annotated[Optional[str], typer.Argument(help="输入 'apply' 以应用已下载的增量包")] = None,
    apply_flag: Annotated[bool, typer.Option("--apply", help="应用已下载的增量包")] = False,
    config_url: Annotated[
        Optional[str], typer.Option("--config-url", help="覆盖自动获取的分包配置地址（通常无需指定）")
    ] = None,
):
    """
    增量更新（predownload 的别名）

    用法与 ww predownload 一致：下载增量包，应用时使用 'apply'。
    """
    _run_incremental(ctx, action, apply_flag, config_url, force_space=force)


@app.command()
def log(
    ctx: typer.Context,
    open_browser: Annotated[bool, typer.Option("--open", "-o", help="使用默认浏览器打开链接")] = False,
):
    """获取抽卡记录链接

    先在游戏中打开抽卡记录，再运行本命令。
    """
    path = get_game_path(ctx)
    log_file = path / "Client/Saved/Logs/Client.log"
    if not log_file.exists():
        typer.secho("未找到日志文件", fg="red")
        return

    pattern = re.compile(r'https?://[^"]+')
    found: Optional[str] = None

    try:
        data = log_file.read_bytes()
        if is_log_encrypted(data):
            data = decrypt_client_log(data)

        content = data.decode("utf-8", errors="ignore")
        for line in content.split("\n"):
            if "aki-gm-resources.aki-game.com/aki/gacha/index.html" in line:
                m = pattern.search(line)
                if m:
                    found = m.group(0)
                    break
    except Exception as e:
        logger.error(e)
        typer.secho("读取日志时出现错误", fg="red")
        return

    if found:
        typer.secho(found, fg="green")
        if open_browser:
            webbrowser.open(found)
    else:
        typer.echo("未找到链接，请先在游戏中打开抽卡记录以更新日志文件。")


if __name__ == "__main__":
    app()
