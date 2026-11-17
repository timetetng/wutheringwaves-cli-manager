# Linux 启动与常见问题

[返回首页](../README.md)

本工具只管理客户端文件，不负责启动游戏。本页整理启动说明和社区排障经验；相关环境与方案具有时效性，仅供参考。

## 启动参数

3.7 起资源按 HD、SD、UHD 分档。启动游戏时需要指定已安装的档位：

| 档位 | 参数 |
|---|---|
| SD · 流畅 | `-krqlv=sd` |
| HD · 高清 | `-krqlv=hd` |
| UHD · 极致 | `-krqlv=uhd` |

只指定一个档位。可以用 `ww status` 查看工具当前默认档位，但工具不会自动修改 Steam 或其他启动器中的启动参数。下面是一些启动游戏的参考：

## Steam + Proton

1. 在 Steam 中添加“非 Steam 游戏”，指向：
   ```text
   安装目录/Client/Binaries/Win64/Client-Win64-Shipping.exe
   ```
2. 在游戏属性的兼容性设置中，选择合适的 Proton 版本。推荐 [dwproton](https://dawn.wine/dawn-winery/dwproton/releases)；是否适用取决于当前游戏与运行环境。
3. 在启动选项中填写对应档位参数。例如 HD：
   ```text
   # 曾经 steamdeck=1/SteamOS=1 参数可能有用，仅参考
   -krqlv=hd
   ```

也可以使用 Lutris，或直接调用 dwproton 自带 Wine（见下节）。

## 一键安装（dwproton + 独立 prefix）

不想经过 Steam 或 Lutris 时，用仓库里的一键部署环境脚本（实验性，仅在 Archlinux + niri 图形环境下测试可用）：

```bash
// 不带参数时默认读 ww-cli 的安装目录
scripts/setup-linux.sh --game-dir ~/Games/WutheringWaves
```

脚本依次完成：下载并校验 dwproton、准备 riched20 UTF-8 补丁组件（修复登录协议正文乱码）、建立独立 prefix（DXVK、vkd3d-proton、dxvk-nvapi、NVIDIA 的 NGX 运行时、ICU、字体、DLL 覆盖），初始化后逐项自检并补装缺项，最后把启动器装到 `~/.local/bin/run_ww`，并写入桌面启动器入口 `ww.desktop`（图标从游戏自带启动器里提取）。

常用开关：

| 开关 | 作用 |
|---|---|
| `-y` | 不询问，缺失的系统包直接装 |
| `--no-install` | 不装任何系统包，只提示 |
| `--check` | 只检查路径与档位，不启动游戏 |
| `--rebuild` | 忽略仓库内的预编译组件，从源码重编 riched20 |
| `--proton <版本>` | 指定 DWProton 版本（如 `11.0-17`），默认 `latest` |

安装时检查 gamemode（启动时默认启用，实测对帧率有提升）与 PE 交叉工具链（仅重编组件时需要，mingw-w64 或 clang+lld 任一即可），缺失则询问是否安装；选择跳过则不打补丁，登录协议等文本可能乱码，但游戏仍可正常启动。MangoHud 属可选项，默认不装。脚本支持断点续传，并会复用其它安装根里已下载的归档；下载失败时可设 `https_proxy` 重试，或用 `WW_WINE_SOURCE_URL`、`WW_DWPROTON_URL` 换镜像源。

安装后用 `run_ww` 启动（或通过应用启动器），默认经 gamemoderun 启动。脚本的画质档位自动读取游戏目录的 `wwm_resource_state.json`，`run_ww --check` 只检查路径和档位。日志在 `~/.local/share/ww-linux/logs/game.log`。

已知问题：`bilibili` 服在登录的 webview 框会固定弹出**网络请求失败，请重试**，但是不影响验证码登录，忽略即可。

### 安装额外的 Proton 版本
- [dwproton](https://dawn.wine/dawn-winery/dwproton/releases)
将相应发布包解压到 `~/.local/share/Steam/steamapps/common/`，重新启动 Steam 后，在游戏的兼容性设置中选择。具体安装要求以对应项目文档为准。

## 常见问题

以下为一些出现过的问题，依旧具有时效性，仅供参考。

### 启动提示 `kuro: Use launcher to start game!`

先检查启动参数是否包含有效且已安装的档位，例如 `-krqlv=hd`。

### 上线约十分钟后需要重新登录

通常由 Kuro 增强 ACE 导致，更新 dwproton 到最新版本通常能解决。如未解决，请向 dwproton 提交 issue,比如 [dwproton issue #92](https://dawn.wine/dawn-winery/dwproton/issues/92)；

或者引用 bilibili 用户 `@神麤詭末` 提供的官服方案：

1. 先备份：
   ```text
   安装目录/Client/Binaries/Win64/ThirdParty/KrPcSdk_Mainland/KRSDKRes/KRSDKConfig.json
   ```
2. 将 `KR_ChannelId` 从 `19` 修改为 `205`。
3. 若启动时弹出“网络错误”，原方案建议点击“重试”。

该方案最后验证有效时间 `2026-11-12`, 游戏版本 `3.8`。

### 剧情文本错误或素材缺失

先运行：

```bash
ww sync
```

工具会按当前默认档位校验并修复文件。可以用 `ww status` 确认当前档位。

如果文件修复后仍有问题，再排查启动方式、桌面环境或 Wine / Proton 版本。

### 登录框无法聚焦输入

提供 Arch Linux 下的临时虚拟桌面方案：

1. 安装 `protontricks`：
   ```bash
   sudo pacman -S protontricks
   ```
2. 运行 `protontricks --gui`，选择鸣潮。
3. 进入 `Select the default wineprefix → Run winecfg`。
4. 在 `Graphics` 中启用 `Emulate a virtual desktop`，设置分辨率后重启游戏。
5. 完成登录后，可以返回同一设置关闭虚拟桌面。

不同环境中的菜单名称和实际效果可能不同。

## 官方启动器

若希望通过官方启动器管理游戏，参考[在 Linux 上运行鸣潮官方启动器](wuwa-launcher.md)。

该文原链接来自 [MoYingJi QwQ](https://site.moyingji.one/linux/gaming/workarounds/wuwa-launcher)，仓库保留了归档版本。