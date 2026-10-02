"""Idle behaviour: screensaver, lock, screen off, sleep and stay-awake.

Omarchy already owns two idle stages and Kelvin uses them as they are:

* screensaver and lock: ``idle.screensaver`` / ``idle.lock`` (seconds) in
  ~/.config/omarchy/shell.json, run by the Omarchy shell's idle service;
* stay awake: Omarchy's switch (``omarchy-shell idle enable|disable``).

Omarchy has no "never" value for its timers, so Kelvin stores NEVER (about 23
days, the largest value Qt timers handle) for a disabled stage.

Kelvin adds the stages Omarchy lacks, run by the "kelvin.idle" shell plugin
(data/omarchy-plugin) from ~/.config/kelvin/idle.json:

* screen off after N seconds (Hyprland DPMS);
* sleep (suspend) after N seconds, separately on AC and on battery.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

NEVER = 2_000_000
OMARCHY_DEFAULTS = {"screensaver": 150, "lock": 300}
PLUGIN_ID = "kelvin.idle"
KELVIN_STAGES = ("screen_off", "sleep_ac", "sleep_battery")
OMARCHY_STAGES = ("screensaver", "lock")
STAGES = ("screensaver", "lock", "screen_off", "sleep_ac", "sleep_battery")
STAGE_LABELS = {
    "screensaver": "Screensaver",
    "lock": "Lock screen",
    "screen_off": "Turn off screen",
    "sleep_ac": "Sleep on AC power",
    "sleep_battery": "Sleep on battery",
}
CHOICES = (0, 60, 120, 180, 300, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200)


def shell_json() -> Path:
    return paths.config_home() / "omarchy" / "shell.json"


def kelvin_json() -> Path:
    return paths.config_dir() / "idle.json"


def stay_awake_file() -> Path:
    return paths._xdg("XDG_STATE_HOME", ".local/state") / "omarchy" / "indicators" / "stay-awake"


def plugin_link() -> Path:
    return paths.config_home() / "omarchy" / "plugins" / PLUGIN_ID


def plugin_source() -> Path:
    return paths.data_dir() / "omarchy-plugin" / PLUGIN_ID


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "Never"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    if seconds > 60:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds} s"


_UNITS = {"": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600}
_TOKEN = re.compile(r"(\d+)\s*([a-z]*)\s*")


def parse_duration(text: str) -> int:
    """'never'/'off'/'0' -> 0; '90' -> 90 s; '5m'/'5 min' -> 300; '1h30m' -> 5400."""
    value = text.strip().lower()
    if value in ("never", "off", "0", "none", "disable", "disabled"):
        return 0
    if not value or not re.fullmatch(r"(?:\d+\s*[a-z]*\s*)+", value):
        raise ValueError(f"invalid duration '{text}'")
    total = 0
    for number, unit in _TOKEN.findall(value):
        if unit not in _UNITS:
            raise ValueError(f"unknown unit '{unit}' in '{text}'")
        total += int(number) * _UNITS[unit]
    if total <= 0 or total >= NEVER:
        raise ValueError(f"invalid duration '{text}'")
    return total


@dataclass
class IdleSettings:
    screensaver: int  # seconds, 0 = never
    lock: int
    screen_off: int
    sleep_ac: int
    sleep_battery: int
    stay_awake: bool
    omarchy_available: bool
    plugin_installed: bool
    plugin_enabled: bool
    lid_action: str | None
    lid_action_ac: str | None

    def get(self, stage: str) -> int:
        return getattr(self, stage)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _omarchy_seconds(idle: dict, key: str) -> int:
    value = idle.get(key, OMARCHY_DEFAULTS[key])
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = OMARCHY_DEFAULTS[key]
    if n < 0:
        n = OMARCHY_DEFAULTS[key]
    return 0 if n >= NEVER // 2 else n


def _plugin_enabled() -> bool:
    config = _read_json(shell_json())
    for entry in config.get("plugins", []) or []:
        if (entry.get("id") if isinstance(entry, dict) else entry) == PLUGIN_ID:
            return PLUGIN_ID not in (config.get("disabledPlugins") or [])
    return False


def _logind(prop: str) -> str | None:
    try:
        out = subprocess.run(
            ["busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
             "org.freedesktop.login1.Manager", prop],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.split(" ", 1)[1].strip('"') if " " in out else None


def read_settings(with_logind: bool = True) -> IdleSettings:
    shell = _read_json(shell_json())
    idle = shell.get("idle") if isinstance(shell.get("idle"), dict) else {}
    kelvin = _read_json(kelvin_json())

    def kelvin_seconds(key: str) -> int:
        try:
            n = int(kelvin.get(key, 0))
        except (TypeError, ValueError):
            return 0
        return n if 0 < n < NEVER else 0

    lid = _logind("HandleLidSwitch") if with_logind else None
    lid_ac = _logind("HandleLidSwitchExternalPower") if with_logind else None
    return IdleSettings(
        screensaver=_omarchy_seconds(idle, "screensaver"),
        lock=_omarchy_seconds(idle, "lock"),
        screen_off=kelvin_seconds("screen_off"),
        sleep_ac=kelvin_seconds("sleep_ac"),
        sleep_battery=kelvin_seconds("sleep_battery"),
        stay_awake=stay_awake_file().exists(),
        omarchy_available=shutil.which("omarchy-shell") is not None,
        plugin_installed=(plugin_link() / "manifest.json").exists(),
        plugin_enabled=_plugin_enabled(),
        lid_action=lid,
        lid_action_ac=lid_ac or lid,
    )


# -- changes -------------------------------------------------------------------------


@dataclass
class IdleResult:
    ok: bool
    changed: bool
    message: str


def _ensure_plugin() -> str | None:
    """Install (symlink) and enable the kelvin.idle shell plugin."""
    source = plugin_source()
    if not (source / "manifest.json").exists():
        return f"The Kelvin idle plugin is missing from {source}."
    link = plugin_link()
    if link.is_symlink() and link.resolve() != source.resolve():
        link.unlink()
    if not link.exists():
        if link.is_symlink():
            link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source, target_is_directory=True)
    if not _plugin_enabled():
        omarchy = shutil.which("omarchy")
        if not omarchy:
            return "The 'omarchy' command is not available to enable the Kelvin idle plugin."
        proc = None
        for attempt in range(6):
            proc = subprocess.run([omarchy, "plugin", "enable", PLUGIN_ID], capture_output=True, text=True, timeout=30)
            if proc.returncode == 0 and _plugin_enabled():
                return None
            if attempt == 0 or "not known" in (proc.stderr + proc.stdout):
                # A freshly installed plugin folder is only seen after a rescan.
                if shutil.which("omarchy-shell"):
                    subprocess.run(["omarchy-shell", "shell", "rescanPlugins"], capture_output=True, timeout=15)
            time.sleep(0.5)
        return "Could not enable the Kelvin idle plugin: " + ((proc.stderr or proc.stdout).strip() if proc else "")
    return None


def remove_plugin() -> IdleResult:
    changed = False
    if _plugin_enabled() and shutil.which("omarchy"):
        subprocess.run(["omarchy", "plugin", "disable", PLUGIN_ID], capture_output=True, text=True, timeout=30)
        changed = True
    link = plugin_link()
    if link.is_symlink() or link.exists():
        if link.is_symlink():
            link.unlink()
            changed = True
    kelvin_json().unlink(missing_ok=True)
    return IdleResult(True, changed, "Kelvin idle plugin removed." if changed else "Kelvin idle plugin was not installed.")


def set_stage(stage: str, seconds: int) -> IdleResult:
    if stage not in STAGES:
        return IdleResult(False, False, f"Unknown idle stage '{stage}'.")
    if seconds < 0 or seconds >= NEVER:
        return IdleResult(False, False, "Invalid duration.")
    current = read_settings(with_logind=False)
    label = STAGE_LABELS[stage]
    if current.get(stage) == seconds:
        return IdleResult(True, False, f"{label}: {fmt_duration(seconds)} (unchanged).")

    if stage in OMARCHY_STAGES:
        path = shell_json()
        config = _read_json(path)
        if not path.exists():
            return IdleResult(False, False, f"{path} does not exist; is the Omarchy shell installed?")
        idle = config.get("idle") if isinstance(config.get("idle"), dict) else {}
        idle = dict(idle)
        for key, default in OMARCHY_DEFAULTS.items():
            idle.setdefault(key, default)
        idle[stage] = seconds if seconds else NEVER
        config["idle"] = idle
        _write_json(path, config)
    else:
        data = _read_json(kelvin_json())
        data = {k: int(data.get(k, 0) or 0) for k in KELVIN_STAGES}
        data[stage] = seconds
        _write_json(kelvin_json(), data)
        if seconds:
            error = _ensure_plugin()
            if error:
                return IdleResult(False, True, error)

    after = read_settings(with_logind=False)
    if after.get(stage) != seconds:
        return IdleResult(False, True, f"{label} did not take the new value.")
    return IdleResult(True, True, f"{label}: {'never' if not seconds else 'after ' + fmt_duration(seconds)}.")


def set_stay_awake(enabled: bool) -> IdleResult:
    if read_settings(with_logind=False).stay_awake == enabled:
        return IdleResult(True, False, f"Stay awake is already {'on' if enabled else 'off'}.")
    command = ["omarchy-shell", "idle", "disable" if enabled else "enable"]
    if shutil.which("omarchy-shell"):
        proc = subprocess.run(command, capture_output=True, text=True, timeout=10)
        ok = proc.returncode == 0
    else:
        ok = False
    if not ok and shutil.which("omarchy-toggle-idle"):
        subprocess.run(["omarchy-toggle-idle", "stay-awake" if enabled else "allow-idle"], capture_output=True, timeout=10)
    for _ in range(20):
        if stay_awake_file().exists() == enabled:
            break
        time.sleep(0.1)
    if stay_awake_file().exists() != enabled:
        return IdleResult(False, False, "Omarchy did not change the stay-awake state.")
    if enabled:
        return IdleResult(True, True, "Stay awake: on — no screensaver, lock, screen off or idle sleep.")
    return IdleResult(True, True, "Stay awake: off — idle timers are active again.")
