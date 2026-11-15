"""原生补丁器 krpatch 接线回归；用假的 krpatch 脚本，不联网、不碰真实游戏。"""

import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ww_manager.incremental import (
    KRPATCH_VERSION,
    IncrementalManager,
    _resource_rel_path,
    find_cached_krpatch,
    get_krpatch_path,
)

PATCH_NAME = "3.7.1_3.8.0_group_7_1720000000000.krpdiff"
SRC = "Client/Content/HD/data.pak"
DST = "Client/Saved/Resources/3.8.0/Option_Pack_HD/Base/data.pak"
SRC_PAYLOAD = b"old-release-data"
# 假 krpatch 约定：输出内容 = b"new:" + dest
DST_PAYLOAD = ("new:" + DST).encode()

FAKE_KRPATCH = """#!{python}
import hashlib, json, os, sys, time
from pathlib import Path

def main():
    argv = sys.argv[1:]
    if not argv or argv[0] != "apply":
        print("fake krpatch: 只支持 apply", file=sys.stderr)
        return 2
    opts, patches, i = {{}}, [], 1
    while i < len(argv):
        a = argv[i]
        if a in ("--old-root", "--out-root", "--expect", "--json", "--workers", "--cache-mb"):
            opts[a] = argv[i + 1]
            i += 2
        else:
            patches.append(a)
            i += 1
    mode = os.environ.get("FAKE_KRPATCH_MODE", "ok")
    marker = os.environ.get("FAKE_KRPATCH_MARKER")
    if marker:
        Path(marker).write_text(" ".join(argv), encoding="utf-8")
        with open(marker, "a", encoding="utf-8") as f:
            f.write("\\n" + str(sorted(opts)) + "\\n" + str(patches))
    if mode == "sleep":
        time.sleep(30)
    out_root = Path(opts["--out-root"])
    entries = []
    for line in Path(opts["--expect"]).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        path, size, md5 = line.split("\\t")
        entries.append((path, int(size), md5))
    files, mismatch = [], []
    if mode != "fail":
        for index, (path, size, md5) in enumerate(entries):
            body = ("new:" + path).encode()
            if mode == "mismatch" and index == 0:
                body = b"corrupted-output"
            if mode == "missing" and index == 0:
                files.append({{"path": path, "size": size, "md5": None,
                              "expected_size": size, "expected_md5": md5,
                              "matched": None}})
                continue
            target = out_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            got = hashlib.md5(body, usedforsecurity=False).hexdigest()
            ok = got == md5.lower() and len(body) == size
            if not ok:
                mismatch.append(path)
            files.append({{"path": path, "size": size, "md5": got,
                          "expected_size": size, "expected_md5": md5,
                          "matched": ok}})
    failed = mode == "fail"
    result = {{
        "ok": not mismatch and not failed,
        "workers": 1,
        "cache_mb": 8,
        "verify": True,
        "expect_checked": True,
        "total_secs": 0.01,
        "groups": [{{
            "group": "group_7",
            "patch": patches[0] if patches else "",
            "ok": not mismatch and not failed,
            "status": "failed" if failed else ("ok" if not mismatch else "mismatch"),
            "old_size": 0,
            "new_size": sum(e[1] for e in entries),
            "secs": 0.01,
            "mismatch": mismatch,
            "expect_missing": [],
            "files": files,
        }}],
    }}
    if failed:
        result["groups"][0]["error"] = "旧文件缺失（模拟）"
    Path(opts["--json"]).write_text(json.dumps(result), encoding="utf-8")
    print("50.0%  1.0 MiB / 2.0 MiB  10 MiB/s", file=sys.stderr)
    return 3 if mismatch else (1 if failed else 0)

sys.exit(main())
"""


