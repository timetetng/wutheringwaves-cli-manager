# cli.py
import json
import logging
import re
import webbrowser
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated

from config import APPID_TO_SERVER, load_app_config, save_app_config
from core import WGameManager, WWError

# --- 初始化 ---
app = typer.Typer(
    help="WutheringWaves CLI Manager",
    no_args_is_help=True,
    add_completion=False,
)
logger = logging.getLogger("WW_Manager")


class ServerType(str, Enum):
    cn = "cn"
    global_server = "global"
    bilibili = "bilibili"


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
        # 如果是 download 命令，允许路径不存在（会自动创建）
        if ctx.invoked_subcommand != "download":
            typer.secho(f"错误: 路径不存在 {p}", fg="red")
            raise typer.Exit(1)
    return p


# --- Callback & Commands ---


@app.callback()
def main(
    ctx: typer.Context,
    path: Annotated[Optional[Path], typer.Option("--path", "-p", help="游戏安装目录")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="显示详细调试日志")] = False,
    apply: Annotated[bool, typer.Option("--apply", help="应用预下载资源")] = False,
):
    """
    鸣潮 (Wuthering Waves) CLI 管理器
    """
    setup_logging(verbose)

    config = load_app_config()
    default_path = config.get("default_path")

    final_path = path if path else (Path(default_path) if default_path else None)

    if path and str(path.resolve()) != default_path:
        config["default_path"] = str(path.resolve())
        save_app_config(config)
        logger.info(f"默认路径已更新为: {path.resolve()}")

    ctx.ensure_object(dict)
    ctx.obj["game_path"] = final_path


@app.command()
def status(ctx: typer.Context):
    """查看当前客户端状态"""
    path = get_game_path(ctx)
    cfg_file = path / "launcherDownloadConfig.json"

    if not cfg_file.exists():
        typer.echo("未找到配置文件，无法确定版本。")
        return

    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        app_id = data.get("appId")
        server = APPID_TO_SERVER.get(app_id, "未知")
        typer.secho(f"目录: {path}", fg="green")
        typer.echo(f"服务器: {server}")
        typer.echo(f"版本: {data.get('version')}")
    except Exception as e:
        logger.error(f"读取状态失败: {e}")


@app.command()
def sync(ctx: typer.Context):
    """全量校验并修复文件"""
    path = get_game_path(ctx)
    # 尝试自动探测服务器
    cfg_file = path / "launcherDownloadConfig.json"
    server = "cn"  # 默认
    if cfg_file.exists():
        try:
            d = json.loads(cfg_file.read_text(encoding="utf-8"))
            server = APPID_TO_SERVER.get(d.get("appId"), "cn")
        except Exception:
            pass

    try:
        mgr = WGameManager(path, server)
        mgr.sync_files(force_check_md5=True)
    except WWError as e:
        typer.secho(f"执行失败: {e}", fg="red")
        raise typer.Exit(1)


@app.command()
def download(ctx: typer.Context, server: ServerType):
    """[初始化] 下载完整客户端"""
    path = get_game_path(ctx)
    try:
        mgr = WGameManager(path, server.value)
        mgr.download_full()
    except WWError as e:
        typer.secho(f"下载失败: {e}", fg="red")
        raise typer.Exit(1)


@app.command()
def checkout(
    ctx: typer.Context,
    server: ServerType,
    force_sync: Annotated[bool, typer.Option("--force-sync", help="切换后强制同步")] = False,
):
    """切换服务器"""
    path = get_game_path(ctx)
    try:
        mgr = WGameManager(path, server.value)
        mgr.checkout(server.value, force_sync=force_sync)
    except WWError as e:
        typer.secho(f"切换失败: {e}", fg="red")
        raise typer.Exit(1)


@app.command()
def predownload(
    ctx: typer.Context,
    action: Annotated[Optional[str], typer.Argument(help="输入 'apply' 以应用预下载资源")] = None,
    apply_flag: Annotated[bool, typer.Option("--apply", help="应用预下载资源")] = False,
):
    """预下载管理"""
    path = get_game_path(ctx)
    # 自动探测服务器
    cfg_file = path / "launcherDownloadConfig.json"
    server = "cn"
    if cfg_file.exists():
        try:
            d = json.loads(cfg_file.read_text(encoding="utf-8"))
            server = APPID_TO_SERVER.get(d.get("appId"), "cn")
        except Exception:
            pass

    # 如果有任一方式指定了 apply，则执行应用逻辑
    is_apply = (action == "apply") or apply_flag

    # 如果输入了 apply 之外的未知参数，抛出提示
    if action and action != "apply":
        typer.secho(f"未知的参数: {action}。直接运行进行预下载，应用更新请使用 'apply'。", fg="red")
        raise typer.Exit(1)

    try:
        mgr = WGameManager(path, server)

        if is_apply:
            typer.confirm(
                "确定要应用预下载资源吗？\n这将会覆盖现有的游戏文件，请确保游戏目前已维护或更新完毕（至少为版本更新当天凌晨4点之后）。",
                abort=True,
            )
            mgr.apply_predownload()
        else:
            mgr.download_predownload()

    except WWError as e:
        typer.secho(f"预下载操作失败: {e}", fg="red")
        raise typer.Exit(1)


@app.command()
def log(
    ctx: typer.Context,
    open_browser: Annotated[bool, typer.Option("--open", "-o", help="使用默认浏览器打开链接")] = False,
):
    """获取抽卡分析链接"""
    path = get_game_path(ctx)
    log_file = path / "Client/Saved/Logs/Client.log"

    if not log_file.exists():
        typer.secho("未找到日志文件", fg="red")
        return

    pattern = re.compile(r'https?://[^"]+')
    found: str | None = None

    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "aki-gm-resources.aki-game.com" in line:
                    m = pattern.search(line)
                    if m:
                        found = m.group(0)
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
