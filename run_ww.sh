#!/bin/bash
# 《鸣潮》启动示例：Proton 裸启动（最简版）
#
# 3.7 起游戏资源分档打包（HD/SD/UHD），必须带画质档位参数，否则挂载完 pak 后直接崩溃：
#   Fatal error: [File:Unknown] [Line: 54] kuro: Use launcher to start game!
# 本工具下载的是高清档资源，所以用 -krqlv=hd。
#
# 常用可选参数：-ResX=1920 -ResY=1080 -fullscreen / -dx11 / -slno（禁用 DLSS）
# 需要 gamescope、mangohud、独显 offload 等，自行在最后一行外面包一层即可。

# Proton 路径（改成自己的，GE-Proton / dwproton 均可）
PROTON_BIN="$HOME/.local/share/Steam/compatibilitytools.d/dwproton-10.0-14/proton"

# 游戏本体路径（ww 的安装目录）
GAME_EXE="$HOME/share/WutheringWaves/Client/Binaries/Win64/Client-Win64-Shipping.exe"

# Proton 前缀：用 Steam 启动过就直接沿用，否则指向自己的 compatdata 目录
export STEAM_COMPAT_DATA_PATH="$HOME/.local/share/Steam/steamapps/compatdata/3139039821"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
export SteamAppId="3139039821"
export SteamGameId="3139039821"

GAME_ARGS="-krqlv=hd"

echo "正在启动鸣潮..."

"$PROTON_BIN" run "$GAME_EXE" $GAME_ARGS
