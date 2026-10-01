#!/bin/bash
# GPU-S installer.
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
pkg=$(find "$work" -maxdepth 1 -name 'gpu-s-*.pkg.tar.*' | head -1)
[[ -n $pkg ]] || { echo "Package build failed." >&2; exit 1; }

echo "==> Installing $(basename "$pkg") (administrator password required)"
as_root pacman -U --noconfirm "$pkg"

# Restart a running GPU-S so it picks up the installed version.
gpu-s quit >/dev/null 2>&1 || true

cat <<'EOF'

==> GPU-S installed.

  gpu-s            open the window (also in the app launcher as "GPU-S")
  gpu-s status     terminal summary
  gpu-s help       all commands

Start-at-login is enabled on first launch (Settings → Startup to turn it off).
EOF
