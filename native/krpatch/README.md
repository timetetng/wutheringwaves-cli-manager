# krpatch — 鸣潮增量补丁的原生 Linux 应用器

替代仓库里 `hpatchz.exe + wine` 那条路径：同样的 `.krpdiff` 容器，同样的输出，但走原生 ELF、多分组并发、无 wine 开销。

## 为什么

原来的增量更新是把官方启动器里的 `hpatchz.exe` 丢进 wine 执行：单进程、单线程，实测一个 CPU 核都吃不满 10%，一次大更新要串行啃几十个分组。差分本身其实不慢（单个几 GB 的分组在现代机器上也就几秒），慢的是“串行 + wine + 每步几次进程往返”。

`krpatch` 把这件事收进一个二进制：

- **差分核心不改写**：直接编译上游 HDiffPatch v4.8.0 的 `patch.c`（原封不动地 vendor 在 `c/hdiffpatch/`），zstd 解压走上游 `decompress_plugin_demo.h` 里的官方插件，所以差分语义与官方 exe 同源，不存在“重新实现一遍算法”的偏差风险。
- **Rust 只做外围**：容器头解析、虚拟 old/new 流、写文件 + md5、线程池、进度与 JSON 结果。
- **分组级并发**：一次调用传入多个 `.krpdiff`，二进制内部起线程池，每个分组一条独立流水线（每组每线程持有自己的 zstd 插件实例）。
- **openat + 相对路径**：路径转换（容器里的 `\` 与大小写）和目录创建在 Rust 侧完成，不依赖 wine 的路径模拟。

## 构建

```bash
cd native/krpatch
cargo build --release      # 产物：target/release/krpatch
```

需要 C 编译器和 `cc` crate；zstd 由 `zstd-sys` 从源码编成静态库（`c/zstd/zstd.h` 是配套的头文件，因为 `zstd-sys` 不导出 include 目录），所以最终二进制只依赖 libc/libgcc_s，不需要系统 libzstd。

## 用法

```bash
# 应用一组补丁（推荐一次传全部，让线程池自己调度）
krpatch apply --old-root /path/to/旧版本 --out-root /path/to/输出 \
    --workers 8 patch/group_01.krpdiff patch/group_02.krpdiff ...

# 只看容器结构，不写任何文件
krpatch info patch/group_38.krpdiff

