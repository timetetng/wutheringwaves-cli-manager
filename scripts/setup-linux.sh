#!/usr/bin/env bash
# 鸣潮 Linux 安装脚本：DWProton + 独立 prefix。
#
# 一次执行完成：
#   1. 下载并校验 DWProton
#   2. 准备 riched20 UTF-8 补丁组件（修复登录协议正文乱码）
#   3. 初始化独立 prefix：DXVK、vkd3d-proton、dxvk-nvapi、NVIDIA NGX 运行时、
#      ICU、字体、DLL 覆盖
#   4. 自检 prefix 里的关键 DLL，缺什么补什么
#   5. 安装启动器 ~/.local/bin/run_ww 与桌面入口
#
# 用法：scripts/setup-linux.sh [--game-dir <目录>] 默认则读 ww-cli 安装目录
set -euo pipefail

readonly DWPROTON_VERSION="11.0-15"
readonly DWPROTON_ASSET="dwproton-${DWPROTON_VERSION}-x86_64"
readonly DWPROTON_URL="https://dawn.wine/dawn-winery/dwproton/releases/download/dwproton-${DWPROTON_VERSION}/${DWPROTON_ASSET}.tar.xz"
readonly DWPROTON_SHA512="ec8985d14b6492df3459f93a3d90278814d9be2fe618e1ccd76f72f732c797e639d7f33fdf801be28585d0855e73bdda27858552ce709ba05cf3f60a17d2e817"

# 补丁针对该版本提交构建，后续升级未验证，升级 DWProton 后需重新编译
readonly WINE_COMMIT="b75a4125963073b01d2b3bfce6060d697d02066b"
readonly WINE_SOURCE_URL="https://dawn.wine/api/v1/repos/dawn-winery/wine-dwproton/archive/${WINE_COMMIT}.tar.gz"
readonly WINE_SOURCE_SHA256="657e9fbb61a3bd3bde414ca09eb60ec94e13edc1f07175861823dc055c076c90"
readonly COMPONENT_SHA256="f7253d99dbb8688c8a64152b9c4cd251267104c30738797cd9aefbf5a1706b82"

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
readonly VENDOR_DIR="$REPO_DIR/vendor/riched20-utf8-patch"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
WW_HOME="${WW_LINUX_HOME:-$DATA_HOME/ww-linux}"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor"
PFX=""

GAME_DIR=""
DW=""
TIER="hd"
JOBS="$(nproc 2>/dev/null || echo 4)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m警告:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：scripts/setup-linux.sh [--game-dir <目录>]

  --game-dir <目录>  游戏安装目录；省略时读取 ww 的配置，读不到则报错
  -h, --help         显示本帮助

安装根目录为 ~/.local/share/ww-linux（可用 WW_LINUX_HOME 覆盖），
启动器安装到 ~/.local/bin/run_ww，桌面入口为 ww.desktop。
画质档位读取游戏目录里的 wwm_resource_state.json。
已有的 DWProton 和 prefix 会被直接复用，不重复下载也不移动。
可用 WW_WINEPREFIX 指定要使用的 prefix。
EOF
}

while (($#)); do
    case "$1" in
        --game-dir) GAME_DIR="${2:-}"; shift 2 ;;
        -h|--help)  usage; exit 0 ;;
        *) die "未知参数: $1（--help 查看用法）" ;;
    esac
done

need_tool() { command -v "$1" >/dev/null 2>&1 || die "缺少命令 $1，请先安装。"; }

# 驱动自带的 NGX 运行时目录（Proton 的 proton 脚本也从这里取）
nvidia_wine_dir() {
    local candidate glx
    for candidate in "${NV_WINE_DIR:-}" /usr/lib/nvidia/wine /usr/lib64/nvidia/wine \
        /usr/lib/x86_64-linux-gnu/nvidia/wine /usr/lib/i386-linux-gnu/nvidia/wine; do
        [[ -n "$candidate" && -f "$candidate/nvngx.dll" ]] || continue
        printf '%s\n' "$candidate"
        return 0
    done
    glx="$(ldconfig -p 2>/dev/null | awk '/libGLX_nvidia[.]so[.]0 /{print $NF; exit}')"
    [[ -n "$glx" ]] || return 1
    candidate="$(dirname -- "$(readlink -f -- "$glx")")/nvidia/wine"
    [[ -f "$candidate/nvngx.dll" ]] || return 1
    printf '%s\n' "$candidate"
}

