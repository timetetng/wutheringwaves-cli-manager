//! Parser for the Kuro Games directory-diff container (`HDIFF19`).
//!
//! Layout (see `~/krpdiff/FORMAT-NOTES.md`):
//! ```text
//! "HDIFF19&" "zstd&" "fadler64\0"
//! 19 x varint head fields
//! checksumByteSize * 4 bytes   (oldRef / newRef / copyFile / newFile checksums)
//! zstd(head data)              headDataCompressedSize bytes -> headDataSize bytes
//! hdiff section                standard single-file "HDIFF13&zstd\0" block, to EOF
//! ```
//! Inside the decompressed head data KR appends two lists (`oldRefSizes`,
//! `newRefExtra`) that upstream HDiffPatch does not have, and drops nothing else;
//! parsing therefore cannot be delegated to `getSingleCompressedDiffInfo()` /
//! `_read_dirdiff_head()`.
use std::fs::File;
use std::io::{self, BufReader, Read};
use std::path::Path;

/// The 19 varint fields, in on-disk order.
#[derive(Debug, Clone, Default)]
pub struct HeadFields {
    pub old_path_is_dir: u64,
    pub new_path_is_dir: u64,
    pub old_path_count: u64,
    pub old_path_sum_size: u64,
    pub new_path_count: u64,
    pub new_path_sum_size: u64,
    pub old_ref_file_count: u64,
    pub saved_old_ref_size: u64,
    pub new_ref_file_count: u64,
    pub saved_new_ref_size: u64,
    pub same_file_pair_count: u64,
    pub same_file_size: u64,
    pub new_execute_count: u64,
    pub private_reserved_data_size: u64,
    pub private_extern_data_size: u64,
    pub extern_data_size: u64,
    pub head_data_size: u64,
    pub head_data_compressed_size: u64,
    pub checksum_byte_size: u64,
}

/// One referenced path in a group, with the byte length it contributes to the
/// concatenated old/new stream.
#[derive(Debug, Clone)]
pub struct RefEntry {
    /// Container-relative path with `/` separators, e.g. `Client/Binaries/x.dll`.
    pub path: String,
    /// Byte length of this reference inside the concatenated stream.
    pub size: u64,
    /// KR-specific 64-bit value stored per new ref (`newRefExtra`); the old ref
    /// list has no such value.
    pub extra: u64,
}

#[derive(Debug, Clone)]
pub struct GroupHead {
    pub version: String,
    pub compress_type: String,
    pub checksum_type: String,
    pub head: HeadFields,
    /// Old path table; entries ending in `/` are directories.
    pub old_refs: Vec<RefEntry>,
    pub new_refs: Vec<RefEntry>,
    /// Checksums as stored: oldRef, newRef, copyFile, newFile
    /// (each `checksum_byte_size` bytes; empty when checksums are disabled).
    pub head_checksums: Vec<Vec<u8>>,
    /// File offset where the `HDIFF13&zstd\0` section starts.
    pub hdiff_offset: u64,
    /// Length of the hdiff section (runs to EOF).
    pub hdiff_len: u64,
    /// Size of the decompressed head data.
    pub head_data_len: u64,
    /// Bytes left over after parsing the head data; must be 0 for a valid KR container.
    pub head_data_leftover: usize,
}

impl GroupHead {
    /// Sum of all new ref sizes — the number of bytes this group produces.
    pub fn new_data_size(&self) -> u64 {
        self.new_refs.iter().map(|r| r.size).sum()
    }

    /// Sum of all old ref sizes — the size of the virtual old stream.
    pub fn old_data_size(&self) -> u64 {
        self.old_refs.iter().map(|r| r.size).sum()
    }

    /// True when the container stores per-ref sizes that match the sums declared
    /// in the header (a cheap structural self-check).
    pub fn ref_sums_consistent(&self) -> bool {
        self.old_data_size() == self.head.saved_old_ref_size
            && self.new_data_size() == self.head.saved_new_ref_size
    }
}

