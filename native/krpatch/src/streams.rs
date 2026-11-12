//! Byte-stream plumbing for HDiffPatch's C `hpatch_TStreamInput/Output`:
//!
//! * `SegRead`  — many old files addressed as one contiguous virtual file
//!   (group refs are concatenated in `oldRefSizes` order);
//! * `DiffRead` — the hdiff section of the `.krpdiff` container;
//! * `SegWrite` — many new files addressed as one contiguous virtual file,
//!   with per-file MD5 computed while data is being written.
//!
//! Everything uses positional I/O (`pread`/`pwrite`), so a single group can be
//! applied by several threads' worth of I/O without any shared cursor.
use std::fs::{File, OpenOptions};
use std::io;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use md5::{Digest, Md5};

use crate::ffi::{
    hpatch_TStreamInput, hpatch_TStreamOutput, hpatch_StreamPos_t, HPATCH_TRUE,
};

/// Which virtual segment a byte offset falls into.
fn locate(starts: &[u64], pos: u64) -> Option<usize> {
    match starts.binary_search(&pos) {
        Ok(i) => Some(i),
        Err(0) => None,
        Err(i) => Some(i - 1),
    }
}

// ---------------------------------------------------------------- read side

struct ReadSeg {
    file: File,
    /// Base offset inside `file` where this segment starts.
    base: u64,
    len: u64,
}

/// Concatenation of several read-only files.
pub struct SegRead {
    segs: Vec<ReadSeg>,
    starts: Vec<u64>,
    total: u64,
}

impl SegRead {
    pub fn new() -> Self {
        SegRead {
            segs: Vec::new(),
            starts: Vec::new(),
            total: 0,
        }
    }

    /// Append `(path, expected_size)`; the file length is validated up front so a
    /// mismatching old file fails with a readable error instead of a patch error.
    pub fn push_file(&mut self, path: &Path, expected_size: u64) -> io::Result<()> {
        let file = File::open(path).map_err(|e| {
            io::Error::new(
                e.kind(),
                format!("无法打开旧文件 {}: {e}", path.display()),
            )
        })?;
        let len = file.metadata()?.len();
        if len != expected_size {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "旧文件大小不符 {}: 实际 {len}，补丁要求 {expected_size}",
                    path.display()
                ),
            ));
        }
        self.starts.push(self.total);
        self.total += len;
        self.segs.push(ReadSeg { file, base: 0, len });
        Ok(())
    }

    /// Read up to `n` bytes from the start (used for the small hdiff section header).
    pub fn read_head(&self, n: usize) -> io::Result<Vec<u8>> {
        let n = std::cmp::min(n as u64, self.total) as usize;
        let mut buf = vec![0u8; n];
        self.read_into(0, &mut buf)?;
        Ok(buf)
    }
    pub fn from_file_region(path: &Path, offset: u64, len: u64) -> io::Result<Self> {
        let file = File::open(path)?;
        let meta_len = file.metadata()?.len();
        if offset.saturating_add(len) > meta_len {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "补丁段越界 {}: offset {offset} + len {len} > {meta_len}",
                    path.display()
                ),
            ));
        }
        let mut s = SegRead::new();
        s.starts.push(0);
        s.total = len;
        s.segs.push(ReadSeg {
            file,
            base: offset,
            len,
        });
        Ok(s)
    }

    fn read_into(&self, mut pos: u64, mut buf: &mut [u8]) -> io::Result<()> {
        while !buf.is_empty() {
            let i = locate(&self.starts, pos).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("读位置 {pos} 超出流长度 {}", self.total),
                )
            })?;
            let seg = &self.segs[i];
            let off_in = pos - self.starts[i];
            let avail = seg.len.saturating_sub(off_in);
            if avail == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("读位置 {pos} 落在空区间"),
                ));
            }
            let n = std::cmp::min(avail as usize, buf.len());
            seg.file.read_exact_at(&mut buf[..n], seg.base + off_in)?;
            buf = &mut buf[n..];
            pos += n as u64;
        }
        Ok(())
    }
}

unsafe extern "C" fn seg_read(
    stream: *const hpatch_TStreamInput,
    read_from_pos: hpatch_StreamPos_t,
    out_data: *mut u8,
    out_data_end: *mut u8,
) -> i32 {
    let len = (out_data_end as usize).wrapping_sub(out_data as usize);
    let ctx = &*((*stream).streamImport as *const SegRead);
    let buf = std::slice::from_raw_parts_mut(out_data, len);
    match ctx.read_into(read_from_pos, buf) {
        Ok(()) => HPATCH_TRUE,
        Err(_) => 0,
    }
}

/// Owns a [`SegRead`] together with the C-visible stream struct.
pub struct InStream {
    pub raw: hpatch_TStreamInput,
    /// Keeps the `SegRead` alive: `raw.streamImport` points into this box.
    _owner: Box<SegRead>,
}

impl InStream {
    pub fn new(sr: SegRead) -> Self {
        let mut owner = Box::new(sr);
        let raw = hpatch_TStreamInput {
            streamImport: (&mut *owner as *mut SegRead).cast(),
            streamSize: owner.total,
            read: Some(seg_read),
            _private_reserved: std::ptr::null_mut(),
        };
        InStream { raw, _owner: owner }
    }

    pub fn as_ptr(&self) -> *const hpatch_TStreamInput {
        &self.raw
    }
}

