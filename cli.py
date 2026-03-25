import json
import logging
import re
import shutil
import subprocess
import threading
import webbrowser
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

import typer
from typing_extensions import Annotated

from config import APPID_TO_SERVER, load_app_config, save_app_config
from core import WGameManager, WWError

__version__ = metadata.version("ww-manager")


def check_pypi_version_silent():
    """后台线程执行：检测 PyPI 版本并保存到配置"""
    try:
        url = "https://pypi.org/pypi/ww-manager/json"
        req = Request(url, headers={"User-Agent": "WW-Manager-Update-Checker"})
        with urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            latest_version = data["info"]["version"]
            # 如果发现新版本，更新本地配置
            if latest_version != __version__:
                config = load_app_config()
                config["latest_available_version"] = latest_version
                save_app_config(config)
    except Exception:
        pass


def get_help_text_with_version():
    """生成帮助文本"""
    help_text = f"WutheringWaves CLI Manager (v{__version__})"
    config = load_app_config()
    latest = config.get("latest_available_version")
    if latest and latest != __version__:
        help_text += f" [bold yellow](最新版本 {latest} 可用！)[/bold yellow]"
    return help_text


# --- 初始化 Typer ---

app = typer.Typer(
    help=get_help_text_with_version(),
    no_args_is_help=True,
    add_completion=False,
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
        if ctx.invoked_subcommand != "download":
            typer.secho(f"错误: 路径不存在 {p}", fg="red")
            raise typer.Exit(1)
    return p


# --- Commands ---


@app.callback()
def main(
    ctx: typer.Context,
    path: Annotated[Optional[Path], typer.Option("--path", "-p", help="游戏安装目录")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="显示详细调试日志")] = False,
    version: Annotated[
        Optional[bool], typer.Option("--version", callback=version_callback, is_eager=True, help="显示版本号")
    ] = None,
):
    """
    鸣潮 (Wuthering Waves) CLI 管理器
    """
    setup_logging(verbose)
    config = load_app_config()
    _ = version
    last_check_version = config.get("latest_available_version")
    if not last_check_version or last_check_version == __version__:
        threading.Thread(target=check_pypi_version_silent, daemon=True).start()

    default_path = config.get("default_path")
    final_path = path if path else (Path(default_path) if default_path else None)

    if path and str(path.resolve()) != default_path:
        config["default_path"] = str(path.resolve())
        save_app_config(config)
        logger.info(f"默认路径已更新为: {path.resolve()}")

    ctx.ensure_object(dict)
    ctx.obj["game_path"] = final_path


@app.command()
def update():
    """更新 ww-manager 工具"""
    typer.echo("开始检测更新环境...")
    current_dir = Path.cwd()
    script_phys_dir = Path(__file__).resolve().parent.parent

    repo_path = None
    if (current_dir / ".git").exists():
        repo_path = current_dir
    elif (script_phys_dir / ".git").exists():
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
    cfg_file = path / "launcherDownloadConfig.json"
    server = "cn"
    if cfg_file.exists():
        try:
            d = json.loads(cfg_file.read_text(encoding="utf-8"))
            server = APPID_TO_SERVER.get(d.get("appId"), "cn")
        except Exception:
            pass
    is_apply = (action == "apply") or apply_flag
    if action and action != "apply":
        typer.secho(f"未知的参数: {action}。直接运行进行预下载，应用更新请使用 'apply'。", fg="red")
        raise typer.Exit(1)
    try:
        mgr = WGameManager(path, server)
        if is_apply:
            typer.confirm(
                "确定要应用预下载资源吗？\n这将会覆盖现有的游戏文件，请确保游戏目前已维护或更新完毕。", abort=True
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
