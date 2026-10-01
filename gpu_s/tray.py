"""StatusNotifierItem + com.canonical.dbusmenu tray icon over GDBus.

Omarchy's shell (Quickshell) hosts org.kde.StatusNotifierWatcher, so this
needs no extra libraries. The icon is drawn with cairo and sent as a pixmap,
which every SNI host supports regardless of icon theme.
"""

from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Callable

import cairo
from gi.repository import Gio, GLib

from .policy import Mode

log = logging.getLogger("gpu-s.tray")

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

MODE_IDS = {Mode.AUTO: 41, Mode.ALWAYS_ON: 42, Mode.POWER_SAVING: 43}


@dataclass
class TrayStatus:
    state: str = "Unknown"  # "Active", "Powered down", ...
    battery: str = ""  # "63% · 80% limit"
    detail: str = ""  # tooltip body
    mode: Mode | None = None
    supported: dict[Mode, bool] = field(default_factory=dict)
    level: str = "unknown"  # active | off | forced | unknown
    fg: tuple[float, float, float] = (0.92, 0.92, 0.95)
    accent: tuple[float, float, float] = (0.27, 0.80, 0.52)


# -- icon drawing ------------------------------------------------------------


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw_icon(size: int, status: TrayStatus) -> bytes:
    """Chip glyph with a status dot, as ARGB32 in network byte order."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    s = size / 22.0
    fg = status.fg
    off = status.level == "off"

    # pins
    cr.set_source_rgba(*fg, 0.55 if off else 0.9)
    cr.set_line_width(1.4 * s)
    for i in range(3):
        p = (7.5 + i * 3.5) * s
        for x1, y1, x2, y2 in ((p, 1.5 * s, p, 4.5 * s), (p, 17.5 * s, p, 20.5 * s),
                               (1.5 * s, p, 4.5 * s, p), (17.5 * s, p, 20.5 * s, p)):
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
    cr.stroke()

    # body
    _rounded(cr, 4.5 * s, 4.5 * s, 13 * s, 13 * s, 2.6 * s)
    cr.set_line_width(1.6 * s)
    cr.stroke()

    # die: filled when the GPU is powered, hollow when it is off
    _rounded(cr, 8 * s, 8 * s, 6 * s, 6 * s, 1.2 * s)
    if status.level in ("active", "forced"):
        cr.set_source_rgba(*status.accent, 1.0)
        cr.fill()
    else:
        cr.set_line_width(1.2 * s)
        cr.set_source_rgba(*fg, 0.55)
        cr.stroke()

    surface.flush()
    data = bytearray(surface.get_data())
    out = bytearray(len(data))
    little = sys.byteorder == "little"
    for i in range(0, len(data), 4):
        if little:
            b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
        else:
            a, r, g, b = data[i], data[i + 1], data[i + 2], data[i + 3]
        if a and a < 255:  # un-premultiply
            r, g, b = (min(255, c * 255 // a) for c in (r, g, b))
        out[i:i + 4] = bytes((a, r, g, b))
    return bytes(out)


# -- tray item ---------------------------------------------------------------


class TrayIcon:
    def __init__(
        self,
        connection: Gio.DBusConnection,
        on_activate: Callable[[], None],
        on_mode: Callable[[Mode], None],
        on_quit: Callable[[], None],
    ):
        self._conn = connection
        self._on_activate = on_activate
        self._on_mode = on_mode
        self._on_quit = on_quit
        self._name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._owner_id = 0
        self._watch_id = 0
        self._reg_ids: list[int] = []
        self._revision = 1
        self._status = TrayStatus()
        self._pixmaps = self._render()
        self.registered = False
        self.host_available = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._reg_ids = [
            self._conn.register_object(ITEM_PATH, sni, self._sni_call, self._sni_prop, None),
            self._conn.register_object(MENU_PATH, menu, self._menu_call, self._menu_prop, None),
        ]
        self._owner_id = Gio.bus_own_name_on_connection(
            self._conn, self._name, Gio.BusNameOwnerFlags.NONE, self._on_name_acquired, None
        )
        self._watch_id = Gio.bus_watch_name_on_connection(
            self._conn, WATCHER, Gio.BusNameWatcherFlags.NONE, self._on_watcher_up, self._on_watcher_down
        )

    def stop(self) -> None:
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
        for reg in self._reg_ids:
            self._conn.unregister_object(reg)
        self._watch_id = self._owner_id = 0
        self._reg_ids = []
        self.registered = False

    def _on_name_acquired(self, *_):
        self._register()

    def _on_watcher_up(self, *_):
        self.host_available = True
        self._register()

    def _on_watcher_down(self, *_):
        self.host_available = False
        self.registered = False

    def _register(self) -> None:
        if not self.host_available:
            return

        def done(conn, res):
            try:
                conn.call_finish(res)
                self.registered = True
            except GLib.Error as exc:
                log.warning("tray registration failed: %s", exc.message)

        self._conn.call(
            WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._name,)), None, Gio.DBusCallFlags.NONE, 3000, None, done,
        )

    # -- state ---------------------------------------------------------

    def update(self, status: TrayStatus) -> None:
        old = self._status
        self._status = status
        if (old.level, old.fg, old.accent) != (status.level, status.fg, status.accent):
            self._pixmaps = self._render()
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon", None)
        if (old.state, old.detail) != (status.state, status.detail):
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewTitle", None)
        if (old.state, old.mode, old.supported, old.battery) != (status.state, status.mode, status.supported, status.battery):
            self._revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self._revision, 0)))

    def _render(self):
        return [(size, size, draw_icon(size, self._status)) for size in (22, 32, 48)]

    def _emit(self, path, iface, signal, params) -> None:
        try:
            self._conn.emit_signal(None, path, iface, signal, params)
        except GLib.Error:
            pass

    # -- StatusNotifierItem ------------------------------------------------

    def _sni_prop(self, _conn, _sender, _path, _iface, name):
        s = self._status
        tooltip_title = f"GPU-S — GPU: {s.state}" + (f" · Battery: {s.battery}" if s.battery else "")
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "gpu-s"),
            "Title": GLib.Variant("s", tooltip_title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", ""),
            "IconThemePath": GLib.Variant("s", ""),
            "IconPixmap": GLib.Variant("a(iiay)", self._pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], tooltip_title, s.detail)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: self._on_activate() and False)
        invocation.return_value(None)

    # -- dbusmenu ------------------------------------------------------------

    def _items(self) -> dict[int, tuple[dict, list[int]]]:
        s = self._status

        def item(label, enabled=True, **extra):
            props = {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", enabled), "visible": GLib.Variant("b", True)}
            for key, value in extra.items():
                props[key.replace("_", "-")] = value
            return props

        sep = {"type": GLib.Variant("s", "separator"), "visible": GLib.Variant("b", True)}
        items: dict[int, tuple[dict, list[int]]] = {
            0: ({"children-display": GLib.Variant("s", "submenu")}, [1, 2, 8, 3, 4, 5, 6, 7]),
            1: (item("GPU-S", False), []),
            2: (item(f"GPU: {s.state}", False), []),
            8: (item(f"Battery: {s.battery or '—'}", False), []),
            3: (sep, []),
            4: (item("Power Profile", children_display=GLib.Variant("s", "submenu")), [41, 42, 43]),
            5: (sep, []),
            6: (item("Open GPU-S"), []),
            7: (item("Quit"), []),
        }
        for mode, ident in MODE_IDS.items():
            enabled = s.supported.get(mode, False)
            label = mode.label if enabled else f"{mode.label} (unavailable)"
            items[ident] = (
                item(
                    label,
                    enabled,
                    toggle_type=GLib.Variant("s", "radio"),
                    toggle_state=GLib.Variant("i", 1 if s.mode is mode else 0),
                ),
                [],
            )
        return items

    def _layout(self, items, ident: int, depth: int):
        props, children = items[ident]
        kids = []
        if depth != 0:
            kids = [GLib.Variant("(ia{sv}av)", self._layout(items, c, depth - 1)) for c in children]
        return (ident, props, kids)

    def _menu_prop(self, _conn, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        items = self._items()
        if method == "GetLayout":
            parent, depth, _names = params.unpack()
            if parent not in items:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error", f"no item {parent}")
                return
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._revision, self._layout(items, parent, depth))))
        elif method == "GetGroupProperties":
            ids, _names = params.unpack()
            ids = ids or list(items)
            result = [(i, items[i][0]) for i in ids if i in items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))
        elif method == "GetProperty":
            ident, name = params.unpack()
            value = items.get(ident, ({}, []))[0].get(name)
            if value is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error", "no such property")
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            ident, event, _data, _ts = params.unpack()
            invocation.return_value(None)
            if event == "clicked":
                GLib.idle_add(lambda: self._clicked(ident) and False)
        elif method == "EventGroup":
            (events,) = params.unpack()
            errors = []
            for ident, event, _data, _ts in events:
                if ident not in items:
                    errors.append(ident)
                elif event == "clicked":
                    GLib.idle_add(lambda i=ident: self._clicked(i) and False)
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _clicked(self, ident: int) -> None:
        if ident == 6:
            self._on_activate()
        elif ident == 7:
            self._on_quit()
        else:
            for mode, mode_id in MODE_IDS.items():
                if ident == mode_id and self._status.supported.get(mode):
                    self._on_mode(mode)
