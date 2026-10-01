"""Filesystem locations: XDG user directories, system roots and install paths."""

import os
from pathlib import Path

from . import APP_ID


def _xdg(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def config_dir() -> Path:
    return config_home() / "gpu-s"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "gpu-s"


def omarchy_theme_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "omarchy" / "current" / "theme"


def autostart_file() -> Path:
    return config_home() / "autostart" / f"{APP_ID}.desktop"


def hypr_config_dir() -> Path:
    return config_home() / "hypr"


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


# Tests point these at fake trees; everything else uses the real system.
SYSFS = Path(os.environ.get("GPU_S_SYSFS", "/sys"))
PROCFS = Path(os.environ.get("GPU_S_PROCFS", "/proc"))

# Privileged helpers must live at the paths named in the polkit policy.
HELPER_DIR = Path("/usr/lib/gpu-s")
PM_HELPER = HELPER_DIR / "gpu-s-pm-helper"
MUX_HELPER = HELPER_DIR / "gpu-s-mux-helper"
BATTERY_HELPER = HELPER_DIR / "gpu-s-battery-helper"
POLKIT_POLICY = Path("/usr/share/polkit-1/actions/dev.erisrtg.gpus.policy")


def data_dir() -> Path:
    """Directory holding icons and other runtime data (source tree or install)."""
    source = Path(__file__).resolve().parent.parent / "data"
    if source.is_dir():
        return source
    return Path("/usr/share/gpu-s")
