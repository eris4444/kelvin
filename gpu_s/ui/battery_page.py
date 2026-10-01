"""Battery tab (charge limits) and the Overview battery card."""

from __future__ import annotations

from gi.repository import Gtk

from .. import charge
from ..charge import ChargeInfo
from .widgets import Callout, Card, InfoRow, StatusPill, label, revealed

STATE_LABELS = {"Full": "Fully charged", "Not charging": "Not charging", "Charging": "Charging", "Discharging": "Discharging"}


def battery_icon(info: ChargeInfo | None) -> str:
    if not info or info.percent is None:
        return "battery-missing-symbolic"
    level = min(100, max(0, int(round(info.percent / 10.0)) * 10))
    suffix = "-charging" if info.charging else ""
    return f"battery-level-{level}{suffix}-symbolic"


def state_text(info: ChargeInfo) -> str:
    text = STATE_LABELS.get(info.status or "", info.status or "Unknown")
    if info.status == "Not charging" and info.ac_online and info.limit_active:
        text += f" — held at the {info.kernel_end}% limit"
    return text


def limit_text(info: ChargeInfo) -> str:
    if not info.supported:
        return "No charge limit support"
    if not info.limit_active:
        return "No limit — charges to 100%"
    if info.start_supported and info.kernel_start:
        return f"{info.kernel_start}% → {info.kernel_end}%"
    return f"Stops at {info.kernel_end}%"


def source_pill(pill: StatusPill, info: ChargeInfo) -> None:
    if info.charging:
        pill.set("good", "CHARGING", pulse=True)
    elif info.ac_online:
        pill.set("good", "AC CONNECTED")
    elif info.ac_online is False:
        pill.set("warn", "ON BATTERY")
    else:
        pill.set("off", "UNKNOWN")


class BatterySummary(Card):
    """Compact battery card for the Overview dashboard."""

    def __init__(self, open_page):
        self.pill = StatusPill()
        super().__init__("Battery · Charge control", "battery-level-80-symbolic", suffix=self.pill)
        row = Gtk.Box(spacing=14)
        self.icon = Gtk.Image(pixel_size=30, valign=Gtk.Align.CENTER)
        self.icon.add_css_class("gs-card-icon")
        self.percent = label("—", "gs-big-number")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.name = label("", "gs-proc-name")
        self.detail = label("", "gs-sub", wrap=True)
        texts.append(self.name)
        texts.append(self.detail)
        row.append(self.icon)
        row.append(self.percent)
        row.append(texts)
        self.append(row)
        self.limit = label("", "gs-row-value", wrap=True)
        self.append(self.limit)
        button = Gtk.Button(label="Battery controls", halign=Gtk.Align.START)
        button.add_css_class("gs-action")
        button.connect("clicked", lambda *_: open_page())
        self.append(button)

    def update(self, info: ChargeInfo | None) -> None:
        if info is None or info.battery is None:
            self.percent.set_label("—")
            self.name.set_label("No battery detected")
            self.detail.set_label("")
            self.limit.set_label("")
            self.pill.set("off", "NONE")
            return
        self.icon.set_from_icon_name(battery_icon(info))
        self.percent.set_label(f"{info.percent:.0f}%" if info.percent is not None else "—")
        self.name.set_label(f"{info.battery} · {state_text(info)}")
        health = f"{info.health_percent:.1f}% health" if info.health_percent else "health unknown"
        self.detail.set_label(health)
        self.limit.set_label("Charge limit: " + limit_text(info))
        source_pill(self.pill, info)