krpatch version
```

`apply` 选项：

| 选项 | 说明 |
| --- | --- |
| `--old-root DIR` | 旧版本根目录，容器里 `oldRefs` 的相对路径在这里解析 |
| `--out-root DIR` | 输出根目录，按容器内相对路径写出（目录自动创建） |
| `--workers N` | 并发分组数，默认 `min(CPU 核数, 分组数)` |
| `--cache-mb N` | 每组交给 `patch_decompress_with_cache()` 的临时缓存 MiB，默认 8（实测 1–32MB 都在噪声内，≥64MB 反而更慢，不必调） |
| `--fsync` | 每个输出文件写完 fsync（默认关；断电即毁的场景再开） |
| `--json FILE` | 结果 JSON 写入文件，默认打到 stdout |
| `--quiet` | 不打印进度和分组合计 |

进度走 stderr（`45.3%  1.2 GiB / 2.6 GiB  530 MiB/s`），结果 JSON 走 stdout，方便 Python 侧直接 `json.loads(stdout)`。退出码：全部成功 0，有分组失败 1，参数/环境错误 2。

结果 JSON：

```json
{
  "ok": true,
  "workers": 8,
  "cache_mb": 8,
  "total_secs": 4.312,
  "groups": [
    {
      "group": "group_16",
      "patch": "/path/group_16.krpdiff",
      "ok": true,
      "old_size": 97716450,
      "new_size": 105844690,
      "secs": 0.246,
      "files": [
        {"path": "Client/Content/Paks/pakchunk42-WindowsNoEditor.pak",
         "size": 105844690, "md5": "37cb7a62c6b2548c43083d079454aaa5"}
      ]
    }
  ]
}
```

失败时该分组变成 `{"ok": false, "error": "..."}`，其余分组照常完成——调用方可以按分组回退（比如用户目录里旧文件缺失、或者补丁没下完）。

## 容器格式（`apply` 实际依赖的部分）

一个 `.krpdiff` = 两段：

1. **head data**（zstd 压缩的 JSON + 一段二进制附录）：`oldRefs[]` / `newRefs[]`（相对路径 + 字节数，拼接顺序有意义）、`oldDataSize` / `newDataSize`、`headChecksums`、hdiff 段起点与长度。
2. **hdiff 段**：`HDIFF13&zstd\0` 的压缩差分。`oldRefs` 与 `newRefs` 是“虚拟连续文件”——把它们按顺序首尾相接，hdiff 就作用在这两条虚拟流上；应用完成后按 `newRefSizes` 切回一个个真实文件。

所以 `krpatch` 里没有“逐文件 patch”的概念，只有两条虚拟流 + 一次 `patch_decompress_with_cache()`。切分和 md5 都在写的时候顺手做，不再读回来。

## 已验证

样本是从官服 index 借来的 3.7.1 → 3.8.0 真实分组（旧版本文件本机已不可得，用朋友提供的同版本文件）。输出与官方索引里的 md5 逐字节一致：

| 分组 | 输入 → 输出 | 差分段 | 输出 md5 |
| --- | --- | --- | --- |
| `group_16` | 97,716,450 → 105,844,690 | 40 MB | `37cb7a62c6b2548c43083d079454aaa5` ✓ |
| `group_38` | 2,351,195,631 + 144,036 → 2,513,149,583 + 153,920 | 520 MB | `3f2dc3c20392c6b4991fb0b972f24e96` / `dcbdcb1389813e972a426a0c54f4d02e` ✓ |
| `group_25` | 108 refs（715 MiB）→ 109 个文件（730 MiB） | 52 MB | 旧文件 CDN 已 404，用同尺寸稀疏文件跑通：109 个输出全写出、尺寸全对；因输入是空洞文件，md5 不可比 |

另外验证过的失败路径：旧文件缺失、补丁被截断、hdiff 段长度与头部不符、输出路径重复（并发写冲突）——都会在写坏任何东西之前报错。

## 性能

`group_38`（2.5 GB 输出，单分组单线程）：

| | wall | user | sys |
| --- | --- | --- | --- |
| `krpatch`（含 md5 校验，缓存 8MB） | 4.77 s | 3.98 s | 0.77 s |
| 官方 `hpatchz.exe` 走 wine（同一份 `.krpdiff`） | 4.86 s | 1.87 s | 0.84 s |
| 上游原生 `hpatchz` v4.8.0（不校验 md5） | 3.9 s | — | — |

`group_16`（106 MB 输出）：`krpatch` 0.21 s，wine + `hpatchz.exe` 1.07 s（exe 自报内部只花 0.11 s，剩下全是 wine 启动）。

也就是说：**最大的那个分组上 wine 只是打平**——2.5 GB 数据面前，wine 那点开销被淡化了，exe 内部的 zstd 也是 native 跑的。真正的差别在每组固定开销（一次 wine 启动 ≈ 0.95 s，六十个分组就是一分钟白烧）、只能串行、以及调用方额外的代价（跑之前把旧文件 `copy2` 到临时目录、跑完再读一遍盘算 md5）。`krpatch` 原地 pread 旧文件、写的时候顺手算 md5、分组并行。

单核吞吐约 530–580 MB/s（已含 md5）。wall 在高负载下会明显超出 user（实测有 13 s 的轮次），那是 dirty page writeback 排队，不是差分算得慢。

和原来那条路径的真实差别在**分组之间**：一次更新几十个分组，`krpatch` 让它们同时跑，总 wall 时间接近最大那组的耗时；wine + `hpatchz.exe` 只能一个接一个。

## 限制

- 只支持 `zstd` 压缩的 `HDIFF13` 压缩差分（这是鸣潮启动器实际用的形态）。`info` 会先报出容器声明的类型，不支持的直接拒绝，不会瞎解。
- 只读 `.krpdiff` 这种两段式容器；上游 hpatchz 支持的 PKG / SF20 / 未压缩差分等形态没有实现。
- 不做“已应用则跳过”的判断，也不做下载：那是调用方（Python 侧）的事。
- 输出路径相对 `--out-root`，不会跟着旧文件去猜挂载点。

## 目录

```
native/krpatch/
├── build.rs                  # cc: 编译 patch.c + kr_shim.c
├── c/
│   ├── hdiffpatch/           # 上游 HDiffPatch v4.8.0 应用侧源码（未改动）
│   ├── zstd/                 # zstd 1.5.7 头文件（与 zstd-sys 同版本）
│   └── kr_shim.c             # 与 Rust 之间的薄胶水：插件模板、版本号、结构体断言
└── src/
    ├── main.rs               # CLI、线程池、进度、JSON
    ├── head.rs               # 容器头解析、refs 表、hdiff 段定位
    ├── streams.rs            # hpatch_TStreamInput/Output 的 Rust 实现、分段读写、md5
    ├── apply.rs              # 单分组应用流程与前置校验
    └── ffi.rs                # 与 C 侧的类型映射和 extern 声明
```
