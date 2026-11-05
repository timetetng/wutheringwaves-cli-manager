# Linux 启动与常见问题

[返回首页](../README.md)

本工具只管理客户端文件，不负责启动游戏。本页整理启动说明和社区排障经验；相关环境与方案具有时效性，未必适用于所有版本。

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
2. 在游戏属性的兼容性设置中，选择合适的 Proton 版本。推荐 dwproton；是否适用取决于当前游戏与运行环境。
3. 在启动选项中填写对应档位参数。例如 HD：
   ```text
   # 曾经 steamdeck=1 参数也有用，仅参考
   -krqlv=hd
   ```

也可以使用 Lutris，或直接调用 DWProton 自带 Wine（见下节）。

## 一键安装（DWProton + 独立 prefix）

不想经过 Steam 或 Lutris 时，用仓库里的一键部署环境脚本（实验性，仅在 Archlinux + niri 图形环境下测试可用）：

```bash
// 不带参数时默认读 ww-cli 的安装目录
scripts/setup-linux.sh --game-dir ~/Games/WutheringWaves
```

脚本依次完成：下载并校验 DWProton、准备 riched20 UTF-8 补丁组件（修复登录协议正文乱码）、建立独立 prefix（DXVK、ICU、字体、DLL 覆盖），最后把启动器装到 `~/.local/bin/run_ww`，并写入桌面启动器入口 `ww.desktop`（图标从游戏自带启动器里提取）。

已存在的 DWProton（`$DWPROTON`、`~/.local/share/dwproton/`、Lutris/Steam 目录等）和已有 prefix 会被直接复用，不重复下载也不移动。

安装后用 `run_ww` 启动（或点桌面图标）。脚本的画质档位自动读取游戏目录的 `wwm_resource_state.json`，`run_ww --check` 只检查路径和档位。日志在 `~/.local/share/ww-linux/logs/game.log`。

已知问题：`bilibili` 服在登录的 webview 框会固定弹出**网络请求失败，请重试**，但是不影响验证码登录，忽略即可。

### 安装额外的 Proton 版本
- [dwproton](https://dawn.wine/dawn-winery/dwproton/releases)
- [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom/releases/)
将相应发布包解压到 `~/.local/share/Steam/steamapps/common/`，重新启动 Steam 后，在游戏的兼容性设置中选择。具体安装要求以对应项目文档为准。

## 常见问题

以下为一些出现过的问题，依旧具有时效性，仅供参考。

### 启动提示 `kuro: Use launcher to start game!`

先检查启动参数是否包含有效且已安装的档位，例如 `-krqlv=hd`。原先记录的错误为：

```text
Fatal error: [File:Unknown] [Line: 54] kuro: Use launcher to start game!
```

若参数正确但仍然报错，不应直接认定是同一原因。

### ACE 反作弊警告或上线后被踢下线

原社区经验包括更换为 dwproton，或尝试上述 `steamdeck=1` 配置。这些方法不保证解决问题。

### 上线约十分钟后需要重新登录

引用 bilibili 用户 `@神麤詭末` 提供的官服方案：

1. 先备份：
   ```text
   安装目录/Client/Binaries/Win64/ThirdParty/KrPcSdk_Mainland/KRSDKRes/KRSDKConfig.json
   ```
2. 将 `KR_ChannelId` 从 `19` 修改为 `205`。
3. 若启动时弹出“网络错误”，原方案建议点击“重试”。

该方案已经长时间未验证。

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
