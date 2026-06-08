
<div align="center"><h1>WutheringWaves CLI Manager</h1><h3>鸣潮命令行管理器</h3><div align="center">
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python 3.9+"></a>&nbsp;<a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/badge/Tool-uv-purple.svg" alt="uv"></a>&nbsp;<img src="https://img.shields.io/badge/Version-2.2-brightgreen.svg" alt="version 2.2">
</div>
</div>

<div align="center" style="width: 80%;">
    <div style="display: flex; justify-content: space-between; align-items: flex-start; width: 80%;">
        <img src="https://cdn.jsdelivr.net/gh/timetetng/Pic-Bed@main/ww-example1.png" alt="徽章" style="width: 40%; height: auto;">
        <img src="https://cdn.jsdelivr.net/gh/timetetng/Pic-Bed@main/ww-example2.png" alt="命令行预览" style="width: 40%; height: auto;">
    </div>
</div>

专为 Linux 用户打造的《鸣潮》客户端命令行管理工具。
结合了 **完整的下载/校验功能** 与 **秒级服务器切换** 技术。一旦完成“烘焙”，即可在官服 (CN)、B服 (Bilibili) 之间瞬时切换，无需重新下载庞大的游戏文件。

> **国际服因为包体差异不支持快速切换**。
>
> 最新版本经过测试，兼容 Windows11。

## ✨ 核心功能

