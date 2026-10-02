# Local package for Kelvin. Built by install.sh; every installed system file is
# owned by this package, so `pacman -R kelvin` removes Kelvin cleanly.
pkgname=kelvin
pkgver=2.0.0
pkgrel=1
pkgdesc="Power & thermal control centre for Omarchy laptops: NVIDIA GPU, battery limit, fans, CPU, sleep (GTK4)"
provides=('gpu-s')
conflicts=('gpu-s')
replaces=('gpu-s')
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
