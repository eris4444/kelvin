"""Fans: live speed, ASUS thermal profile and custom fan curves.

* Fan speed (RPM): asus-wmi hwmon "asus" (fan1 = CPU, fan2 = GPU).
* Thermal profile (Silent / Balanced / Performance): the ASUS
  platform_profile, which also selects the EC's fan behaviour. On Omarchy it
  is owned by power-profiles-daemon, so Kelvin switches it through PPD
  (power-saver / balanced / performance) instead of writing sysfs.
* Custom curves: asus-wmi "asus_custom_fan_curve" (8 temperature/PWM points
  per fan), written by a polkit-gated root helper that enforces minimum fan
  speeds. The kernel drops custom curves when the thermal profile changes and
  at boot, so Kelvin's agent re-applies the saved curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import paths, privilege
from .sysfs import read, read_int

PPD = "net.hadess.PowerProfiles"
PPD_PATH = "/net/hadess/PowerProfiles"

FANS = ("cpu", "gpu")
FAN_INDEX = {"cpu": 1, "gpu": 2}
FAN_LABELS = {"cpu": "CPU fan", "gpu": "GPU fan"}

PROFILES = ("power-saver", "balanced", "performance")
PROFILE_LABELS = {"power-saver": "Silent", "balanced": "Balanced", "performance": "Performance"}
PROFILE_ICONS = {
    "power-saver": "power-profile-power-saver-symbolic",
    "balanced": "power-profile-balanced-symbolic",
    "performance": "power-profile-performance-symbolic",
}

Curve = list[tuple[int, int]]  # [(°C, pwm 0-255)] x 8

PRESETS: dict[str, Curve] = {
    # Fan off until 50 °C, gentle ramp; still >= safety floors.
    "quiet": [(50, 0), (58, 38), (64, 64), (70, 102), (75, 140), (80, 179), (85, 217), (90, 255)],
    # Always spinning, reaches full speed by 85 °C.
    "cool": [(40, 64), (50, 90), (58, 115), (64, 140), (70, 166), (75, 191), (80, 230), (85, 255)],
    # Full speed at all temperatures.
    "max": [(30, 255), (40, 255), (50, 255), (60, 255), (70, 255), (80, 255), (90, 255), (95, 255)],
}
MODES = ("auto", "quiet", "cool", "max", "custom")
MODE_LABELS = {"auto": "Auto", "quiet": "Quiet", "cool": "Cool", "max": "Full speed", "custom": "Custom"}
MODE_HINTS = {
    "auto": "Firmware curve for the current thermal profile",
    "quiet": "Fan off below 50 °C, gentle ramp",
    "cool": "Always spinning, full speed by 85 °C",
    "max": "100 % at every temperature",
    "custom": "Your own curve (kelvin fan … curve)",
}


def validate_curve(curve: Curve) -> str | None:
    """Mirror of the root helper's rules, for friendly errors before auth."""
    if len(curve) != 8:
        return "A curve needs exactly 8 points."
    prev_t, prev_p = -1, -1
    for t, p in curve:
        if not 20 <= t <= 100:
            return f"Temperature {t} °C is out of range (20–100)."
        if not 0 <= p <= 255:
            return f"Fan speed {p} is out of range (0–255)."
        if t <= prev_t:
            return "Temperatures must strictly increase."
        if p < prev_p:
            return "Fan speed must not decrease as temperature rises."
        if t >= 70 and p < 64:
            return f"Unsafe: at {t} °C the fan must run at least 25 %."
        if t >= 80 and p < 128:
            return f"Unsafe: at {t} °C the fan must run at least 50 %."
        prev_t, prev_p = t, p
    if curve[-1][0] > 95 or curve[-1][1] < 128:
        return "Unsafe: the last point must be at or below 95 °C and at least 50 %."
    return None


def parse_curve(text: str) -> Curve:
    """'40:0,50:40,...' -> [(40, 0), ...]; pwm may be given as a percentage ('50%')."""
    curve = []
    for part in text.replace(" ", "").split(","):
        t, _, p = part.partition(":")
        if not t.isdigit() or not p:
            raise ValueError(f"invalid point '{part}' (expected TEMP:SPEED)")
        if p.endswith("%"):
            if not p[:-1].isdigit():
                raise ValueError(f"invalid speed '{p}'")
            pwm = round(int(p[:-1]) * 255 / 100)
        elif p.isdigit():
            pwm = int(p)
        else:
            raise ValueError(f"invalid speed '{p}'")
        curve.append((int(t), pwm))
    return curve


def pct(pwm: int) -> int:
    return round(pwm * 100 / 255)


@dataclass
class FanState:
    fan: str
    rpm: int | None
    curve: Curve
    custom_enabled: bool | None  # pwmN_enable == 1


@dataclass
class FanInfo:
    fans: dict[str, FanState] = field(default_factory=dict)
    curves_supported: bool = False
    profile: str | None = None  # PPD profile
    profiles: list[str] = field(default_factory=list)
    platform_profile: str | None = None
    ppd_available: bool = False

    @property
    def any_fan(self) -> bool:
        return bool(self.fans)


def _hwmon(name: str):
    root = paths.SYSFS / "class/hwmon"
    try:
        for hwmon in sorted(root.iterdir()):
            if read(hwmon / "name") == name:
                return hwmon
    except OSError:
        pass
    return None


