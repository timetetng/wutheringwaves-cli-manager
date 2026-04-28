import json
from typing import Any, Dict, Optional


def get_incremental_patch_info(
    api_json_data: Dict[str, Any], local_version: str, cdn_url: str = "https://pcdownload-aliyun.aki-game.com/"
) -> Optional[Dict[str, Any]]:
    """
    从官方 API 响应中提取适用于当前本地版本的增量更新清单信息。

    :param api_json_data: 解压并解析后的官方 index.json 字典数据
    :param local_version: 本地游戏当前的版本号，例如 "3.2.2"
    :param cdn_url: 选定的 CDN 基础地址
    :return: 包含增量清单 URL 及大小信息的字典，如果没有匹配的增量包则返回 None
    """
    # 优先检查 predownload，如果没有则检查 default 的更新
    for update_type in ["predownload", "default"]:
        if update_type not in api_json_data:
            continue

        update_section = api_json_data[update_type]
        if "config" not in update_section or "patchConfig" not in update_section["config"]:
            continue

        target_version = update_section["config"].get("version", "unknown")

        # 如果目标版本和本地版本一样，说明不需要更新
        if target_version == local_version:
            continue

        patch_configs = update_section["config"]["patchConfig"]

        # 在 patchConfig 数组中寻找匹配当前本地版本的增量配置
        for patch in patch_configs:
            if patch.get("version") == local_version:
                # 找到了对应的增量包！
                patch_size = patch.get("size", 0)
                index_file_path = patch.get("indexFile", "")
                base_url_path = patch.get("baseUrl", "")

                # 拼接完整的清单下载地址
                full_index_url = f"{cdn_url.rstrip('/')}/{index_file_path.lstrip('/')}"
                full_base_url = f"{cdn_url.rstrip('/')}/{base_url_path.lstrip('/')}"

                return {
                    "update_type": update_type,
                    "from_version": local_version,
                    "target_version": target_version,
                    "patch_size_bytes": patch_size,
                    "patch_size_gb": round(patch_size / (1024**3), 2),
                    "index_file_url": full_index_url,
                    "base_url": full_base_url,
                }

    # 遍历完毕未找到，说明可能跨越了太多版本，官方没有提供对应的增量包，只能全量下载
    return None


# ================= 使用示例 =================
if __name__ == "__main__":
    # 假设你已经把刚才的 JSON 存为了 api_dump.json
    try:
        with open("api_dump.json", "r", encoding="utf-8") as f:
            data = json.load(f)

        local_ver = "3.2.2"
        patch_info = get_incremental_patch_info(data, local_ver)

        if patch_info:
            print("发现增量更新可供下载！")
            print(f"更新类型: {patch_info['update_type']}")
            print(f"版本路径: {patch_info['from_version']} -> {patch_info['target_version']}")
            print(f"增量包大小: {patch_info['patch_size_gb']} GB")
            print(f"增量清单文件地址: {patch_info['index_file_url']}")
            print(f"资源基础 URL: {patch_info['base_url']}")

            print("\n下一步测试建议:")
            print("请使用 curl 或 wget 下载上述的 '增量清单文件地址'")
            print("分析这个新 JSON，它将包含那 30G 增量切片的实际下载路径。")
        else:
            print("未找到适用于本地版本的增量包，需要执行全量更新。")

    except FileNotFoundError:
        print("找不到 api_dump.json，请先确保文件存在。")
