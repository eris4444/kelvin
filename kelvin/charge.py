"""Battery charge thresholds.

What controls the threshold on this kind of system:

* the kernel exposes the firmware threshold(s) as
  /sys/class/power_supply/BATx/charge_control_{start,end}_threshold
  (ASUS laptops: asus-wmi provides the *end* threshold only);
* UPower (>= 1.90) owns them: when its charge limit is enabled it writes
  CHARGE_LIMIT (from udev hwdb) to the kernel, persists the enabled state in
  /var/lib/upower and re-applies it at boot and after resume.

Kelvin therefore never writes the sysfs threshold itself. It changes the value
through UPower's documented hwdb override (root helper, polkit) and toggles
the limit with UPower's EnableChargeThreshold (polkit: allowed for the active
user). The result is verified against the kernel attribute.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import paths, privilege
from .sysfs import read, read_int

UPOWER = "org.freedesktop.UPower"
DEVICE_IFACE = "org.freedesktop.UPower.Device"
OVERRIDE = Path("/etc/udev/hwdb.d/61-kelvin-battery.hwdb")
LEGACY_OVERRIDE = Path("/etc/udev/hwdb.d/61-gpu-s-battery.hwdb")  # pre-rename
HWDB_DIR = Path("/etc/udev/hwdb.d")
LEGACY_UNITS = ("battery-charge-limit.service",)

MIN_END = 50
MAX_END = 100

SETTING_START = 1 << 0  # UPower ChargeThresholdSettingsSupported bits
SETTING_END = 1 << 1


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    start: int
    end: int


PRESETS = (
    Preset("care", "Battery Care", 75, 80),
    Preset("balanced", "Balanced", 50, 80),
    Preset("maximum", "Maximum Runtime", 20, 100),
)


@dataclass
class ChargeInfo:
    battery: str | None
    upower_path: str | None = None
    kernel_end: int | None = None
    kernel_start: int | None = None
    start_supported: bool = False
    upower_supported: bool | None = None
    upower_enabled: bool | None = None
    upower_start: int | None = None
    upower_end: int | None = None
    override: tuple[str, int] | None = None  # (start or "_", end) from Kelvin's hwdb file
    foreign_overrides: list[str] = field(default_factory=list)
    legacy_units_enabled: list[str] = field(default_factory=list)
    # live battery figures
    percent: float | None = None
    status: str | None = None  # kernel: Charging / Discharging / Not charging / Full
    voltage_v: float | None = None
    energy_wh: float | None = None
    energy_full_wh: float | None = None
    energy_design_wh: float | None = None
    cycles: int | None = None
    vendor: str | None = None
    model: str | None = None
    ac_online: bool | None = None

    @property
    def supported(self) -> bool:
        return self.battery is not None and self.kernel_end is not None

    @property
    def limit_active(self) -> bool:
        return self.kernel_end is not None and self.kernel_end < 100

    @property
    def health_percent(self) -> float | None:
        if self.energy_full_wh and self.energy_design_wh:
            return 100 * self.energy_full_wh / self.energy_design_wh
        return None

    @property
    def managed_by_upower(self) -> bool:
        return bool(self.upower_supported)

    @property
    def charging(self) -> bool:
        return self.status == "Charging"


def _battery_dir() -> Path | None:
    root = paths.SYSFS / "class/power_supply"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if read(entry / "type") == "Battery" and read(entry / "present") != "0":
            return entry
    return None


def read_override(path: Path | None = None) -> tuple[str, int] | None:
    if path is None:
        path = OVERRIDE if OVERRIDE.exists() or not LEGACY_OVERRIDE.exists() else LEGACY_OVERRIDE
    try:
        text = path.read_text()
    except OSError:
        return None
    match = re.search(r"CHARGE_LIMIT=(_|\d+),(\d+)", text)
    return (match.group(1), int(match.group(2))) if match else None


def _foreign_overrides() -> list[str]:
    found = []
    try:
        files = sorted(HWDB_DIR.glob("*.hwdb"))
    except OSError:
        return found
    for file in files:
        if file in (OVERRIDE, LEGACY_OVERRIDE):
            continue
        try:
            if "CHARGE_LIMIT" in file.read_text():
                found.append(str(file))
        except OSError:
            continue
    return found


def _legacy_units() -> list[str]:
    enabled = []
    for unit in LEGACY_UNITS:
        try:
            out = subprocess.run(["systemctl", "is-enabled", unit], capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out in ("enabled", "enabled-runtime"):
            enabled.append(unit)
    return enabled


def _bus():
    from gi.repository import Gio

    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def _upower_props(battery: str) -> tuple[str | None, dict]:
    from gi.repository import Gio, GLib

    path = f"/org/freedesktop/UPower/devices/battery_{battery}"
    try:
        (props,) = _bus().call_sync(
            UPOWER, path, "org.freedesktop.DBus.Properties", "GetAll",
            GLib.Variant("(s)", (DEVICE_IFACE,)), GLib.VariantType.new("(a{sv})"),
            Gio.DBusCallFlags.NONE, 2000, None,
        ).unpack()
        return path, props
    except Exception:  # noqa: BLE001 - UPower unavailable
        return None, {}


def read_charge(check_units: bool = True) -> ChargeInfo:
    bat = _battery_dir()
    if bat is None:
        return ChargeInfo(battery=None)
    path, props = _upower_props(bat.name)
    supported_bits = props.get("ChargeThresholdSettingsSupported")
    voltage = read_int(bat / "voltage_now")
    ac_online = None
    for supply in (paths.SYSFS / "class/power_supply").iterdir():
        if read(supply / "type") in ("Mains", "USB") and read(supply / "online") is not None:
            ac_online = bool(ac_online) or read(supply / "online") == "1"
    cycles = read_int(bat / "cycle_count")
    upower_cycles = props.get("ChargeCycles")
    return ChargeInfo(
        battery=bat.name,
        upower_path=path,
        kernel_end=read_int(bat / "charge_control_end_threshold"),
        kernel_start=read_int(bat / "charge_control_start_threshold"),
        start_supported=(bat / "charge_control_start_threshold").exists()
        and (supported_bits is None or bool(supported_bits & SETTING_START)),
        upower_supported=props.get("ChargeThresholdSupported"),
        upower_enabled=props.get("ChargeThresholdEnabled"),
        upower_start=props.get("ChargeStartThreshold"),
        upower_end=props.get("ChargeEndThreshold"),
        override=read_override(),
        foreign_overrides=_foreign_overrides(),
        legacy_units_enabled=_legacy_units() if check_units else [],
        percent=props.get("Percentage", read_int(bat / "capacity")),
        status=read(bat / "status"),
        voltage_v=voltage / 1e6 if voltage else props.get("Voltage"),
        energy_wh=props.get("Energy"),
        energy_full_wh=props.get("EnergyFull"),
        energy_design_wh=props.get("EnergyFullDesign"),
        cycles=cycles if cycles else (upower_cycles if upower_cycles and upower_cycles > 0 else None),
        vendor=read(bat / "manufacturer") or props.get("Vendor"),
        model=read(bat / "model_name") or props.get("Model"),
        ac_online=ac_online,
    )


# -- validation / presets ------------------------------------------------------


def validate(info: ChargeInfo, end: int, start: int | None = None) -> str | None:
    """Return an error message, or None if the thresholds are applicable."""
    if not info.supported:
        return "This battery has no kernel charge threshold, so charging limits are not supported."
    if not info.managed_by_upower:
        return "UPower does not manage charge thresholds for this battery, so Kelvin cannot persist one safely."
    if not isinstance(end, int) or not MIN_END <= end <= MAX_END:
        return f"The charge limit must be a whole number from {MIN_END} to {MAX_END} %."
    if start is not None:
        if not info.start_supported:
            return ("This battery only supports an end threshold (the kernel exposes no "
                    "charge_control_start_threshold); the firmware resumes charging on its own.")
        if not isinstance(start, int) or not 0 <= start < end:
            return "The start threshold must be lower than the charge limit."
    if info.foreign_overrides:
        return f"{info.foreign_overrides[0]} already sets CHARGE_LIMIT; Kelvin will not override it."
    return None


def preset_status(info: ChargeInfo, preset: Preset) -> tuple[bool, str]:
    """(available, explanation) for a preset on this hardware."""
    if not info.supported or not info.managed_by_upower:
        return False, "Charge limits are not supported here."
    if info.start_supported:
        return True, f"{preset.start}% → {preset.end}%"
    if preset.key == "balanced":
        return False, "Needs a start threshold, which this battery does not support (it would equal Battery Care)."
    if preset.end >= 100:
        return True, "No limit: charges to 100%"
    return True, f"Stops charging at {preset.end}%"


def matching_preset(info: ChargeInfo) -> Preset | None:
    for preset in PRESETS:
        ok, _ = preset_status(info, preset)
        if not ok:
            continue
        if preset.end >= 100 and not info.limit_active:
            return preset
        if info.limit_active and info.kernel_end == preset.end and (
            not info.start_supported or info.kernel_start == preset.start
        ):
            return preset
    return None


# -- applying -------------------------------------------------------------------


@dataclass
class ChargeResult:
    ok: bool
    changed: bool
    message: str


def _enable_upower(info: ChargeInfo, enabled: bool) -> str | None:
    from gi.repository import Gio, GLib

    try:
        _bus().call_sync(
            UPOWER, info.upower_path, DEVICE_IFACE, "EnableChargeThreshold",
            GLib.Variant("(b)", (enabled,)), None, Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 10000, None,
        )
        return None
    except GLib.Error as exc:
        return f"UPower refused to {'enable' if enabled else 'disable'} the charge limit: {exc.message}"


def _wait_kernel(end: int, timeout: float = 4.0) -> int | None:
    deadline = time.time() + timeout
    while True:
        info = read_charge(check_units=False)
        if info.kernel_end == end or time.time() > deadline:
            return info.kernel_end
        time.sleep(0.2)


def _plan(info: ChargeInfo, end: int, start: int | None):
    """-> (helper args or None, enable flag)."""
    if end >= 100 and start is None:
        return None, False
    # Root is only needed when UPower's stored value must change.
    needs_change = info.upower_end != end or (start is not None and info.upower_start != start)
    helper = ["set", info.battery, "_" if start is None else str(start), str(end)] if needs_change else None
    return helper, True


def _wait_upower(end: int | None, timeout: float = 8.0) -> ChargeInfo:
    """Wait for UPower (possibly just restarted) to report the battery, and
    the expected stored end threshold if one is given."""
    deadline = time.time() + timeout
    while True:
        info = read_charge(check_units=False)
        ready = info.upower_path is not None and info.upower_supported is not None
        if (ready and (end is None or info.upower_end == end)) or time.time() > deadline:
            return info
        time.sleep(0.25)


def _finish(info_before: ChargeInfo, end: int, enable: bool) -> ChargeResult:
    info = _wait_upower(end if enable else None)
    if enable and info.upower_end != end:
        return ChargeResult(False, False, f"UPower still reports a {info.upower_end}% limit after the update; "
                                          "the new value was not picked up.")
    error = _enable_upower(info, enable)
    if error:
        return ChargeResult(False, False, error)
    got = _wait_kernel(end if enable else 100)
    if enable and got != end:
        return ChargeResult(False, True, f"UPower set {end}% but the kernel threshold reads {got}%: "
                                         "the firmware did not accept this value.")
    if not enable and got != 100:
        return ChargeResult(False, True, f"UPower disabled the limit but the kernel threshold is {got}%.")
    changed = info_before.kernel_end != got or info_before.upower_enabled != enable
    text = f"Battery limit applied: {end}%" if enable else "Battery limit removed: charges to 100%"
    return ChargeResult(True, changed, text)


def apply_sync(end: int, start: int | None = None) -> ChargeResult:
    info = read_charge()
    error = validate(info, end, start)
    if error:
        return ChargeResult(False, False, error)
    helper, enable = _plan(info, end, start)
    if helper:
        result = privilege.run_sync(paths.BATTERY_HELPER, *helper)
        if not result.ok:
            return ChargeResult(False, False, result.message)
    return _finish(info, end, enable)


def apply_async(end: int, start: int | None, callback: Callable[[ChargeResult], None]) -> None:
    from gi.repository import GLib

    info = read_charge()
    error = validate(info, end, start)
    if error:
        GLib.idle_add(lambda: callback(ChargeResult(False, False, error)) and False)
        return
    helper, enable = _plan(info, end, start)

    def finish() -> None:
        # D-Bus calls + waiting for the kernel take a few seconds: off the UI thread.
        def work():
            result = _finish(info, end, enable)
            GLib.idle_add(lambda: callback(result) and False)

        threading.Thread(target=work, daemon=True).start()

    if not helper:
        finish()
        return

    def done(result: privilege.HelperResult) -> None:
        if not result.ok:
            callback(ChargeResult(False, False, result.message))
        else:
            finish()

    privilege.run_async(paths.BATTERY_HELPER, helper, done)


def remove_override_sync() -> ChargeResult:
    info = read_charge(check_units=False)
    if info.override is None or info.battery is None:
        return ChargeResult(True, False, "No Kelvin battery override is installed.")
    result = privilege.run_sync(paths.BATTERY_HELPER, "clear", info.battery)
    if not result.ok:
        return ChargeResult(False, False, result.message)
    _wait_upower(None)
    return ChargeResult(True, True, "Kelvin battery override removed (UPower default thresholds restored).")