#[derive(Debug)]
pub enum HeadError {
    Io(io::Error),
    Bad(&'static str, String),
    Truncated(&'static str),
    Zstd(String),
}

impl std::fmt::Display for HeadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HeadError::Io(e) => write!(f, "读取补丁失败: {e}"),
            HeadError::Bad(what, detail) => write!(f, "{what}: {detail}"),
            HeadError::Truncated(what) => write!(f, "补丁数据被截断（{what}）"),
            HeadError::Zstd(e) => write!(f, "head data zstd 解压失败: {e}"),
        }
    }
}

impl std::error::Error for HeadError {}

impl From<io::Error> for HeadError {
    fn from(e: io::Error) -> Self {
        if e.kind() == io::ErrorKind::UnexpectedEof {
            HeadError::Truncated("头部")
        } else {
            HeadError::Io(e)
        }
    }
}

/// Buffered reader that counts exactly how many logical bytes were consumed.
struct Rd<'a> {
    inner: BufReader<&'a File>,
    count: u64,
}

impl<'a> Rd<'a> {
    fn byte(&mut self, what: &'static str) -> Result<u8, HeadError> {
        let mut b = [0u8; 1];
        self.inner
            .read_exact(&mut b)
            .map_err(|_| HeadError::Truncated(what))?;
        self.count += 1;
        Ok(b[0])
    }

    fn exact(&mut self, n: usize) -> Result<Vec<u8>, HeadError> {
        let mut buf = vec![0u8; n];
        self.inner.read_exact(&mut buf)?;
        self.count += n as u64;
        Ok(buf)
    }

    /// String terminated by `end` (terminator consumed).
    fn type_until(&mut self, end: u8, what: &'static str) -> Result<String, HeadError> {
        let mut s = Vec::new();
        loop {
            let b = self.byte(what)?;
            if b == end {
                break;
            }
            s.push(b);
            if s.len() > (1 << 12) {
                return Err(HeadError::Bad(what, "字符串过长".into()));
            }
        }
        Ok(String::from_utf8_lossy(&s).into_owned())
    }

    /// Big-endian 7-bit varint: first byte holds the most significant group,
    /// bit 7 of each byte is the continuation flag.
    fn varint(&mut self) -> Result<u64, HeadError> {
        let mut c = self.byte("head 字段")?;
        let mut v = (c & 0x7F) as u64;
        while c & 0x80 != 0 {
            c = self.byte("head 字段")?;
            if v > (u64::MAX >> 7) {
                return Err(HeadError::Bad("varint", "溢出".into()));
            }
            v = (v << 7) | (c & 0x7F) as u64;
        }
        Ok(v)
    }
}

/// Cursor over the decompressed head data.
struct Cur<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Cur<'a> {
    fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    fn byte(&mut self) -> Result<u8, HeadError> {
        let b = *self
            .buf
            .get(self.pos)
            .ok_or(HeadError::Truncated("head data"))?;
        self.pos += 1;
        Ok(b)
    }

    fn varint(&mut self) -> Result<u64, HeadError> {
        let mut c = self.byte()?;
        let mut v = (c & 0x7F) as u64;
        while c & 0x80 != 0 {
            c = self.byte()?;
            if v > (u64::MAX >> 7) {
                return Err(HeadError::Bad("varint", "溢出".into()));
            }
            v = (v << 7) | (c & 0x7F) as u64;
        }
        Ok(v)
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], HeadError> {
        if self.remaining() < n {
            return Err(HeadError::Truncated("head data 字段"));
        }
        let s = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }

    /// HDiffPatch "inc" list: each item is `prev + 1 + delta`, starting at -1.
    fn inc_list(&mut self, count: usize, max_value: u64) -> Result<Vec<usize>, HeadError> {
        let mut out = Vec::with_capacity(count);
        let mut back: i64 = -1;
        for _ in 0..count {
            let delta = self.varint()?;
            back = back
                .checked_add(delta as i64 + 1)
                .ok_or_else(|| HeadError::Bad("ref 列表", "索引溢出".into()))?;
            if back < 0 || back as u64 >= max_value {
                return Err(HeadError::Bad(
                    "ref 列表",
                    format!("索引 {back} 超出路径表 {max_value}"),
                ));
            }
            out.push(back as usize);
        }
        Ok(out)
    }
}

