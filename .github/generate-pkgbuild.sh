#!/bin/bash
# 从 pyproject.toml 提取版本号
VERSION=$(grep -m 1 "version =" pyproject.toml | cut -d '"' -f 2)
REPO="timetetng/wutheringwaves-cli-manager"

# 下载源码并计算校验和
URL="https://github.com/$REPO/archive/refs/tags/v$VERSION.tar.gz"
curl -L $URL -o source.tar.gz
SHA256=$(sha256sum source.tar.gz | cut -d ' ' -f 1)

# 生成 PKGBUILD
cat <<EOF >PKGBUILD
pkgname=ww-manager
pkgver=$VERSION
pkgrel=1
pkgdesc="ww-manager (A Wuthering Waves CLI Manager)"
arch=('any')
url="https://github.com/$REPO"
license=('MIT')
depends=(
  'python'
  'python-typer'
'python-rich'
  'python-certifi'
  'python-typing_extensions'
)
makedepends=('python-build' 'python-installer' 'python-hatchling')
source=("\$pkgname-\$pkgver.tar.gz::$URL")
sha256sums=('$SHA256')

build() {
  cd "\${srcdir}/wutheringwaves-cli-manager-\$pkgver"
  python -m build --wheel --no-isolation
}

package() {
  cd "\${srcdir}/wutheringwaves-cli-manager-\$pkgver"
  python -m installer --destdir="\$pkgdir" dist/*.whl
  install -Dm644 LICENSE "\$pkgdir/usr/share/licenses/\$pkgname/LICENSE"
}
EOF
