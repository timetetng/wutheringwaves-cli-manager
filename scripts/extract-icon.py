#!/usr/bin/env python3
"""从 Windows PE 可执行文件提取最大尺寸的图标。

用法：extract-icon.py <exe> <输出目录> [尺寸...]

把图标写成 <输出目录>/<尺寸>/apps/ww.png。没有 ImageMagick 时只写 256x256。
"""
import os
import pathlib
import shutil
import struct
import subprocess
import sys


def u16(buf, off):
    return struct.unpack_from('<H', buf, off)[0]


def u32(buf, off):
    return struct.unpack_from('<I', buf, off)[0]


def read_pe_resources(path):
    data = pathlib.Path(path).read_bytes()
    pe = u32(data, 0x3C)
    if data[pe:pe + 4] != b'PE\0\0':
        raise SystemExit('不是 PE 文件')
    coff = pe + 4
    opt = coff + 20
    ndd = opt + (96 if u16(data, opt) == 0x10B else 112)
    rsrc_rva = u32(data, ndd + 16)

    sections = []
    for i in range(u16(data, coff + 2)):
        base = opt + u16(data, coff + 16) + i * 40
        vaddr = u32(data, base + 12)
        vsize = max(u32(data, base + 8), u32(data, base + 16))
        sections.append((vaddr, vsize, u32(data, base + 20)))

    def rva2off(rva):
        for vaddr, vsize, raw in sections:
            if vaddr <= rva < vaddr + vsize:
                return raw + rva - vaddr
        raise SystemExit('资源段地址映射失败')

    def entries(dir_off):
        named, ids = u16(data, dir_off + 12), u16(data, dir_off + 14)
        out = []
        for i in range(named + ids):
            e = dir_off + 16 + i * 8
            out.append((u32(data, e), u32(data, e + 4)))
        return out

    rsrc = rva2off(rsrc_rva)

    def leaf(off):
        """目录下第一个语言条目的数据位置与长度。"""
        _name, target = entries(off)[0]
        pos = rsrc + (target & 0x7FFFFFFF)
        return rva2off(u32(data, pos)), u32(data, pos + 4)

    return data, rsrc, entries, leaf


def largest_icon(path):
    data, rsrc, entries, leaf = read_pe_resources(path)
    types = {name: i for i, (name, _t) in enumerate(entries(rsrc))}
    if 3 not in types or 14 not in types:
        raise SystemExit('文件里没有图标资源')

    images = {}
    _name, target = entries(rsrc)[types[3]]
    for name, sub in entries(rsrc + (target & 0x7FFFFFFF)):
        off, size = leaf(rsrc + (sub & 0x7FFFFFFF))
        images[name] = data[off:off + size]

    best, best_width = None, -1
    _name, target = entries(rsrc)[types[14]]
    for _gname, sub in entries(rsrc + (target & 0x7FFFFFFF)):
        off, size = leaf(rsrc + (sub & 0x7FFFFFFF))
        group = data[off:off + size]
        count = u16(group, 4)
        items = [struct.unpack_from('<BBBBHHIH', group, 6 + i * 14) for i in range(count)]
        width = max(int(item[0]) or 256 for item in items)
        if width > best_width:
            best, best_width = items, width

    # 256x256 条目在 ICO 内以 PNG 存储，可直接作为图片使用。
    if not best:
        raise SystemExit('找不到图标条目')
    for item in best:
        width, _height, _colors, _res, _planes, _bpp, _size, image_id = item
        if (int(width) or 256) >= 256 and image_id in images:
            return images[image_id]
    raise SystemExit('找不到 256x256 图标')


def write_ico(png, path):
    header = struct.pack('<HHH', 0, 1, 1)
    entry = struct.pack('<BBBBHHII', 0, 0, 0, 0, 1, 32, len(png), 6 + 16)
    path.write_bytes(header + entry + png)


def write_png(data, dest):
    tmp = dest.with_name(f'.{dest.name}.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def main():
    exe, out_dir, *sizes = sys.argv[1:]
    sizes = sizes or ['256x256']
    png = largest_icon(exe)
    have_magick = shutil.which('magick') is not None

    icon_file = pathlib.Path('/tmp') / f'ww-icon-{os.getpid()}.ico'
    write_ico(png, icon_file)
    try:
        for size in sizes:
            if size != '256x256' and not have_magick:
                continue
            dest = pathlib.Path(out_dir) / size / 'apps' / 'ww.png'
            dest.parent.mkdir(parents=True, exist_ok=True)
            if have_magick:
                tmp = dest.with_name(f'.{dest.name}.tmp.png')
                try:
                    subprocess.run(
                        ['magick', str(icon_file), '-resize', size, f'PNG:{tmp}'],
                        check=True, capture_output=True,
                    )
                    os.replace(tmp, dest)
                except (subprocess.CalledProcessError, OSError) as exc:
                    tmp.unlink(missing_ok=True)
                    print(f'警告：ImageMagick 缩放 {size} 失败（{exc}），改用原图。', file=sys.stderr)
                    write_png(png, dest)
            else:
                write_png(png, dest)
    finally:
        icon_file.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