/// Normalise a container path into a safe relative path with `/` separators.
/// Returns `None` for the empty root entry and for anything that could escape
/// the output directory.
pub fn normalize_rel_path(raw: &str) -> Option<String> {
    let p = raw.replace('\\', "/");
    let trimmed = p.trim_end_matches('/');
    if trimmed.is_empty() || trimmed.starts_with('/') || trimmed.contains('\0') {
        return None;
    }
    if trimmed.len() > 1 && trimmed.as_bytes()[1] == b':' {
        return None; // drive-letter path
    }
    for seg in trimmed.split('/') {
        if seg.is_empty() || seg == ".." || seg == "." {
            return None;
        }
    }
    Some(trimmed.to_string())
}

fn refs_from(
    path_table: &[String],
    indices: &[usize],
    sizes: &[u64],
    extras: Option<&[u64]>,
) -> Vec<RefEntry> {
    let mut out = Vec::with_capacity(indices.len());
    for (i, &pi) in indices.iter().enumerate() {
        out.push(RefEntry {
            path: normalize_rel_path(&path_table[pi]).unwrap_or_else(|| path_table[pi].clone()),
            size: sizes[i],
            extra: extras.map(|e| e[i]).unwrap_or(0),
        });
    }
    out
}

/// Parse the container header of `patch_path`. Only the (small) compressed head
/// data is read into memory; the hdiff section is left on disk.
pub fn parse(patch_path: &Path) -> Result<GroupHead, HeadError> {
    let file = File::open(patch_path)?;
    let file_len = file.metadata()?.len();
    let mut rd = Rd {
        inner: BufReader::with_capacity(64 << 10, &file),
        count: 0,
    };

    let version = rd.type_until(b'&', "格式版本")?;
    if version != "HDIFF19" {
        return Err(HeadError::Bad(
            "格式版本",
            format!("不支持的容器版本 {version:?}（需要 HDIFF19）"),
        ));
    }
    let compress_type = rd.type_until(b'&', "压缩类型")?;
    let checksum_type = rd.type_until(0, "校验类型")?;

    let head = HeadFields {
        old_path_is_dir: rd.varint()?,
        new_path_is_dir: rd.varint()?,
        old_path_count: rd.varint()?,
        old_path_sum_size: rd.varint()?,
        new_path_count: rd.varint()?,
        new_path_sum_size: rd.varint()?,
        old_ref_file_count: rd.varint()?,
        saved_old_ref_size: rd.varint()?,
        new_ref_file_count: rd.varint()?,
        saved_new_ref_size: rd.varint()?,
        same_file_pair_count: rd.varint()?,
        same_file_size: rd.varint()?,
        new_execute_count: rd.varint()?,
        private_reserved_data_size: rd.varint()?,
        private_extern_data_size: rd.varint()?,
        extern_data_size: rd.varint()?,
        head_data_size: rd.varint()?,
        head_data_compressed_size: rd.varint()?,
        checksum_byte_size: rd.varint()?,
    };

    let cs = head.checksum_byte_size as usize;
    if cs != 0 && cs != 4 && cs != 8 {
        return Err(HeadError::Bad(
            "校验长度",
            format!("意外的 checksumByteSize={cs}"),
        ));
    }
    let csum_buf = rd.exact(cs * 4)?;
    let head_checksums: Vec<Vec<u8>> = if cs == 0 {
        Vec::new()
    } else {
        (0..4)
            .map(|i| csum_buf[i * cs..(i + 1) * cs].to_vec())
            .collect()
    };

    if head.head_data_compressed_size > file_len {
        return Err(HeadError::Bad(
            "head data",
            format!(
                "headDataCompressedSize={} 超过补丁长度 {file_len}",
                head.head_data_compressed_size
            ),
        ));
    }
    let head_blob = rd.exact(head.head_data_compressed_size as usize)?;
    let hdiff_offset = rd.count;
    if hdiff_offset >= file_len {
        return Err(HeadError::Truncated("hdiff 段"));
    }

    let head_data = decompress_head(&head_blob, head.head_data_size)?;
    let (old_refs, new_refs, leftover) = parse_head_data(&head, &head_data)?;

    Ok(GroupHead {
        version,
        compress_type,
        checksum_type,
        old_refs,
        new_refs,
        head_data_len: head.head_data_size,
        head_data_leftover: leftover,
        head_checksums,
        hdiff_offset,
        hdiff_len: file_len - hdiff_offset,
        head,
    })
}

