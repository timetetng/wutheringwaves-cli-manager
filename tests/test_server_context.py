"""切服上下文要同时吃老 schema（default）和 3.8 新 schema（resourcePacks）。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ww_manager.core import WGameManager

CDN = "https://cdn.example/"
INDEX_PATH = "launcher/game/G152/10003/x/common/indexFile.json"

NEW_SCHEMA = {
    "cdnList": [
        {"K1": 1, "K2": 1, "P": 5, "url": "https://slow.example/"},
        {"K1": 1, "K2": 1, "P": 9, "url": CDN},
        {"K1": 0, "K2": 1, "P": 99, "url": "https://closed.example/"},
    ],
    "resourcePacks": {
        "common": {
            "version": "3.8.0",
            "baseUrl": "launcher/game/G152/10003/x/zip/",
            "indexFile": INDEX_PATH,
            "patchConfig": [],
        }
    },
    "bundles": {},
}

LEGACY = {
    "default": {
        "version": "3.7.1",
        "cdnList": [{"K1": 1, "K2": 1, "P": 9, "url": CDN}],
        "baseUrl": "launcher/game/G152/10003/3.7.1/abc/",
        "config": {"indexFile": INDEX_PATH},
    }
}

MANIFEST = {"resource": [{"dest": "Client/Content/Paks/pakchunk0.pak", "md5": "0" * 32, "size": 1}]}


class ServerContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _fetch(self, index):
        def fake(url, *_args, **_kwargs):
            return MANIFEST if INDEX_PATH in url else index

        mgr = WGameManager(Path(self.tmp.name), "cn")
        with patch.object(WGameManager, "_http_get_json", side_effect=fake):
            return mgr._fetch_server_context("cn")

    def test_new_schema_context(self):
        ctx = self._fetch(NEW_SCHEMA)
        self.assertEqual(ctx["version"], "3.8.0")
        self.assertEqual(ctx["cdn"], CDN)
        self.assertEqual(ctx["res_base"], "launcher/game/G152/10003/x/zip")
        self.assertEqual(len(ctx["resource"]), 1)

    def test_legacy_schema_context(self):
        ctx = self._fetch(LEGACY)
        self.assertEqual(ctx["version"], "3.7.1")
        self.assertEqual(ctx["res_base"], "launcher/game/G152/10003/3.7.1/abc")

    def test_index_file_is_normalized_once(self):
        mgr = WGameManager(Path(self.tmp.name), "cn")
        with patch.object(WGameManager, "_http_get_json", return_value=NEW_SCHEMA):
            self.assertEqual(mgr.launcher_info["default"]["version"], "3.8.0")


if __name__ == "__main__":
    unittest.main()
