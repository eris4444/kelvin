"""Kelvin application: window, tray icon, profile agent and refresh loop."""

from __future__ import annotations

import logging
import platform
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME, VERSION, autostart, charge, fans, idle, notify, paths, power_manager, privilege, session_tuning  # noqa: E402
from ..agent import ProfileAgent  # noqa: E402
from ..config import config_path, load_config, load_state, save_config, save_state, state_path  # noqa: E402
from ..hardware import Facts, gather_facts  # noqa: E402
from ..monitor import Monitor, Snapshot  # noqa: E402
from ..policy import Assessment, Mode, assess, effective_mode  # noqa: E402
from ..tray import TrayIcon, TrayStatus  # noqa: E402
from . import theme  # noqa: E402
from .settings import SettingsDialog, mux_dialog  # noqa: E402
from .window import MainWindow, UiContext  # noqa: E402

log = logging.getLogger("kelvin")

BACKGROUND_TICK_S = 5


class GpuSApp(Adw.Application):
    def __init__(self, background: bool):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.config = load_config()
        self.state = load_state()
        self.monitor = Monitor()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kelvin-monitor")
        self.window: MainWindow | None = None
        self.tray: TrayIcon | None = None
        self.agent: ProfileAgent | None = None
        self.busy = False
        self.last_snapshot: Snapshot | None = None
        self._suppress_activation = background and self.config.start_hidden
        self._job_running = False
        self._pending_force = False
        self._refresh_id = 0
        self._background_id = 0
        self._file_monitors: list[Gio.FileMonitor] = []
        self._debounce: dict[str, int] = {}
        self._css_static = Gtk.CssProvider()
        self._css_theme = Gtk.CssProvider()
        self._palette: theme.Palette | None = None
        self._tray_cpu = None  # created lazily (CPU load needs two samples)

    # -- lifecycle -------------------------------------------------------------

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        Gtk.Window.set_default_icon_name(APP_ID)

        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(paths.data_dir() / "icons"))
        self._css_static.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(display, self._css_static, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_display(display, self._css_theme, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.apply_theme())
        self.apply_theme()

        for name, callback, accels in (
            ("quit", lambda *_: self.quit_app(), ["<Control>q"]),
            ("show", lambda *_: self.show_window(), []),
            ("settings", lambda *_: self.show_settings(), ["<Control>comma"]),
            ("about", lambda *_: self.show_about(), []),
            ("refresh", lambda *_: self.refresh(force=True), ["<Control>r", "F5"]),
            ("switch-graphics", lambda *_: self.ask_mux_switch(), []),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

        # Keep running without a window: the agent and tray live here.
        self.hold()
        power_manager.record_baseline()
        autostart.set_enabled(self.config.autostart)

        self.agent = ProfileAgent(lambda: self.config, self._on_agent_change)
        GLib.idle_add(lambda: self.agent.start() and False)
        if self.config.tray:
            self._start_tray()

        self._watch_file(config_path(), "config", self._on_config_file)
        self._watch_file(state_path(), "state", lambda: self.refresh())
        self._watch_dir(paths.omarchy_theme_dir().parent, "theme", self.apply_theme)
        self._background_id = GLib.timeout_add_seconds(BACKGROUND_TICK_S, self._background_tick)
        GLib.idle_add(lambda: self.refresh() and False)

    def do_activate(self):
        if self._suppress_activation:
            self._suppress_activation = False
            return
        self.show_window()

    def quit_app(self) -> None:
        if self.tray:
            self.tray.stop()
        if self.agent:
            self.agent.stop()
        for source in (self._refresh_id, self._background_id):
            if source:
                GLib.source_remove(source)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.release()
        self.quit()

    # -- window ----------------------------------------------------------------

    def show_window(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
            self.window.connect("close-request", self._on_close_request)
            self._apply_accent()
        self.window.present()
        self._restart_refresh_timer()
        self.refresh()

    def toggle_window(self) -> None:
        if self.window and self.window.get_visible() and self.window.is_active():
            self._on_close_request(self.window)
        else:
            self.show_window()

    def _on_close_request(self, window) -> bool:
        if self.config.tray or self.config.auto_switch:
            window.set_visible(False)
            self._stop_refresh_timer()
            return True
        self.quit_app()
        return True

    def _window_visible(self) -> bool:
        return bool(self.window and self.window.get_visible())

    def show_settings(self) -> None:
        self.show_window()
        SettingsDialog(self).present(self.window)

    def show_about(self) -> None:
        self.show_window()
        facts = gather_facts()
        debug = "\n".join(
            [
                f"Kelvin {VERSION}",
                f"Kernel {platform.release()}",
                f"NVIDIA driver {facts.driver_version}",
                f"Product {facts.product}",
                f"MUX {facts.mux.configured_mode} (effective {facts.effective_mux_mode}, via {facts.mux.source})",
                f"Runtime D3 {facts.rtd3.status if facts.rtd3 else 'n/a'}",
                f"Renderer {facts.session.renderer_address} ({facts.session.renderer_source})",
            ]
        )
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=VERSION,
            developer_name="Kelvin",
            comments="Monitor and control NVIDIA hybrid graphics power on Omarchy.",
            license_type=Gtk.License.MIT_X11,
            debug_info=debug,
        )
        about.present(self.window)

    # -- refresh loop ------------------------------------------------------------

    def _restart_refresh_timer(self) -> None:
        self._stop_refresh_timer()
        interval_ms = int(self.config.refresh_interval * 1000)
        self._refresh_id = GLib.timeout_add(interval_ms, self._refresh_tick)

    def _stop_refresh_timer(self) -> None:
        if self._refresh_id:
            GLib.source_remove(self._refresh_id)
            self._refresh_id = 0

    def _refresh_tick(self) -> bool:
        if self._window_visible():
            self.refresh()
            return True
        self._refresh_id = 0
        return False

    def refresh(self, force: bool = False) -> None:
        if self._job_running:
            self._pending_force = self._pending_force or force
            return
        self._job_running = True
        self.state = load_state()
        visible = self._window_visible()
        on_apps_page = visible and self.window.stack.get_visible_child_name() == "power"
        applied, interval = self.state.applied_mode, self.config.refresh_interval
        future = self.executor.submit(
            self.monitor.snapshot, applied, interval,
            want_processes=visible, allow_nvml=visible, force=force,
            nvml_processes=on_apps_page,
        )
        future.add_done_callback(lambda f: GLib.idle_add(self._on_snapshot, f))

    def _on_snapshot(self, future: Future) -> bool:
        self._job_running = False
        try:
            snap = future.result()
        except Exception:  # noqa: BLE001
            log.exception("refresh failed")
            return False
        self.last_snapshot = snap
        if snap.sample_fresh and snap.sample and snap.sample.name and snap.sample.name != self.state.gpu_name:
            self.state.gpu_name = snap.sample.name
            save_state(self.state)
        self._update_tray(snap.facts, snap.assessment, snap.mode, snap.gpu_power_w, snap.charge, snap.cpu, snap.idle)
        if self._window_visible():
            self.window.update(snap, self._context())
        if self._pending_force:
            self._pending_force = False
            self.refresh(force=True)
        return False

    def _background_tick(self) -> bool:
        if self._tray_cpu is None:
            from ..cpu import CpuMonitor

            self._tray_cpu = CpuMonitor()
        if not self._window_visible() and not self._job_running:
            facts = gather_facts()
            assessment = assess(facts)
            mode = effective_mode(facts, assessment, load_state().applied_mode)
            self._update_tray(facts, assessment, mode, None, charge.read_charge(check_units=False),
                              self._tray_cpu.sample(), idle.read_settings(with_logind=False))
        return True

    def _context(self) -> UiContext:
        return UiContext(config=self.config, state=self.state, busy=self.busy, tuning_enabled=session_tuning.is_enabled())

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        if self._window_visible() and self.last_snapshot:
            self.window.update(self.last_snapshot, self._context())

    # -- tray --------------------------------------------------------------------

    def _start_tray(self) -> None:
        if self.tray is not None:
            return
        connection = self.get_dbus_connection()
        if connection is None:
            return
        self.tray = TrayIcon(connection, self.toggle_window, self.request_mode, self.quit_app, self.request_stay_awake)
        self.tray.start()
        if self.last_snapshot:
            s = self.last_snapshot
            self._update_tray(s.facts, s.assessment, s.mode, s.gpu_power_w)

    def _stop_tray(self) -> None:
        if self.tray:
            self.tray.stop()
            self.tray = None

    def tray_host_available(self) -> bool:
        if self.tray is not None:
            return self.tray.host_available
        connection = self.get_dbus_connection()
        try:
            reply = connection.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)), None, Gio.DBusCallFlags.NONE, 1000, None,
            )
            return bool(reply.unpack()[0])
        except (GLib.Error, AttributeError):
            return False

    def _update_tray(self, facts: Facts, assessment: Assessment, mode: Mode | None, watts: float | None,
                     charge_info: charge.ChargeInfo | None = None, cpu_info=None,
                     idle_info: idle.IdleSettings | None = None) -> None:
        if self.tray is None:
            return
        power = facts.power
        if not facts.gpu or power is None:
            state, level = "Not detected", "unknown"
        elif power.powered_down:
            state, level = f"Powered down ({power.pci_state})", "off"
        elif power.suspended:
            state, level = "Suspended", "off"
        else:
            state, level = "Active", "forced" if assessment.forced_mode else "active"
        detail = [f"Mode: {mode.label}" if mode else "Mode: —"]
        if watts is not None:
            detail.append(f"Power: {watts:.1f} W")
        if assessment.unsupported and assessment.forced_mode:
            detail.append(assessment.unsupported.title)
        battery = ""
        if charge_info and charge_info.battery and charge_info.percent is not None:
            limit = f"{charge_info.kernel_end}% limit" if charge_info.limit_active else "no limit"
            battery = f"{charge_info.percent:.0f}% · {limit}"
            if charge_info.charging:
                battery += " · charging"
        elif self.tray._status.battery:
            battery = self.tray._status.battery
        cpu_text = self.tray._status.cpu
        if cpu_info is not None and cpu_info.package_temp is not None:
            load = f" · {cpu_info.usage:.0f}% load" if cpu_info.usage is not None else ""
            cpu_text = f"{cpu_info.package_temp:.0f}°C{load}"
        stay_awake = idle_info.stay_awake if idle_info else self.tray._status.stay_awake
        status = TrayStatus(
            state=state,
            battery=battery,
            cpu=cpu_text,
            stay_awake=stay_awake,
            detail="\n".join(detail),
            mode=mode,
            supported={m: assessment.supports(m) for m in Mode},
            level=level,
        )
        if self._palette:
            status.fg = self._palette.text
            status.accent = self._palette.accent_text
        self.tray.update(status)

    # -- actions -------------------------------------------------------------------

    def request_mode(self, mode: Mode) -> None:
        if self.busy:
            return
        self._set_busy(True)
        on_ac = self.agent.on_ac if self.agent else None

        def done(result: power_manager.ApplyResult) -> None:
            self._set_busy(False)
            if self._window_visible():
                self.window.toast(result.message)
            elif self.config.notifications and (result.changed or not result.ok):
                notify.send("Kelvin", result.message)
            self.refresh()

        power_manager.apply_async(mode, "manual", on_ac, done)

    # -- cooling ------------------------------------------------------------------------

    def _finish(self, ok: bool, message: str, notify_hidden: bool = True) -> None:
        self._set_busy(False)
        self.monitor.invalidate_supply()
        if self._window_visible():
            self.window.toast(message, timeout=5)
        elif notify_hidden and self.config.notifications:
            notify.send(APP_NAME, message)
        self.refresh()

    def request_fan_profile(self, profile: str) -> None:
        if self.busy:
            return
        self._set_busy(True)
        result = fans.set_profile(profile)
        self._finish(result.ok, result.message)
        if result.ok and result.changed and self.agent:
            self.agent.schedule_fan_reapply(1500)

    def request_fan_mode(self, fan: str, mode: str) -> None:
        if self.busy or getattr(self.config, f"fan_{fan}") == mode:
            return
        curve = getattr(self.config, f"fan_{fan}_curve")
        curve = [tuple(p) for p in curve] if curve else None
        self._set_busy(True)
        previous = getattr(self.config, f"fan_{fan}")
        setattr(self.config, f"fan_{fan}", mode)
        save_config(self.config)

        def done(result: fans.FanResult) -> None:
            if not result.ok:
                setattr(self.config, f"fan_{fan}", previous)
                save_config(self.config)
            self._finish(result.ok, result.message)

        fans.apply_async(fan, mode, curve, done)

    # -- sleep ---------------------------------------------------------------------------

    def request_stay_awake(self, enabled: bool) -> None:
        if self.busy:
            return
        self._set_busy(True)
        result = idle.set_stay_awake(enabled)
        self._finish(result.ok, result.message)

    def request_idle_stage(self, stage: str, seconds: int) -> None:
        if self.busy:
            return
        self._set_busy(True)
        result = idle.set_stage(stage, seconds)
        self._finish(result.ok, result.message, notify_hidden=False)

    # -- battery charge limit --------------------------------------------------------

    def request_charge(self, end: int, start: int | None) -> None:
        if self.busy:
            return
        self._set_busy(True)

        def done(result: charge.ChargeResult) -> None:
            self._set_busy(False)
            self.monitor.invalidate_supply()
            if self._window_visible():
                self.window.toast(result.message, timeout=5)
            if result.ok and result.changed and self.config.notifications and self.config.battery_notifications:
                notify.send("Kelvin", result.message)
            elif not result.ok and not self._window_visible():
                notify.send("Kelvin", result.message)
            self.refresh()

        charge.apply_async(end, start, done)

    def confirm_charge_preset(self, preset: charge.Preset) -> None:
        info = charge.read_charge(check_units=False)
        ok, why = charge.preset_status(info, preset)
        if not ok:
            self.window.toast(why, timeout=5)
            return
        if charge.matching_preset(info) == preset:
            self.window.toast(f"{preset.label} is already active.")
            return
        start = preset.start if info.start_supported else None
        if preset.end >= 100 and start is None:
            body = "The charge limit will be removed and the battery will charge to 100%."
        elif start is None:
            body = (f"Charging will stop at {preset.end}%. This battery has no start threshold; the "
                    "firmware resumes charging on its own once the level drops below the limit.")
        else:
            body = f"Charging will resume below {start}% and stop at {preset.end}%."
        body += "\n\nApplied by UPower to the kernel threshold and kept across reboots."
        if charge._plan(info, preset.end, start)[0]:
            body += " Changing the stored value needs administrator authentication."
        dialog = Adw.AlertDialog(heading=f"Apply {preset.label}?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "apply" and self.request_charge(preset.end, start))
        dialog.present(self.window)

    def set_profile(self, source: str, mode: Mode) -> None:
        key = "ac_mode" if source == "ac" else "battery_mode"
        if getattr(self.config, key) == mode.value:
            return
        setattr(self.config, key, mode.value)
        save_config(self.config)
        on_ac = self.agent.on_ac if self.agent else None
        if (source == "ac") == bool(on_ac) and on_ac is not None:
            # Editing the profile for the current source replaces a manual choice.
            state = load_state()
            state.manual_mode = None
            state.manual_on_ac = None
            save_state(state)
        if self.agent:
            self.agent.evaluate("config-changed")

    def set_auto_switch(self, enabled: bool) -> None:
        self.update_config(auto_switch=enabled)
        if self.agent and enabled:
            self.agent.evaluate("config-changed")

    def update_config(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self.config, key, value)
        save_config(self.config)
        if "autostart" in changes:
            autostart.set_enabled(self.config.autostart)
        if "tray" in changes:
            self._start_tray() if self.config.tray else self._stop_tray()
        if "theme" in changes or "translucent" in changes:
            self.apply_theme()
        if "refresh_interval" in changes and self._window_visible():
            self._restart_refresh_timer()
        self.refresh()

    def restore_default(self) -> None:
        def done(result: privilege.HelperResult) -> None:
            state = load_state()
            state.manual_mode = state.manual_on_ac = None
            state.applied_mode = Mode.AUTO.value if result.ok else state.applied_mode
            save_state(state)
            if self.window:
                self.window.toast("Runtime PM restored to 'auto'." if result.ok else result.message)
            self.refresh()

        privilege.run_async(paths.PM_HELPER, ["auto"], done)

    def ask_mux_switch(self) -> None:
        facts = gather_facts()
        if not facts.mux.available:
            return
        self.show_window()
        target = "hybrid" if facts.mux.configured_mode == "discrete" else "discrete"
        mux_dialog(self, target).present(self.window)

    def switch_mux(self, target: str, tuning: bool | None) -> None:
        check = power_manager.mux_check(target)
        if not check.ok:
            self.window.toast(check.message, timeout=6)
            return
        if check.output.startswith("unchanged"):
            self.window.toast(f"The MUX is already set to {target.capitalize()}.")
            return
        self._set_busy(True)

        def done(result: privilege.HelperResult) -> None:
            self._set_busy(False)
            if not result.ok:
                self.window.toast(result.message, timeout=6)
                self.refresh()
                return
            message = f"{target.capitalize()} mode will be active after the next reboot."
            if tuning and not session_tuning.is_enabled():
                ok, tuning_message = session_tuning.enable()
                if not ok:
                    message += " " + tuning_message.splitlines()[0]
            self.window.toast(message, timeout=6)
            if self.config.notifications:
                notify.send("Kelvin", message)
            self.refresh()

        power_manager.mux_switch_async(target, done)

    def undo_mux_switch(self) -> None:
        facts = gather_facts()
        effective = facts.effective_mux_mode
        if effective:
            self.switch_mux(effective, None)

    # -- config/theme/file watching --------------------------------------------------

    def _on_agent_change(self, message: str | None) -> None:
        # The agent runs on power-source changes: show the new source right away.
        self.monitor.invalidate_supply()
        if message and self._window_visible():
            self.window.toast(message)
        self.refresh()

    def _on_config_file(self) -> None:
        fresh = load_config()
        if fresh == self.config:
            return
        old = self.config
        self.config = fresh
        if old.tray != fresh.tray:
            self._start_tray() if fresh.tray else self._stop_tray()
        if (old.theme, old.translucent) != (fresh.theme, fresh.translucent):
            self.apply_theme()
        if old.autostart != fresh.autostart:
            autostart.set_enabled(fresh.autostart)
        if self.agent and (old.ac_mode, old.battery_mode, old.auto_switch) != (fresh.ac_mode, fresh.battery_mode, fresh.auto_switch):
            self.agent.evaluate("config-changed")
        self.refresh()

    def _debounced(self, key: str, callback, delay_ms: int = 300) -> None:
        if self._debounce.get(key):
            GLib.source_remove(self._debounce[key])

        def fire():
            self._debounce[key] = 0
            callback()
            return False

        self._debounce[key] = GLib.timeout_add(delay_ms, fire)

    def _watch_file(self, path: Path, key: str, callback) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        monitor = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        monitor.connect("changed", lambda *_: self._debounced(key, callback))
        self._file_monitors.append(monitor)

    def _watch_dir(self, path: Path, key: str, callback) -> None:
        if not path.is_dir():
            return
        monitor = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        monitor.connect("changed", lambda *_: self._debounced(key, callback, 500))
        self._file_monitors.append(monitor)

    def apply_theme(self) -> None:
        cfg = self.config
        manager = Adw.StyleManager.get_default()
        palette = theme.load_palette() if cfg.theme == "omarchy" else None
        if cfg.theme == "omarchy" and palette:
            scheme = Adw.ColorScheme.FORCE_LIGHT if palette.light else Adw.ColorScheme.FORCE_DARK
        elif cfg.theme == "dark":
            scheme = Adw.ColorScheme.FORCE_DARK
        elif cfg.theme == "light":
            scheme = Adw.ColorScheme.FORCE_LIGHT
        else:
            scheme = Adw.ColorScheme.DEFAULT
        if manager.get_color_scheme() != scheme:
            manager.set_color_scheme(scheme)
        self._palette = palette
        self._css_theme.load_from_string(theme.palette_css(palette, cfg.translucent, not manager.get_dark()))
        self._apply_accent()
        if self.last_snapshot:
            s = self.last_snapshot
            self._update_tray(s.facts, s.assessment, s.mode, s.gpu_power_w)

    def _apply_accent(self) -> None:
        if not self.window:
            return
        rgba = Gdk.RGBA()
        if self._palette:
            rgba.parse(theme.css(self._palette.accent_text))
        else:
            rgba = Adw.StyleManager.get_default().get_accent_color_rgba()
        self.window.set_accent(rgba)


def _instance_running() -> bool:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (APP_ID,)), None, Gio.DBusCallFlags.NONE, 1000, None,
        )
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def run(background: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    if background and _instance_running():
        return 0  # autostart while Kelvin already runs: nothing to do
    # If another instance owns the name, GApplication forwards "activate" to it
    # (which shows its window) and returns immediately.
    return GpuSApp(background).run([sys.argv[0]])
