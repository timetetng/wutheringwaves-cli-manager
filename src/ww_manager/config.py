# config.py
import json
import logging
import os
import platform
from pathlib import Path
from typing import Any, Dict, Optional


# --- 常量定义 ---
def get_config_dir() -> Path:
    """根据操作系统获取配置目录"""
    if platform.system() == "Windows":
        # Windows
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "ww_manager"
        return Path.home() / "AppData" / "Roaming" / "ww_manager"
    else:
        # Linux / macOS
        return Path.home() / ".config" / "ww_manager"


CONFIG_DIR = get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"

# 服务器差异文件缓存：用于官服(cn)与 b服(bilibili)之间秒级切换
# 存放另一服版本的差异文件，避免每次切换都从 CDN 全量重下。
# 缓存放于游戏目录下的子目录，集中管理（路径由 core 在游戏目录下解析）。
SERVER_DIFF_CACHE_DIR_NAME = "wwm_server_diff_cache"
SERVER_DIFF_CACHE_MANIFEST = "manifest.json"
SERVER_DIFF_CACHE_VERSION = 1  # 缓存格式版本，结构变更时递增以触发重建

# 参与差异缓存的服务器（国际服差距过大、文件集几乎全异，不适用）
DIFF_CACHE_SERVERS = ("cn", "bilibili")


def parse_major_minor(version: str) -> Optional[tuple]:
    """把版本号解析成 (major, minor)，如 "3.6.0" -> (3, 6)。

    用于判定是否发生"大版本更新"(如 3.4 -> 3.5)，此时差异文件集必然变化，
    需要重置差异缓存。解析失败返回 None。
    """
    if not version:
        return None
    parts = str(version).strip().split(".")
    nums = []
    for p in parts[:2]:
        try:
            nums.append(int(p))
        except ValueError:
            break
    if len(nums) == 2:
        return (nums[0], nums[1])
    return None


SERVER_CONFIGS = {
    "cn": {
        "api_url": "https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/G152/10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/index.json",
        "appId": "10003",
    },
    "global": {
        "api_url": "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/50004_obOHXFrFanqsaIEOmuKroCcbZkQRBC7c/index.json",
        "appId": "50004",
    },
    "bilibili": {
        "api_url": "https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/G152/10004_j5GWFuUFlb8N31Wi2uS3ZAVHcb7ZGN7y/index.json",
        "appId": "10004",
    },
}

APPID_TO_SERVER = {v["appId"]: k for k, v in SERVER_CONFIGS.items()}


# --- 配置管理 ---
def load_app_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.getLogger("WW_Manager").warning(f"无法加载配置文件 {CONFIG_FILE}: {e}")
        return {}


def save_app_config(config: Dict[str, Any]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logging.getLogger("WW_Manager").error(f"无法保存配置 {CONFIG_FILE}: {e}")
