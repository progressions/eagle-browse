# Maintainer: Isaac Priestley <progressions@gmail.com>
pkgname=eagle-browse
pkgver=0.1.2
pkgrel=1
pkgdesc="Keyboard-first GTK browser and tools for an Eagle.cool library"
arch=('any')
url="https://github.com/progressions/eagle-browse"
license=('LicenseRef-Proprietary')
depends=(
  'ffmpeg'
  'gdk-pixbuf2'
  'gst-libav'
  'gst-plugins-base'
  'gst-plugins-good'
  'gtk4'
  'libadwaita'
  'python'
  'python-gobject'
)
makedepends=('git' 'python-build' 'python-installer' 'python-setuptools' 'python-wheel')
optdepends=(
  'avahi: phone-browse mDNS publication'
  'clip-editor: send selected video or audio to Clip Editor'
  'imagemagick: image thumbnail and crop fallback'
  'imv: external image preview'
  'libnotify: inbox import notifications'
  'mpv: external audio and media preview'
  'pipewire-audio: notification sound playback'
  'wl-clipboard: copy paths and file URIs under Wayland'
)
source=("$pkgname-$pkgver::git+$url.git#tag=v$pkgver")
sha256sums=('SKIP')

# Build from a local checkout before the tag exists:
#   EAGLE_BROWSE_SRC=$PWD makepkg -f
if [[ -n "${EAGLE_BROWSE_SRC:-}" ]]; then
  source=()
  sha256sums=()
fi

prepare() {
  if [[ -n "${EAGLE_BROWSE_SRC:-}" ]]; then
    rm -rf "$srcdir/$pkgname-$pkgver"
    mkdir -p "$srcdir/$pkgname-$pkgver"
    git -C "$EAGLE_BROWSE_SRC" archive --format=tar HEAD | tar -x -C "$srcdir/$pkgname-$pkgver"
  fi
}

build() {
  cd "$pkgname-$pkgver"
  python -m build --wheel --no-isolation
}

check() {
  cd "$pkgname-$pkgver"
  python -m unittest discover -s tests
  python -m compileall -q .
}

package() {
  cd "$pkgname-$pkgver"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 eagle-browse.desktop \
    "$pkgdir/usr/share/applications/eagle-browse.desktop"
  install -Dm644 eagle-inbox-watch.service \
    "$pkgdir/usr/lib/systemd/user/eagle-inbox-watch.service"
  install -Dm644 eagle-phone-browse.service \
    "$pkgdir/usr/lib/systemd/user/eagle-phone-browse.service"
  install -Dm644 config.toml.example \
    "$pkgdir/usr/share/doc/eagle-browse/config.toml.example"
  install -Dm644 docs/API.md docs/SMART_FOLDERS.md \
    -t "$pkgdir/usr/share/doc/eagle-browse"
}
