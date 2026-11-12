//! Applying one `.krpdiff` group: virtual old stream -> vendored C patcher ->
//! virtual new stream, split into files on the way out.
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicU64;
use std::sync::Arc;
use std::time::Instant;

use crate::ffi;
use crate::head::GroupHead;
use crate::streams::{FileOutcome, InStream, OutStream, SegRead, SegWrite};

/// Result of applying one group.
pub struct GroupOutcome {
    pub patch: PathBuf,
    pub group: String,
    pub files: Vec<FileOutcome>,
    pub old_data_size: u64,
    pub new_data_size: u64,
    pub secs: f64,
}

impl GroupOutcome {
    /// Bytes per second for this group.
    pub fn throughput(&self) -> f64 {
        if self.secs > 0.0 {
            self.new_data_size as f64 / self.secs
        } else {
            0.0
        }
    }
}

pub struct ApplyOptions {
    pub old_root: PathBuf,
    pub out_root: PathBuf,
    /// Memory cache handed to `patch_decompress_with_cache()`.
    pub cache_bytes: usize,
    pub fsync: bool,
    /// Compute per-file MD5 while writing (the manager uses it to decide
    /// whether the result can be kept). `--no-verify` turns this off.
    pub verify: bool,
    /// Global progress counter (bytes written across all groups).
    pub counter: Arc<AtomicU64>,
}

/// Minimal view of the standard `HDIFF13&zstd\0` section header, used as a
/// structural check before handing the section to the C patcher.
struct Diff13Head {
    compress_type: String,
    new_data_size: u64,
    old_data_size: u64,
    cover_count: u64,
    /// Offset of the first data block after the header.
    head_end: u64,
    /// Sum of the five data blocks (compressed size where a block is compressed).
    body_len: u64,
}

fn varint(buf: &[u8], pos: &mut usize) -> Result<u64, String> {
    let mut c = *buf
        .get(*pos)
        .ok_or_else(|| "HDIFF13 头被截断".to_string())?;
    *pos += 1;
    let mut v = (c & 0x7F) as u64;
    while c & 0x80 != 0 {
        c = *buf
            .get(*pos)
            .ok_or_else(|| "HDIFF13 头被截断".to_string())?;
        *pos += 1;
        v = (v << 7) | (c & 0x7F) as u64;
    }
    Ok(v)
}

fn parse_diff13_head(diff: &SegRead) -> Result<Diff13Head, String> {
    let buf = diff
        .read_head(256)
        .map_err(|e| format!("读取 hdiff 段头失败: {e}"))?;
    let mut pos = 0usize;
    let mut next_type = |end: u8, what: &str| -> Result<String, String> {
        let start = pos;
        while *buf
            .get(pos)
            .ok_or_else(|| format!("HDIFF13 头缺少 {what} 结束符"))?
            != end
        {
            pos += 1;
        }
        let s = String::from_utf8_lossy(&buf[start..pos]).into_owned();
        pos += 1;
        Ok(s)
    };
    let version = next_type(b'&', "版本")?;
    if version != "HDIFF13" {
        return Err(format!(
            "hdiff 段版本不是 HDIFF13 而是 {version:?}（容器可能已变化）"
        ));
    }
    let compress_type = next_type(0, "压缩类型")?;
    let new_data_size = varint(&buf, &mut pos)?;
    let old_data_size = varint(&buf, &mut pos)?;
    let cover_count = varint(&buf, &mut pos)?;
    let mut sizes = [0u64; 8];
    for s in sizes.iter_mut() {
        *s = varint(&buf, &mut pos)?;
    }
    let [cover, cover_c, rle_ctrl, rle_ctrl_c, rle_code, rle_code_c, ndd, ndd_c] = sizes;
    let pick = |plain: u64, compressed: u64| if compressed > 0 { compressed } else { plain };
    let body_len = pick(cover, cover_c)
        + pick(rle_ctrl, rle_ctrl_c)
        + pick(rle_code, rle_code_c)
        + pick(ndd, ndd_c);
    Ok(Diff13Head {
        compress_type,
        new_data_size,
        old_data_size,
        cover_count,
        head_end: pos as u64,
        body_len,
    })
}

