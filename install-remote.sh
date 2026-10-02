#!/bin/bash
# One-line Kelvin installer:
#
#   curl -fsSL https://raw.githubusercontent.com/eris4444/kelvin/main/install-remote.sh | bash
#
# Clones (or updates) the Kelvin source to ~/.local/share/kelvin and runs its
# own install.sh, which builds a pacman package and installs it — see that
# script and the repo's README for exactly what it does. This wrapper does
# nothing else: no privileged steps happen until install.sh's own pacman
# step, which asks for your password itself.

set -euo pipefail

REPO_URL=${KELVIN_REPO_URL:-https://github.com/eris4444/kelvin.git}
DEST=${KELVIN_SRC_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/kelvin"}

if ((EUID == 0)); then
  echo "Run this as your normal user, not root." >&2
  exit 1
fi
command -v git >/dev/null || { echo "git is required." >&2; exit 1; }
command -v makepkg >/dev/null || { echo "makepkg not found: this installer targets Arch/Omarchy." >&2; exit 1; }

if [[ -d $DEST/.git ]]; then
  echo "==> Updating existing checkout at $DEST"
  git -C "$DEST" fetch --quiet origin
  git -C "$DEST" checkout --quiet main
  git -C "$DEST" reset --quiet --hard origin/main
else
  echo "==> Cloning Kelvin to $DEST"
  git clone --quiet "$REPO_URL" "$DEST"
fi

exec "$DEST/install.sh"