def file_item(dest, payload):
    return {"dest": dest, "size": len(payload), "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest()}


class KrpatchApplyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ww-krpatch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.game = self.root / "game"
        self.download_dir = self.game / ".incremental_download"
        self.manager = IncrementalManager(self.game, "cn", {}, "http://127.0.0.1:1/")
        self.manager.applier = "krpatch"

        fake = self.root / "krpatch"
        fake.write_text(FAKE_KRPATCH.format(python=sys.executable), encoding="utf-8")
        fake.chmod(0o755)
        self.fake = fake
        self.manager.krpatch_path = fake
        self.marker = self.root / "invoked.txt"

        self.game_path = self.game / _resource_rel_path(SRC)
        self.game_path.parent.mkdir(parents=True, exist_ok=True)
        self.game_path.write_bytes(SRC_PAYLOAD)
        patch_path = self.download_dir / _resource_rel_path(PATCH_NAME)
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_bytes(b"krpdiff-bytes")

        self.group = {
            "dest": PATCH_NAME,
            "srcFiles": [file_item(SRC, SRC_PAYLOAD)],
            "dstFiles": [file_item(DST, DST_PAYLOAD)],
        }
        self.fallbacks = []
        fallback = patch.object(
            IncrementalManager, "_download_full_file_to_stage", autospec=True, side_effect=self._record_fallback
        )
        fallback.start()
        self.addCleanup(fallback.stop)
        env = patch.dict(os.environ, {"FAKE_KRPATCH_MARKER": str(self.marker)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FAKE_KRPATCH_MODE", None)
        self.addCleanup(os.environ.pop, "FAKE_KRPATCH_MODE", None)
        timeout = patch.dict(os.environ, {"WW_KRPATCH_TIMEOUT": "5"})
        timeout.start()
        self.addCleanup(timeout.stop)

    def _record_fallback(self, engine, dst_info, staged_path, log_error):
        self.fallbacks.append(dst_info["dest"])
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(b"downloaded-full-file")
        return True

    def staged_path(self, dest=DST):
        return self.manager._get_staged_patch_path(self.download_dir, dest)

    def apply(self):
        return self.manager._apply_single_group(
            self.group, self.download_dir, lambda _msg: None, lambda _msg: None, lambda _msg: None
        )

    def test_success_stages_verified_output(self):
        name, status = self.apply()
        self.assertEqual((name, status), (PATCH_NAME, "success"))
        self.assertEqual(self.staged_path().read_bytes(), DST_PAYLOAD)
        self.assertEqual(self.fallbacks, [])
        self.assertIn(DST, self.manager._verified_staged_patch_dests)

    def test_command_uses_game_root_and_official_expect(self):
        self.apply()
        argv = self.marker.read_text(encoding="utf-8")
        # 不再 copy 源文件：old-root 直接指向游戏目录
        self.assertIn(f"--old-root {self.game}", argv)
        self.assertIn("--expect", argv)
        self.assertIn("--json", argv)

    def test_mismatch_falls_back_only_that_file(self):
        os.environ["FAKE_KRPATCH_MODE"] = "mismatch"
        name, status = self.apply()
        self.assertEqual(status, "success")
        self.assertEqual(self.fallbacks, [DST])
        self.assertEqual(self.staged_path().read_bytes(), b"downloaded-full-file")

    def test_missing_output_falls_back(self):
        os.environ["FAKE_KRPATCH_MODE"] = "missing"
        _, status = self.apply()
        self.assertEqual(status, "success")
        self.assertEqual(self.fallbacks, [DST])

    def test_group_failure_falls_back_whole_group(self):
        os.environ["FAKE_KRPATCH_MODE"] = "fail"
        _, status = self.apply()
        self.assertEqual(status, "success")
        self.assertEqual(self.fallbacks, [DST])

    def test_missing_source_skips_krpatch_entirely(self):
        self.game_path.unlink()
        _, status = self.apply()
        self.assertEqual(status, "success")
        self.assertEqual(self.fallbacks, [DST])
        self.assertFalse(self.marker.exists(), "源文件缺失时不应再调用 krpatch")

    def test_missing_source_restores_backup(self):
        backup = self.game_path.with_suffix(self.game_path.suffix + ".bak")
        self.game_path.rename(backup)
        _, status = self.apply()
        self.assertEqual(status, "success")
        self.assertEqual(self.fallbacks, [])
        self.assertFalse(backup.exists())

    def test_timeout_falls_back(self):
        os.environ["FAKE_KRPATCH_MODE"] = "sleep"
        os.environ["WW_KRPATCH_TIMEOUT"] = "1"
        started = time.monotonic()
        _, status = self.apply()
        self.assertEqual(status, "success")
        self.assertEqual(self.fallbacks, [DST])
        self.assertLess(time.monotonic() - started, 25, "超时应按 WW_KRPATCH_TIMEOUT 及时返回")


class KrpatchSelectionTests(unittest.TestCase):
    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "my-krpatch"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            with patch.dict(os.environ, {"WW_KRPATCH": str(binary)}):
                self.assertEqual(find_cached_krpatch(Path(tmp) / "cache"), binary)

    def test_cached_binary_prevents_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            binary = cache / "krpatch"
            binary.write_bytes(b"\x7fELF")
            binary.chmod(0o755)
            (cache / "krpatch.version").write_text(KRPATCH_VERSION + "\n")
            with (
                patch.dict(os.environ, {"WW_KRPATCH": ""}),
                patch("ww_manager.incremental.shutil.which", return_value=None),
                patch("ww_manager.incremental.requests.get", side_effect=AssertionError("不应联网")),
            ):
                self.assertEqual(get_krpatch_path(cache), binary)

    def test_forced_hpatchz_skips_krpatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = IncrementalManager(Path(tmp), "cn", {}, "http://127.0.0.1:1/")
            with (
                patch.dict(os.environ, {"WW_APPLIER": "hpatchz"}),
                patch("ww_manager.incremental.get_krpatch_path", side_effect=AssertionError("不应尝试 krpatch")),
                patch("ww_manager.incremental.check_hpatchz_requirements", return_value=(True, "")),
            ):
                self.assertEqual(manager._select_applier(), "hpatchz")

    def test_no_applier_reports_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = IncrementalManager(Path(tmp), "cn", {}, "http://127.0.0.1:1/")
            with (
                patch.dict(os.environ, {"WW_APPLIER": ""}),
                patch("ww_manager.incremental.get_krpatch_path", return_value=None),
                patch("ww_manager.incremental.check_hpatchz_requirements", return_value=(False, "no wine")),
            ):
                self.assertIsNone(manager._select_applier())


if __name__ == "__main__":
    unittest.main()