# 读取 ww 记录的游戏目录。
ww_default_path() {
    local config="$HOME/.config/ww_manager/config.json"
    [[ -f "$config" ]] || return 0
    python3 - "$config" <<'PY' 2>/dev/null || true
import json, sys
path = json.load(open(sys.argv[1], encoding='utf-8')).get('default_path')
if path:
    print(path)
PY
}

# 读取资源档位，供启动器拼 -krqlv 参数。
read_tier() {
    local state="$GAME_DIR/wwm_resource_state.json"
    [[ -f "$state" ]] || return 0
    python3 - "$state" <<'PY' 2>/dev/null || true
import json, sys
data = json.load(open(sys.argv[1], encoding='utf-8'))
tier = data.get('active_tier') or data.get('default_tier') or data.get('tier')
if tier in ('sd', 'hd', 'uhd'):
    print(tier)
PY
}

# 复用已有 prefix：环境变量 > 安装根目录；都没有时在安装根目录新建
find_prefix() {
    local candidate
    for candidate in "${WW_WINEPREFIX:-}" "$WW_HOME/prefix/pfx" "$DATA_HOME/ww-prefix/pfx"; do
        [[ -n "$candidate" && -f "$candidate/drive_c/windows/system32/dxgi.dll" ]] || continue
        printf '%s\n' "$(cd -- "$candidate" && pwd)"
        return
    done
    printf '%s\n' "$WW_HOME/prefix/pfx"
}

preflight() {
    local game_exe

    [[ -n "$GAME_DIR" ]] || GAME_DIR="$(ww_default_path)"
    [[ -n "$GAME_DIR" ]] || die "未指定游戏目录，且读不到 ww 配置。请先运行 ww -p '/your/game/path' download，或用 --game-dir 指定。"
    [[ -d "$GAME_DIR" ]] || die "游戏目录不存在: $GAME_DIR"
    game_exe="$GAME_DIR/Client/Binaries/Win64/Client-Win64-Shipping.exe"
    [[ -f "$game_exe" ]] || die "找不到游戏主程序: $game_exe"

    for tool in curl tar xz sha512sum sha256sum python3 awk grep ln cp mv mkdir; do
        need_tool "$tool"
    done
    command -v magick >/dev/null 2>&1 \
        || log "提示：未安装 ImageMagick，桌面图标只装 256x256（可选）。"

    TIER="$(read_tier)"
    TIER="${TIER:-hd}"

    PFX="$(find_prefix)"
    [[ "$PFX" == "$WW_HOME/prefix/pfx" ]] || log "复用已有 prefix: $PFX"

    mkdir -p "$WW_HOME/logs" "$WW_HOME/downloads" "$WW_HOME/component" "$BIN_DIR"
    log "游戏目录: $GAME_DIR"
    log "资源档位: $TIER"
    log "安装根目录: $WW_HOME"
}

# 判断目录里是不是本脚本要求的 DWProton 版本
dwproton_ok() {
    local dir="$1"
    [[ -n "$dir" && -x "$dir/files/bin/wine" && -f "$dir/version" ]] || return 1
    grep -q "dwproton-${DWPROTON_VERSION}" "$dir/version"
}

