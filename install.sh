#!/bin/bash
# Kelvin installer.
#
# Builds an Arch package from this directory and installs it with pacman, so
# every system file (command, root helpers, polkit policy, desktop entry,
# icons) is tracked by pacman and removed cleanly by uninstall.sh. pacman also
# installs any missing dependencies. Nothing outside the package is changed.
#
# Run as your normal user: ./install.sh

set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

if ((EUID == 0)); then
  echo "Run this as your normal user; you'll be asked for your password for the pacman step." >&2
  exit 1
fi
command -v makepkg >/dev/null || { echo "makepkg not found: this installer targets Arch/Omarchy." >&2; exit 1; }

as_root() {
  if [[ -t 0 ]]; then sudo "$@"; else pkexec "$@"; fi
}

echo "==> Running tests"
python3 -m unittest discover -s tests -q
bash tests/test_helpers.sh

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "==> Building package"
BUILDDIR="$work/build" PKGDEST="$work" SRCDEST="$work" makepkg -f --noconfirm >/dev/null
pkg=$(find "$work" -maxdepth 1 -name 'kelvin-*.pkg.tar.*' | head -1)
[[ -n $pkg ]] || { echo "Package build failed." >&2; exit 1; }

echo "==> Installing $(basename "$pkg") (administrator password required)"
if [[ $(pacman -Qq gpu-s 2>/dev/null) == gpu-s ]]; then  # not just "provided by kelvin"
  # Kelvin was called GPU-S: stop the old app and swap the packages in one step.
  gpu-s quit >/dev/null 2>&1 || true
  as_root bash -c 'pacman -Rdd --noconfirm gpu-s >/dev/null && pacman -U --noconfirm "$1"' _ "$pkg"
else
  as_root pacman -U --noconfirm "$pkg"
fi

# Restart a running Kelvin so it picks up the installed version.
kelvin quit >/dev/null 2>&1 || true

cat <<'EOF'

==> Kelvin installed.

  kelvin            open the window (also in the app launcher as "Kelvin")
  kelvin status     terminal summary
  kelvin help       all commands

Start-at-login is enabled on first launch (Settings → Startup to turn it off).
EOF