class BatteryPage:
    """Builds the Battery tab into `page` and keeps it updated."""

    def __init__(self, page: Gtk.Box, app):
        self.app = app
        self._info: ChargeInfo | None = None
        self._syncing = False
        self._dirty = False

        # Level
        self.pill = StatusPill()
        hero = Card("Battery", "battery-level-80-symbolic", suffix=self.pill)
        top = Gtk.Box(spacing=14)
        self.percent = label("—", "gs-big-number")
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, valign=Gtk.Align.CENTER)
        self.state = label("", "gs-sub", wrap=True)
        self.level = Gtk.LevelBar(min_value=0, max_value=100, value=0)
        self.level.add_css_class("gs-battery")
        self.level.add_offset_value("low", 20)
        right.append(self.state)
        right.append(self.level)
        top.append(self.percent)
        top.append(right)
        hero.append(top)
        self.identity = label("", "gs-faint")
        hero.append(self.identity)
        page.append(hero)

        # Charge limit
        limit = Card("Charge Limit", "battery-level-80-charging-symbolic")
        self.limit_status = label("", "gs-sub", wrap=True)
        limit.append(self.limit_status)

        presets = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
            min_children_per_line=1, max_children_per_line=3, column_spacing=8, row_spacing=8,
        )
        self.preset_buttons: dict[str, tuple[Gtk.Button, Gtk.Label]] = {}
        for preset in charge.PRESETS:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(label(preset.label, "gs-preset-title", xalign=0.5, wrap=True))
            desc = label("", "gs-faint", xalign=0.5, wrap=True)
            desc.set_justify(Gtk.Justification.CENTER)
            box.append(desc)
            button = Gtk.Button(child=box)
            button.add_css_class("gs-preset")
            button.connect("clicked", lambda _b, p=preset: self.app.confirm_charge_preset(p))
            presets.append(button)
            button.get_parent().set_focusable(False)
            self.preset_buttons[preset.key] = (button, desc)
        limit.append(presets)

        stop_row = Gtk.Box(spacing=10)
        stop_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        stop_texts.append(label("Stop Charging At", "gs-proc-name"))
        stop_texts.append(label(f"Custom maximum charge ({charge.MIN_END}–{charge.MAX_END}%)", "gs-faint", wrap=True))
        self.end_spin = Gtk.SpinButton.new_with_range(charge.MIN_END, charge.MAX_END, 1)
        self.end_spin.set_valign(Gtk.Align.CENTER)
        self.end_spin.add_css_class("gs-spin")
        self.end_spin.connect("value-changed", self._on_spin)
        self.apply = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        self.apply.add_css_class("gs-action")
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", self._on_apply)
        stop_row.append(stop_texts)
        stop_row.append(self.end_spin)
        stop_row.append(self.apply)
        limit.append(stop_row)

        start_row = Gtk.Box(spacing=10)
        start_texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        start_texts.append(label("Start Charging At", "gs-proc-name"))
        self.start_note = label("", "gs-faint", wrap=True)
        start_texts.append(self.start_note)
        self.start_spin = Gtk.SpinButton.new_with_range(0, 99, 1)
        self.start_spin.set_valign(Gtk.Align.CENTER)
        self.start_spin.add_css_class("gs-spin")
        start_row.append(start_texts)
        start_row.append(self.start_spin)
        limit.append(start_row)

        self.callout = Callout("warn")
        limit.append(revealed(self.callout))
        limit.append(
            label(
                "Applied by UPower to the kernel threshold (charge_control_end_threshold, ASUS firmware). "
                "UPower re-applies it at every boot and resume, so GPU-S does not need to keep running.",
                "gs-faint",
                wrap=True,
            )
        )
        page.append(limit)

        # Details
        details = Card("Battery Information", "dialog-information-symbolic")
        self.r_level = InfoRow("Level", "measured")
        self.r_state = InfoRow("State", "kernel")
        self.r_voltage = InfoRow("Voltage", "measured")
        self.r_energy = InfoRow("Current energy", "measured")
        self.r_full = InfoRow("Full energy", "measured")
        self.r_design = InfoRow("Design energy", "kernel")
        self.r_health = InfoRow("Battery health", "computed")
        self.r_cycles = InfoRow("Charge cycles")
        self.r_ac = InfoRow("AC adapter", "kernel")
        self.r_charging = InfoRow("Charging", "kernel")
        self.r_start = InfoRow("Charge start threshold", "kernel")
        self.r_end = InfoRow("Charge end threshold", "kernel")
        self.r_manager = InfoRow("Managed by")
        for row in (self.r_level, self.r_state, self.r_voltage, self.r_energy, self.r_full, self.r_design,
                    self.r_health, self.r_cycles, self.r_ac, self.r_charging, self.r_start, self.r_end, self.r_manager):
            details.append(row)
        page.append(details)

        notes = Card("Battery Notifications", "preferences-system-notifications-symbolic")
        row = Gtk.Box(spacing=12)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        texts.append(label("Charging notifications", "gs-proc-name"))
        texts.append(label("“Charging stopped at 80%”, “Charging resumed at 75%”, limit changes. Only on real changes.", "gs-faint", wrap=True))
        self.notify_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.notify_switch.connect("notify::active", self._on_notify_switch)
        row.append(texts)
        row.append(self.notify_switch)
        notes.append(row)
        page.append(notes)

    # -- events -------------------------------------------------------------------

    def _on_spin(self, _spin) -> None:
        if not self._syncing:
            self._dirty = True
            self._update_apply()

    def _on_apply(self, _button) -> None:
        end = int(self.end_spin.get_value())
        start = int(self.start_spin.get_value()) if self._info and self._info.start_supported else None
        self._dirty = False
        self.app.request_charge(end, start)

    def _on_notify_switch(self, switch, _pspec) -> None:
        if not self._syncing:
            self.app.update_config(battery_notifications=switch.get_active())

    def _current_end(self) -> int | None:
        info = self._info
        if not info or info.kernel_end is None:
            return None
        return info.kernel_end

    def _update_apply(self) -> None:
        current = self._current_end()
        value = int(self.end_spin.get_value())
        self.apply.set_sensitive(
            bool(self._info and self._info.supported and self._info.managed_by_upower)
            and not self.app.busy
            and value != current
        )

    # -- update -------------------------------------------------------------------

    def update(self, info: ChargeInfo | None, notifications: bool, busy: bool) -> None:
        self._info = info
        self._syncing = True
        try:
            self.notify_switch.set_active(notifications)
            if info is None or info.battery is None:
                self.percent.set_label("—")
                self.state.set_label("No battery detected")
                return
            self.percent.set_label(f"{info.percent:.0f}%" if info.percent is not None else "—")
            self.level.set_value(info.percent or 0)
            self.state.set_label(state_text(info))
            self.identity.set_label(f"{info.battery} · {info.vendor or ''} {info.model or ''}".strip())
            source_pill(self.pill, info)

            # limit
            if info.limit_active:
                status = f"Charging stops at {info.kernel_end}%. "
            elif info.supported:
                status = "No limit: the battery charges to 100%. "
            else:
                status = ""
            if info.managed_by_upower:
                status += "UPower charge limit: " + ("enabled" if info.upower_enabled else "disabled") + "."
            self.limit_status.set_label(status)

            match = charge.matching_preset(info)
            for preset in charge.PRESETS:
                button, desc = self.preset_buttons[preset.key]
                ok, why = charge.preset_status(info, preset)
                desc.set_label(why)
                button.set_sensitive(ok and not busy)
                button.set_tooltip_text(None if ok else why)
                if match and match.key == preset.key:
                    button.add_css_class("active")
                else:
                    button.remove_css_class("active")

            if not self._dirty and info.kernel_end is not None:
                self.end_spin.set_value(max(charge.MIN_END, info.kernel_end))
            self.end_spin.set_sensitive(info.supported and info.managed_by_upower and not busy)
            self._update_apply()

            if info.start_supported:
                self.start_spin.set_sensitive(not busy)
                if info.kernel_start is not None and not self._dirty:
                    self.start_spin.set_value(info.kernel_start)
                self.start_note.set_label("Charging resumes below this level (must be lower than the limit).")
            else:
                self.start_spin.set_sensitive(False)
                self.start_spin.set_value(0)
                self.start_note.set_label(
                    "Not supported by this battery: the kernel exposes only an end threshold, and the ASUS "
                    "firmware resumes charging on its own once the level drops below the limit."
                )

            warnings = []
            if not info.supported:
                warnings.append(("Charge limits are not supported", "This battery exposes no kernel charge threshold."))
            elif not info.managed_by_upower:
                warnings.append(("UPower does not manage this battery's threshold", "GPU-S will not write it directly."))
            for unit in info.legacy_units_enabled:
                warnings.append((f"{unit} is enabled",
                                 "It also writes the threshold at boot and may override the value set here. "
                                 "Disable it to let UPower manage the limit."))
            for path in info.foreign_overrides:
                warnings.append(("Another CHARGE_LIMIT override exists", f"{path} sets the limit; GPU-S will not override it."))
            if warnings:
                self.callout.set(warnings[0][0], warnings[0][1], kind="warn")
            self.callout.get_parent().set_reveal_child(bool(warnings))

            # details
            self.r_level.set(f"{info.percent:.0f} %" if info.percent is not None else "—")
            self.r_state.set(STATE_LABELS.get(info.status or "", info.status or "—"))
            self.r_voltage.set(f"{info.voltage_v:.2f} V" if info.voltage_v else "—")
            self.r_energy.set(f"{info.energy_wh:.1f} Wh" if info.energy_wh is not None else "—")
            self.r_full.set(f"{info.energy_full_wh:.1f} Wh" if info.energy_full_wh else "—")
            self.r_design.set(f"{info.energy_design_wh:.1f} Wh" if info.energy_design_wh else "—")
            self.r_health.set(f"{info.health_percent:.1f} %" if info.health_percent else "—")
            self.r_cycles.set(str(info.cycles) if info.cycles else "not reported by firmware")
            self.r_ac.set("Connected" if info.ac_online else "Disconnected" if info.ac_online is False else "—")
            self.r_charging.set("Yes" if info.charging else "No")
            self.r_start.set(f"{info.kernel_start} %" if info.start_supported and info.kernel_start is not None
                             else "not supported", tag="kernel" if info.start_supported else None)
            self.r_end.set(f"{info.kernel_end} %" if info.kernel_end is not None else "not supported")
            if info.managed_by_upower:
                configured = f"{'_' if not info.start_supported else info.upower_start},{info.upower_end}"
                origin = "GPU-S override" if info.override else "UPower default"
                self.r_manager.set(f"UPower ({configured}, {origin})", tooltip="/etc/udev/hwdb.d/61-gpu-s-battery.hwdb"
                                   if info.override else "/usr/lib/udev/hwdb.d/60-upower-battery.hwdb")
            else:
                self.r_manager.set("—")
        finally:
            self._syncing = False