/// Apply one group whose header has already been parsed (the driver parses all
/// containers up front so it can report the total workload and detect conflicts).
pub fn apply_head(
    patch: &Path,
    head: &GroupHead,
    opts: &ApplyOptions,
) -> Result<GroupOutcome, String> {
    let started = Instant::now();
    validate_head(head)?;

    let diff = SegRead::from_file_region(patch, head.hdiff_offset, head.hdiff_len)
        .map_err(|e| format!("打开 hdiff 段失败: {e}"))?;
    let dh = parse_diff13_head(&diff)?;
    if dh.compress_type != "zstd" {
        return Err(format!(
            "hdiff 段压缩类型为 {:?}，本工具只支持 zstd",
            dh.compress_type
        ));
    }
    let old_sum = head.old_data_size();
    let new_sum = head.new_data_size();
    if dh.old_data_size != old_sum || dh.new_data_size != new_sum {
        return Err(format!(
            "段头与容器头部不一致: hdiff(old={}, new={}) vs head(old={old_sum}, new={new_sum})",
            dh.old_data_size, dh.new_data_size
        ));
    }
    let expect_len = dh.head_end + dh.body_len;
    if expect_len != head.hdiff_len {
        return Err(format!(
            "hdiff 段长度不符（补丁可能未下载完整）: 头部声明 {expect_len} 字节，实际 {}",
            head.hdiff_len
        ));
    }

    // Virtual old stream: every referenced file in ref order.
    let mut old = SegRead::new();
    for r in &head.old_refs {
        old.push_file(&opts.old_root.join(&r.path), r.size)
            .map_err(|e| e.to_string())?;
    }
    // Virtual new stream: the group's outputs, split by newRefSizes.
    let mut writer = SegWrite::new(opts.counter.clone(), opts.verify);
    for r in &head.new_refs {
        writer
            .push_file(r.path.clone(), opts.out_root.join(&r.path), r.size)
            .map_err(|e| e.to_string())?;
    }

    let cache_bytes = opts.cache_bytes.max(4 << 20);
    let mut cache = vec![0u8; cache_bytes];
    let in_diff = InStream::new(diff);
    let in_old = InStream::new(old);
    let out = OutStream::new(writer);
    let mut plugin = ffi::new_zstd_plugin();

    let ok = unsafe {
        ffi::krpatch_patch_decompress_with_cache(
            out.as_ptr(),
            in_old.as_ptr(),
            in_diff.as_ptr(),
            &mut plugin,
            cache.as_mut_ptr(),
            cache.as_mut_ptr().add(cache.len()),
        )
    };
    let dec_error = plugin.dec_error();
    let files = out.finish(opts.fsync).map_err(|e| e.to_string())?;
    if ok != ffi::HPATCH_TRUE {
        return Err(if dec_error != 0 {
            format!("patch 失败（zstd 解压错误码 {dec_error}，片段 {} 个）", dh.cover_count)
        } else {
            format!("patch 失败（hdiff 校验或数据错误，片段 {} 个）", dh.cover_count)
        });
    }

    Ok(GroupOutcome {
        patch: patch.to_path_buf(),
        group: group_name(patch),
        files,
        old_data_size: old_sum,
        new_data_size: new_sum,
        secs: started.elapsed().as_secs_f64(),
    })
}

fn validate_head(head: &GroupHead) -> Result<(), String> {
    if head.compress_type != "zstd" {
        return Err(format!(
            "容器压缩类型为 {:?}，本工具只支持 zstd",
            head.compress_type
        ));
    }
    if head.head_data_leftover != 0 {
        return Err(format!(
            "head data 解析后剩余 {} 字节（预期 0）",
            head.head_data_leftover
        ));
    }
    if !head.ref_sums_consistent() {
        return Err(format!(
            "ref 大小之和与头部声明不一致（old {} vs {}、new {} vs {}）",
            head.old_data_size(),
            head.head.saved_old_ref_size,
            head.new_data_size(),
            head.head.saved_new_ref_size
        ));
    }
    if head.new_refs.is_empty() {
        return Err("该分组没有任何输出文件".to_string());
    }
    for p in head
        .old_refs
        .iter()
        .map(|r| &r.path)
        .chain(head.new_refs.iter().map(|r| &r.path))
    {
        if p.starts_with('/') || p.contains("..") {
            return Err(format!("补丁中的路径不安全: {p:?}"));
        }
    }
    Ok(())
}

/// Derive a short group label from a patch file name, e.g. `3.7.1_3.8.0_group_16_1793.krpdiff`
/// becomes `group_16`, and anything else falls back to the file stem.
pub fn group_name(patch: &Path) -> String {
    let stem = patch
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| patch.display().to_string());
    let stem = stem.trim_end_matches(".krpdiff");
    if let Some(idx) = stem.find("_group_") {
        let rest = &stem[idx + 1..];
        let end = rest
            .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
            .unwrap_or(rest.len());
        return rest[..end].to_string();
    }
    stem.to_string()
}
