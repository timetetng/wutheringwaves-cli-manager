//! FFI mirror of the parts of HDiffPatch v4.8.0's `patch_types.h` / `patch.h`
//! that the driver needs. The layouts are asserted on the C side in
//! `c/kr_shim.c` (`_Static_assert`), so a mismatch fails the build.
#![allow(non_camel_case_types, non_snake_case, dead_code)]

use std::os::raw::{c_char, c_void};

pub type hpatch_BOOL = i32;
pub type hpatch_StreamPos_t = u64;

pub const HPATCH_TRUE: hpatch_BOOL = 1;

/// `hpatch_TStreamInput` — 4 pointer-sized fields.
#[repr(C)]
pub struct hpatch_TStreamInput {
    pub streamImport: *mut c_void,
    pub streamSize: hpatch_StreamPos_t,
    pub read: Option<
        unsafe extern "C" fn(
            stream: *const hpatch_TStreamInput,
            readFromPos: hpatch_StreamPos_t,
            out_data: *mut u8,
            out_data_end: *mut u8,
        ) -> hpatch_BOOL,
    >,
    pub _private_reserved: *mut c_void,
}

/// `hpatch_TStreamOutput` — 4 pointer-sized fields.
#[repr(C)]
pub struct hpatch_TStreamOutput {
    pub streamImport: *mut c_void,
    pub streamSize: hpatch_StreamPos_t,
    pub read_writed: Option<
        unsafe extern "C" fn(
            stream: *const hpatch_TStreamOutput,
            readFromPos: hpatch_StreamPos_t,
            out_data: *mut u8,
            out_data_end: *mut u8,
        ) -> hpatch_BOOL,
    >,
    pub write: Option<
        unsafe extern "C" fn(
            stream: *const hpatch_TStreamOutput,
            writeToPos: hpatch_StreamPos_t,
            data: *const u8,
            data_end: *const u8,
        ) -> hpatch_BOOL,
    >,
}

/// `hpatch_TDecompress` is opaque to us: 6 pointer-sized fields, with `decError`
/// last. Rust only stores one instance per worker thread and hands the pointer to C.
#[repr(C, align(8))]
#[derive(Clone, Copy)]
pub struct hpatch_TDecompress {
    _opaque: [usize; 6],
}

impl hpatch_TDecompress {
    /// The plugin's accumulated decompress error code (`hpatch_dec_error_t`,
    /// last field of the struct; 0 means no error).
    pub fn dec_error(&self) -> u32 {
        // The field is an `int` stored in the last pointer-sized slot.
        (self._opaque[5] & 0xffff_ffff) as u32
    }
}

// Size check against the C `_Static_assert`s in c/kr_shim.c.
const _: () = assert!(std::mem::size_of::<hpatch_TDecompress>() == 6 * std::mem::size_of::<usize>());
const _: () = assert!(std::mem::size_of::<hpatch_TStreamInput>() == 4 * std::mem::size_of::<usize>());
const _: () = assert!(std::mem::size_of::<hpatch_TStreamOutput>() == 4 * std::mem::size_of::<usize>());

extern "C" {
    /// Fills `out` with a fresh copy of the upstream zstd decompress plugin
    /// (per-instance because the plugin carries `decError` state).
    pub fn krpatch_zstd_plugin_init(out: *mut hpatch_TDecompress);
    /// Returns non-zero when the compiled plugin really supports "zstd".
    pub fn krpatch_zstd_plugin_available() -> hpatch_BOOL;
    /// `patch_decompress_with_cache()` from the vendored patch.c.
    pub fn krpatch_patch_decompress_with_cache(
        out_newData: *const hpatch_TStreamOutput,
        oldData: *const hpatch_TStreamInput,
        compressedDiff: *const hpatch_TStreamInput,
        decompressPlugin: *mut hpatch_TDecompress,
        temp_cache: *mut u8,
        temp_cache_end: *mut u8,
    ) -> hpatch_BOOL;
    /// Upstream version string baked into patch_types.h, e.g. "4.8.0".
    pub fn krpatch_hdiffpatch_version() -> *const c_char;
}

/// Upstream HDiffPatch version this driver is linked against.
pub fn upstream_version() -> String {
    unsafe {
        let p = krpatch_hdiffpatch_version();
        if p.is_null() {
            return "?".to_string();
        }
        std::ffi::CStr::from_ptr(p).to_string_lossy().into_owned()
    }
}

/// A fresh, per-thread zstd decompress plugin instance.
pub fn new_zstd_plugin() -> hpatch_TDecompress {
    let mut plugin = hpatch_TDecompress { _opaque: [0; 6] };
    unsafe { krpatch_zstd_plugin_init(&mut plugin) };
    plugin
}

/// True when the linked zstd plugin accepts "zstd" diffs.
pub fn zstd_plugin_available() -> bool {
    unsafe { krpatch_zstd_plugin_available() == HPATCH_TRUE }
}
