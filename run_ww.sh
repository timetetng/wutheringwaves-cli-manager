#!/bin/bash

# Proton 路径
PROTON_BIN="$HOME/.local/share/Steam/compatibilitytools.d/GE-Proton10-25/proton"

# 游戏路径
GAME_EXE="$HOME/share/WutheringWaves/Wuthering Waves bilibili/Wuthering Waves Game/Client/Binaries/Win64/Client-Win64-Shipping.exe"

# 兼容层路径以及steam游戏目录
export STEAM_COMPAT_DATA_PATH="$HOME/.local/share/Steam/steamapps/compatdata/3139039821"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"

# 环境变量
export steamdeck=1
export MANGOHUD=1
# === 关键修改 ===
# 定义 Steam 内部自带的 Runtime 脚本路径
STEAM_RUNTIME_SCRIPT="$HOME/.local/share/Steam/ubuntu12_32/steam-runtime/run.sh"

echo "正在启动鸣潮 (使用 Steam Internal Runtime)..."

# 检查脚本是否存在，存在则使用，不存在则裸奔
if [ -f "$STEAM_RUNTIME_SCRIPT" ]; then
  "$STEAM_RUNTIME_SCRIPT" gamemoderun "$PROTON_BIN" run "$GAME_EXE"
else
  echo "警告：未找到 Steam Runtime，过场动画可能黑屏"
  gamemoderun "$PROTON_BIN" run "$GAME_EXE"
fi
