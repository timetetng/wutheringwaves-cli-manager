#!/bin/bash

# Proton 路径(实测终末地需要dwproton)
PROTON_BIN="$HOME/.local/share/Steam/compatibilitytools.d/dwproton-10.0-14/proton"

# 游戏路径
GAME_EXE="/home/xingjian/.local/share/Steam/steamapps/compatdata/4046059116/pfx/drive_c/Program Files/Hypergryph Launcher/games/Endfield Game/Endfield.exe"

# steam 游戏目录路径
export STEAM_COMPAT_DATA_PATH="$HOME/.local/share/Steam/steamapps/compatdata/4046059116"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"

# 环境变量
export STEAMDESK=1
# 用于帧率显示(sudo pacman -S mangohud 安装)
#export MANGOHUD=1

# 定义 Steam 内部自带的 Runtime 脚本路径
STEAM_RUNTIME_SCRIPT="$HOME/.local/share/Steam/ubuntu12_32/steam-runtime/run.sh"

echo "正在启动终末地 (使用 Steam Internal Runtime)..."

# 检查脚本是否存在，存在则使用，不存在则裸奔
if [ -f "$STEAM_RUNTIME_SCRIPT" ]; then
  "$STEAM_RUNTIME_SCRIPT" gamemoderun "$PROTON_BIN" run "$GAME_EXE"
else
  echo "警告：未找到 Steam Runtime，过场动画可能黑屏"
  gamemoderun "$PROTON_BIN" run "$GAME_EXE"
fi
