"""空间错误演示仅调用模拟业务入口，不能写入游戏或用户配置。"""

import tempfile
import unittest
from pathlib import Path

from scripts.demo_disk_space_errors import SCENARIOS, preview


class DiskSpacePreviewTests(unittest.TestCase):
    def test_each_command_shows_real_cli_error_without_writing_files(self):
        prefixes = {
            "download": "下载失败:",
            "resource": "执行失败:",
            "sync": "执行失败:",
            "sync-new": "执行失败:",
            "checkout": "切换失败:",
            "predownload": "增量更新操作失败:",
            "incremental": "增量更新操作失败:",
            "apply": "增量更新操作失败:",
        }
        with tempfile.TemporaryDirectory(prefix="ww-space-demo-test-") as temporary:
            root = Path(temporary)
            for name, scenario in SCENARIOS.items():
                with self.subTest(command=name):
                    output = preview(scenario, root)
                    for text in (prefixes[name], "空间不足", "5.00 GiB", "差额", "--force"):
                        self.assertIn(text, output)
                    self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
