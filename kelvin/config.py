"""User configuration (~/.config/kelvin/config.json) and runtime state
(~/.local/state/kelvin/state.json). Both are written atomically."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import paths

MODES = ("always-on", "auto", "power-saving")
THEMES = ("omarchy", "system", "dark", "light")


@dataclass
class Config:
    ac_mode: str = "always-on"
    battery_mode: str = "power-saving"
    auto_switch: bool = True
    autostart: bool = True
    start_hidden: bool = True
    tray: bool = True
    refresh_interval: float = 2.0
    notifications: bool = True
    battery_notifications: bool = True
    theme: str = "omarchy"
    translucent: bool = True
    # Fan curves: auto (firmware) | quiet | cool | max | custom
    fan_cpu: str = "auto"
    fan_gpu: str = "auto"
    fan_cpu_curve: list | None = None  # [[°C, pwm], x8] for "custom"
    fan_gpu_curve: list | None = None

    def validate(self) -> "Config":
        defaults = Config()
        if self.ac_mode not in MODES:
            self.ac_mode = defaults.ac_mode
        if self.battery_mode not in MODES:
            self.battery_mode = defaults.battery_mode
        if self.theme not in THEMES:
            self.theme = defaults.theme
        try:
            self.refresh_interval = min(30.0, max(1.0, float(self.refresh_interval)))
        except (TypeError, ValueError):
            self.refresh_interval = defaults.refresh_interval
        from .fans import MODES as FAN_MODES, validate_curve

        for fan in ("cpu", "gpu"):
            curve = getattr(self, f"fan_{fan}_curve")
            try:
                curve = [(int(t), int(p)) for t, p in curve] if curve else None
            except (TypeError, ValueError):
                curve = None
            if curve is not None and validate_curve(curve):
                curve = None
            setattr(self, f"fan_{fan}_curve", [list(pt) for pt in curve] if curve else None)
            mode = getattr(self, f"fan_{fan}")
            if mode not in FAN_MODES or (mode == "custom" and not curve):
                setattr(self, f"fan_{fan}", "auto")
        for f in fields(self):
            if f.type == "bool" and not isinstance(getattr(self, f.name), bool):
                setattr(self, f.name, getattr(defaults, f.name))
        return self

    def mode_for(self, on_ac: bool) -> str:
        return self.ac_mode if on_ac else self.battery_mode


@dataclass
class State:
    manual_mode: str | None = None  # user override, cleared on power-source change
    manual_on_ac: bool | None = None  # power source when the override was made
    applied_mode: str | None = None  # last mode Kelvin applied
    applied_by: str | None = None  # "ac-profile" | "battery-profile" | "manual"
    last_on_ac: bool | None = None
    gpu_name: str | None = None  # cached from NVML so we never wake the GPU for it
    baseline_pm_control: str | None = None  # value found before Kelvin first changed it
    baseline_mux: str | None = None  # ASUS MUX mode when Kelvin first ran ("discrete"/"hybrid")


def _load(path: Path, cls):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return cls(), False
    except (OSError, ValueError):
        return cls(), True
    known = {f.name for f in fields(cls)}
    obj = cls(**{k: v for k, v in data.items() if k in known}) if isinstance(data, dict) else cls()
    return obj, True


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(obj), fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def config_path() -> Path:
    return paths.config_dir() / "config.json"


def state_path() -> Path:
    return paths.state_dir() / "state.json"


def _migrate(new: Path, legacy: Path) -> None:
    """Copy a GPU-S (pre-rename) file into Kelvin's location once."""
    if not new.exists() and legacy.is_file():
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_text(legacy.read_text())


def load_config() -> Config:
    _migrate(config_path(), paths.legacy_config_dir() / "config.json")
    cfg, existed = _load(config_path(), Config)
    cfg.validate()
    if not existed:
        save_config(cfg)
    return cfg


def save_config(cfg: Config) -> None:
    _save(config_path(), cfg.validate())


def load_state() -> State:
    _migrate(state_path(), paths.legacy_state_dir() / "state.json")
    state, _ = _load(state_path(), State)
    if state.manual_mode not in (None, *MODES):
        state.manual_mode = None
    return state


def save_state(state: State) -> None:
    _save(state_path(), state)