* **🚀 秒级切换 (`checkout`)**：利用 MD5 快速校验，仅替换差异文件，在不同服务器间瞬间切换。
* **🛠️ 智能修复 (`sync`)**：校验全量文件 MD5，自动修复损坏文件，下载缺失资源。
* **📦 完整下载 (`download`)**：从零开始下载任一服务器的纯净客户端。
* **🔥 增量更新(`incremental`)**: 测试中，基于官方启动器解包的补丁二进制(见release），欢迎测试并提交问题。
* **💾 自动记忆**：自动记录游戏路径，一次设置，永久生效。
* **⚡️ 现代化 CLI**：基于 `Typer` 构建，支持自动补全和帮助信息。
* **👯 并行下载**: 使用多线程并行下载，避免 CDN 节点降速，支持断点续传。


## 🔧 安装指南

`dev` 分支仅用于开发测试，未提交到 AUR 以及 PyPI，请通过源码构建：

```bash
# 直接拉取 dev 分支仓库，已有仓库也可以直接 git checkout dev
git clone -b dev --single-branch https://github.com/timetetng/wutheringwaves-cli-manager.git wwcli-dev &&cd wwcli-dev

# 构建并安装
uv tool install . --force

# (可选)安装pre-commit
make setup
```

发现任何问题请提交 Issue, 贡献代码欢迎提交 PR.

## 📖 使用说明

首次运行时，需要指定游戏路径（只需指定一次，后续会自动记忆）：

```bash
# 创建安装目录(如果你还没有创建游戏目录)
mkdir -p "$HOME/Games/WutheringWaves"
# 初始化路径
ww -p "$HOME/Games/WutheringWaves" status
```

> `-p`、`--path` 参数用于指定安装路径。

### 常用命令

#### 1\. 查看状态 (`status`)

检查当前游戏目录属于哪个服务器，以及版本号。

```bash
ww status
```

#### 2\. 快速切换服务器 (`checkout`)

**秒级**切换服务器（仅限官/b服）。

```bash
# 切换到 Bilibili 服
ww checkout bilibili
# 切换到 官服
ww checkout cn
# 切换到 国际服（需要完整下载）
ww checkout global
```
#### 3\. 同步与修复 (`sync`)

切换服务器或下载中断导致文件缺失时使用。它会联网校验所有文件并下载更新。版本更新请 `ww incremental` 命令增量更新

```bash
ww sync
```

#### 4\. 下载完整客户端 (`download`)

使用此命令从零开始下载。

```bash
# 下载完整的官服客户端到配置的目录
ww download cn
```

#### 5\. 增量更新 (`incremental`)

（**开发测试中**）在新版本预下载开放期间提前下载增量包，维护后应用。也可用于新版本直接增量更新，代替原先 `ww sync` 命令：

```bash
# 下载增量包
ww incremental

# 应用增量更新
ww incremental --apply
```

> [!CAUTION]
> 1. **新版本预下载开放之后才可以下载**；
> 2. **如果应用中断，可重试 `ww incremental --apply`**；
> 3. **应用后若有缺失文件，运行 `ww sync` 修复文件**；
> 4. **请确保安装目录有足够磁盘空间（需要两倍的空间复制源文件用于合并补丁）**


#### 6\. 获取抽卡记录链接 (`log`)

可以一键获取抽卡链接，用于导入小程序或者鸣潮机器人。

```bash
# 使用前需点开 唤取-唤取记录 以更新日志文件
ww log
```

> `-o`、`--open`: 获取链接并跳转打开。

#### 7\. 更新工具本身（`update`）
自动检查安装方式并尝试更新

```bash
ww upadate
```

## 🎮 启动游戏 (Linux)

本工具仅负责文件管理。启动游戏推荐使用 Steam + Proton。

如果你熟悉 Linux，你可以参考项目中的 `run_ww.sh` 脚本并添加 .desktop 文件绕过 Steam 启动，或者按照以下步骤通过 Steam 启动：

1. **Steam 设置**：

      * 添加“非 Steam 游戏”，指向 `安装目录/Client/Binaries/Win64/Client-Win64-Shipping.exe`。
      * 在兼容性中强制使用 `GE-Proton` 或 `dwproton` (如果 ACE 警告频繁请尝试切换 proton)

2. **启动参数**：
    在 Steam 启动选项中添加：

    ```bash
    steamdeck=1
    ```

## 🛠️ 常见问题 (FAQ)

> [!TIP]
> 由于 Linux 用户环境各异，以下解决方案均来自社区，仅供参考。

---

### Q1: 被 ACE 反作弊警告下线如何解决？
* **推荐方案**：使用 `dwproton` 或添加环境变量 `steamdeck=1`。
* **效果说明**：能有效减少被检测次数，但无法完全杜绝。
* **实验性方案**：社区提到使用 *卡拉彼丘* 的 ACE 组件替换原文件，此方法**未验证**，感兴趣可自行研究。

---

### Q2: 上线 10 分钟就被踢下线，需要重新登录？
这通常是启动环境识别问题。若更换启动方式无效，**官服用户**可尝试 bilibili 用户 `@神麤詭末` 提供的方案：

1.  **定位文件**：`安装目录/Client/Binaries/Win64/ThirdParty/KrPcSdk_Mainland/KRSDKRes/KRSDKConfig.json`
2.  **修改字段**：将 `KR_ChannelId` 从官服的 `19` 修改为 Steam 的 `205`。
3.  **启动提示**：启动时若弹出“网络错误”，直接点击 **重试** 即可。

> [!CAUTION]
> 1. **修改前请务必备份原文件**，以免出错无法修复！
> 2. 此方法 **Bilibili 服无法使用**。

---

### Q3: 出现剧情文本错误或素材缺失？
1.  **执行同步**：优先运行 `ww sync` 命令。
2.  **环境排查**：若同步后仍有问题，通常不在游戏本体，建议尝试更换 **启动方式**、**桌面环境 (DE)** 或 **Wine/Proton 版本**。

---

### Q4: 登录框无法聚焦输入？
以 ArchLinux 中通过 Steam 启动的鸣潮为例，按照以下步骤操作即可:
1. `sudo pacman -S protontricks` 安装 `protontricks`；
2. 运行 `protontricks --gui`，在弹出的列表里找到鸣潮；
3. 选择 `Select the default wineprefix -> Run winecfg`;
4. 此时弹出 wincfg 窗口，在窗口中找到**显示 (Graphics)**栏，下方勾选**虚拟桌面 (Emulate a virtual desktop)** 并自行设置一个分辨率；
5. 应用退出后重启游戏，此时应该可以正常输入并登录；
6. 登录完成后，重新运行 `protontricks` 取消虚拟桌面即可。

---

### 📢 反馈
任何问题请在 **Issue** 中提出，也欢迎 **PR**。

## 致谢

感谢以下优秀项目：

* [WutheringWavesTool](https://github.com/leck995/WutheringWavesTool)
* [LutheringLaves](https://github.com/last-live/LutheringLaves)
* [Wuthering-Waves-Official-Bilibili](https://github.com/Hurry1027/Wuthering-Waves-Official-Bilibili)
* [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom/releases/): 下载最新版本的 `GE-Proton` 并解压到 `~/.local/share/Steam/steamapps/common/` 目录下，启动 Steam，在**属性-兼容性**中能找到下载的 `GE-Proton` 即说明配置成功。
* [dwproton](https://dawn.wine/dawn-winery/dwproton/releases): 安装方式同上

<!-- end list -->
