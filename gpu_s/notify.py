"""Desktop notifications over org.freedesktop.Notifications (no extra deps)."""

from __future__ import annotations

from . import APP_ID, APP_NAME

_last_id = 0


def send(summary: str, body: str = "", *, icon: str = APP_ID, urgency: int = 1) -> bool:
    """Show (or replace GPU-S's previous) notification. Returns False on failure."""
    global _last_id
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        hints = {
            "desktop-entry": GLib.Variant("s", APP_ID),
            "urgency": GLib.Variant("y", urgency),
        }
        reply = bus.call_sync(
            "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications",
            "Notify",
            GLib.Variant("(susssasa{sv}i)", (APP_NAME, _last_id, icon, summary, body, [], hints, 6000)),
            GLib.VariantType.new("(u)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        (_last_id,) = reply.unpack()
        return True
    except Exception:  # noqa: BLE001 - no notification daemon is not an error
        return False
