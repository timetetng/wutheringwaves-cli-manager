# ==============================================================================
# 鸣潮 CLI 管理器 (ww-manager)
# ==============================================================================

# 提取 pyproject.toml 中的版本号
VERSION := $(shell grep -m 1 "version =" pyproject.toml | cut -d '"' -f 2)
PKG_NAME := ww-manager

# 伪目标声明
.PHONY: help setup lint format build clean uninstall aur-local

help:
	@echo "可用命令列表:"
	@echo "  make setup      - 初始化开发环境 (同步依赖并安装 pre-commit)"
	@echo "  make format     - 自动格式化代码并修复可自动修复的 Lint 错误"
	@echo "  make lint       - 运行严格的代码与格式检查"
	@echo "  make build      - 构建 Wheel 和 sdist 发行包"
	@echo "  make aur-local  - 在本地生成 PKGBUILD 并构建 Arch Linux 安装包"
	@echo "  make clean      - 清理所有构建产物、缓存和虚拟环境"
	@echo "  make uninstall  - 卸载AUR包"

# ==============================================================================
# 本地安装
# ==============================================================================

setup:
	@echo "=> 同步 uv 依赖..."
	uv sync
	@echo "=> 安装 pre-commit 钩子..."
	uv run pre-commit install

# ==============================================================================
# 代码审查与格式化
# ==============================================================================

format:
	@echo "=> 格式化代码并尝试自动修复..."
	uv run ruff check --fix .
	uv run ruff format .

lint:
	@echo "=> 执行严格审查 (Lint & Format Check)..."
	uv run ruff check .
	uv run ruff format --check .

# ==============================================================================
# 构建与分发
# ==============================================================================

build: clean
	@echo "=> 开始构建 v$(VERSION) 发行版..."
	uv build

# 专为 Arch 环境提供的本地 AUR 测试打包工作流
aur-local:
	@echo "=> 生成 PKGBUILD..."
	bash .github/generate-pkgbuild.sh
	@echo "=> 使用 makepkg 进行本地构建测试..."
	makepkg -si

# ==============================================================================
# 环境清理
# ==============================================================================

clean:
	@echo "=> 清理 Python 缓存与构建产物..."
	rm -rf .venv/
	rm -rf .ruff_cache/
	rm -rf dist/
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.py[co]" -delete
	@echo "=> 清理 Arch 打包残留..."
	rm -rf pkg/
	rm -rf src/wutheringwaves-cli-manager-* src/*.tar.gz
	rm -f PKGBUILD source.tar.gz
	rm -f *.pkg.tar.zst
	rm -f *.tar.gz
	@echo "=> 清理完成！"

uninstall:
		@echo "=> 从 Arch Linux 系统中卸载本地测试包..."
			sudo pacman -Rns $(PKG_NAME) || true
				@echo "=> 卸载完成！"1