# 复用用户已有的 DWProton（环境变量 → 已有安装 → 常见目录），不移动也不复制
# 找不到匹配版本时下载到安装根目录。
ensure_dwproton() {
    local candidate record="$WW_HOME/dwproton.path"
    local -a candidates=(
        "${DWPROTON:-}"
        "$WW_HOME/$DWPROTON_ASSET"
        "$(cat "$record" 2>/dev/null || true)"
        "$DATA_HOME/dwproton/$DWPROTON_ASSET"
        "$DATA_HOME/ww-prefix/$DWPROTON_ASSET"
        "$DATA_HOME/lutris/runners/wine/"*dwproton*
        "$DATA_HOME/steam/steamapps/common/"*dwproton*
        "$HOME/.steam/steam/steamapps/common/"*dwproton*
    )

    for candidate in "${candidates[@]}"; do
        if dwproton_ok "$candidate"; then
            DW="$(cd -- "$candidate" && pwd)"
            printf '%s\n' "$DW" >"$record"
            log "复用已有 DWProton: $DW"
            return
        fi
    done
    if [[ -n "${DWPROTON:-}" ]]; then
        warn "DWPROTON=$DWPROTON 不是 ${DWPROTON_VERSION} 版本，改用安装根目录内的副本。"
    fi

    DW="$WW_HOME/$DWPROTON_ASSET"
    if [[ -x "$DW/files/bin/wine" ]]; then
        log "DWProton $DWPROTON_VERSION 已就绪"
        printf '%s\n' "$DW" >"$record"
        return
    fi

    local archive="$WW_HOME/downloads/${DWPROTON_ASSET}.tar.xz"
    if [[ ! -f "$archive" ]]; then
        log "下载 DWProton $DWPROTON_VERSION（约 324 MiB）"
        curl -fL -C - --retry 3 --progress-bar -o "$archive.part" "$DWPROTON_URL"
        mv "$archive.part" "$archive"
    fi

    log "校验 DWProton"
    if [[ "$(sha512sum "$archive" | awk '{print $1}')" != "$DWPROTON_SHA512" ]]; then
        rm -f "$archive" "$archive.part"
        die "DWProton 校验失败，已删除下载文件，请重试。"
    fi

    log "解压 DWProton"
    rm -rf "$DW"
    tar -xJf "$archive" -C "$WW_HOME"
    [[ -x "$DW/files/bin/wine" ]] || die "解压后找不到 $DW/files/bin/wine"
    printf '%s\n' "$DW" >"$record"
}

# 优先使用仓库内的预编译组件，缺失或哈希不符时从源码编译
install_component() {
    local source_dll="$VENDOR_DIR/riched20.dll"
    local target="$WW_HOME/component/riched20.dll"

    if [[ -f "$source_dll" ]] \
        && [[ "$(sha256sum "$source_dll" | awk '{print $1}')" == "$COMPONENT_SHA256" ]]; then
        cp -p "$source_dll" "$target"
        log "补丁组件已就绪"
        return
    fi
    build_component "$target"
}

build_component() {
    local target="$1"
    local work="$WW_HOME/build"
    local src="$work/wine-src"
    local archive="$WW_HOME/downloads/wine-${WINE_COMMIT}.tar.gz"

    for tool in gcc make bison flex autoconf automake libtool patch; do need_tool "$tool"; done

    log "下载 Wine 源码（提交 ${WINE_COMMIT:0:12}）"
    if [[ ! -f "$archive" ]]; then
        curl -fL --retry 3 --progress-bar -o "$archive.part" "$WINE_SOURCE_URL"
        mv "$archive.part" "$archive"
    fi
    if [[ "$(sha256sum "$archive" | awk '{print $1}')" != "$WINE_SOURCE_SHA256" ]]; then
        rm -f "$archive" "$archive.part"
        die "Wine 源码校验失败，已删除下载文件，请重试。"
    fi

    log "准备源码树"
    rm -rf "$src" "$work/build"
    mkdir -p "$src"
    tar -xzf "$archive" -C "$src" --strip-components=1
    patch -d "$src" -p1 <"$VENDOR_DIR/riched20-utf8-rtf.patch"

    # autogen.sh 会生成 include/wine/vulkan.h 等头文件
    log "生成构建脚本"
    ( cd "$src" && ./autogen.sh >"$WW_HOME/logs/autogen.log" 2>&1 ) \
        || die "autogen.sh 失败，见 $WW_HOME/logs/autogen.log"

    log "配置构建"
    mkdir -p "$work/build"
    ( cd "$work/build" && "$src/configure" \
        --enable-win64 --disable-tests \
        --without-x --without-wayland --without-alsa --without-pulse --without-dbus \
        --without-gstreamer --without-cups --without-sane --without-udev --without-usb \
        --without-gnutls --without-opengl --without-vulkan \
        >"$WW_HOME/logs/configure.log" 2>&1 ) || die "configure 失败，见 $WW_HOME/logs/configure.log"

    log "编译 riched20（并行度 $JOBS）"
    local built="$work/build/dlls/riched20/x86_64-windows/riched20.dll"
    ( cd "$work/build" && make -j"$JOBS" dlls/riched20/x86_64-windows/riched20.dll \
        >"$WW_HOME/logs/build-riched20.log" 2>&1 ) || die "编译失败，见 $WW_HOME/logs/build-riched20.log"

    # 默认链接产物带 builtin 标记，加载器会用它替换显式指定的 DLL；去掉标记才能作为 native 加载。
    log "重新链接为 native 组件"
    local link_cmd
    # 构建日志里的链接命令带反斜杠续行，需拼接后再切分。
    link_cmd="$(awk '
        /winegcc -o dlls\/riched20\/x86_64-windows\/riched20.dll/ { grab = 1 }
        grab {
            line = $0
            sub(/[ \t]*\\$/, "", line)
            printf "%s ", line
            if ($0 !~ /\\$/) exit
        }
    ' "$WW_HOME/logs/build-riched20.log")"
    [[ -n "$link_cmd" ]] || die "未能从构建日志提取链接命令。"

    local -a args kept=()
    read -r -a args <<<"$link_cmd"
    local arg
    for arg in "${args[@]}"; do
        [[ "$arg" == "-Wl,--wine-builtin" ]] || kept+=("$arg")
    done
    ( cd "$work/build" && "${kept[@]}" ) >"$WW_HOME/logs/link-riched20.log" 2>&1 \
        || die "重新链接失败，见 $WW_HOME/logs/link-riched20.log"

    [[ -f "$built" ]] || die "重新链接后找不到组件。"
    local actual
    actual="$(sha256sum "$built" | awk '{print $1}')"
    [[ "$actual" == "$COMPONENT_SHA256" ]] \
        || warn "重建产物哈希与发布值不同（工具链差异属正常），继续使用。"
    cp -p "$built" "$target"
}

