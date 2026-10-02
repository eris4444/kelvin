"""Background profile agent: follows AC/battery changes and applies profiles.

Event driven: UPower's OnBattery property (system bus) and logind's
PrepareForSleep signal. A slow sysfs poll is kept as a safety net in case
UPower is unavailable.
"""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import Gio, GLib

import time

from . import charge, fans, notify, power_manager
from .battery import ac_online_sysfs, read_power_supply
from .config import Config, load_config, load_state, save_state
from .policy import Mode, target_mode

log = logging.getLogger("kelvin.agent")

DEBOUNCE_MS = 1500
POLL_SECONDS = 15

# UPower battery State values
CHARGING, DISCHARGING, FULLY_CHARGED, PENDING_CHARGE = 1, 2, 4, 5


def charge_transition(old: int | None, new: int, on_ac: bool | None) -> str | None:
    """'stopped' / 'resumed' when charging really changed while plugged in."""
    if old is None or old == new or not on_ac:
        return None
    if old == CHARGING and new in (FULLY_CHARGED, PENDING_CHARGE):
        return "stopped"
    if old in (FULLY_CHARGED, PENDING_CHARGE) and new == CHARGING:
        return "resumed"
    return None


class ProfileAgent:
    def __init__(self, get_config: Callable[[], Config], on_change: Callable[[str | None], None]):
        self._get_config = get_config
        self._on_change = on_change  # UI refresh hook; receives a status message
        self._bus: Gio.DBusConnection | None = None
        self._subs: list[int] = []
        self._debounce_id = 0
        self._poll_id = 0
        self._busy = False
        self._battery_state: int | None = None
        self._fan_busy = False
        self._fan_fail_until: dict[str, float] = {}
        self._fan_timer = 0
        self.on_ac: bool | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._subs.append(
                self._bus.signal_subscribe(
                    "org.freedesktop.UPower", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                    "/org/freedesktop/UPower", None, Gio.DBusSignalFlags.NONE, self._on_upower,
                )
            )
            info = charge.read_charge(check_units=False)
            if info.upower_path:
                self._battery_state = self._read_battery_state(info.upower_path)
                self._subs.append(
                    self._bus.signal_subscribe(
                        "org.freedesktop.UPower", "org.freedesktop.DBus.Properties", "PropertiesChanged",
                        info.upower_path, None, Gio.DBusSignalFlags.NONE, self._on_battery,
                    )
                )
            # Changing the thermal profile drops custom fan curves in the kernel.
            self._subs.append(
                self._bus.signal_subscribe(
                    fans.PPD, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                    fans.PPD_PATH, None, Gio.DBusSignalFlags.NONE,
                    lambda *_a: self.schedule_fan_reapply(1500),
                )
            )
            self._subs.append(
                self._bus.signal_subscribe(
                    "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                    "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self._on_sleep,
                )
            )
        except GLib.Error as exc:
            log.warning("system bus unavailable, polling sysfs only: %s", exc.message)
        self._poll_id = GLib.timeout_add_seconds(POLL_SECONDS, self._poll)
        self.on_ac = read_power_supply().on_ac
        self.evaluate("startup")
        self.schedule_fan_reapply(3000)

    def stop(self) -> None:
        if self._bus:
            for sub in self._subs:
                self._bus.signal_unsubscribe(sub)
        self._subs.clear()
        for source in (self._debounce_id, self._poll_id):
            if source:
                GLib.source_remove(source)
        self._debounce_id = self._poll_id = 0

    # -- events --------------------------------------------------------------

    def _on_upower(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        _iface_name, changed, _invalidated = params.unpack()
        if "OnBattery" in changed:
            self._schedule_source_check()

    def _read_battery_state(self, path: str) -> int | None:
        try:
            reply = self._bus.call_sync(
                "org.freedesktop.UPower", path, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", ("org.freedesktop.UPower.Device", "State")),
                GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 2000, None,
            )
            return reply.unpack()[0]
        except GLib.Error:
            return None

    def _on_battery(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        _iface_name, changed, _invalidated = params.unpack()
        if "State" not in changed:
            return
        old, new = self._battery_state, changed["State"]
        self._battery_state = new
        info = charge.read_charge(check_units=False)
        event = charge_transition(old, new, info.ac_online)
        config = self._get_config()
        if event and config.notifications and config.battery_notifications and info.percent is not None:
            notify.send("Kelvin", f"Battery charging {event} at {info.percent:.0f}%.")
        self._on_change(None)

    def _on_sleep(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        (going_to_sleep,) = params.unpack()
        if not going_to_sleep:
            # Resumed: the power source may have changed while asleep.
            GLib.timeout_add_seconds(2, lambda: self._check_source("resume") and False)
            self.schedule_fan_reapply(4000)

    def _poll(self) -> bool:
        self.schedule_fan_reapply(0)
        on_ac, _ = ac_online_sysfs()
        if on_ac is not None and on_ac != self.on_ac:
            self._schedule_source_check()
        return True

    def _schedule_source_check(self) -> None:
        # Plugging in can bounce; wait for it to settle.
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(DEBOUNCE_MS, self._debounced)

    def _debounced(self) -> bool:
        self._debounce_id = 0
        self._check_source("source-changed")
        return False

    def _check_source(self, reason: str) -> None:
        on_ac = read_power_supply().on_ac
        changed = on_ac != self.on_ac
        self.on_ac = on_ac
        if changed or reason == "resume":
            self.evaluate("source-changed" if changed else reason)
        else:
            self._on_change(None)

    # -- decisions -------------------------------------------------------------

    def evaluate(self, reason: str) -> None:
        """Work out which mode should be active and apply it if needed."""
        if self._busy:
            GLib.timeout_add(500, lambda: self.evaluate(reason) and False)
            return
        config = self._get_config()
        state = load_state()
        on_ac = self.on_ac

        if reason == "source-changed" and state.manual_mode and config.auto_switch:
            state.manual_mode = None
            state.manual_on_ac = None
        state.last_on_ac = on_ac
        save_state(state)

        mode, applied_by = target_mode(
            config.ac_mode, config.battery_mode, on_ac,
            state.manual_mode, state.manual_on_ac, config.auto_switch,
        )
        if mode is None:
            self._on_change(None)
            return

        self._busy = True

        def done(result: power_manager.ApplyResult) -> None:
            self._busy = False
            if result.ok and result.changed:
                self._announce(mode, applied_by, reason, on_ac)
            elif not result.ok:
                log.info("profile %s not applied: %s", mode.value, result.message)
            self._on_change(result.message if result.ok and result.changed else None)

        power_manager.apply_async(mode, applied_by, on_ac, done)

    def _announce(self, mode: Mode, applied_by: str, reason: str, on_ac: bool | None) -> None:
        if not self._get_config().notifications or applied_by == "manual":
            return
        if reason == "source-changed":
            if on_ac:
                notify.send("Kelvin", f"AC power detected.\nGPU switched to {mode.label}.")
            else:
                notify.send("Kelvin", f"Running on battery.\nSwitched to Battery {mode.label} mode.")
        elif reason in ("startup", "resume"):
            source = "AC" if on_ac else "Battery"
            notify.send("Kelvin", f"{source} profile applied.\nGPU set to {mode.label}.")


    # -- fan curves -------------------------------------------------------------

    def schedule_fan_reapply(self, delay_ms: int) -> None:
        """Re-apply saved (non-Auto) fan curves the kernel has dropped."""
        if self._fan_timer:
            GLib.source_remove(self._fan_timer)

        def fire() -> bool:
            self._fan_timer = 0
            self.reapply_fans()
            return False

        self._fan_timer = GLib.timeout_add(max(1, delay_ms), fire)

    def reapply_fans(self) -> None:
        if self._fan_busy:
            return
        # Read from disk: the CLI may have changed the fan mode a moment ago.
        config = load_config()
        wanted = [(f, getattr(config, f"fan_{f}"), getattr(config, f"fan_{f}_curve")) for f in fans.FANS]
        wanted = [(f, m, c) for f, m, c in wanted if m != "auto"]
        if not wanted:
            return
        info = fans.read_fans()
        now = time.time()
        todo = [
            (f, m, [tuple(p) for p in c] if c else None)
            for f, m, c in wanted
            if self._fan_fail_until.get(f, 0) <= now
            and fans.needs_apply(info.fans.get(f), m, [tuple(p) for p in c] if c else None)
        ]
        if not todo:
            return
        self._fan_busy = True

        def step(index: int) -> None:
            if index >= len(todo):
                self._fan_busy = False
                self._on_change(None)
                return
            fan, mode, curve = todo[index]

            def done(result: fans.FanResult) -> None:
                if result.ok:
                    self._fan_fail_until.pop(fan, None)
                else:
                    self._fan_fail_until[fan] = time.time() + 300
                    log.warning("fan curve %s/%s not re-applied: %s", fan, mode, result.message)
                step(index + 1)

            fans.apply_async(fan, mode, curve, done)

        step(0)
