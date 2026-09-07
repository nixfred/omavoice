# Maintainer: Fred Nix <frednix@gmail.com>
pkgname=omavoice
pkgver=0.3.12
pkgrel=1
pkgdesc="Voice recorder with live transcription for Omarchy (GTK4, PipeWire, whisper.cpp)"
arch=('any')
url="https://github.com/nixfred/omavoice"
license=('MIT')
depends=(
  'python'
  'python-gobject'
  'gtk4'
  'libadwaita'
  'pipewire-audio'
  'pulse-native-provider'
  'libpulse'
  'ffmpeg'
  'whisper-cpp'
  'ggml-cpu'
  'xdg-utils'
  'curl'
  'hicolor-icon-theme'
)
makedepends=('git')
optdepends=(
  'voxtype: reuse its downloaded whisper models'
  'ggml-vulkan: GPU acceleration for whisper.cpp on machines with Vulkan'
)
source=("$pkgname-$pkgver::git+$url.git#tag=v$pkgver")
sha256sums=('SKIP')

check() {
  cd "$srcdir/$pkgname-$pkgver"
  python3 -m unittest discover -s tests -q
}

package() {
  cd "$srcdir/$pkgname-$pkgver"
  install -d "$pkgdir/usr/lib/omavoice/omavoice"
  install -Dm644 omavoice/*.py -t "$pkgdir/usr/lib/omavoice/omavoice"
  install -Dm755 bin/omavoice "$pkgdir/usr/bin/omavoice"
  install -Dm644 data/io.github.nixfred.omavoice.desktop -t "$pkgdir/usr/share/applications"
  install -Dm644 data/io.github.nixfred.omavoice.svg -t "$pkgdir/usr/share/icons/hicolor/scalable/apps"
  install -Dm644 LICENSE -t "$pkgdir/usr/share/licenses/$pkgname"
  install -Dm644 README.md -t "$pkgdir/usr/share/doc/$pkgname"
}
