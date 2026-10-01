#!/bin/bash
# GPU-S uninstaller.
#
#   ./uninstall.sh            revert everything GPU-S changed and remove the package
#   ./uninstall.sh --purge    ...and also delete ~/.config/gpu-s and ~/.local/state/gpu-s
#   ./uninstall.sh --keep-mux leave the ASUS GPU MUX as it is now
#
# "Revert" means: stop GPU-S, remove the Hyprland session tuning (if enabled),
# remove the autostart entry, set NVIDIA runtime PM back to the driver default
# ('auto') and, if GPU-S switched the ASUS MUX, switch it back (applies after
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

if command -v gpu-s >/dev/null; then
  echo "==> Reverting GPU-S changes"
  gpu-s reset "${reset_args[@]}" || echo "   (some changes could not be reverted; see above)"
fi

rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/dev.erisrtg.GpuS.desktop"

if pacman -Qq gpu-s >/dev/null 2>&1; then
  echo "==> Removing the gpu-s package (administrator password required)"
  as_root pacman -R --noconfirm gpu-s
fi

if $purge; then
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/gpu-s" "${XDG_STATE_HOME:-$HOME/.local/state}/gpu-s"
  echo "==> Removed GPU-S configuration and state"
fi

echo "==> GPU-S uninstalled."
