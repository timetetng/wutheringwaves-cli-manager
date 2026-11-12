# 游戏资源 index 地址（appKey）的更新流程

本工具把各渠道的 index 地址写死在 `src/ww_manager/config.py`（`SERVER_CONFIGS`、`RESOURCE_CONFIG_PATHS`）。
地址形如：

```
https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/<appId>_<appKey>/<gameId>/<serverCode>/index.json
```

其中 `appKey` 由 KURO 在换大版本时轮换，**不在服务端下发、也不是 HTTP 接口返回的**。
它藏在启动器安装包的 `Assets/KRApp.conf` 里（整文件 base64 → 逐字节 XOR → JSON）。

因此换 token 时只需要**下载渠道安装包并解包**。

> [!IMPORTANT]
> 旧路径（如 `launcher/game/G152/10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/index.json`）在 3.8 之后**仍然可能返回 200**，
> 但内容冻结在切换前的旧版本（实测冻结在 3.7.1 的旧 schema）。它不会报错，只会让工具"更新到旧版本"或与客户端清单对不上。
> 所以更新是否必要不能靠"能不能访问"判断，要靠下面第 6 步的版本号比对。

## 什么时候需要做这件事

- index 请求返回 404 / `NoSuchKey`；
- 工具读到的版本号明显落后于游戏实际版本（如 2026-11 B 服旧 index 停在 3.7.1）；
- 新大版本发布后清单里缺少新的 `resourcePacks` / `bundles` 项；
- 增量更新大量报"源文件 MD5 不匹配"（多半是拿了别的渠道的清单）。

## 1. 下载渠道安装包

| 渠道 | 安装包命名 |
| --- | --- |
| 国服（官服） | `WutheringWaves_official_setup_<版本>.exe` |
| B 服 | `mcPCb_bilibili_<日期>_<hash>.exe` |

安装包是 NSIS 自解压包，里面的启动器本体被包成一个 zip（`$PLUGINSDIR/KRLauncher.zip`），`KRApp.conf` 就在这个 zip 里。

## 2. 从安装包取出 `KRLauncher.zip`

```bash
mkdir -p /tmp/krcfg && cd /tmp/krcfg

# 官服
7z x -y -o. ~/Downloads/WutheringWaves_official_setup_3.1.0.1.exe '$PLUGINSDIR/KRLauncher.zip'

# B 服
7z x -y -o. ~/Downloads/mcPCb_bilibili_20261015_014428dc00ef663ccc9bdaf30bf1192507aa7e9600d3ec.exe '$PLUGINSDIR/KRLauncher.zip'
```

`$PLUGINSDIR` 里的 `$` 必须用**单引号**包住，否则会被 shell 当变量展开。

## 3. 解出 `Assets/KRApp.conf`

```bash
7z x -y -oapp '$PLUGINSDIR/KRLauncher.zip' '*/Assets/KRApp.conf'
# 得到 app/<启动器版本>/Assets/KRApp.conf
```

## 4. 解码

```bash
python3 - <<'PY'
import base64, glob, json

path = glob.glob("app/*/Assets/KRApp.conf")[0]
raw = open(path, "rb").read()
data = base64.b64decode(raw + b"=" * (-len(raw) % 4))          # 补 padding
cfg = json.loads(bytes(b ^ 0x63 for b in data).decode())       # XOR 0x63

g = cfg["games"][0]
for key in ("resId", "gameId", "serverCode", "appKey", "configUrl", "backUpConfigUrl"):
    print(f"{key:16} {g[key]}")
PY
```

### 一步到位脚本（已验证，官服 / B 服通用）

保存为 `krconf.sh`，`chmod +x krconf.sh`，然后 `./krconf.sh <安装包.exe>`：

```bash
#!/usr/bin/env bash
# 用法: ./krconf.sh <渠道安装包.exe>   → 打印 games[0] 的 appKey / configUrl
set -euo pipefail
exe="$1"; tmp="$(mktemp -d)"; cd "$tmp"
7z x -bso0 -bsp0 -y -o. "$exe" '$PLUGINSDIR/KRLauncher.zip'
7z x -bso0 -bsp0 -y -oapp '$PLUGINSDIR/KRLauncher.zip' '*/Assets/KRApp.conf'
python3 - <<'PY'
import base64, glob, json
p = glob.glob("app/*/Assets/KRApp.conf")[0]
raw = open(p, "rb").read()
cfg = json.loads(bytes(b ^ 0x63 for b in base64.b64decode(raw + b"=" * (-len(raw) % 4))).decode())
g = cfg["games"][0]
for k in ("resId", "gameId", "serverCode", "appKey", "configUrl", "backUpConfigUrl"):
    print(f"{k:16} {g[k]}")
PY
```

输出示例（官服 3.1.0.1 安装包）：

