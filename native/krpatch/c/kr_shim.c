//kr_shim.c
//  Thin C glue between the Rust driver and the vendored upstream HDiffPatch v4.8.0
//  patch implementation (libHDiffPatch/HPatch/patch.c).
//
//  Why a shim instead of calling patch.c directly from Rust:
//    * patch.c is compiled as-is (unmodified upstream source, guaranteed identical
//      patch semantics to the reference hpatchz binary);
//    * the zstd decompress plugin from upstream decompress_plugin_demo.h is exposed
//      as a per-thread template that Rust can memcpy (its `decError` field is
//      per-instance state, so one instance per worker thread is required);
//    * the struct layouts used by the plugin are asserted here at compile time.
#include <errno.h>   // LOG_ERRNO
#include <stddef.h>
#include <string.h>

#include "libHDiffPatch/HPatch/patch.h"

#define _CompressPlugin_zstd 1
#include "decompress_plugin_demo.h"

// Compile-time check: the Rust mirror of hpatch_TDecompress must match C exactly.
static_assert(sizeof(hpatch_TDecompress) == 6 * sizeof(void*),
               "hpatch_TDecompress layout changed; update src/ffi.rs");
static_assert(offsetof(hpatch_TDecompress, decError) == 5 * sizeof(void*),
               "hpatch_TDecompress.decError offset changed");

hpatch_size_t krpatch_plugin_struct_size(void) {
    return (hpatch_size_t)sizeof(hpatch_TDecompress);
}

// Copy the upstream zstd plugin template into caller-owned storage.
void krpatch_zstd_plugin_init(hpatch_TDecompress* out_plugin) {
    memcpy(out_plugin, &zstdDecompressPlugin, sizeof(hpatch_TDecompress));
}

// Cross-language checksum for the vendored plugin table: lets the Rust side assert
// that is_can_open really reports zstd support before any patch work starts.
hpatch_BOOL krpatch_zstd_plugin_available(void) {
    return zstdDecompressPlugin.is_can_open("zstd");
}

// Wrapper so Rust does not have to reproduce the patch.h prototypes verbatim.
hpatch_BOOL krpatch_patch_decompress_with_cache(
        const hpatch_TStreamOutput* out_newData,
        const hpatch_TStreamInput*  oldData,
        const hpatch_TStreamInput*  compressedDiff,
        hpatch_TDecompress*         decompressPlugin,
        unsigned char*              temp_cache,
        unsigned char*              temp_cache_end) {
    return patch_decompress_with_cache(out_newData, oldData, compressedDiff,
                                       decompressPlugin, temp_cache, temp_cache_end);
}

// Expose the compiled-in upstream version so `krpatch version` can print it.
const char* krpatch_hdiffpatch_version(void) {
    return HDIFFPATCH_VERSION_STRING;
}
