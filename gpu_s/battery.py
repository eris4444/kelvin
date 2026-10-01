"""AC and battery state from UPower (D-Bus), with a sysfs fallback."""

from __future__ import annotations

from dataclasses import dataclass

from . import paths
from .sysfs import read, read_int

UPOWER = "org.freedesktop.UPower"
UPOWER_PATH = "/org/freedesktop/UPower"
DEVICE_IFACE = "org.freedesktop.UPower.Device"

_STATES = {
    0: "Unknown",
    1: "Charging",
    2: "Discharging",
    3: "Empty",
    4: "Fully charged",
    5: "Not charging",
    6: "Pending discharge",
}


@dataclass
class BatteryInfo:
    name: str
    percent: float | None
    state: str
    energy_rate_w: float | None  # UPower: positive magnitude, direction from state
    energy_wh: float | None
    energy_full_wh: float | None
    energy_design_wh: float | None
    time_to_empty_s: int | None
    time_to_full_s: int | None
    charge_start: int | None
    charge_end: int | None
    threshold_enabled: bool | None
    threshold_supported: bool | None
    sysfs_end_threshold: int | None
    cycles: int | None

    @property
    def health_percent(self) -> float | None:
        if self.energy_full_wh and self.energy_design_wh:
            return 100.0 * self.energy_full_wh / self.energy_design_wh
        return None

    @property
    def discharging(self) -> bool:
        return self.state == "Discharging"


@dataclass
class PowerSupply:
    on_ac: bool | None
    ac_names: list[str]
    battery: BatteryInfo | None
    source: str  # upower | sysfs


def ac_online_sysfs() -> tuple[bool | None, list[str]]:
    root = paths.SYSFS / "class/power_supply"
    online, seen = [], False
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None, []
    for supply in entries:
        kind = read(supply / "type")
        if kind not in ("Mains", "USB"):
            continue
        state = read(supply / "online")
        if state is None:
            continue
        seen = True
        if state == "1":
            online.append(supply.name)
    return (bool(online) if seen else None), online


def _battery_sysfs() -> BatteryInfo | None:
    root = paths.SYSFS / "class/power_supply"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for bat in entries:
        if read(bat / "type") != "Battery" or read(bat / "present") == "0":
            continue

        def wh(attr: str) -> float | None:
            value = read_int(bat / attr)
            return value / 1e6 if value is not None else None

        power = read_int(bat / "power_now")
        return BatteryInfo(
            name=bat.name,
            percent=read_int(bat / "capacity"),
            state=(read(bat / "status") or "Unknown").replace("Full", "Fully charged"),
            energy_rate_w=power / 1e6 if power is not None else None,
            energy_wh=wh("energy_now"),
            energy_full_wh=wh("energy_full"),
            energy_design_wh=wh("energy_full_design"),
            time_to_empty_s=None,
            time_to_full_s=None,
            charge_start=read_int(bat / "charge_control_start_threshold"),
            charge_end=read_int(bat / "charge_control_end_threshold"),
            threshold_enabled=None,
            threshold_supported=None,
            sysfs_end_threshold=read_int(bat / "charge_control_end_threshold"),
            cycles=read_int(bat / "cycle_count") or None,
        )
    return None


def _sysfs_end_threshold(name: str) -> int | None:
    return read_int(paths.SYSFS / "class/power_supply" / name / "charge_control_end_threshold")


def _upower() -> PowerSupply | None:
    try:
        from gi.repository import Gio, GLib
    except ImportError:
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

        def call(path: str, iface: str, method: str, params, reply: str):
            return bus.call_sync(
                UPOWER, path, iface, method, params,
                GLib.VariantType.new(reply), Gio.DBusCallFlags.NONE, 1500, None,
            ).unpack()

        on_battery = call(
            UPOWER_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (UPOWER, "OnBattery")), "(v)",
        )[0]
        (devices,) = call(UPOWER_PATH, UPOWER, "EnumerateDevices", None, "(ao)")
        battery, ac_names = None, []
        for path in devices:
            (props,) = call(
                path, "org.freedesktop.DBus.Properties", "GetAll",
                GLib.Variant("(s)", (DEVICE_IFACE,)), "(a{sv})",
            )
            kind = props.get("Type")
            if kind == 1 and props.get("Online"):  # line power
                ac_names.append(props.get("NativePath", path.rsplit("/", 1)[-1]))
            if kind == 2 and props.get("PowerSupply") and props.get("IsPresent") and battery is None:
                name = props.get("NativePath") or path.rsplit("/", 1)[-1]
                battery = BatteryInfo(
                    name=name,
                    percent=props.get("Percentage"),
                    state=_STATES.get(props.get("State", 0), "Unknown"),
                    energy_rate_w=props.get("EnergyRate"),
                    energy_wh=props.get("Energy"),
                    energy_full_wh=props.get("EnergyFull"),
                    energy_design_wh=props.get("EnergyFullDesign"),
                    time_to_empty_s=props.get("TimeToEmpty") or None,
                    time_to_full_s=props.get("TimeToFull") or None,
                    charge_start=props.get("ChargeStartThreshold"),
                    charge_end=props.get("ChargeEndThreshold"),
                    threshold_enabled=props.get("ChargeThresholdEnabled"),
                    threshold_supported=props.get("ChargeThresholdSupported"),
                    sysfs_end_threshold=_sysfs_end_threshold(name),
                    cycles=(props.get("ChargeCycles") or 0) if (props.get("ChargeCycles") or 0) > 0 else None,
                )
        return PowerSupply(on_ac=not on_battery, ac_names=ac_names, battery=battery, source="upower")
    except Exception:  # noqa: BLE001 - UPower missing or D-Bus unavailable
        return None


def read_power_supply() -> PowerSupply:
    info = _upower()
    if info is not None:
        return info
    on_ac, names = ac_online_sysfs()
    return PowerSupply(on_ac=on_ac, ac_names=names, battery=_battery_sysfs(), source="sysfs")