fn decompress_head(blob: &[u8], size: u64) -> Result<Vec<u8>, HeadError> {
    use zstd::zstd_safe::DParameter;
    let mut dec = zstd::bulk::Decompressor::new().map_err(|e| HeadError::Zstd(e.to_string()))?;
    // The producer may use a window larger than the crate's default limit.
    let _ = dec.set_parameter(DParameter::WindowLogMax(31));
    dec.decompress(blob, size as usize)
        .map_err(|e| HeadError::Zstd(e.to_string()))
}

/// Ref entries plus the number of bytes left unparsed in the head data block.
type ParsedHead = (Vec<RefEntry>, Vec<RefEntry>, usize);

fn parse_head_data(head: &HeadFields, data: &[u8]) -> Result<ParsedHead, HeadError> {
    let mut c = Cur {
        buf: data,
        pos: 0,
    };
    let path_sum = (head.old_path_sum_size + head.new_path_sum_size) as usize;
    let path_block = c.take(path_sum)?;
    if path_sum > 0 && *path_block.last().unwrap_or(&1) != 0 {
        return Err(HeadError::Bad("路径表", "块末尾缺少 NUL".into()));
    }
    let mut all_paths: Vec<String> = path_block
        .split(|b| *b == 0)
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect();
    // split() yields a trailing empty element for the final NUL.
    if all_paths.last().map(|s| s.is_empty()).unwrap_or(false) {
        all_paths.pop();
    }
    let old_count = head.old_path_count as usize;
    let new_count = head.new_path_count as usize;
    if all_paths.len() != old_count + new_count {
        return Err(HeadError::Bad(
            "路径表",
            format!(
                "解析出 {} 个路径，头部声明 {old_count} + {new_count}",
                all_paths.len()
            ),
        ));
    }
    let new_paths = all_paths.split_off(old_count);
    let old_paths = all_paths;

    let old_idx = c.inc_list(head.old_ref_file_count as usize, head.old_path_count)?;
    let new_idx = c.inc_list(head.new_ref_file_count as usize, head.new_path_count)?;

    let mut old_sizes = Vec::with_capacity(old_idx.len());
    for _ in 0..old_idx.len() {
        old_sizes.push(c.varint()?);
    }
    let mut new_sizes = Vec::with_capacity(new_idx.len());
    for _ in 0..new_idx.len() {
        new_sizes.push(c.varint()?);
    }
    let mut new_extras = Vec::with_capacity(new_idx.len());
    for _ in 0..new_idx.len() {
        new_extras.push(c.varint()?);
    }
    // sameFilePairList / newExecuteList are parsed for validation but not needed
    // to apply a group (same-file copies are already expanded by the producer).
    for _ in 0..head.same_file_pair_count {
        let _ = c.varint()?;
    }
    let _new_exec = c.inc_list(head.new_execute_count as usize, head.new_path_count)?;

    let leftover = c.remaining();
    let old_refs = refs_from(&old_paths, &old_idx, &old_sizes, None);
    let new_refs = refs_from(&new_paths, &new_idx, &new_sizes, Some(&new_extras));
    Ok((old_refs, new_refs, leftover))
}
