#!/bin/bash
# Kelvin uninstaller.
#
#   ./uninstall.sh            revert everything Kelvin changed and remove the package
#   ./uninstall.sh --purge    ...and also delete ~/.config/kelvin and ~/.local/state/kelvin
#   ./uninstall.sh --keep-mux leave the ASUS GPU MUX as it is now
#
# "Revert" means: stop Kelvin, remove the Hyprland session tuning (if enabled),
# remove the autostart entry, set NVIDIA runtime PM back to the driver default
# ('auto') and, if Kelvin switched the ASUS MUX, switch it back (applies after
# the next reboot; nothing reboots automatically).

set -euo pipefail

purge=false
reset_args=()
for arg in "$@"; do
  case $arg in
  --purge) purge=true ;;
  --keep-mux) reset_args+=(--keep-mux) ;;
  *) echo "usage: $0 [--purge] [--keep-mux]" >&2; exit 2 ;;
  esac
done

as_root() {
  if [[ -t 0 ]]; then sudo "$@"; else pkexec "$@"; fi
}

if command -v kelvin >/dev/null; then
  echo "==> Reverting Kelvin changes"
  kelvin reset "${reset_args[@]}" || echo "   (some changes could not be reverted; see above)"
fi

rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/dev.erisrtg.Kelvin.desktop"

if pacman -Qq kelvin >/dev/null 2>&1; then
  echo "==> Removing the kelvin package (administrator password required)"
  as_root pacman -R --noconfirm kelvin
fi

if $purge; then
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/kelvin" "${XDG_STATE_HOME:-$HOME/.local/state}/kelvin"
  echo "==> Removed Kelvin configuration and state"
fi

echo "==> Kelvin uninstalled."
