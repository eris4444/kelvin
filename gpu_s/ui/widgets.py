"""Reusable GPU-S widgets."""

from __future__ import annotations

import math

import cairo
from gi.repository import Gdk, GLib, Gtk, Pango


def label(text: str = "", *classes: str, xalign: float = 0.0, wrap: bool = False, selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign, wrap=wrap, selectable=selectable)
    if wrap:
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    for cls in classes:
        widget.add_css_class(cls)
    return widget


def set_visible_animated(widget: Gtk.Widget, visible: bool) -> None:
    parent = widget.get_parent()
    if isinstance(parent, Gtk.Revealer):
        parent.set_reveal_child(visible)
    else:
        widget.set_visible(visible)


def revealed(child: Gtk.Widget, visible: bool = False) -> Gtk.Revealer:
    return Gtk.Revealer(
        child=child,
        reveal_child=visible,
        transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
        transition_duration=220,
    )


class Card(Gtk.Box):
    def __init__(self, title: str | None = None, icon: str | None = None, suffix: Gtk.Widget | None = None, css: str | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add_css_class("gs-card")
        if css:
            self.add_css_class(css)
        if title:
            header = Gtk.Box(spacing=8)
            if icon:
                image = Gtk.Image(icon_name=icon, pixel_size=14)
                image.add_css_class("gs-card-icon")
                header.append(image)
            title_label = label(title.upper(), "gs-card-title", xalign=0, wrap=True)
            title_label.set_hexpand(True)
            header.append(title_label)
            if suffix:
                suffix.set_halign(Gtk.Align.END)
                header.append(suffix)
            self.append(header)


class StatusPill(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=7, valign=Gtk.Align.CENTER)
        self.add_css_class("gs-pill")
        self.dot = Gtk.Box(valign=Gtk.Align.CENTER)
        self.dot.add_css_class("gs-dot")
        self.text = label("", xalign=0)
        self.append(self.dot)
        self.append(self.text)
        self._level = None

    def set(self, level: str, text: str, pulse: bool = False) -> None:
        if level != self._level:
            for cls in ("good", "warn", "bad", "off", "accent"):
                self.remove_css_class(cls)
            self.add_css_class(level)
            self._level = level
        if pulse:
            self.dot.add_css_class("pulse")
        else:
            self.dot.remove_css_class("pulse")
        self.text.set_label(text)


class StatTile(Gtk.Box):
    def __init__(self, icon: str, caption: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.add_css_class("gs-tile")
        top = Gtk.Box(spacing=6)
        top.append(Gtk.Image(icon_name=icon, pixel_size=14))
        top.append(label(caption, "gs-tile-label"))
        self.append(top)
        row = Gtk.Box(spacing=3, valign=Gtk.Align.BASELINE_FILL)
        self.value = label("—", "gs-tile-value")
        self.unit = label("", "gs-tile-unit")
        self.unit.set_valign(Gtk.Align.BASELINE)
        self.value.set_valign(Gtk.Align.BASELINE)
        row.append(self.value)
        row.append(self.unit)
        self.append(row)

    def set(self, value: str, unit: str = "", dim: bool = False) -> None:
        self.value.set_label(value)
        self.unit.set_label(unit)
        if dim:
            self.add_css_class("dim")
        else:
            self.remove_css_class("dim")


class Tag(Gtk.Label):
    def __init__(self, kind: str = "measured", text: str | None = None):
        super().__init__(valign=Gtk.Align.CENTER)
        self.add_css_class("gs-tag")
        self._kind = None
        self.set_kind(kind, text)

    def set_kind(self, kind: str | None, text: str | None = None) -> None:
        if kind is None:
            self.set_visible(False)
            return
        self.set_visible(True)
        if self._kind:
            self.remove_css_class(self._kind)
        self.add_css_class(kind)
        self._kind = kind
        self.set_label((text or kind).upper())


class InfoRow(Gtk.Box):
    def __init__(self, key: str, tag: str | None = None):
        super().__init__(spacing=8)
        self.add_css_class("gs-row")
        self.key = label(key, "gs-row-key", xalign=0)
        self.key.set_hexpand(True)
        self.value = label("—", "gs-row-value", xalign=1, selectable=False)
        self.value.set_ellipsize(Pango.EllipsizeMode.END)
        self.value.set_max_width_chars(34)
        self.tag = Tag(tag or "measured")
        if tag is None:
            self.tag.set_visible(False)
        self.append(self.key)
        self.append(self.value)
        self.append(self.tag)

    def set(self, value: str, tag: str | None = "keep", tooltip: str | None = None) -> None:
        self.value.set_label(value)
        if tag != "keep":
            self.tag.set_kind(tag)
        self.value.set_tooltip_text(tooltip)


class Callout(Gtk.Box):
    ICONS = {"warn": "dialog-warning-symbolic", "good": "emblem-ok-symbolic", "info": "dialog-information-symbolic"}

    def __init__(self, kind: str = "warn"):
        super().__init__(spacing=12)
        self.add_css_class("gs-callout")
        self.icon = Gtk.Image(pixel_size=18, valign=Gtk.Align.START)
        self.icon.add_css_class("gs-callout-icon")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.title = label("", "gs-callout-title", wrap=True)
        self.body = label("", wrap=True)
        self.hint = label("", "gs-sub", wrap=True)
        box.append(self.title)
        box.append(self.body)
        box.append(self.hint)
        self.append(self.icon)
        self.append(box)
        self._kind = None
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        if self._kind:
            self.remove_css_class(self._kind)
        self.add_css_class(kind)
        self._kind = kind
        self.icon.set_from_icon_name(self.ICONS.get(kind, self.ICONS["info"]))

    def set(self, title: str, body: str = "", hint: str = "", kind: str | None = None) -> None:
        if kind:
            self.set_kind(kind)
        self.title.set_label(title)
        self.body.set_label(body)
        self.body.set_visible(bool(body))
        self.hint.set_label(hint)
        self.hint.set_visible(bool(hint))


class Sparkline(Gtk.DrawingArea):
    """Power (accent, filled) and utilisation (dim line) over time."""

    WINDOW = 180.0  # seconds shown

    def __init__(self):
        super().__init__(hexpand=True, content_height=58)
        self.add_css_class("gs-spark")
        self._history: list[tuple[float, float | None, float | None]] = []
        self._max_power = 10.0
        self._accent = Gdk.RGBA()
        self._accent.parse("#7aa2f7")
        self.set_draw_func(self._draw)

    def set_accent(self, rgba: Gdk.RGBA) -> None:
        self._accent = rgba
        self.queue_draw()

    def set_data(self, history, power_limit: float | None) -> None:
        self._history = history
        peak = max((p for _, p, _ in history if p is not None), default=0.0)
        # Scale to what the GPU actually does, capped by its limit.
        top = max(10.0, peak * 1.25)
        if power_limit:
            top = min(top, power_limit)
        self._max_power = top
        self.queue_draw()

    def _segments(self, now, w, h, index, scale):
        segments, current = [], []
        for t, *values in self._history:
            value = values[index - 1]
            x = w - (now - t) / self.WINDOW * w
            if x < -4:
                continue
            if value is None:
                if current:
                    segments.append(current)
                current = []
                continue
            y = h - 3 - (min(value, scale) / scale) * (h - 8)
            current.append((x, y))
        if current:
            segments.append(current)
        return segments

    def _draw(self, _area, cr, w, h) -> None:
        fg = self.get_color()
        # baseline grid
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08)
        cr.set_line_width(1)
        for frac in (0.25, 0.5, 0.75):
            y = round(h * frac) + 0.5
            cr.move_to(0, y)
            cr.line_to(w, y)
        cr.stroke()
        if not self._history:
            return
        now = self._history[-1][0]

        # utilisation
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
        cr.set_line_width(1.2)
        for seg in self._segments(now, w, h, 2, 100.0):
            self._path(cr, seg)
            cr.stroke()

        # power
        a = self._accent
        for seg in self._segments(now, w, h, 1, self._max_power):
            if len(seg) >= 2:
                self._path(cr, seg)
                cr.line_to(seg[-1][0], h)
                cr.line_to(seg[0][0], h)
                cr.close_path()
                grad = cairo.LinearGradient(0, 0, 0, h)
                grad.add_color_stop_rgba(0, a.red, a.green, a.blue, 0.35)
                grad.add_color_stop_rgba(1, a.red, a.green, a.blue, 0.0)
                cr.set_source(grad)
                cr.fill()
            cr.set_source_rgba(a.red, a.green, a.blue, 1.0)
            cr.set_line_width(2)
            self._path(cr, seg)
            cr.stroke()
            if seg:
                x, y = seg[-1]
                cr.arc(x - 1, y, 3, 0, 2 * math.pi)
                cr.fill()

    @staticmethod
    def _path(cr, seg) -> None:
        cr.move_to(*seg[0])
        if len(seg) == 1:
            cr.line_to(seg[0][0] + 0.1, seg[0][1])
            return
        # smooth with midpoint quadratic curves
        for (x0, y0), (x1, y1) in zip(seg, seg[1:]):
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            cr.curve_to(x0, y0, x0, y0, mx, my)
        cr.line_to(*seg[-1])


class ProcessRow(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=12)
        self.add_css_class("gs-proc")
        self.avatar = label("", "gs-avatar", xalign=0.5)
        self.avatar.set_valign(Gtk.Align.CENTER)
        self.avatar.set_size_request(34, 34)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.name = label("", "gs-proc-name")
        self.name.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail = label("", "gs-faint")
        self.detail.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(self.name)
        text.append(self.detail)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.vram = label("", "gs-row-value", xalign=1)
        self.meter = Gtk.ProgressBar(fraction=0.0, width_request=84)
        self.meter.add_css_class("gs-meter")
        right.append(self.vram)
        right.append(self.meter)
        self.append(self.avatar)
        self.append(text)
        self.append(right)

    def set(self, proc, total_vram: float | None) -> None:
        self.avatar.set_label((proc.name[:1] or "?").upper())
        self.name.set_label(proc.name + (" (this app)" if proc.is_self else ""))
        self.detail.set_label(f"PID {proc.pid} · {proc.kind}")
        self.set_tooltip_text(proc.command or None)
        if proc.vram_mb is not None:
            sm = f" · {proc.sm:.0f}% GPU" if proc.sm is not None else ""
            self.vram.set_label(f"{proc.vram_mb:.0f} MB{sm}")
            self.meter.set_visible(True)
            self.meter.set_fraction(min(1.0, proc.vram_mb / total_vram) if total_vram else 0.0)
        else:
            self.vram.set_label("idle")
            self.meter.set_visible(False)


def wrapping(text: str, *classes: str) -> Gtk.Label:
    """Centered label that wraps instead of forcing a wide minimum size."""
    widget = label(text, *classes, xalign=0.5, wrap=True)
    widget.set_justify(Gtk.Justification.CENTER)
    widget.set_wrap_mode(Pango.WrapMode.WORD)
    return widget


def toggle_child(icon: str | None, text: str) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, halign=Gtk.Align.CENTER)
    if icon:
        box.append(Gtk.Image(icon_name=icon, pixel_size=16))
    box.append(wrapping(text, "gs-mode-label"))
    return box


def button(text: str, *classes: str) -> Gtk.Button:
    widget = Gtk.Button(child=wrapping(text))
    for cls in classes:
        widget.add_css_class(cls)
    return widget


def idle(fn, *args) -> None:
    GLib.idle_add(lambda: fn(*args) and False)
