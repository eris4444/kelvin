# Local package for GPU-S. Built by install.sh; every installed system file is
# owned by this package, so `pacman -R gpu-s` removes GPU-S cleanly.
pkgname=gpu-s
pkgver=1.1.0
pkgrel=1
pkgdesc="NVIDIA hybrid-graphics GPU power manager for Omarchy (GTK4/libadwaita)"
arch=('any')
license=('MIT')
depends=(
  'python'
  'python-gobject'
  'python-cairo'
  'gtk4'
  'libadwaita'
  'polkit'
  'upower'
  'nvidia-utils'
  'hicolor-icon-theme'
)
optdepends=('hyprland: renderer detection from the compositor log and session tuning')

package() {
  cd "$startdir"
  make DESTDIR="$pkgdir" PREFIX=/usr install
}
