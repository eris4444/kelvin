"""Sleep tab (stay awake, screensaver, lock, screen off, suspend) and its Overview card."""

from __future__ import annotations

from gi.repository import Gtk

from .. import idle
from ..idle import IdleSettings
from .widgets import Card, StatusPill, Tag, label

STAGE_HINTS = {
    "screensaver": "Omarchy's animated screensaver",
    "lock": "Locks the session (Omarchy blanks the screen 5 s after locking)",
    "screen_off": "Turns the displays off (DPMS); any input turns them back on",
    "sleep_ac": "Suspends the laptop while plugged in",
    "sleep_battery": "Suspends the laptop while running on battery",
}
STAGE_ICONS = {
    "screensaver": "preferences-desktop-screensaver-symbolic",
    "lock": "system-lock-screen-symbolic",
    "screen_off": "video-display-symbolic",
    "sleep_ac": "ac-adapter-symbolic",
    "sleep_battery": "battery-level-50-symbolic",
}
LID_LABELS = {"suspend": "Sleep", "ignore": "Do nothing", "lock": "Lock", "poweroff": "Shut down",
              "hibernate": "Hibernate", "hybrid-sleep": "Hybrid sleep", "suspend-then-hibernate": "Sleep, then hibernate"}


class SleepSummary(Card):
    def __init__(self, open_page):
        self.pill = StatusPill()
        super().__init__("Sleep & lock", "system-lock-screen-symbolic", suffix=self.pill)
        self.rows = label("", "gs-row-value", wrap=True)
        self.append(self.rows)
        button = Gtk.Button(label="Sleep settings", halign=Gtk.Align.START)
        button.add_css_class("gs-action")
        button.connect("clicked", lambda *_: open_page())
        self.append(button)

    def update(self, s: IdleSettings | None) -> None:
        if s is None:
            return
        if s.stay_awake:
            self.pill.set("warn", "STAY AWAKE")
        else:
            self.pill.set("good", "IDLE TIMERS ON")
        f = idle.fmt_duration
        self.rows.set_label(
            f"Screensaver {f(s.screensaver)} · Lock {f(s.lock)} · Screen off {f(s.screen_off)}\n"
            f"Sleep: on AC {f(s.sleep_ac)} · on battery {f(s.sleep_battery)}"
        )


class SleepPage:
    def __init__(self, page: Gtk.Box, app):
        self.app = app
        self._syncing = False

        awake = Card("Stay Awake", "weather-clear-night-symbolic")
        row = Gtk.Box(spacing=12)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        texts.append(label("Keep the system awake", "gs-proc-name"))
        texts.append(label("Pauses every idle timer below: no screensaver, lock, screen off or idle sleep. "
                           "Same switch as Omarchy's stay-awake indicator.", "gs-faint", wrap=True))
        self.awake_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.awake_switch.connect("notify::active", self._on_awake)
        row.append(texts)
        row.append(self.awake_switch)
        awake.append(row)
        page.append(awake)

        timers = Card("Idle Timers", "preferences-system-time-symbolic")
        timers.append(label("Each stage has its own timer, counted from your last input. "
                            "Video playback and apps that inhibit idle hold every timer off.", "gs-sub", wrap=True))
        self.rows: dict[str, tuple[Gtk.DropDown, Gtk.StringList, list[int]]] = {}
        for stage in idle.STAGES:
            row = Gtk.Box(spacing=10)
            icon = Gtk.Image(icon_name=STAGE_ICONS[stage], pixel_size=16, valign=Gtk.Align.CENTER)
            icon.add_css_class("gs-card-icon")
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            head = Gtk.Box(spacing=6)
            head.append(label(idle.STAGE_LABELS[stage], "gs-proc-name"))
            owner = Tag("kernel" if stage in idle.OMARCHY_STAGES else "computed",
                        "omarchy" if stage in idle.OMARCHY_STAGES else "kelvin")
            head.append(owner)
            texts.append(head)
            texts.append(label(STAGE_HINTS[stage], "gs-faint", wrap=True))
            model = Gtk.StringList()
            dropdown = Gtk.DropDown(model=model, valign=Gtk.Align.CENTER)
            dropdown.add_css_class("gs-dropdown")
            dropdown.connect("notify::selected", self._on_choice, stage)
            row.append(icon)
            row.append(texts)
            row.append(dropdown)
            timers.append(row)
            self.rows[stage] = (dropdown, model, [])
        self.order_note = label("", "gs-faint", wrap=True)
        timers.append(self.order_note)
        page.append(timers)

        lid = Card("Lid", "computer-symbolic")
        self.lid = label("", "gs-row-value", wrap=True)
        lid.append(self.lid)
        lid.append(label("Set by systemd-logind (/etc/systemd/logind.conf). Kelvin shows it but does not change it.",
                         "gs-faint", wrap=True))
        page.append(lid)

    # -- events ---------------------------------------------------------------------

    def _on_awake(self, switch, _pspec) -> None:
        if not self._syncing:
            self.app.request_stay_awake(switch.get_active())

    def _on_choice(self, dropdown, _pspec, stage: str) -> None:
        if self._syncing:
            return
        _dd, _model, values = self.rows[stage]
        index = dropdown.get_selected()
        if 0 <= index < len(values):
            self.app.request_idle_stage(stage, values[index])

    # -- update ---------------------------------------------------------------------

    def update(self, s: IdleSettings | None, busy: bool) -> None:
        if s is None:
            return
        self._syncing = True
        try:
            self.awake_switch.set_active(s.stay_awake)
            self.awake_switch.set_sensitive(s.omarchy_available and not busy)
            for stage, (dropdown, model, values) in self.rows.items():
                current = s.get(stage)
                choices = sorted(set(idle.CHOICES) | {current})
                if choices != values:
                    values[:] = choices
                    model.splice(0, model.get_n_items(), [idle.fmt_duration(v) for v in choices])
                dropdown.set_selected(values.index(current))
                omarchy = stage in idle.OMARCHY_STAGES
                dropdown.set_sensitive(not busy and (not omarchy or s.omarchy_available))
                dropdown.get_parent().set_opacity(0.55 if s.stay_awake else 1.0)

            notes = []
            if s.lock and s.screensaver and s.screensaver >= s.lock:
                notes.append("The screensaver is set at or after the lock, so it never shows (Omarchy locks first).")
            if s.screen_off and s.lock and s.screen_off > s.lock:
                notes.append("Screen off is later than the lock; the lock screen already blanks the display after 5 s.")
            if (s.sleep_ac or s.sleep_battery) and not s.plugin_enabled:
                notes.append("The Kelvin idle plugin is not enabled in the Omarchy shell, so screen off and sleep timers are inactive.")
            self.order_note.set_label(" ".join(notes))
            self.order_note.set_visible(bool(notes))

            lid = LID_LABELS.get(s.lid_action or "", s.lid_action or "unknown")
            lid_ac = LID_LABELS.get(s.lid_action_ac or "", s.lid_action_ac or "unknown")
            self.lid.set_label(f"Closing the lid: {lid} (on battery) · {lid_ac} (plugged in)")
        finally:
            self._syncing = False
