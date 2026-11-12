//! Build script: compiles the vendored upstream HDiffPatch v4.8.0 patch core
//! plus a small C shim, and links it into the binary.
//!
//! zstd headers/libraries come from the `zstd-sys` crate (bundled zstd sources,
//! built as a static library), so the result is a self-contained binary that does
//! not depend on a system libzstd.
use std::path::PathBuf;

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let c_dir = manifest.join("c");
    let hdiff = c_dir.join("hdiffpatch");

    println!("cargo:rerun-if-changed=c/kr_shim.c");
    println!("cargo:rerun-if-changed=c/hdiffpatch/libHDiffPatch/HPatch/patch.c");
    println!("cargo:rerun-if-changed=c/zstd/zstd.h");

    // zstd.h is vendored in c/zstd (same 1.5.7 sources that zstd-sys compiles into
    // its static library), because zstd-sys does not export its include directory.
    let zstd_include = c_dir.join("zstd");

    let mut build = cc::Build::new();
    build
        .include(&c_dir)
        .include(&hdiff)
        .include(&zstd_include)
        // Upstream sources are unmodified; silence their (intentional) warnings.
        .warnings(false)
        .flag_if_supported("-O3")
        .flag_if_supported("-fno-strict-aliasing")
        .file(hdiff.join("libHDiffPatch/HPatch/patch.c"))
        .file(c_dir.join("kr_shim.c"));

    build.compile("krpatch_c");
}
