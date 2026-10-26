<div align="center">
  <h1>WutheringWaves CLI Manager</h1>
  <h3>鸣潮命令行管理器</h3>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python 3.9+"></a>&nbsp;<a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/badge/Tool-uv-purple.svg" alt="uv"></a>&nbsp;<img src="https://img.shields.io/badge/Version-2.3.2-brightgreen.svg" alt="version 2.3.2">
</div>

<p align="center">
  <img src="docs/images/cli-preview.webp" alt="命令帮助预览" width="45%">
  <img src="docs/images/download-preview.webp" alt="下载进度预览" width="45%">
</p>

鸣潮命令行管理工具，用于下载客户端、更新和修复文件、管理画质资源包及切换服务器。

本项目只负责客户端的下载、更新和文件管理，不负责启动游戏。启动方式见下方[启动游戏](#启动游戏)。

[English](docs/README.en.md)（尚未同步近期功能）

## 安装

### uv（推荐）

安装 uv（已安装可跳过）：

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装后重新打开终端，再安装本工具：

```bash
uv tool install ww-manager
```

### AUR

```bash
yay -S ww-manager
```

### 源码安装

需要 Git 和 uv：

```bash
git clone https://github.com/timetetng/wutheringwaves-cli-manager.git
cd wutheringwaves-cli-manager
uv tool install .
```

### 更新与卸载

更新工具：

```bash
ww update
```

卸载 uv 安装的工具（包括上述源码安装方式）：

```bash
uv tool uninstall ww-manager
```

卸载 AUR 安装的工具：

```bash
yay -R ww-manager
```

## 快速上手

首次使用时指定游戏安装目录，工具会记住该路径：

```bash
ww -p "$HOME/Games/WutheringWaves" download
```

按菜单选择服务器和画质档位，确认后下载。已有游戏文件也可以使用此目录，正确文件会复用。

Windows 路径示例：

```powershell
ww -p "D:\Games\WutheringWaves" download
```

查看当前状态：

```bash
ww status
```

后续使用无需重复指定路径。需要更换目录时，再通过 `-p` 指定。

## 启动游戏

启动时需要在参数中指定已安装的画质档位：

| 档位 | 启动参数 |
|---|---|
| SD · 标清 | `-krqlv=sd` |
| HD · 高清 | `-krqlv=hd` |
| UHD · 极致 | `-krqlv=uhd` |

例如，安装 HD 后使用 `-krqlv=hd`。工具切换默认档位不会自动修改你的游戏启动配置。

Linux 启动方式和常见问题见 [Linux 启动指南](docs/linux.md)。

## 常用命令

| 命令 | 用途 |
|---|---|
| `ww download` | 下载客户端，菜单选择服务器和画质 |
| `ww resource` | 管理画质资源包 |
| `ww checkout` | 切换服务器 |
| `ww status` | 查看游戏和工具状态 |
| `ww predownload` | 下载增量更新 |
| `ww predownload --apply` | 应用已下载的增量更新 |
| `ww sync` | 校验并修复文件 |
| `ww log` | 获取抽卡记录链接 |
| `ww update` | 更新工具 |

`download`、`resource`、`checkout` 不带参数时进入菜单，也可以直接指定参数：

```bash
ww download bilibili HD
ww resource UHD
ww checkout cn
```

菜单使用 ↑↓ 选择、回车确认、Esc 取消。资源菜单中按 `d` 可以删除高亮档位，删除前需要确认。

服务器参数：`cn` 官服、`bilibili` B 服、`global` 国际服。`incremental` 是 `predownload` 的别名。

查看具体参数：

```bash
ww --help
ww resource --help
```

## 详细文档

- [Linux 启动与常见问题](docs/linux.md)
- [在 Linux 上运行官方启动器](docs/wuwa-launcher.md)

## 反馈

使用中遇到问题，可以提交 Issue，附上执行命令、工具版本和相关日志。也欢迎提交 PR.

## 致谢

- [WutheringWavesTool](https://github.com/leck995/WutheringWavesTool)
- [LutheringLaves](https://github.com/last-live/LutheringLaves)
- [wuwa-uhd-tool](https://github.com/TianXyousa/wuwa-uhd-tool)
- [Wuthering-Waves-Official-Bilibili](https://github.com/Hurry1027/Wuthering-Waves-Official-Bilibili)
- [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom)
- [dwproton](https://dawn.wine/dawn-winery/dwproton/releases)