def _ppd() -> tuple[str | None, list[str]]:
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        (props,) = bus.call_sync(
            PPD, PPD_PATH, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (PPD,)),
            GLib.VariantType.new("(a{sv})"), Gio.DBusCallFlags.NONE, 1500, None,
        ).unpack()
        names = [p.get("Profile") for p in props.get("Profiles", []) if p.get("Profile")]
        return props.get("ActiveProfile"), names
    except Exception:  # noqa: BLE001 - PPD not running
        return None, []


def read_fans() -> FanInfo:
    info = FanInfo()
    speeds = _hwmon("asus")
    curves = _hwmon("asus_custom_fan_curve")
    info.curves_supported = curves is not None
    for fan, n in FAN_INDEX.items():
        rpm = None
        if speeds is not None:
            label = read(speeds / f"fan{n}_label")
            if label is None and not (speeds / f"fan{n}_input").exists():
                continue
            rpm = read_int(speeds / f"fan{n}_input")
        curve: Curve = []
        enabled = None
        if curves is not None and (curves / f"pwm{n}_enable").exists():
            for i in range(1, 9):
                t = read_int(curves / f"pwm{n}_auto_point{i}_temp")
                p = read_int(curves / f"pwm{n}_auto_point{i}_pwm")
                if t is not None and p is not None:
                    curve.append((t, p))
            enabled = read(curves / f"pwm{n}_enable") == "1"
        if rpm is None and not curve:
            continue
        info.fans[fan] = FanState(fan, rpm, curve, enabled)
    info.platform_profile = read(paths.SYSFS / "firmware/acpi/platform_profile")
    info.profile, info.profiles = _ppd()
    info.ppd_available = info.profile is not None
    return info


# -- actions -------------------------------------------------------------------------


@dataclass
class FanResult:
    ok: bool
    changed: bool
    message: str


def set_profile(profile: str) -> FanResult:
    if profile not in PROFILES:
        return FanResult(False, False, f"Unknown profile '{profile}'.")
    current, available = _ppd()
    if current is None:
        return FanResult(False, False, "power-profiles-daemon is not running; Kelvin will not write the platform profile directly.")
    if profile not in available:
        return FanResult(False, False, f"This system does not offer the {PROFILE_LABELS[profile]} profile.")
    if current == profile:
        return FanResult(True, False, f"Thermal profile is already {PROFILE_LABELS[profile]}.")
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        bus.call_sync(
            PPD, PPD_PATH, "org.freedesktop.DBus.Properties", "Set",
            GLib.Variant("(ssv)", (PPD, "ActiveProfile", GLib.Variant("s", profile))),
            None, Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 5000, None,
        )
    except GLib.Error as exc:
        return FanResult(False, False, f"power-profiles-daemon refused the change: {exc.message}")
    return FanResult(True, True, f"Thermal profile set to {PROFILE_LABELS[profile]}.")


def target_curve(mode: str, custom: Curve | None) -> Curve | None:
    if mode == "custom":
        return custom
    return PRESETS.get(mode)


def needs_apply(state: FanState | None, mode: str, custom: Curve | None) -> bool:
    if state is None:
        return False
    if mode == "auto":
        return bool(state.custom_enabled)
    curve = target_curve(mode, custom)
    return curve is not None and (not state.custom_enabled or state.curve != curve)


def _helper_args(fan: str, mode: str, custom: Curve | None) -> list[str] | str:
    if fan not in FANS:
        return f"Unknown fan '{fan}'."
    if mode == "auto":
        return ["auto", fan]
    curve = target_curve(mode, custom)
    if curve is None:
        return "No custom curve is saved for this fan." if mode == "custom" else f"Unknown fan mode '{mode}'."
    error = validate_curve(curve)
    if error:
        return error
    return ["set", fan, ",".join(f"{t}:{p}" for t, p in curve)]


def _verify(fan: str, mode: str, custom: Curve | None) -> FanResult:
    state = read_fans().fans.get(fan)
    label = FAN_LABELS[fan]
    if state is None:
        return FanResult(False, True, f"The {label} disappeared.")
    if mode == "auto":
        if state.custom_enabled:
            return FanResult(False, True, f"The {label} still reports a custom curve.")
        return FanResult(True, True, f"{label}: firmware curve (Auto).")
    if not state.custom_enabled or state.curve != target_curve(mode, custom):
        return FanResult(False, True, f"The kernel did not keep the {label} curve.")
    return FanResult(True, True, f"{label}: {MODE_LABELS[mode]} curve applied.")


def apply_sync(fan: str, mode: str, custom: Curve | None = None) -> FanResult:
    args = _helper_args(fan, mode, custom)
    if isinstance(args, str):
        return FanResult(False, False, args)
    if not read_fans().curves_supported:
        return FanResult(False, False, "This system has no ASUS custom fan curves.")
    result = privilege.run_sync(paths.FAN_HELPER, *args)
    if not result.ok:
        return FanResult(False, False, result.message)
    return _verify(fan, mode, custom)


def apply_async(fan: str, mode: str, custom: Curve | None, callback: Callable[[FanResult], None]) -> None:
    from gi.repository import GLib

    args = _helper_args(fan, mode, custom)
    if isinstance(args, str):
        GLib.idle_add(lambda: callback(FanResult(False, False, args)) and False)
        return

    def done(result: privilege.HelperResult) -> None:
        callback(_verify(fan, mode, custom) if result.ok else FanResult(False, False, result.message))

    privilege.run_async(paths.FAN_HELPER, args, done)