```
resId            10003
gameId           G152
serverCode       official
appKey           oLNgHF1CESo51DGHN2odtp40e3oI1HfZ
configUrl        https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/10003_oLNgHF1CESo51DGHN2odtp40e3oI1HfZ/G152/official/index.json
backUpConfigUrl  https://prod-volcdn-gamestarter.kurogame.xyz/launcher/game/10003_oLNgHF1CESo51DGHN2odtp40e3oI1HfZ/G152/official/index.json
```

## 5. 结果解读与写入代码

2026-11 实测（官服 3.1.0.1 / B 服 3.0.1.0 安装包）：

| 渠道 | resId | gameId | serverCode | appKey | index |
| --- | --- | --- | --- | --- | --- |
| 国服 | 10003 | G152 | `official` | `oLNgHF1CESo51DGHN2odtp40e3oI1HfZ` | `https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/10003_oLNgHF1CESo51DGHN2odtp40e3oI1HfZ/G152/official/index.json` |
| B 服 | 10004 | G152 | `bilibili` | `IY6xmlAusbImBjWrpPj1OwNuirXEO44x` | `https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/10004_IY6xmlAusbImBjWrpPj1OwNuirXEO44x/G152/bilibili/index.json` |

`backUpConfigUrl` 是备用 CDN（实测为 `prod-volcdn-gamestarter.kurogame.xyz` 同路径），可直接用于 `api_urls` 的第二条。

把新的 `configUrl` / `backUpConfigUrl` 填进 `src/ww_manager/config.py`：

- `SERVER_CONFIGS[<server>]["api_url"]` 与 `"api_urls"`；
- `RESOURCE_CONFIG_PATHS[<server>]`（去掉协议和域名的相对路径，多个 CDN host 由 `RESOURCE_CONFIG_HOSTS` 拼接）。

注意每个渠道的 index **不能混用**：官服与 B 服的 `contentId` 目录、`resourcePacks` 内容不同，用错渠道的地址会下载到错误的分包清单。

## 6. 验证新地址

```bash
curl -s --compressed -o index.json -w '%{http_code}\n' '<新的 configUrl>'
python3 -c "
import json; d = json.load(open('index.json'))
print('顶层键:', list(d))
print('版本:', d['resourcePacks']['common']['version'])
print('schema:', '3.7 老结构' if 'default' in d else '3.8+ 新结构')
"
```

判定标准：

- HTTP 200；
- 顶层有 `cdnList` / `resourcePacks` / `bundles` / `config` / `resourcesGray` / `predownload`，**没有** `default`（3.7 老结构才有 `default`）；
- `resourcePacks.common.version` 等于当前游戏版本。

## 其他获取途径

- **已安装启动器**：直接读 `<启动器根目录>/<启动器版本>/Assets/KRApp.conf`，编码与上面一致。新版本启动器启动后会用内置更新逻辑覆盖这个文件，所以已装启动器时优先读它。
- **运行时**：启动器 WebView 前端的 jsBridge 方法 `kr_get_launcher_conf` 会把同样的字段回吐给页面（`appId` / `appKey` / `launcherIdentity` / `configUrl` 等）。这是**本地 Bridge 调用，不是 HTTP 请求**，抓包看不到"取 appKey 的接口"。
- **启动器日志**：`AppData/Roaming/KRLauncher/<appId>/log/App_*.log`（XOR 0x09 混淆），其中 `CallbackDataToH5` 那一行就是上面那份 JSON。
- **国际服**（`50004` / `G153`）：尚未实测，理论上是同一份 `KRApp.conf`，国际服安装包按本文流程解即可；在拿到实测 token 前，代码里继续使用旧路径（会冻结在旧版本，可能无法更新）。

## 编码细节备忘

- `KRApp.conf` 是**整文件** base64，可能缺尾部 `=`，解码前补齐 `len % 4`。
- 解开 base64 后逐字节 XOR `0x63`；容器格式里 `KRByteUtils.Xor` 的这条常量随启动器大版本可能变动，**如果解码出的内容不是合法 JSON，先怀疑这个 key**（例如找出十六进制里可疑单字节常量再试）。
- JSON 顶层是启动器自身配置（`appKey`、`launcherConfigUrl`、`crashSight*`、`shushu*` 等），`games[]` 才是各渠道游戏配置；本工具只需要 `games[0]`。
- 若 `KRApp.conf` 缺失，启动器会回退读取同目录的 `KRConfig.json`（明文 JSON，字段相近）。

## 附：3.8 迁移踩坑记录（2026-11-12）

- 官服旧 token `10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5` 已 404；B 服旧 token `10004_j5GW…` 仍返回 200，但内容是 3.7.1 老 schema（有 `default`），是最阴的那种"不报错但过期"。
- 新 schema 没有 `default` 段；工具侧从 `resourcePacks.common` 合成（版本 / `cdnList` / `baseUrl` / 基础 indexFile），见 `WGameManager._normalize_channel_index`。
- 渠道配置必须各取各的：B 服用官服清单去算增量时，`Client/Binaries/Win64/ClearThirdParty.exe` 等 68 个文件的源 md5 对不上（官服 1859d3ae… vs B 服 cf807c09…），整次 apply 会在迁移前中止。
