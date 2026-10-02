"""Kelvin main window."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from gi.repository import Adw, Gio, GLib, Gtk

from .. import APP_NAME
from ..config import Config, State
from ..hardware import Facts, PciDevice
from ..monitor import Snapshot
from ..policy import MODE_ICONS, MODE_LABELS, MODE_SUMMARIES, Mode, impact_estimate
from .battery_page import BatteryPage, BatterySummary
from .cooling_page import CoolingPage, CoolingSummary
from .sleep_page import SleepPage, SleepSummary
from .widgets import (
    Callout,
    Card,
    InfoRow,
    ProcessRow,
    Sparkline,
    StatTile,
    StatusPill,
    button,
    label,
    revealed,
    toggle_child,
)

MODE_ORDER = (Mode.AUTO, Mode.ALWAYS_ON, Mode.POWER_SAVING)
PROFILE_ORDER = (Mode.ALWAYS_ON, Mode.AUTO, Mode.POWER_SAVING)


@dataclass
class UiContext:
    config: Config
    state: State
    busy: bool
    tuning_enabled: bool


def gpu_display_name(dev: PciDevice | None, cached: str | None = None) -> str:
    if dev is None:
        return "—"
    if cached and dev.is_nvidia:
        return cached
    name = dev.pci_name or ""
    bracket = re.search(r"\[(.+)\]", name)
    short = bracket.group(1) if bracket else name
    return f"{dev.vendor_name} {short}".strip() if short else f"{dev.vendor_name} GPU"


def fmt(value, unit: str = "", digits: int = 0) -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=540, default_height=820)
        self.app = app
        self.set_size_request(360, 420)
        self.add_css_class("gs-window")
        self._updating = False
        self._last_snapshot: Snapshot | None = None
        self._power_down_requested_at: float | None = None
        self._scrollers: list[Gtk.ScrolledWindow] = []
        # Open with nothing focused and every page at the top (otherwise the
        # first focusable widget, e.g. a spin button, scrolls into view).
        self.connect("map", lambda *_: GLib.idle_add(self._reset_view))

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.stack = Adw.ViewStack(enable_transitions=True)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        menu = Gio.Menu()
        menu.append("Refresh", "app.refresh")
        menu.append("Settings", "app.settings")
        menu.append("About Kelvin", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        self.spinner = Adw.Spinner(visible=False)
        header.pack_start(self.spinner)
        toolbar.add_top_bar(header)

        self.banner = Adw.Banner(button_label="Undo")
        self.banner.connect("button-clicked", self._on_banner_undo)
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self.stack)
        switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        toolbar.add_bottom_bar(switcher_bar)

        overview = self._page("overview", "Overview", "view-grid-symbolic")
        self.gpu_page = self._page("power", "GPU", "gs-chip-symbolic")
        battery = self._page("battery", "Battery", "battery-level-80-symbolic")
        cooling = self._page("cooling", "Cooling", "gs-fan-symbolic")
        sleep = self._page("sleep", "Sleep", "system-lock-screen-symbolic")
        self._build_overview(overview)
        self._build_power()
        self._build_apps()
        self.battery_page = BatteryPage(battery, app)
        self.cooling_page = CoolingPage(cooling, app)
        self.sleep_page = SleepPage(sleep, app)

        # Narrow (e.g. a tiled Hyprland column): page switcher at the bottom,
        # metric tiles in a 2x2 grid.
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        narrow.add_setter(switcher_bar, "reveal", True)
        narrow.add_setter(header, "title-widget", Adw.WindowTitle(title=APP_NAME))
        narrow.add_setter(self.tiles, "max-children-per-line", 2)
        self.add_breakpoint(narrow)
        start_page = os.environ.get("KELVIN_PAGE")  # used by screenshot tests
        if start_page in ("overview", "power", "battery", "cooling", "sleep"):
            self.stack.set_visible_child_name(start_page)
        # Per-process GPU stats are only sampled while the GPU page is shown.
        self.stack.connect("notify::visible-child-name", lambda *_: self.app.refresh())

    # -- page scaffolding ------------------------------------------------------

    def _page(self, name: str, title: str, icon: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.add_css_class("gs-page")
        clamp = Adw.Clamp(maximum_size=620, tightening_threshold=480, child=box)
        # Don't jump to the first focusable widget (the mode selector) on open.
        viewport = Gtk.Viewport(child=clamp, scroll_to_focus=False)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=viewport)
        self.stack.add_titled_with_icon(scroller, name, title, icon)
        self._scrollers.append(scroller)
        return box

    # -- overview --------------------------------------------------------------

    def _build_overview(self, page: Gtk.Box) -> None:

        # Hero: GPU status
        hero = Card("GPU · Power management", "gs-chip-symbolic", css="gs-hero")
        top = Gtk.Box(spacing=12)
        icon = Gtk.Image(icon_name="gs-chip-symbolic", pixel_size=30, valign=Gtk.Align.CENTER)
        icon.add_css_class("gs-card-icon")
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.gpu_name = label("NVIDIA GPU", "gs-hero-name", wrap=True)
        self.kelvinub = label("", "gs-sub", wrap=True)
        names.append(self.gpu_name)
        names.append(self.kelvinub)
        self.pill = StatusPill()
        top.append(icon)
        top.append(names)
        top.append(self.pill)
        hero.append(top)

        tiles = self.tiles = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
            min_children_per_line=2, max_children_per_line=4, column_spacing=8, row_spacing=8,
        )
        self.t_temp = StatTile("gs-thermometer-symbolic", "Temp")
        self.t_power = StatTile("gs-bolt-symbolic", "Power")
        self.t_vram = StatTile("gs-memory-symbolic", "VRAM")
        self.t_util = StatTile("gs-activity-symbolic", "Load")
        for tile in (self.t_temp, self.t_power, self.t_vram, self.t_util):
            tiles.append(tile)
            tile.get_parent().set_focusable(False)
        hero.append(tiles)

        self.spark = Sparkline()
        hero.append(self.spark)
        legend = Gtk.Box(spacing=12)
        legend.append(label("━ Power", "gs-faint", "gs-legend-power"))
        legend.append(label("━ Load", "gs-faint", "gs-legend-util"))
        spacer = label("last 3 min", "gs-faint", xalign=1)
        spacer.set_hexpand(True)
        legend.append(spacer)
        hero.append(legend)
        self.hero_facts = label("", "gs-sub", "gs-mono", wrap=True)
        hero.append(self.hero_facts)
        self.sensor_note = label("", "gs-faint", wrap=True)
        hero.append(self.sensor_note)
        page.append(hero)

        # Battery (a separate system: charge limits)
        self.battery_summary = BatterySummary(lambda: self.stack.set_visible_child_name("battery"))
        page.append(self.battery_summary)
        self.cooling_summary = CoolingSummary(lambda: self.stack.set_visible_child_name("cooling"))
        page.append(self.cooling_summary)
        self.sleep_summary = SleepSummary(lambda: self.stack.set_visible_child_name("sleep"))
        page.append(self.sleep_summary)
        gpu_button = Gtk.Button(label="GPU controls", halign=Gtk.Align.START)
        gpu_button.add_css_class("gs-action")
        gpu_button.connect("clicked", lambda *_: self.stack.set_visible_child_name("power"))
        hero.append(gpu_button)

        # Everything below lives on the GPU tab.
        page = self.gpu_page

        # GPU mode
        self.mode_spinner = Adw.Spinner(visible=False)
        mode_card = Card("GPU Mode", "power-profile-balanced-symbolic", suffix=self.mode_spinner)
        self.mode_group = Adw.ToggleGroup(homogeneous=True)
        self.mode_group.add_css_class("gs-modes")
        for mode in MODE_ORDER:
            self.mode_group.add(Adw.Toggle(name=mode.value, child=toggle_child(MODE_ICONS[mode], mode.label), tooltip=MODE_SUMMARIES[mode]))
        self.mode_group.connect("notify::active-name", self._on_mode_toggled)
        mode_card.append(self.mode_group)
        self.mode_desc = label("", "gs-sub", wrap=True)
        mode_card.append(self.mode_desc)
        self.mode_status = label("", "gs-faint", wrap=True)
        mode_card.append(self.mode_status)
        self.mode_callout = Callout("warn")
        mode_card.append(revealed(self.mode_callout))
        page.append(mode_card)

        # Graphics session
        session = Card("Graphics Session", "video-display-symbolic")
        self.r_renderer = InfoRow("Renderer")
        self.r_panel = InfoRow("Internal display")
        self.r_mux = InfoRow("GPU MUX")
        self.r_saving = InfoRow("Power saving")
        for row in (self.r_renderer, self.r_panel, self.r_mux, self.r_saving):
            session.append(row)
        self.session_callout = Callout("info")
        session.append(revealed(self.session_callout))
        self.mux_button = Gtk.Button(label="Switch to Hybrid (Optimus)…", halign=Gtk.Align.START)
        self.mux_button.add_css_class("gs-action")
        self.mux_button.get_child().set_wrap(True)
        self.mux_button.set_action_name("app.switch-graphics")
        session.append(self.mux_button)
        self.session_note = label("", "gs-faint", wrap=True)
        session.append(self.session_note)
        page.append(session)

        # Manual control
        manual = Card("GPU Manual Control", "system-shutdown-symbolic")
        buttons = Gtk.Box(spacing=10, homogeneous=True)
        self.btn_on = button("Turn GPU On", "gs-action")
        self.btn_on.connect("clicked", lambda *_: self.app.request_mode(Mode.ALWAYS_ON))
        self.btn_off = button("Allow GPU To Power Down", "gs-action")
        self.btn_off.connect("clicked", self._on_power_down_clicked)
        buttons.append(self.btn_on)
        buttons.append(self.btn_off)
        manual.append(buttons)
        self.manual_callout = Callout("warn")
        manual.append(revealed(self.manual_callout))
        page.append(manual)

    # -- power page ------------------------------------------------------------

    def _profile_group(self, source: str) -> Adw.ToggleGroup:
        group = Adw.ToggleGroup(homogeneous=True)
        group.add_css_class("gs-modes")
        group.add_css_class("gs-modes-small")
        for mode in PROFILE_ORDER:
            group.add(Adw.Toggle(name=mode.value, child=toggle_child(None, mode.label), tooltip=MODE_SUMMARIES[mode]))
        group.connect("notify::active-name", self._on_profile_toggled, source)
        return group

    def _build_power(self) -> None:
        page = self.gpu_page

        self.source_pill = StatusPill()
        profiles = Card("Power Profiles", "ac-adapter-symbolic", suffix=self.source_pill)
        for icon, title, source in (("ac-adapter-symbolic", "Plugged In", "ac"), ("battery-level-50-symbolic", "On Battery", "battery")):
            head = Gtk.Box(spacing=8)
            head.append(Gtk.Image(icon_name=icon, pixel_size=16))
            head.append(label(title, "gs-proc-name"))
            active = label("", "gs-faint", xalign=1)
            active.set_hexpand(True)
            head.append(active)
            profiles.append(head)
            group = self._profile_group(source)
            profiles.append(group)
            if source == "ac":
                self.ac_group, self.ac_active = group, active
            else:
                self.bat_group, self.bat_active = group, active

        auto_row = Gtk.Box(spacing=12)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        texts.append(label("Switch automatically", "gs-proc-name"))
        texts.append(label("Apply the matching profile when AC is connected or disconnected.", "gs-faint", wrap=True))
        self.auto_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.auto_switch.connect("notify::active", self._on_auto_switch)
        auto_row.append(texts)
        auto_row.append(self.auto_switch)
        profiles.append(auto_row)
        self.profile_note = label("", "gs-faint", wrap=True)
        profiles.append(self.profile_note)
        page.append(profiles)

        state = Card("Power State", "gs-chip-symbolic")
        self.r_rpm = InfoRow("Runtime PM", "kernel")
        self.r_pci = InfoRow("PCI power state", "kernel")
        self.r_control = InfoRow("Runtime PM control", "kernel")
        self.r_rtd3 = InfoRow("Runtime D3 (driver)", "kernel")
        self.r_vidmem = InfoRow("Video memory", "kernel")
        self.r_gpu_power = InfoRow("GPU power", "measured")
        self.r_slept = InfoRow("Powered down since boot", "kernel")
        for row in (self.r_rpm, self.r_pci, self.r_control, self.r_rtd3, self.r_vidmem, self.r_gpu_power, self.r_slept):
            state.append(row)
        self.state_callout = Callout("good")
        state.append(revealed(self.state_callout))
        page.append(state)

        impact = Card("Battery Impact", "gs-bolt-symbolic")
        self.r_gpu_w = InfoRow("NVIDIA GPU draw", "measured")
        self.r_sys_w = InfoRow("System draw", "measured")
        self.r_share = InfoRow("GPU share", "computed")
        self.r_extra = InfoRow("Extra consumption", "estimate")
        self.r_gain = InfoRow("Runtime without dGPU", "estimate")
        for row in (self.r_gpu_w, self.r_sys_w, self.r_share, self.r_extra, self.r_gain):
            impact.append(row)
        impact.append(
            label(
                "Measured: GPU board power from NVML and battery discharge rate from UPower. "
                "Share is computed from those two readings. Extra consumption and runtime are "
                "estimates: they assume the dGPU's draw would drop to ~0 W and ignore any extra iGPU load.",
                "gs-faint",
                wrap=True,
            )
        )
        page.append(impact)


    # -- apps page ---------------------------------------------------------------

    def _build_apps(self) -> None:
        page = self.gpu_page
        self.apps_count = label("", "gs-faint", xalign=1)
        apps = Card("Applications using NVIDIA", "view-app-grid-symbolic", suffix=self.apps_count)
        self.apps_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        apps.append(self.apps_list)
        self.apps_empty = label("No application is using the NVIDIA GPU.", "gs-sub", xalign=0.5)
        self.apps_empty.set_margin_top(12)
        self.apps_empty.set_margin_bottom(12)
        apps.append(self.apps_empty)
        self.apps_note = label("", "gs-faint", wrap=True)
        apps.append(self.apps_note)
        self._rows: list[ProcessRow] = []
        page.append(apps)

        run = Card("Run An App On NVIDIA", "system-run-symbolic")
        self.run_text = label("", "gs-sub", wrap=True)
        run.append(self.run_text)
        for text, caption in (("kelvin run <command>", None), ("kelvin run %command%", "Steam launch options")):
            if caption:
                run.append(label(caption, "gs-faint"))
            row = Gtk.Box(spacing=6)
            row.append(label(text, "gs-code", "gs-mono"))
            copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy", valign=Gtk.Align.CENTER)
            copy.add_css_class("flat")
            copy.connect("clicked", self._copy, text)
            row.append(copy)
            run.append(row)
        page.append(run)

    def _reset_view(self) -> bool:
        self.set_focus(None)
        for scroller in self._scrollers:
            scroller.get_vadjustment().set_value(0)
        return False

    def _copy(self, _button, text: str) -> None:
        self.get_clipboard().set(text)
        self.toast("Copied to clipboard")

    # -- events ------------------------------------------------------------------

    def _on_mode_toggled(self, group, _pspec) -> None:
        if self._updating:
            return
        mode = Mode.parse(group.get_active_name())
        if mode is not None:
            self.app.request_mode(mode)

    def _on_profile_toggled(self, group, _pspec, source: str) -> None:
        if self._updating:
            return
        mode = Mode.parse(group.get_active_name())
        if mode is not None:
            self.app.set_profile(source, mode)

    def _on_auto_switch(self, switch, _pspec) -> None:
        if not self._updating:
            self.app.set_auto_switch(switch.get_active())

    def _on_power_down_clicked(self, *_):
        self._power_down_requested_at = time.time()
        self.app.request_mode(Mode.POWER_SAVING)

    def _on_banner_undo(self, *_):
        self.app.undo_mux_switch()

    def toast(self, text: str, timeout: int = 4) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))

    def set_accent(self, rgba) -> None:
        self.spark.set_accent(rgba)
        self.cooling_page.set_accent(rgba)

    # -- updates -----------------------------------------------------------------

    def update(self, snap: Snapshot, ctx: UiContext) -> None:
        self._updating = True
        try:
            self._last_snapshot = snap
            self.spinner.set_visible(ctx.busy)
            self.mode_spinner.set_visible(ctx.busy)
            self._update_hero(snap, ctx)
            self._update_mode(snap, ctx)
            self._update_session(snap, ctx)
            self._update_manual(snap, ctx)
            self._update_profiles(snap, ctx)
            self._update_power_state(snap)
            self._update_impact(snap)
            self.battery_summary.update(snap.charge)
            self.battery_page.update(snap.charge, ctx.config.battery_notifications, ctx.busy)
            self.cooling_summary.update(snap.cpu, snap.fans)
            self.sleep_summary.update(snap.idle)
            gpu_temp = snap.sample.temperature if snap.sample_fresh and snap.sample else None
            self.cooling_page.update(snap.cpu, snap.fans, ctx.config, gpu_temp, ctx.busy)
            self.sleep_page.update(snap.idle, ctx.busy)
            self._update_apps(snap)
        finally:
            self._updating = False

    def _update_hero(self, snap: Snapshot, ctx: UiContext) -> None:
        facts = snap.facts
        sample = snap.sample
        fresh = snap.sample_fresh or (sample is not None and snap.sample_age is not None and snap.sample_age < 90 and not (facts.power and facts.power.suspended))
        self.gpu_name.set_label(gpu_display_name(facts.gpu, ctx.state.gpu_name))
        sub = [f"Driver {facts.driver_version}" if facts.driver_version else "NVIDIA driver not loaded"]
        if facts.product:
            sub.append(facts.product)
        self.kelvinub.set_label(" · ".join(sub))

        power = facts.power
        if not facts.gpu:
            self.pill.set("bad", "NOT FOUND")
        elif power and power.powered_down:
            self.pill.set("off", "POWERED DOWN")
        elif power and power.suspended:
            self.pill.set("off", "SUSPENDED")
        elif power and power.runtime_status in ("suspending", "resuming"):
            self.pill.set("warn", power.runtime_status.upper())
        else:
            self.pill.set("good", "ACTIVE", pulse=True)

        powered_down = bool(power and power.suspended)
        if powered_down:
            self.t_temp.set("—", "", dim=True)
            self.t_power.set("Off", "", dim=True)
            self.t_vram.set("—", "", dim=True)
            self.t_util.set("—", "", dim=True)
        elif sample and fresh:
            self.t_temp.set(fmt(sample.temperature), "°C")
            self.t_power.set(fmt(sample.power_draw, digits=1), "W")
            self.t_vram.set(fmt(sample.mem_used), "MB")
            self.t_util.set(fmt(sample.util_gpu), "%")
        else:
            for tile in (self.t_temp, self.t_power, self.t_vram, self.t_util):
                tile.set("—", "", dim=True)

        self.spark.set_data(snap.history, sample.power_limit if sample else None)
        parts = [f"Mode {snap.mode.label}"] if snap.mode else []
        if sample and fresh:
            if sample.power_limit:
                parts.append(f"Limit {sample.power_limit:.0f} W")
            if sample.pstate:
                parts.append(sample.pstate)
            if sample.clock_gr is not None:
                parts.append(f"{sample.clock_gr:.0f} MHz core")
            if sample.mem_total:
                parts.append(f"{sample.mem_used:.0f}/{sample.mem_total:.0f} MB")
        if power:
            parts.append(f"PCI {power.pci_state or '?'} · runtime PM {power.runtime_status}")
        self.hero_facts.set_label(" · ".join(parts))
        note = snap.nvml_error or snap.sensor_note or ""
        if snap.sample and not snap.sample_fresh and fresh and snap.sample_age is not None:
            note = (note + " " if note else "") + f"Last reading {snap.sample_age:.0f} s ago."
        self.sensor_note.set_label(note)
        self.sensor_note.set_visible(bool(note))

    def _update_mode(self, snap: Snapshot, ctx: UiContext) -> None:
        a = snap.assessment
        for mode in MODE_ORDER:
            toggle = self.mode_group.get_toggle_by_name(mode.value)
            toggle.set_enabled(a.supports(mode) and not ctx.busy)
        if snap.mode is not None:
            self.mode_group.set_active_name(snap.mode.value)
        self.mode_desc.set_label(MODE_SUMMARIES[snap.mode] if snap.mode else "")

        state = ctx.state
        if a.forced_mode is not None and a.unsupported:
            status = f"{a.forced_mode.label} is required by the current hardware wiring."
        elif state.applied_by == "manual" and state.manual_mode:
            status = "Manual choice" + (" — the AC/battery profile takes over when the power source changes." if ctx.config.auto_switch else ".")
        elif state.applied_by == "ac-profile":
            status = "Set by the Plugged In profile."
        elif state.applied_by == "battery-profile":
            status = "Set by the On Battery profile."
        else:
            status = ""
        self.mode_status.set_label(status)
        self.mode_status.set_visible(bool(status))

        reason = a.unsupported
        show = reason is not None and a.gpu_present
        if show:
            self.mode_callout.set(
                "Auto and Power Saving are unavailable right now" if a.forced_mode else reason.title,
                reason.detail if a.forced_mode else "",
                reason.hint or "",
                kind="warn",
            )
        self.mode_callout.get_parent().set_reveal_child(show)

    def _update_session(self, snap: Snapshot, ctx: UiContext) -> None:
        facts, a = snap.facts, snap.assessment
        renderer = next((g for g in facts.gpus if g.address == facts.session.renderer_address), None)
        via = {
            "hyprland-log": "Hyprland primary GPU",
            "internal-panel": "inferred from panel wiring",
            "boot-vga": "inferred from boot GPU",
        }.get(facts.session.renderer_source, "unknown")
        self.r_renderer.set(gpu_display_name(renderer, ctx.state.gpu_name), tooltip=via)
        panel = next((g for g in facts.gpus if g.address == facts.session.panel_address), None)
        self.r_panel.set(f"{facts.session.panel_connector} → {panel.vendor_name}" if panel and facts.session.panel_connector else "—")

        mux = facts.mux
        if mux.available:
            effective = (facts.effective_mux_mode or mux.configured_mode or "unknown").capitalize()
            text = effective + (" (Optimus)" if effective == "Hybrid" else "")
            if facts.mux_change_pending and mux.configured_mode:
                text += f" → {mux.configured_mode.capitalize()}"
            self.r_mux.set(text, tooltip=f"Firmware attribute via {mux.source}")
        else:
            self.r_mux.set("Not available")

        if a.can_power_down_now:
            self.r_saving.set("Available")
        elif a.unsupported and a.forced_mode:
            self.r_saving.set("Unavailable (session uses NVIDIA)" if facts.session_on_nvidia else "Unavailable", tooltip=a.unsupported.title)
        elif a.blockers:
            self.r_saving.set("Blocked right now", tooltip=a.blockers[0].title)
        else:
            self.r_saving.set("Unavailable")

        pending = facts.mux_change_pending and mux.configured_mode
        if pending:
            target = mux.configured_mode.capitalize()
            self.banner.set_title(f"Graphics switches to {target} after the next reboot")
            self.banner.set_revealed(True)
            self.session_callout.set(
                f"{target} mode is pending",
                "The firmware applies the MUX change when you reboot. Kelvin never reboots on its own.",
                kind="info",
            )
        else:
            self.banner.set_revealed(False)
        self.session_callout.get_parent().set_reveal_child(bool(pending))

        self.mux_button.set_visible(mux.available)
        configured = mux.configured_mode
        if configured == "discrete":
            self.mux_button.set_label("Switch to Hybrid (Optimus)…")
            self.mux_button.add_css_class("suggested-action")
            self.session_note.set_label(
                "Hybrid mode wires the laptop panel to the Intel iGPU after a reboot. The desktop then runs on "
                "Intel, the NVIDIA GPU can power down, and individual apps still use it via “kelvin run”."
            )
        elif configured == "hybrid":
            self.mux_button.set_label("Switch to Discrete…")
            self.mux_button.remove_css_class("suggested-action")
            tuning = "on" if ctx.tuning_enabled else "off"
            self.session_note.set_label(
                f"Discrete mode wires the panel straight to NVIDIA (max performance, no power saving). Hyprland session tuning is {tuning}."
            )
        else:
            self.session_note.set_label("")
        self.session_note.set_visible(bool(self.session_note.get_label()))

    def _update_manual(self, snap: Snapshot, ctx: UiContext) -> None:
        a, facts = snap.assessment, snap.facts
        power = facts.power
        pinned = bool(power and power.control == "on")
        active = bool(power and power.runtime_status == "active")
        self.btn_on.set_sensitive(a.runtime_pm_available and a.forced_mode is None and not (pinned and active) and not ctx.busy)
        self.btn_off.set_sensitive(a.dynamic_modes_possible and not ctx.busy and (pinned or snap.mode is not Mode.POWER_SAVING))

        callout = self.manual_callout
        show = True
        if not a.gpu_present:
            callout.set("No NVIDIA GPU detected.", kind="warn")
        elif a.forced_mode is not None and a.unsupported:
            reason = next((b for b in a.blockers if b.code == "session-on-nvidia"), a.blockers[0] if a.blockers else a.unsupported)
            extra = [b.title for b in a.blockers if b is not reason]
            callout.set(
                "GPU cannot power down right now.",
                f"Reason: {reason.title}" + ("\n" + "\n".join(extra) if extra else ""),
                reason.hint or "",
                kind="warn",
            )
        elif power and power.powered_down:
            callout.set("GPU powered down", f"The kernel reports runtime PM 'suspended' and PCI state {power.pci_state}.", kind="good")
        elif a.blockers:
            b = a.blockers[0]
            callout.set("GPU cannot power down right now.", f"Reason: {b.title}", b.hint or "", kind="warn")
        elif power and power.control == "auto" and active:
            holders = [p.name for p in snap.processes if not p.is_self]
            waited = time.time() - self._power_down_requested_at if self._power_down_requested_at else 0
            body = "Power-down is allowed. The driver turns the GPU off once it has been idle for a few seconds."
            if holders and waited > 20:
                body += "\nStill awake. Apps with the GPU open: " + ", ".join(holders[:6]) + "."
            callout.set("Waiting for the GPU to go idle…", body, kind="info")
        else:
            show = False
        callout.get_parent().set_reveal_child(show)
        if power and power.powered_down:
            self._power_down_requested_at = None

    def _update_profiles(self, snap: Snapshot, ctx: UiContext) -> None:
        config = ctx.config
        self.ac_group.set_active_name(config.ac_mode)
        self.bat_group.set_active_name(config.battery_mode)
        self.auto_switch.set_active(config.auto_switch)
        on_ac = snap.supply.on_ac if snap.supply else None
        if on_ac is True:
            self.source_pill.set("good", "AC POWER")
        elif on_ac is False:
            self.source_pill.set("warn", "BATTERY")
        else:
            self.source_pill.set("off", "UNKNOWN")
        self.ac_active.set_label("active" if on_ac is True and config.auto_switch else "")
        self.bat_active.set_label("active" if on_ac is False and config.auto_switch else "")

        a = snap.assessment
        notes = []
        if a.gpu_present and not a.dynamic_modes_possible:
            notes.append("Profiles are saved, but only Always On can take effect until the GPU can power down "
                         "(see Graphics Session on the Overview page).")
        if ctx.state.manual_mode and config.auto_switch:
            notes.append(f"A manual choice ({MODE_LABELS[Mode(ctx.state.manual_mode)]}) is active until the power source changes.")
        self.profile_note.set_label(" ".join(notes))
        self.profile_note.set_visible(bool(notes))

    def _update_power_state(self, snap: Snapshot) -> None:
        facts = snap.facts
        power = facts.power
        if power is None:
            for row in (self.r_rpm, self.r_pci, self.r_control, self.r_rtd3, self.r_vidmem, self.r_gpu_power, self.r_slept):
                row.set("—")
            self.state_callout.get_parent().set_reveal_child(False)
            return
        self.r_rpm.set(power.runtime_status.capitalize())
        self.r_pci.set(power.pci_state or "—")
        self.r_control.set(f"{power.control} ({'pinned on' if power.control == 'on' else 'may suspend'})" if power.control else "—")
        self.r_rtd3.set(facts.rtd3.status if facts.rtd3 and facts.rtd3.status else "—")
        vid = facts.rtd3.video_memory if facts.rtd3 else None
        self.r_vidmem.set(vid or "—")
        if power.suspended:
            self.r_gpu_power.set("Off", tag="kernel")
        elif snap.gpu_power_w is not None:
            self.r_gpu_power.set(f"{snap.gpu_power_w:.1f} W", tag="measured")
        else:
            self.r_gpu_power.set("not read", tag=None)
        frac = power.suspended_fraction
        self.r_slept.set(f"{frac * 100:.1f} %" if frac is not None else "—")
        self.state_callout.set(
            "GPU: Powered Down",
            f"Confirmed by the kernel: runtime PM suspended, PCI {power.pci_state}.",
            kind="good",
        )
        self.state_callout.get_parent().set_reveal_child(power.powered_down)

    def _update_impact(self, snap: Snapshot) -> None:
        power = snap.facts.power
        battery = snap.supply.battery if snap.supply else None
        on_ac = snap.supply.on_ac if snap.supply else None
        gpu_w = snap.gpu_power_w
        off = bool(power and power.suspended)

        if off:
            self.r_gpu_w.set(f"Off ({power.pci_state})", tag="kernel")
        elif gpu_w is not None:
            self.r_gpu_w.set(f"{gpu_w:.1f} W", tag="measured")
        else:
            self.r_gpu_w.set("not read", tag=None, tooltip=snap.sensor_note)

        system_w = battery.energy_rate_w if battery and battery.discharging and battery.energy_rate_w else None
        if system_w:
            self.r_sys_w.set(f"{system_w:.1f} W", tag="measured")
        else:
            self.r_sys_w.set("on AC — not discharging" if on_ac else "—", tag=None)

        if system_w and gpu_w is not None and not off:
            self.r_share.set(f"{min(100.0, gpu_w / system_w * 100):.0f} %", tag="computed")
        else:
            self.r_share.set("—", tag=None)

        if off:
            self.r_extra.set("None", tag="kernel")
        else:
            estimate = impact_estimate(gpu_w, system_w)
            self.r_extra.set(estimate or "—", tag="estimate" if estimate else None)

        if system_w and gpu_w and battery and battery.energy_wh and not off and system_w > gpu_w:
            now_h = battery.energy_wh / system_w * 3600
            without_h = battery.energy_wh / (system_w - gpu_w) * 3600
            self.r_gain.set(f"{duration(without_h)} (vs {duration(now_h)})", tag="estimate")
        else:
            self.r_gain.set("on battery only" if not off else "—", tag=None)

    def _update_apps(self, snap: Snapshot) -> None:
        procs = snap.processes
        total = snap.sample.mem_total if snap.sample else None
        while len(self._rows) < len(procs):
            row = ProcessRow()
            self._rows.append(row)
            self.apps_list.append(row)
        for row, proc in zip(self._rows, procs):
            row.set(proc, total)
            row.set_visible(True)
        for row in self._rows[len(procs):]:
            row.set_visible(False)
        self.apps_empty.set_visible(not procs)
        vram = sum(p.vram_mb or 0 for p in procs)
        self.apps_count.set_label(f"{len(procs)} apps · {vram:.0f} MB" if procs and vram else f"{len(procs)} apps" if procs else "")
        note = ""
        if procs and not snap.sample_fresh:
            note = "VRAM and GPU% are only read while sensors are active. " + (snap.sensor_note or "")
        elif procs:
            note = "“idle” = the app has the NVIDIA driver open but no memory on the GPU right now."
        self.apps_note.set_label(note.strip())
        self.apps_note.set_visible(bool(note))

        if snap.facts.session_on_nvidia:
            self.run_text.set_label(
                "The session currently renders on NVIDIA, so every app already uses it. In Hybrid mode, this "
                "command offloads a single app (game, Blender, CUDA tool…) to the NVIDIA GPU while the desktop stays on Intel."
            )
        else:
            self.run_text.set_label(
                "The desktop runs on the Intel iGPU. Launch an individual app on the NVIDIA GPU with PRIME render offload:"
            )