# 链接内置 DLL 回 DWProton 的库目录
slim_prefix() {
    local dw="$DW"
    # 已手工拷入的 DXVK/vkd3d-proton/NVAPI 不能在这里被 Wine 内置 DLL 的软链盖回。
    local -a keep=(dxgi.dll d3d11.dll d3d12.dll d3d12core.dll riched20.dll
        nvapi64.dll nvapi.dll nvofapi64.dll nvngx.dll _nvngx.dll)
    local pair arch dest src dll name linked=0

    for pair in 'x86_64-windows system32' 'i386-windows syswow64'; do
        read -r arch dest <<<"$pair"
        for src in "$dw/files/lib/wine/$arch" "$dw/files/lib/wine/icu/$arch"; do
            [[ -d "$src" ]] || continue
            for dll in "$src"/*.dll; do
                [[ -f "$dll" ]] || continue
                name="$(basename "$dll")"
                case " ${keep[*]} " in
                    *" $name "*) continue ;;
                esac
                ln -sfn "$dll" "$PFX/drive_c/windows/$dest/$name"
                linked=$((linked + 1))
            done
        done
    done
    log "内置 DLL 已链接（$linked 个）"
}

# prefix 初始化完成的标志：wineboot 结束会留下 system.reg 与 kernel32.dll
prefix_ready() {
    [[ -f "$1/system.reg" && -f "$1/drive_c/windows/system32/kernel32.dll" ]]
}

init_prefix() {
    local dw="$DW"
    local wine="$dw/files/bin/wine"
    local log_file="$WW_HOME/logs/wineboot.log"

    export WINEPREFIX="$PFX" WINEARCH=wow64
    export LD_LIBRARY_PATH="$dw/files/lib:$dw/files/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export WINEDLLPATH="$dw/files/lib/vkd3d:$dw/files/lib/wine"
    export WINEDEBUG=fixme-all

    if prefix_ready "$PFX"; then
        log "prefix 已就绪，更新组件"
    else
        mkdir -p "$WW_HOME/prefix"
        if [[ -d "$PFX" ]]; then
            local aside="$PFX.incomplete-$(date +%Y%m%d-%H%M%S)"
            warn "上次初始化未完成，残缺 prefix 已移到 $aside"
            mv "$PFX" "$aside"
        fi
        log "初始化独立 prefix（WoW64）"
        LANG="${LANG:-zh_CN.UTF-8}" LC_ALL="${LC_ALL:-zh_CN.UTF-8}" \
            "$wine" wineboot.exe -i >"$log_file" 2>&1 \
            || die "wineboot 失败，见 $log_file"
        "$dw/files/bin/wineserver" -w
        prefix_ready "$PFX" \
            || die "wineboot 结束后 prefix 仍不完整，见 $log_file（可删除 $PFX 后重试）"
    fi

    # 图形库：DXGI/D3D11 用 DWProton 自带 DXVK
    local pair arch dest dll
    for pair in 'x86_64-windows system32' 'i386-windows syswow64'; do
        read -r arch dest <<<"$pair"
        for dll in dxgi d3d11; do
            cp --remove-destination "$dw/files/lib/wine/dxvk/$arch/$dll.dll" \
                "$PFX/drive_c/windows/$dest/$dll.dll"
        done
        # D3D12 用 DWProton 自带的 vkd3d-proton：Wine 自带的 d3d12 是旧 vkd3d 存根，
        # UE4 游戏在它上面会报 “Failed to choose a D3D12 Adapter” 并退回 DX11。
        # 走到 D3D12 才可能出现 DLSS 选项。
        for dll in d3d12 d3d12core; do
            [[ -f "$dw/files/lib/wine/vkd3d-proton/$arch/$dll.dll" ]] || continue
            cp --remove-destination "$dw/files/lib/wine/vkd3d-proton/$arch/$dll.dll" \
                "$PFX/drive_c/windows/$dest/$dll.dll"
        done
        # NVAPI / NVOFAPI（dxvk-nvapi）：DLSS、Reflex、帧生成的 NVIDIA 能力检测依赖它。
        # proton 只在 PROTON_ENABLE_NVAPI=1 时把这组 DLL 拷进 prefix，这里直连 wine 所以自己拷。
        for dll in "$dw/files/lib/wine/nvapi/$arch"/*.dll; do
            [[ -f "$dll" ]] || continue
            cp --remove-destination "$dll" "$PFX/drive_c/windows/$dest/$(basename "$dll")"
        done
        for dll in icuin68 icuuc68 icudt68; do
            cp --remove-destination "$dw/files/lib/wine/icu/$arch/$dll.dll" \
                "$PFX/drive_c/windows/$dest/$dll.dll"
        done
    done

    # NGX 运行时（DLSS 4 架构）：游戏自带的 nvngx_dlss.dll 只接受 nvngx.dll 的转调，
    # 直接调用一律返回 0xBAD00002 PlatformError。缺这两个 DLL 时设置里只有 FSR，
    # 补上后 DLSS 与帧生成才会出现。Proton 也从驱动目录取同两个文件进 system32。
    local nvdir
    if nvdir="$(nvidia_wine_dir)"; then
        for dll in nvngx _nvngx; do
            cp --remove-destination "$nvdir/$dll.dll" \
                "$PFX/drive_c/windows/system32/$dll.dll"
        done
        log "NGX 运行时已安装: $nvdir"
    else
        warn "未找到驱动自带的 nvngx.dll（/usr/lib/nvidia/wine），DLSS 选项不会出现；"
        warn "请确认已安装 NVIDIA 闭源驱动。"
    fi

    cp --remove-destination "$WW_HOME/component/riched20.dll" \
        "$PFX/drive_c/windows/system32/riched20.dll"

    slim_prefix

    # 不注册这条覆盖，Wine 仍优先加载内置 riched20，补丁不生效。
    "$wine" reg.exe add \
        'HKCU\Software\Wine\AppDefaults\Client-Win64-Shipping.exe\DllOverrides' \
        /v riched20 /t REG_SZ /d n,b /f >>"$log_file" 2>&1
    "$dw/files/bin/wineserver" -w

    verify_prefix

    log "prefix 就绪: $PFX"
}

# 逐项核对 prefix 里的关键 DLL，缺的按来源补上
verify_prefix() {
    local dw="$DW" nvdir="" spec dest dll src target total=0 miss=0 fixed=0
    nvdir="$(nvidia_wine_dir || true)"

    local -a specs=(
        "system32|dxgi.dll|dxvk/x86_64-windows"
        "system32|d3d11.dll|dxvk/x86_64-windows"
        "system32|d3d12.dll|vkd3d-proton/x86_64-windows"
        "system32|d3d12core.dll|vkd3d-proton/x86_64-windows"
        "system32|nvapi64.dll|nvapi/x86_64-windows"
        "system32|nvofapi64.dll|nvapi/x86_64-windows"
        "system32|icuin68.dll|icu/x86_64-windows"
        "system32|icuuc68.dll|icu/x86_64-windows"
        "system32|icudt68.dll|icu/x86_64-windows"
        "system32|riched20.dll|component"
        "system32|nvngx.dll|driver-ngx"
        "system32|_nvngx.dll|driver-ngx"
        "syswow64|dxgi.dll|dxvk/i386-windows"
        "syswow64|d3d11.dll|dxvk/i386-windows"
        "syswow64|d3d12.dll|vkd3d-proton/i386-windows"
        "syswow64|d3d12core.dll|vkd3d-proton/i386-windows"
        "syswow64|nvapi.dll|nvapi/i386-windows"
        "syswow64|icuin68.dll|icu/i386-windows"
        "syswow64|icuuc68.dll|icu/i386-windows"
        "syswow64|icudt68.dll|icu/i386-windows"
    )

    for spec in "${specs[@]}"; do
        IFS='|' read -r dest dll src <<<"$spec"
        total=$((total + 1))
        target="$PFX/drive_c/windows/$dest/$dll"
        [[ -f "$target" ]] && continue
        miss=$((miss + 1))

        printf '  \033[1;33m补装\033[0m %s/%s\n' "$dest" "$dll"
        case "$src" in
            component)
                cp --remove-destination "$WW_HOME/component/riched20.dll" "$target" 2>/dev/null || true ;;
            driver-ngx)
                if [[ -n "$nvdir" ]]; then
                    cp --remove-destination "$nvdir/$dll" "$target" 2>/dev/null || true
                fi ;;
            *)
                cp --remove-destination "$dw/files/lib/wine/$src/$dll" "$target" 2>/dev/null || true ;;
        esac
        if [[ -f "$target" ]]; then
            fixed=$((fixed + 1))
        else
            warn "补装失败: $dest/$dll（来源 $src）"
        fi
    done

    if ((fixed > 0)); then
        log "prefix 自检补装 $fixed 项（共 $total 项）"
    else
        log "prefix 自检通过（$total 项）"
    fi
}

# 链接登录框所需 ms 字体
install_fonts() {
    local dw="$DW"
    local src="$dw/files/share/fonts"
    local dest="$PFX/drive_c/windows/Fonts"

    [[ -d "$src" ]] || die "DWProton 缺少字体目录: $src"
    mkdir -p "$dest"

    local font name added=0
    for font in "$src"/*; do
        [[ -f "$font" ]] || continue
        name="$(basename "$font")"
        if [[ ! -e "$dest/$name" ]]; then
            ln -sfn "$font" "$dest/$name"
            added=$((added + 1))
        fi
    done

    [[ -f "$dest/msyh.ttf" && -f "$dest/simsun.ttc" ]] \
        || die "字体链接不完整，缺少 msyh.ttf 或 simsun.ttc。"
    log "字体已链接（新增 $added 个）"
}

# 从游戏自带启动器里提取图标
install_icon() {
    local exe="$GAME_DIR/Wuthering Waves.exe"
    [[ -f "$exe" ]] || { warn "找不到 $exe，跳过图标安装。"; return; }

    local -a sizes=(256x256)
    command -v magick >/dev/null 2>&1 && sizes=(48x48 64x64 128x128 256x256)

    python3 "$SCRIPT_DIR/extract-icon.py" "$exe" "$ICON_DIR" "${sizes[@]}" \
        || { warn "图标提取失败，跳过。"; return; }
    log "桌面图标已安装（${sizes[*]}）"
}

write_launcher() {
    local launcher="$BIN_DIR/run_ww"
    local dw="$DW"

    mkdir -p "$BIN_DIR"
    local tmp="$launcher.tmp.$$"
    cat >"$tmp" <<EOF
#!/usr/bin/env bash
# 鸣潮启动脚本（由 $SCRIPT_DIR/setup-linux.sh 生成）
set -euo pipefail

DW="\${DWPROTON:-$dw}"
GAME="\${WW_GAME_DIR:-$GAME_DIR}"
STATE="\${WW_STATE_DIR:-$WW_HOME}"
export WINEPREFIX="\${WW_WINEPREFIX:-$PFX}"
GAME_EXE="\$GAME/Client/Binaries/Win64/Client-Win64-Shipping.exe"

# ────────── 可选启动选项（默认全部关闭，改成 1 或填值即启用）──────────
# 分辨率与全屏：填入分辨率后拼 -ResX/-ResY；FULLSCREEN=1 时附 -fullscreen
RES_X="\${WW_RES_X:-}"          # 例：1920
RES_Y="\${WW_RES_Y:-}"          # 例：1080
FULLSCREEN="\${WW_FULLSCREEN:-0}"
# MangoHud 帧率叠加（需自行安装 mangohud）
MANGOHUD="\${WW_MANGOHUD:-0}"
MANGOHUD_CONFIG="\${WW_MANGOHUD_CONFIG:-fps,frametime,gpu_temp,cpu_temp}"
# GameMode 性能调度（需自行安装 gamemode）
GAMEMODE="\${WW_GAMEMODE:-0}"
# Gamescope 嵌套合成器（需自行安装 gamescope）
GAMESCOPE="\${WW_GAMESCOPE:-0}"
GAMESCOPE_ARGS="\${WW_GAMESCOPE_ARGS:--W 1920 -H 1080 -f -F fsr}"
# ────────────────────────────────────────────────────────────────

if [[ "\${1:-}" == --help ]]; then
    printf '%s\n' '用法：run_ww [游戏参数...]' \\
        '      run_ww --check   只检查路径与档位，不启动' \\
        '环境变量：DWPROTON、WW_GAME_DIR、WW_WINEPREFIX、WW_TIER（sd/hd/uhd）' \\
        '画质档位默认读取游戏目录的 wwm_resource_state.json。' \\
        '可选开关（默认关闭）：WW_RES_X/WW_RES_Y、WW_FULLSCREEN、WW_MANGOHUD、WW_GAMEMODE、WW_GAMESCOPE' \\
        '日志：\$STATE/logs/game.log'
    exit 0
fi

[[ -x "\$DW/files/bin/wine" ]] || { echo "找不到 DWProton：\$DW" >&2; exit 1; }
[[ -f "\$GAME_EXE" ]] || { echo "找不到游戏：\$GAME_EXE" >&2; exit 1; }
[[ -f "\$WINEPREFIX/drive_c/windows/system32/dxgi.dll" ]] || {
    echo "prefix 未配置：\$WINEPREFIX（请重新运行安装脚本）" >&2; exit 1;
}

TIER="\${WW_TIER:-}"
if [[ -z "\$TIER" ]]; then
    TIER="\$(python3 - "\$GAME/wwm_resource_state.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding='utf-8'))
    tier = data.get('active_tier') or data.get('default_tier') or data.get('tier')
    if tier not in ('sd', 'hd', 'uhd'):
        raise ValueError('档位未知')
    if not data.get('versions', {}).get(tier):
        raise ValueError(f'{tier} 档位尚未安装')
    if tier in data.get('incomplete', []) or 'common' in data.get('incomplete', []):
        raise ValueError('资源不完整，请先运行 ww sync')
    print(tier)
except (OSError, ValueError, TypeError, AttributeError) as exc:
    sys.exit(f'无法确认资源档位：{exc}')
PY
)" || exit 1
fi

for arg in "\$@"; do
    [[ "\${arg,,}" != -krqlv=* ]] || { echo '请用 WW_TIER 指定档位，避免重复 -krqlv 参数。' >&2; exit 1; }
done

printf '游戏：%s\n档位：%s\n日志：%s\n' "\$GAME_EXE" "\$TIER" "\$STATE/logs/game.log"
[[ "\${1:-}" != --check ]] || exit 0

export WINEARCH=wow64
export LD_LIBRARY_PATH="\$DW/files/lib:\$DW/files/lib/x86_64-linux-gnu\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export WINEDLLPATH="\$DW/files/lib/vkd3d:\$DW/files/lib/wine\${WINEDLLPATH:+:\$WINEDLLPATH}"
# DXVK 默认在未启用 NVAPI 时把 N 卡伪装成 AMD（dxgi.hideNvidiaGpu=Auto），
# 游戏会据此只给 FSR、隐藏 DLSS，D3D12 适配器选择也会失败退回 DX11。
export DXVK_ENABLE_NVAPI=1
# dcomp 置空规避登录界面白屏（DXVK 未实现合成交换链）。
export WINEDLLOVERRIDES="dxgi,d3d11=n,b;d3d12,d3d12core=n;nvapi,nvapi64=n;dcomp="
export WINEDEBUG="\${WINEDEBUG:-fixme-all}"
export GST_PLUGIN_SYSTEM_PATH_1_0="\$DW/files/lib/x86_64-linux-gnu/gstreamer-1.0"
export WINE_GST_REGISTRY_DIR="\$STATE/gstreamer-1.0"
export DXVK_LOG_PATH="\$STATE/logs"
export DXVK_STATE_CACHE_PATH="\$STATE/cache"
export VKD3D_SHADER_CACHE_PATH="\$STATE/cache"

mkdir -p "\$DXVK_LOG_PATH" "\$DXVK_STATE_CACHE_PATH"
cd "\$(dirname "\$GAME_EXE")"

GAME_ARGS=("-krqlv=\$TIER")
[[ -n "\$RES_X" && -n "\$RES_Y" ]] && GAME_ARGS+=("-ResX=\$RES_X" "-ResY=\$RES_Y")
[[ "\$FULLSCREEN" == 1 ]] && GAME_ARGS+=("-fullscreen")

CMD=("\$DW/files/bin/wine" ./Client-Win64-Shipping.exe "\${GAME_ARGS[@]}" "\$@")

if [[ "\$MANGOHUD" == 1 ]]; then
    if command -v mangohud >/dev/null 2>&1 || [[ -d "\$HOME/.local/share/mangohud" ]]; then
        export MANGOHUD=1 MANGOHUD_CONFIG="\$MANGOHUD_CONFIG"
        [[ -d "\$HOME/.local/share/mangohud/implicit_layer.d" ]] && \\
            export VK_ADD_IMPLICIT_LAYER_PATH="\$HOME/.local/share/mangohud/implicit_layer.d\${VK_ADD_IMPLICIT_LAYER_PATH:+:\$VK_ADD_IMPLICIT_LAYER_PATH}"
        [[ -f "\$HOME/.config/MangoHud/MangoHud.conf" ]] && \\
            export MANGOHUD_CONFIGFILE="\$HOME/.config/MangoHud/MangoHud.conf"
    else
        echo '警告：未安装 mangohud，忽略 WW_MANGOHUD=1。' >&2
    fi
fi

if [[ "\$GAMESCOPE" == 1 ]]; then
    if command -v gamescope >/dev/null 2>&1; then
        read -r -a GS_ARGS <<<"\$GAMESCOPE_ARGS"
        CMD=(gamescope "\${GS_ARGS[@]}" -- "\${CMD[@]}")
    else
        echo '警告：未安装 gamescope，忽略 WW_GAMESCOPE=1。' >&2
    fi
fi

if [[ "\$GAMEMODE" == 1 ]]; then
    if command -v gamemoderun >/dev/null 2>&1; then
        CMD=(gamemoderun "\${CMD[@]}")
    else
        echo '警告：未安装 gamemode，忽略 WW_GAMEMODE=1。' >&2
    fi
fi

exec "\${CMD[@]}" >>"\$STATE/logs/game.log" 2>&1
EOF
    chmod +x "$tmp"
    mv -f "$tmp" "$launcher"
    log "启动器: $launcher"
}

write_desktop() {
    local entry="$APP_DIR/ww.desktop"

    mkdir -p "$APP_DIR"
    local tmp="$entry.tmp.$$"
    cat >"$tmp" <<EOF
[Desktop Entry]
Type=Application
Name=鸣潮
Name[zh_CN]=鸣潮
Name[en]=Wuthering Waves
GenericName=Wuthering Waves
Keywords=ww;wuwa;wuthering;wutheringwaves;waves;mingchao;鸣潮;潮;
Comment=鸣潮启动！
Exec="$BIN_DIR/run_ww"
Icon=ww
Terminal=false
Categories=Game;
StartupNotify=false
EOF
    chmod +x "$tmp"
    mv -f "$tmp" "$entry"

    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 \
        && gtk-update-icon-cache -f -t "$ICON_DIR" >/dev/null 2>&1 || true
    log "桌面入口: $entry"
}

main() {
    preflight
    ensure_dwproton
    install_component
    init_prefix
    install_fonts
    install_icon
    write_launcher
    write_desktop

    log "安装完成，启动方式：run_ww"
}

main
