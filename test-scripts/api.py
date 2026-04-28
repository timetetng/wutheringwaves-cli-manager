import gzip
import json
from urllib.request import Request, urlopen


def dump_launcher_api():
    """
    获取并保存完整的启动器 API 响应，用于分析增量更新字段。
    """
    api_url = "https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/G152/10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/index.json"

    print(f"正在请求 API: {api_url}")
    # 显式声明接受 gzip 编码
    req = Request(api_url, headers={"User-Agent": "WW-Manager/2.0", "Accept-Encoding": "gzip"})

    try:
        with urlopen(req, timeout=10) as rsp:
            raw_data = rsp.read()

            # 判断是否被 gzip 压缩 (通过 header 或文件头魔数 \x1f\x8b)
            if rsp.info().get("Content-Encoding") == "gzip" or raw_data[:2] == b"\x1f\x8b":
                print("检测到 gzip 压缩，正在解压...")
                raw_data = gzip.decompress(raw_data)

            data = json.loads(raw_data.decode("utf-8"))

            output_file = "api_dump.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            print(f"成功！API 数据已保存到 {output_file}")
            print("提示: 请在输出文件中搜索 'diff', 'patch' 或当前版本号。")

    except Exception as e:
        print(f"请求失败: {e}")


if __name__ == "__main__":
    dump_launcher_api()
