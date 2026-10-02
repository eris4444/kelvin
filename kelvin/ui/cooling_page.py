"""Cooling tab (CPU temperatures/cores, fans, fan curves) and its Overview card."""

from __future__ import annotations

import math

from gi.repository import Adw, Gdk, Gtk, Pango

from .. import fans
from ..cpu import CpuInfo, PhysicalCore
from ..fans import FanInfo
from .widgets import Card, StatusPill, label, toggle_child


def temp_level(temp: float | None, high: float | None = None) -> str:
    if temp is None:
        return "off"
    if temp >= (high or 100) - 10 or temp >= 90:
        return "bad"
    if temp >= 75:
        return "warn"
    return "good"


def temp_text(temp: float | None) -> str:
    return f"{temp:.0f}°C" if temp is not None else "—"


def rpm_text(rpm: int | None) -> str:
    if rpm is None:
        return "—"
    return "Stopped" if rpm == 0 else f"{rpm} rpm"


class CoreTile(Gtk.Box):
    """One physical core: label, temperature, per-thread load bars, frequency."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("gs-tile")
        self.add_css_class("gs-core")
        top = Gtk.Box(spacing=6)
        self.name = label("", "gs-core-name")
        self.temp = label("", "gs-core-temp", xalign=1)
        self.temp.set_hexpand(True)
        top.append(self.name)
        top.append(self.temp)
        self.append(top)
        self.bars = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.append(self.bars)
        self.freq = label("", "gs-faint", "gs-mono")
        self.append(self.freq)
        self._meters: list[Gtk.LevelBar] = []

    def set(self, core: PhysicalCore, index: int) -> None:
        prefix = core.kind or "C"
        self.name.set_label(f"{prefix}{index}")
        self.temp.set_label(temp_text(core.temp))
        for cls in ("good", "warn", "bad", "off"):
            self.temp.remove_css_class(cls)
        self.temp.add_css_class(temp_level(core.temp))
        while len(self._meters) < len(core.threads):
            meter = Gtk.LevelBar(min_value=0, max_value=100)
            meter.add_css_class("gs-thread")
            meter.remove_offset_value("low")
            meter.remove_offset_value("high")
            meter.remove_offset_value("full")
            self.bars.append(meter)
            self._meters.append(meter)
        for meter, thread in zip(self._meters, core.threads):
            meter.set_value(thread.usage or 0)
            meter.set_tooltip_text(f"CPU {thread.cpu}: {thread.usage or 0:.0f}%")
            meter.set_visible(True)
        for meter in self._meters[len(core.threads):]:
            meter.set_visible(False)
        usage = core.usage
        self.freq.set_label(f"{usage:.0f}% · {core.freq_mhz / 1000:.1f} GHz" if usage is not None and core.freq_mhz else "—")


class CurveGraph(Gtk.DrawingArea):
    """Fan curve (speed vs temperature) with the current temperature marked."""

    def __init__(self):
        super().__init__(hexpand=True, content_height=110)
        self._curve: fans.Curve = []
        self._temp: float | None = None
        self._custom = False
        self._accent = Gdk.RGBA()
        self._accent.parse("#7aa2f7")
        self.set_draw_func(self._draw)

    def set_accent(self, rgba: Gdk.RGBA) -> None:
        self._accent = rgba
        self.queue_draw()

    def set_data(self, curve: fans.Curve, temp: float | None, custom: bool) -> None:
        if (curve, temp, custom) != (self._curve, self._temp, self._custom):
            self._curve, self._temp, self._custom = list(curve), temp, custom
            self.queue_draw()

    def _draw(self, _area, cr, w, h) -> None:
        fg = self.get_color()
        pad_l, pad_r, pad_t, pad_b = 30, 8, 6, 16
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        t_min, t_max = 30, 100

        def x(t):
            return pad_l + (min(max(t, t_min), t_max) - t_min) / (t_max - t_min) * gw

        def y(pwm):
            return pad_t + gh - pwm / 255 * gh

        cr.set_line_width(1)
        cr.set_font_size(9)
        for pct in (0, 50, 100):
            yy = round(y(pct * 255 / 100)) + 0.5
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08)
            cr.move_to(pad_l, yy)
            cr.line_to(w - pad_r, yy)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.move_to(2, yy + 3)
            cr.show_text(f"{pct}%")
        for t in (40, 60, 80, 100):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.move_to(x(t) - 8, h - 3)
            cr.show_text(f"{t}°")
        if not self._curve:
            return
        a = self._accent
        pts = [(x(t), y(p)) for t, p in self._curve]
        # Firmware/EC holds the last speed above the last point.
        pts = [(x(t_min), pts[0][1])] + pts + [(x(t_max), pts[-1][1])]
        cr.set_source_rgba(a.red, a.green, a.blue, 0.18)
        cr.move_to(pts[0][0], pad_t + gh)
        for px, py in pts:
            cr.line_to(px, py)
        cr.line_to(pts[-1][0], pad_t + gh)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(a.red, a.green, a.blue, 1.0 if self._custom else 0.7)
        cr.set_line_width(2)
        cr.move_to(*pts[0])
        for px, py in pts[1:]:
            cr.line_to(px, py)
        cr.stroke()
        for t, p in self._curve:
            cr.arc(x(t), y(p), 2.5, 0, 2 * math.pi)
            cr.fill()
        if self._temp is not None:
            tx = x(self._temp)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.75)
            cr.set_dash([3, 3])
            cr.move_to(tx, pad_t)
            cr.line_to(tx, pad_t + gh)
            cr.stroke()
            cr.set_dash([])
            cr.move_to(min(tx + 3, w - 34), pad_t + 9)
            cr.show_text(f"{self._temp:.0f}°C")


class CoolingSummary(Card):
    """Overview card: CPU temperature/load and fan speeds."""

    def __init__(self, open_page):
        self.pill = StatusPill()
        super().__init__("Cooling · CPU & fans", "gs-thermometer-symbolic", suffix=self.pill)
        row = Gtk.Box(spacing=14)
        self.temp = label("—", "gs-big-number")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.model = label("", "gs-proc-name")
        self.model.set_ellipsize(Pango.EllipsizeMode.END)
        self.load = label("", "gs-sub", wrap=True)
        texts.append(self.model)
        texts.append(self.load)
        row.append(self.temp)
        row.append(texts)
        self.append(row)
        self.fans = label("", "gs-row-value", wrap=True)
        self.append(self.fans)
        button = Gtk.Button(label="Cooling controls", halign=Gtk.Align.START)
        button.add_css_class("gs-action")
        button.connect("clicked", lambda *_: open_page())
        self.append(button)

    def update(self, cpu: CpuInfo | None, info: FanInfo | None) -> None:
        if cpu is None:
            return
        self.temp.set_label(temp_text(cpu.package_temp))
        level = temp_level(cpu.package_temp, cpu.temp_high)
        self.pill.set(level, {"good": "COOL", "warn": "WARM", "bad": "HOT", "off": "—"}[level])
        self.model.set_label(cpu.model or "CPU")
        usage = f"{cpu.usage:.0f}% load" if cpu.usage is not None else "load —"
        self.load.set_label(f"{usage} · {cpu.in_use} of {len(cpu.logical)} threads busy")
        if info and info.fans:
            parts = [f"{fans.FAN_LABELS[f].split()[0]} {rpm_text(s.rpm)}" for f, s in info.fans.items()]
            profile = fans.PROFILE_LABELS.get(info.profile or "", info.platform_profile or "—")
            self.fans.set_label("Fans: " + " · ".join(parts) + f" · {profile} profile")
        else:
            self.fans.set_label("No fan sensors")


class CoolingPage:
    def __init__(self, page: Gtk.Box, app):
        self.app = app
        self._syncing = False

        # CPU
        self.cpu_pill = StatusPill()
        cpu = Card("CPU", "gs-thermometer-symbolic", suffix=self.cpu_pill)
        top = Gtk.Box(spacing=14)
        self.package = label("—", "gs-big-number")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.model = label("", "gs-proc-name", wrap=True)
        self.summary = label("", "gs-sub", wrap=True)
        texts.append(self.model)
        texts.append(self.summary)
        top.append(self.package)
        top.append(texts)
        cpu.append(top)
        self.p_title = label("", "gs-card-title")
        self.p_cores = self._core_box()
        self.e_title = label("", "gs-card-title")
        self.e_cores = self._core_box()
        for widget in (self.p_title, self.p_cores, self.e_title, self.e_cores):
            cpu.append(widget)
        cpu.append(label("Temperatures from coretemp (per physical core); load per thread from /proc/stat.", "gs-faint", wrap=True))
        self._tiles: dict[int, CoreTile] = {}
        page.append(cpu)

        # Fans + profile
        fan_card = Card("Fans", "gs-fan-symbolic")
        self.fan_rows: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        for fan in fans.FANS:
            row = Gtk.Box(spacing=10)
            name = label(fans.FAN_LABELS[fan], "gs-proc-name")
            name.set_hexpand(True)
            rpm = label("—", "gs-row-value")
            mode = label("", "gs-faint")
            row.append(name)
            row.append(mode)
            row.append(rpm)
            fan_card.append(row)
            self.fan_rows[fan] = (rpm, mode)
        fan_card.append(label("Thermal profile", "gs-proc-name"))
        self.profile_group = Adw.ToggleGroup(homogeneous=True)
        self.profile_group.add_css_class("gs-modes")
        for profile in fans.PROFILES:
            self.profile_group.add(Adw.Toggle(name=profile, child=toggle_child(fans.PROFILE_ICONS[profile], fans.PROFILE_LABELS[profile])))
        self.profile_group.connect("notify::active-name", self._on_profile)
        fan_card.append(self.profile_group)
        self.profile_note = label(
            "The ASUS thermal profile sets the firmware's fan behaviour and power limits (like Fn+F5). "
            "Switched through power-profiles-daemon.",
            "gs-faint", wrap=True,
        )
        fan_card.append(self.profile_note)
        page.append(fan_card)

        # Curves
        curves = Card("Fan Curves", "gs-fan-symbolic")
        self.curve_ui: dict[str, tuple[Adw.ToggleGroup, CurveGraph, Gtk.Label]] = {}
        for fan in fans.FANS:
            curves.append(label(fans.FAN_LABELS[fan], "gs-proc-name"))
            group = Adw.ToggleGroup(homogeneous=True)
            group.add_css_class("gs-modes")
            group.add_css_class("gs-modes-small")
            for mode in fans.MODES:
                group.add(Adw.Toggle(name=mode, child=toggle_child(None, fans.MODE_LABELS[mode]), tooltip=fans.MODE_HINTS[mode]))
            group.connect("notify::active-name", self._on_curve_mode, fan)
            graph = CurveGraph()
            note = label("", "gs-faint", wrap=True)
            curves.append(group)
            curves.append(graph)
            curves.append(note)
            self.curve_ui[fan] = (group, graph, note)
        curves.append(label(
            "Curves are written to the ASUS firmware through a root helper that never allows less than "
            "25% fan at 70°C or 50% at 80°C. The kernel drops custom curves at boot and when the thermal "
            "profile changes; Kelvin re-applies yours automatically while it runs (it starts at login).",
            "gs-faint", wrap=True,
        ))
        page.append(curves)

    def _core_box(self) -> Gtk.FlowBox:
        box = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
            min_children_per_line=2, max_children_per_line=6, column_spacing=6, row_spacing=6,
        )
        return box

    def set_accent(self, rgba) -> None:
        for _group, graph, _note in self.curve_ui.values():
            graph.set_accent(rgba)

    # -- events ---------------------------------------------------------------------

    def _on_profile(self, group, _pspec) -> None:
        if not self._syncing and group.get_active_name():
            self.app.request_fan_profile(group.get_active_name())

    def _on_curve_mode(self, group, _pspec, fan: str) -> None:
        if not self._syncing and group.get_active_name():
            self.app.request_fan_mode(fan, group.get_active_name())

    # -- update ---------------------------------------------------------------------

    def update(self, cpu: CpuInfo | None, info: FanInfo | None, config, gpu_temp: float | None, busy: bool) -> None:
        self._syncing = True
        try:
            if cpu is not None:
                self._update_cpu(cpu)
            self._update_fans(info, config, cpu, gpu_temp, busy)
        finally:
            self._syncing = False

    def _update_cpu(self, cpu: CpuInfo) -> None:
        self.package.set_label(temp_text(cpu.package_temp))
        level = temp_level(cpu.package_temp, cpu.temp_high)
        self.cpu_pill.set(level, {"good": "COOL", "warn": "WARM", "bad": "HOT", "off": "—"}[level])
        self.model.set_label(cpu.model or "CPU")
        hottest = cpu.hottest_core
        parts = [f"{cpu.usage:.0f}% load" if cpu.usage is not None else "load —",
                 f"{cpu.in_use}/{len(cpu.logical)} threads busy"]
        if cpu.avg_freq_mhz:
            parts.append(f"avg {cpu.avg_freq_mhz / 1000:.2f} GHz")
        if hottest is not None:
            parts.append(f"hottest core {temp_text(hottest.temp)}")
        if cpu.temp_high:
            parts.append(f"throttles at {cpu.temp_high:.0f}°C")
        self.summary.set_label(" · ".join(parts))

        p_cores = [c for c in cpu.cores if c.kind == "P"]
        e_cores = [c for c in cpu.cores if c.kind == "E"]
        other = [c for c in cpu.cores if c.kind not in ("P", "E")]
        groups = (
            (self.p_title, self.p_cores, p_cores or other,
             f"Performance cores · {len(p_cores)} × {len(p_cores[0].threads)} threads" if p_cores
             else f"Cores · {len(other)}"),
            (self.e_title, self.e_cores, e_cores, f"Efficient cores · {len(e_cores)}"),
        )
        for title, box, cores, text in groups:
            title.set_visible(bool(cores))
            box.set_visible(bool(cores))
            title.set_label(text.upper())
            for index, core in enumerate(cores):
                tile = self._tiles.get(core.core_id)
                if tile is None:
                    tile = CoreTile()
                    self._tiles[core.core_id] = tile
                    box.append(tile)
                    tile.get_parent().set_focusable(False)
                tile.set(core, index)

    def _update_fans(self, info: FanInfo | None, config, cpu: CpuInfo | None, gpu_temp: float | None, busy: bool) -> None:
        if info is None:
            return
        for fan, (rpm, mode_label) in self.fan_rows.items():
            state = info.fans.get(fan)
            rpm.set_label(rpm_text(state.rpm) if state else "not present")
            if state is None:
                mode_label.set_label("")
            else:
                mode_label.set_label("custom curve" if state.custom_enabled else "firmware curve")

        self.profile_group.set_sensitive(info.ppd_available and not busy)
        if info.profile in fans.PROFILES:
            self.profile_group.set_active_name(info.profile)
        if not info.ppd_available:
            self.profile_note.set_label("power-profiles-daemon is not running, so the thermal profile cannot be changed safely.")

        temps = {"cpu": cpu.package_temp if cpu else None, "gpu": gpu_temp}
        for fan, (group, graph, note) in self.curve_ui.items():
            state = info.fans.get(fan)
            usable = info.curves_supported and state is not None and bool(state.curve)
            group.set_sensitive(usable and not busy)
            mode = getattr(config, f"fan_{fan}")
            custom_curve = getattr(config, f"fan_{fan}_curve")
            group.get_toggle_by_name("custom").set_enabled(bool(custom_curve))
            group.set_active_name(mode)
            if not usable:
                graph.set_data([], None, False)
                note.set_label("No ASUS custom fan curve for this fan.")
                continue
            graph.set_data(state.curve, temps[fan], bool(state.custom_enabled))
            if mode == "auto":
                text = "Firmware curve for the current thermal profile."
            elif state.custom_enabled:
                text = f"{fans.MODE_LABELS[mode]} curve active."
            else:
                text = f"{fans.MODE_LABELS[mode]} is selected but the firmware curve is active; Kelvin is re-applying it."
            if fan == "gpu" and temps["gpu"] is None:
                text += " (GPU temperature hidden while the GPU sleeps.)"
            note.set_label(text)
