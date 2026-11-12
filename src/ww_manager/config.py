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
        # 3.8 起启动器 index 换成 <appId>_<appKey>/G152/<channel>/index.json，旧 10003_Y8x… 已 404
        "api_url": "https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/10003_oLNgHF1CESo51DGHN2odtp40e3oI1HfZ/G152/official/index.json",
        "api_urls": (
            "https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/10003_oLNgHF1CESo51DGHN2odtp40e3oI1HfZ/G152/official/index.json",
            "https://prod-volcdn-gamestarter.kurogame.xyz/launcher/game/10003_oLNgHF1CESo51DGHN2odtp40e3oI1HfZ/G152/official/index.json",
        ),
        "appId": "10003",
    },
    "global": {
        "api_url": "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/50004_obOHXFrFanqsaIEOmuKroCcbZkQRBC7c/index.json",
        "appId": "50004",
    },
    "bilibili": {
        # B 服自己的 appKey（2026-11-12 实测抓到）；换 token 时改这里
        "api_url": "https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/10004_IY6xmlAusbImBjWrpPj1OwNuirXEO44x/G152/bilibili/index.json",
        "api_urls": (
            "https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/10004_IY6xmlAusbImBjWrpPj1OwNuirXEO44x/G152/bilibili/index.json",
            "https://prod-volcdn-gamestarter.kurogame.xyz/launcher/game/10004_IY6xmlAusbImBjWrpPj1OwNuirXEO44x/G152/bilibili/index.json",
        ),
        "appId": "10004",
    },
}

RESOURCE_CONFIG_HOSTS = (
    "https://prod-cn-alicdn-gamestarter.kurogame.com/",
    "https://prod-volcdn-gamestarter.kurogame.xyz/",
    "https://prod-tencentcdn-gamestarter.kurogame.com/",
)

# 各渠道自己的画质/分包配置路径（官/B 服 contentId 目录不同，不能共用）
RESOURCE_CONFIG_PATHS = {
    "cn": "launcher/game/10003_oLNgHF1CESo51DGHN2odtp40e3oI1HfZ/G152/official/index.json",
    "bilibili": "launcher/game/10004_IY6xmlAusbImBjWrpPj1OwNuirXEO44x/G152/bilibili/index.json",
}


def resource_config_urls(server_type: str) -> tuple:
    """渠道配置的多 CDN 地址；未登记的国际服沿用官服入口。"""
    path = RESOURCE_CONFIG_PATHS.get(server_type) or RESOURCE_CONFIG_PATHS["cn"]
    return tuple(host + path for host in RESOURCE_CONFIG_HOSTS)


# 兼容旧引用（官服）
RESOURCE_CONFIG_URLS = resource_config_urls("cn")

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