// --------------------------------------------------------------- write side

/// One produced file.
pub struct OutFile {
    pub rel: String,
    pub abs: PathBuf,
    file: File,
    len: u64,
    hasher: Md5,
    hashed: u64,
    need_rehash: bool,
}

impl OutFile {
    pub fn new(rel: String, abs: PathBuf, len: u64) -> io::Result<Self> {
        if let Some(dir) = abs.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&abs)
            .map_err(|e| {
                io::Error::new(e.kind(), format!("无法创建输出文件 {}: {e}", abs.display()))
            })?;
        file.set_len(len)?;
        Ok(OutFile {
            rel,
            abs,
            file,
            len,
            hasher: Md5::new(),
            hashed: 0,
            need_rehash: false,
        })
    }

    fn write_at(&mut self, off_in: u64, data: &[u8]) -> io::Result<()> {
        self.file.write_all_at(data, off_in)?;
        if !self.need_rehash {
            if off_in == self.hashed {
                self.hasher.update(data);
                self.hashed += data.len() as u64;
            } else if off_in < self.hashed {
                // Rewritten region (should not happen for linear patches).
                self.hasher = Md5::new();
                self.hashed = 0;
                self.need_rehash = true;
            } else {
                // Gap: fall back to hashing the finished file.
                self.need_rehash = true;
            }
        }
        Ok(())
    }
}

pub struct FileOutcome {
    pub rel: String,
    pub size: u64,
    pub md5: String,
}

/// Concatenation of several write-only files.
pub struct SegWrite {
    segs: Vec<OutFile>,
    starts: Vec<u64>,
    total: u64,
    counter: Arc<AtomicU64>,
}

impl SegWrite {
    pub fn new(counter: Arc<AtomicU64>) -> Self {
        SegWrite {
            segs: Vec::new(),
            starts: Vec::new(),
            total: 0,
            counter,
        }
    }

    pub fn push_file(&mut self, rel: String, abs: PathBuf, len: u64) -> io::Result<()> {
        self.starts.push(self.total);
        self.total += len;
        self.segs.push(OutFile::new(rel, abs, len)?);
        Ok(())
    }

    fn write_into(&mut self, mut pos: u64, mut data: &[u8]) -> io::Result<()> {
        while !data.is_empty() {
            let i = locate(&self.starts, pos).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("写位置 {pos} 超出流长度 {}", self.total),
                )
            })?;
            let off_in = pos - self.starts[i];
            let seg = &mut self.segs[i];
            let avail = seg.len.saturating_sub(off_in);
            let n = std::cmp::min(avail as usize, data.len());
            if n == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("写位置 {pos} 越过文件末尾"),
                ));
            }
            seg.write_at(off_in, &data[..n])?;
            data = &data[n..];
            pos += n as u64;
            self.counter.fetch_add(n as u64, Ordering::Relaxed);
        }
        Ok(())
    }

    /// Flush, hash the files that were written out of order, and report results.
    pub fn finish(mut self, fsync: bool) -> io::Result<Vec<FileOutcome>> {
        let mut out = Vec::with_capacity(self.segs.len());
        for seg in self.segs.drain(..) {
            if fsync {
                seg.file.sync_all()?;
            }
            let mut seg = seg;
            let md5 = if seg.need_rehash || seg.hashed != seg.len {
                hash_file(&seg.abs)?
            } else {
                let digest = std::mem::take(&mut seg.hasher).finalize();
                hex32(&digest)
            };
            out.push(FileOutcome {
                rel: seg.rel.clone(),
                size: seg.len,
                md5,
            });
        }
        Ok(out)
    }
}

unsafe extern "C" fn seg_write(
    stream: *const hpatch_TStreamOutput,
    write_to_pos: hpatch_StreamPos_t,
    data: *const u8,
    data_end: *const u8,
) -> i32 {
    let len = (data_end as usize).wrapping_sub(data as usize);
    let ctx = &mut *((*stream).streamImport as *mut SegWrite);
    let slice = std::slice::from_raw_parts(data, len);
    match ctx.write_into(write_to_pos, slice) {
        Ok(()) => HPATCH_TRUE,
        Err(_) => 0,
    }
}

/// Owns a [`SegWrite`] together with the C-visible stream struct.
pub struct OutStream {
    pub raw: hpatch_TStreamOutput,
    owner: Box<SegWrite>,
}

impl OutStream {
    pub fn new(sw: SegWrite) -> Self {
        let total = sw.total;
        let mut owner = Box::new(sw);
        let raw = hpatch_TStreamOutput {
            streamImport: (&mut *owner as *mut SegWrite).cast(),
            streamSize: total,
            read_writed: None,
            write: Some(seg_write),
        };
        OutStream { raw, owner }
    }

    pub fn as_ptr(&self) -> *const hpatch_TStreamOutput {
        &self.raw
    }

    pub fn finish(self, fsync: bool) -> io::Result<Vec<FileOutcome>> {
        self.owner.finish(fsync)
    }
}

/// Sequential MD5 of a finished file (only used when writes were reordered).
fn hash_file(path: &Path) -> io::Result<String> {
    use std::io::Read;
    let mut f = File::open(path)?;
    let mut h = Md5::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex32(&h.finalize()))
}

pub fn hex32(digest: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(digest.len() * 2);
    for b in digest {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0xf) as usize] as char);
    }
    s
}
