"""Settings dialog."""

from __future__ import annotations

from gi.repository import Adw, Gio, Gtk

from .. import autostart, paths, session_tuning
from ..config import MODES, THEMES
from ..policy import MODE_LABELS, Mode

THEME_LABELS = {
    "omarchy": "Omarchy theme",
    "system": "System (libadwaita)",
    "dark": "Dark",
    "light": "Light",
}


def _combo(title: str, subtitle: str, labels: list[str], selected: int, on_change) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new(labels), selected=selected)
    row.connect("notify::selected", lambda r, _p: on_change(r.get_selected()))
    return row


def _switch(title: str, subtitle: str, active: bool, on_change) -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
    row.connect("notify::active", lambda r, _p: on_change(r.get_active()))
    return row


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title="Settings", search_enabled=False)
        self.app = app
        cfg = app.config
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.add(page)

        mode_labels = [MODE_LABELS[Mode(m)] for m in MODES]
        profiles = Adw.PreferencesGroup(title="Profiles", description="Which GPU mode to use for each power source.")
        profiles.add(_combo("Plugged in", "AC adapter connected", mode_labels, MODES.index(cfg.ac_mode),
                            lambda i: app.set_profile("ac", Mode(MODES[i]))))
        profiles.add(_combo("On battery", "Running from the battery", mode_labels, MODES.index(cfg.battery_mode),
                            lambda i: app.set_profile("battery", Mode(MODES[i]))))
        profiles.add(_switch("Automatic profile switching", "Apply the matching profile when AC is plugged in or removed",
                             cfg.auto_switch, app.set_auto_switch))
        page.add(profiles)

        startup = Adw.PreferencesGroup(title="Startup")
        startup.add(_switch("Start at login", "Uses XDG autostart (~/.config/autostart), started by uwsm",
                            cfg.autostart and autostart.is_enabled(), lambda v: app.update_config(autostart=v)))
        startup.add(_switch("Start hidden", "At login, run in the background (tray + profiles) without opening the window",
                            cfg.start_hidden, lambda v: app.update_config(start_hidden=v)))
        page.add(startup)

        ui = Adw.PreferencesGroup(title="Interface")
        tray_state = "tray host detected" if app.tray_host_available() else "no tray host detected right now"
        ui.add(_switch("Tray icon", f"StatusNotifierItem in the Omarchy bar ({tray_state})", cfg.tray,
                       lambda v: app.update_config(tray=v)))
        spin = Adw.SpinRow.new_with_range(1, 30, 1)
        spin.set_title("Refresh interval")
        spin.set_subtitle("Seconds between updates while the window is open")
        spin.set_value(cfg.refresh_interval)
        spin.connect("notify::value", lambda r, _p: app.update_config(refresh_interval=r.get_value()))
        ui.add(spin)
        ui.add(_switch("Notifications", "Notify when the GPU mode actually changes", cfg.notifications,
                       lambda v: app.update_config(notifications=v)))
        ui.add(_combo("Theme", "Omarchy theme follows your current Omarchy colours live",
                      [THEME_LABELS[t] for t in THEMES], THEMES.index(cfg.theme),
                      lambda i: app.update_config(theme=THEMES[i])))
        ui.add(_switch("Translucent window", "Subtle glass effect over the wallpaper", cfg.translucent,
                       lambda v: app.update_config(translucent=v)))
        page.add(ui)

        hybrid = Adw.PreferencesGroup(
            title="Hybrid graphics",
            description="Omarchy routes video decoding and GLX to NVIDIA by default. In Hybrid mode that wakes "
            "the dGPU needlessly; session tuning routes them to Intel only when the panel is on the iGPU.",
        )
        self.tuning = _switch("Hyprland session tuning", f"{session_tuning.snippet_path()} · applies at next login",
                              session_tuning.is_enabled(), self._on_tuning)
        hybrid.add(self.tuning)
        page.add(hybrid)

        maintenance = Adw.PreferencesGroup(title="Maintenance")
        restore = Adw.ActionRow(title="Restore driver default", subtitle="Set NVIDIA runtime PM back to 'auto' (the driver default)")
        button = Gtk.Button(label="Restore", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: app.restore_default())
        restore.add_suffix(button)
        maintenance.add(restore)
        folder = Adw.ActionRow(title="Configuration folder", subtitle=str(paths.config_dir()))
        open_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Open")
        open_btn.add_css_class("flat")
        open_btn.connect("clicked", lambda *_: Gtk.FileLauncher.new(Gio.File.new_for_path(str(paths.config_dir()))).launch(app.window, None, None))
        folder.add_suffix(open_btn)
        maintenance.add(folder)
        page.add(maintenance)

    def _on_tuning(self, enabled: bool) -> None:
        ok, message = session_tuning.enable() if enabled else session_tuning.disable()
        self.add_toast(Adw.Toast(title=message.splitlines()[0], timeout=5))
        if not ok:
            self.tuning.set_active(session_tuning.is_enabled())
        self.app.refresh()


def mux_dialog(app, target: str) -> Adw.AlertDialog:
    if target == "hybrid":
        heading = "Switch to Hybrid graphics?"
        body = (
            "After the next reboot the ASUS MUX wires the laptop panel to the Intel iGPU. Hyprland will run on "
            "Intel, the NVIDIA GPU can power down when idle, and apps can still use it with “kelvin run”.\n\n"
            "• Needs administrator authentication\n"
            "• Takes effect after you reboot — Kelvin never reboots for you\n"
            "• The HDMI port stays wired to the NVIDIA GPU\n\n"
            "To undo: Kelvin, or “kelvin mux discrete”, then reboot."
        )
        action = "Switch to Hybrid"
    else:
        heading = "Switch to Discrete graphics?"
        body = (
            "After the next reboot the ASUS MUX wires the laptop panel directly to the NVIDIA GPU. Maximum "
            "performance, but the NVIDIA GPU can never power down and battery life drops.\n\n"
            "• Needs administrator authentication\n"
            "• Takes effect after you reboot — Kelvin never reboots for you"
        )
        action = "Switch to Discrete"

    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("switch", action)
    dialog.set_response_appearance("switch", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    tuning_row = None
    if target == "hybrid":
        group = Adw.PreferencesGroup()
        tuning_row = Adw.SwitchRow(
            title="Tune the Hyprland session",
            subtitle="Route VA-API video decoding and GLX to Intel in Hybrid mode (writes ~/.config/hypr/kelvin.lua)",
            active=True,
        )
        group.add(tuning_row)
        dialog.set_extra_child(group)

    def on_response(_dialog, response):
        if response == "switch":
            app.switch_mux(target, tuning_row.get_active() if tuning_row else None)

    dialog.connect("response", on_response)
    return dialog
