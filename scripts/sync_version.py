#!/usr/bin/env python3
# 同步 pyproject.toml 版本号到 README, 妈妈再也不会担心我忘记改版本号了

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
README_FILES = [ROOT / "README.md", ROOT / "docs" / "README.en.md"]

# 徽章中的版本号：URL 里 badge/Version-<ver> 与 alt 文本 alt="version <ver>"
BADGE_VERSION_RE = re.compile(
    r"(img\.shields\.io/badge/Version-)\d+\.\d+\.\d+"
    r"|(alt=\"version )\d+\.\d+\.\d+"
)


def read_version() -> str:
    """从 pyproject.toml 提取 version = "x.y.z"。"""
    match = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        sys.exit(f"ERROR: 未在 {PYPROJECT} 中找到 version 字段")
    return match.group(1)


def sync_file(path: Path, version: str) -> bool:
    """同步单个 README 的徽章版本号，有改动时写回并返回 True。"""
    text = path.read_text(encoding="utf-8")
    new_text = BADGE_VERSION_RE.sub(lambda m: f"{m.group(1) or m.group(2)}{version}", text)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    version = read_version()
    changed = [str(p.relative_to(ROOT)) for p in README_FILES if sync_file(p, version)]
    if changed:
        print(f"[sync-version] 已把徽章版本号同步为 {version}: {', '.join(changed)}")
        print("文件已被修改，请 git add 后重新 commit。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
