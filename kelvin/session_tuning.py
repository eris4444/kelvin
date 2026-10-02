"""Optional Hyprland session tuning for Hybrid (Optimus) mode.

Omarchy's NVIDIA defaults (default/hypr/nvidia.lua) export
LIBVA_DRIVER_NAME=nvidia and __GLX_VENDOR_LIBRARY_NAME=nvidia whenever an
NVIDIA GPU exists. That is right in Discrete mode, but in Hybrid mode it wakes
the dGPU for every hardware-decoded video and every X11 OpenGL app.

When enabled, Kelvin writes ~/.config/hypr/kelvin.lua and loads it from
hyprland.lua with a pcall'd require (an error can never break the config).
The snippet only acts when the internal panel is really wired to the Intel
iGPU at session start, so it is a no-op in Discrete mode. Apps can still be
offloaded to NVIDIA with `kelvin run`.
"""

from __future__ import annotations

import shutil
import subprocess
import time

from . import paths

MARKER = "-- Kelvin: hybrid graphics session tuning"
REQUIRE_LINE = f'pcall(require, "hypr.kelvin") {MARKER} (managed by Kelvin)'
LEGACY_MARKER = "-- GPU-S: hybrid graphics session tuning"  # pre-rename installs

SNIPPET = """\
-- Kelvin: hybrid graphics session tuning. Managed by Kelvin; turn it off in
-- Kelvin Settings (or delete this file and the matching line in hyprland.lua).
--
-- Omarchy's NVIDIA defaults route VA-API video decoding and GLX to the NVIDIA
-- driver. That's right when the NVIDIA GPU drives the display (ASUS MUX in
-- Discrete mode), but in Hybrid mode it wakes the dGPU for every video and
-- X11 OpenGL app. Only when the internal panel is wired to the Intel iGPU,
-- route them to Intel. `kelvin run <app>` still offloads apps to NVIDIA.

local function first_line(path)
  local file = io.open(path, "r")
  if not file then
    return nil
  end
  local value = file:read("*l")
  file:close()
  return value
end

local function internal_panel_on_intel()
  for card = 0, 7 do
    for _, connector in ipairs({ "eDP-1", "eDP-2", "eDP-3" }) do
      local status = first_line(string.format("/sys/class/drm/card%d-%s/status", card, connector))
      if status == "connected" then
        return first_line(string.format("/sys/class/drm/card%d/device/vendor", card)) == "0x8086"
      end
    end
  end
  return false
end

if internal_panel_on_intel() then
  hl.env("LIBVA_DRIVER_NAME", "iHD")
  hl.env("__GLX_VENDOR_LIBRARY_NAME", "mesa")
end
"""


def snippet_path():
    return paths.hypr_config_dir() / "kelvin.lua"


def hyprland_config():
    return paths.hypr_config_dir() / "hyprland.lua"


def is_enabled() -> bool:
    try:
        return MARKER in hyprland_config().read_text() and snippet_path().is_file()
    except OSError:
        return False


def _config_errors() -> str | None:
    """Reload Hyprland and return its config errors (None if clean or unavailable)."""
    hyprctl = shutil.which("hyprctl")
    if not hyprctl:
        return None
    try:
        subprocess.run([hyprctl, "reload"], capture_output=True, timeout=10, check=False)
        time.sleep(0.5)
        out = subprocess.run([hyprctl, "configerrors"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line for line in out.strip().splitlines() if line.strip() and "no errors" not in line.lower()]
    return "\n".join(lines) or None


def enable(validate: bool = True) -> tuple[bool, str]:
    config = hyprland_config()
    if not config.is_file():
        return False, f"{config} does not exist, so the session tuning cannot be loaded."
    original = config.read_text()
    snippet_existed = snippet_path().is_file()
    snippet_path().write_text(SNIPPET)
    if MARKER not in original:
        backup = config.with_name(f"hyprland.lua.bak.kelvin.{int(time.time())}")
        backup.write_text(original)
        config.write_text(original.rstrip("\n") + "\n\n" + REQUIRE_LINE + "\n")
    if validate:
        errors = _config_errors()
        if errors and "kelvin" in errors.lower():
            config.write_text(original)
            if not snippet_existed:
                snippet_path().unlink(missing_ok=True)
            _config_errors()
            return False, "Hyprland rejected the session tuning, so it was rolled back:\n" + errors
    return True, "Hybrid session tuning enabled. It takes effect at your next login."


def disable(validate: bool = True) -> tuple[bool, str]:
    config = hyprland_config()
    changed = False
    try:
        text = config.read_text()
        if MARKER in text or LEGACY_MARKER in text:
            kept = [line for line in text.splitlines() if MARKER not in line and LEGACY_MARKER not in line]
            config.write_text("\n".join(kept).rstrip("\n") + "\n")
            changed = True
    except OSError:
        pass
    for snippet in (snippet_path(), paths.hypr_config_dir() / "gpu-s.lua"):
        if snippet.is_file():
            snippet.unlink()
            changed = True
    if changed and validate:
        _config_errors()
    return True, "Hybrid session tuning removed. It takes effect at your next login." if changed else "Hybrid session tuning was not enabled."
