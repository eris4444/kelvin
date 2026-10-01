"""`gpu-s` command-line interface."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_ID, APP_NAME, VERSION, autostart, charge, paths, power_manager, session_tuning
from .battery import read_power_supply
from .config import MODES, load_config, load_state, save_config, save_state
from .hardware import Facts, gather_facts
from .monitor import Monitor
from .policy import MODE_LABELS, Mode, assess, target_mode

HELP = f"""\
{APP_NAME} {VERSION} — NVIDIA GPU power manager for Omarchy

Usage: gpu-s [COMMAND] [ARGS]

  gpu-s                  Open the GPU-S window
  gpu-s status [--json]  Show GPU, power-state and profile summary
  gpu-s on               Keep the NVIDIA GPU on            (Always On)
  gpu-s off              Allow the GPU to power down       (Power Saving)
  gpu-s auto             Power down when idle, wake on use (Auto)
  gpu-s profile          Re-apply the AC/battery profile for the current power source
  gpu-s ac [MODE]        Show or set the plugged-in GPU profile
  gpu-s battery [...]    Battery info and charge limits (gpu-s battery help)
  gpu-s apps             List applications using the NVIDIA GPU
  gpu-s run CMD [ARGS]   Run a command on the NVIDIA GPU (PRIME render offload)
  gpu-s mux [hybrid|discrete]
                         Show or switch the ASUS GPU MUX (takes effect after reboot)
  gpu-s tuning [on|off]  Show or toggle Hyprland hybrid-session tuning
  gpu-s autostart [on|off]
                         Show or toggle starting GPU-S at login
  gpu-s quit             Stop the running GPU-S instance
  gpu-s reset [--keep-mux]
                         Undo everything GPU-S changed (runtime PM, session
                         tuning, autostart, battery override, MUX); used
                         before uninstalling
  gpu-s help             Show this help
  gpu-s --version        Show the version

MODE is one of: always-on, auto, power-saving

on/off/auto are manual overrides: they hold until the power source changes
(when automatic switching is enabled), then the AC/battery profile applies.

GUI options: --background  start hidden (tray icon + automatic profiles)
             --foreground  do not detach from the terminal
"""

MODE_ALIASES = {
    "always-on": Mode.ALWAYS_ON, "always": Mode.ALWAYS_ON, "on": Mode.ALWAYS_ON,
    "auto": Mode.AUTO,
    "power-saving": Mode.POWER_SAVING, "powersave": Mode.POWER_SAVING,
    "saving": Mode.POWER_SAVING, "off": Mode.POWER_SAVING,
}


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def green(self, t): return self._wrap("32", t)
    def yellow(self, t): return self._wrap("33", t)
    def red(self, t): return self._wrap("31", t)
    def cyan(self, t): return self._wrap("36", t)


S = Style(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))


def err(message: str) -> None:
    style = Style(sys.stderr.isatty() and not os.environ.get("NO_COLOR"))
    print(style.red("gpu-s: ") + message, file=sys.stderr)


def _fmt(value, unit="", digits=0, missing="—"):
    if value is None:
        return missing
    return f"{value:.{digits}f}{unit}"


def _renderer_label(facts: Facts) -> str:
    addr = facts.session.renderer_address
    gpu = next((g for g in facts.gpus if g.address == addr), None)
    return gpu.vendor_name if gpu else "unknown"


def graphics_summary(facts: Facts) -> str:
    compositor = facts.session.compositor or "the session"
    effective = facts.effective_mux_mode
    if effective == "discrete":
        base = "Discrete (ASUS MUX)"
    elif effective == "hybrid":
        base = "Hybrid / Optimus (ASUS MUX)"
    else:
        base = "Hybrid" if facts.igpu and facts.gpu and not facts.panel_on_nvidia else "Unknown wiring"
    text = f"{base} — {compositor} renders on {_renderer_label(facts)}"
    if facts.mux_change_pending and facts.mux.configured_mode:
        text += f" (switching to {facts.mux.configured_mode.capitalize()} after reboot)"
    return text


def gpu_state_label(facts: Facts) -> tuple[str, str]:
    power = facts.power
    if not facts.gpu:
        return "Not detected", "red"
    if not power:
        return "Unknown", "yellow"
    if power.powered_down:
        return f"Powered down ({power.pci_state})", "dim"
    if power.suspended:
        return f"Suspended ({power.pci_state or '?'})", "dim"
    return f"Active ({power.pci_state or 'D0'})", "green"


# -- commands ----------------------------------------------------------------


def cmd_status(args: list[str]) -> int:
    as_json = "--json" in args
    config = load_config()
    state = load_state()
    snapshot = Monitor().snapshot(state.applied_mode, config.refresh_interval, want_processes=False)
    facts, assessment = snapshot.facts, snapshot.assessment
    sample = snapshot.sample if snapshot.sample_fresh else None
    if sample and sample.name and state.gpu_name != sample.name:
        state.gpu_name = sample.name
        save_state(state)
    supply = snapshot.supply
    battery = supply.battery if supply else None
    name = (sample.name if sample else None) or state.gpu_name or (facts.gpu.pci_name if facts.gpu else None) or "No NVIDIA GPU"
    state_text, color = gpu_state_label(facts)
    power = facts.power

    if as_json:
        data = {
            "version": VERSION,
            "gpu": {
                "name": name,
                "pci_address": facts.gpu.address if facts.gpu else None,
                "driver": facts.driver_version,
                "state": state_text,
                "runtime_status": power.runtime_status if power else None,
                "pci_power_state": power.pci_state if power else None,
                "runtime_pm_control": power.control if power else None,
                "rtd3": facts.rtd3.status if facts.rtd3 else None,
                "sensors_read": sample is not None,
                "sensor_note": snapshot.sensor_note,
                "temperature_c": sample.temperature if sample else None,
                "power_w": sample.power_draw if sample else None,
                "power_limit_w": sample.power_limit if sample else None,
                "utilization_pct": sample.util_gpu if sample else None,
                "vram_used_mb": sample.mem_used if sample else None,
                "vram_total_mb": sample.mem_total if sample else None,
                "pstate": sample.pstate if sample else None,
            },
            "graphics": {
                "mux_available": facts.mux.available,
                "mux_configured": facts.mux.configured_mode,
                "mux_effective": facts.effective_mux_mode,
                "mux_change_pending": facts.mux_change_pending,
                "compositor": facts.session.compositor,
                "renderer": _renderer_label(facts),
                "internal_panel_gpu": facts.session.panel_address,
            },
            "mode": snapshot.mode.value if snapshot.mode else None,
            "supported_modes": [m.value for m in Mode if assessment.supports(m)],
            "power_down_blockers": [b.title for b in assessment.blockers],
            "profiles": {"ac": config.ac_mode, "battery": config.battery_mode, "auto_switch": config.auto_switch},
            "manual_override": state.manual_mode,
            "power_source": {"on_ac": supply.on_ac if supply else None},
            "battery": None if not battery else {
                "percent": battery.percent,
                "state": battery.state,
                "health_pct": round(battery.health_percent, 1) if battery.health_percent else None,
                "rate_w": battery.energy_rate_w,
                "charge_end_threshold": battery.sysfs_end_threshold,
            },
        }
        print(json.dumps(data, indent=2))
        return 0

    rows: list[tuple[str, str]] = []
    rows.append(("GPU", S.bold(name)))
    rows.append(("Driver", facts.driver_version or "—"))
    dot = {"green": S.green("●"), "dim": S.dim("○"), "yellow": S.yellow("●"), "red": S.red("●")}[color]
    rows.append(("State", f"{dot} {state_text}"))
    if sample:
        rows.append(("Temperature", _fmt(sample.temperature, " °C")))
        limit = f"  {S.dim('(limit ' + _fmt(sample.power_limit, ' W') + ')')}" if sample.power_limit else ""
        rows.append(("Power", _fmt(sample.power_draw, " W", 1) + limit))
        rows.append(("Utilization", _fmt(sample.util_gpu, " %")))
        rows.append(("VRAM", f"{_fmt(sample.mem_used)} / {_fmt(sample.mem_total)} MB"))
        rows.append(("Perf state", sample.pstate or "—"))
    elif snapshot.nvml_error:
        rows.append(("Sensors", S.yellow(snapshot.nvml_error)))
    else:
        rows.append(("Sensors", S.dim(snapshot.sensor_note or "not read")))
    if power:
        rtd3 = f" · RTD3 {facts.rtd3.status}" if facts.rtd3 and facts.rtd3.status else ""
        rows.append(("Runtime PM", f"{power.runtime_status} · control '{power.control}'{rtd3}"))
        rows.append(("PCI power", power.pci_state or "—"))
    rows.append(("", ""))
    rows.append(("Graphics", graphics_summary(facts)))
    mode = snapshot.mode
    mode_text = mode.label if mode else "—"
    if assessment.forced_mode is not None:
        mode_text += S.dim(" (required by the current wiring)")
    elif state.manual_mode:
        mode_text += S.dim(" (manual override)")
    rows.append(("Mode", S.bold(mode_text)))
    rows.append(("AC profile", MODE_LABELS[Mode(config.ac_mode)]))
    rows.append(("Battery profile", MODE_LABELS[Mode(config.battery_mode)]))
    rows.append(("Auto-switch", "On" if config.auto_switch else "Off"))
    rows.append(("", ""))
    if supply:
        source = "AC connected" if supply.on_ac else "On battery" if supply.on_ac is False else "Unknown"
        rows.append(("Power source", source))
    if battery:
        parts = [_fmt(battery.percent, " %"), battery.state]
        if battery.health_percent:
            parts.append(f"health {battery.health_percent:.1f} %")
        if battery.discharging and battery.energy_rate_w:
            parts.append(f"draw {battery.energy_rate_w:.1f} W")
        rows.append(("Battery", " · ".join(parts)))
        if battery.sysfs_end_threshold is not None:
            rows.append(("Charge limit", f"{battery.sysfs_end_threshold} %"))
    rows.append(("", ""))
    if assessment.can_power_down_now:
        rows.append(("Power saving", S.green("Available")))
    else:
        reason = assessment.unsupported or (assessment.blockers[0] if assessment.blockers else None)
        rows.append(("Power saving", S.yellow("Unavailable: " + (reason.title if reason else "unknown reason"))))
        if reason and reason.hint:
            rows.append(("", S.dim("→ " + reason.hint)))
    for note in assessment.notes:
        rows.append(("Note", note.title))

    print(S.bold(f"{APP_NAME} {VERSION}"))
    print()
    width = max(len(k) for k, _ in rows) + 2
    for key, value in rows:
        print(f"  {S.cyan(key.ljust(width))}{value}" if key or value else "")
    return 0


def _apply(mode: Mode, applied_by: str) -> int:
    on_ac = read_power_supply().on_ac
    result = power_manager.apply_sync(mode, applied_by, on_ac)
    if not result.ok:
        err(result.message)
        return 1
    print(result.message)
    if applied_by == "manual" and result.changed and load_config().auto_switch:
        print(S.dim("Manual override: the AC/battery profile takes over again when the power source changes."))
    return 0


def cmd_mode(mode: Mode) -> int:
    return _apply(mode, "manual")


def cmd_profile(_args) -> int:
    config = load_config()
    state = load_state()
    on_ac = read_power_supply().on_ac
    if on_ac is None:
        err("Could not determine the power source.")
        return 1
    state.manual_mode = None
    state.manual_on_ac = None
    save_state(state)
    mode = Mode(config.mode_for(on_ac))
    return _apply(mode, "ac-profile" if on_ac else "battery-profile")


def cmd_source_profile(source: str, args: list[str]) -> int:
    config = load_config()
    key = "ac_mode" if source == "ac" else "battery_mode"
    title = "AC (plugged in)" if source == "ac" else "Battery"
    if not args:
        mode = Mode(getattr(config, key))
        on_ac = read_power_supply().on_ac
        active = (on_ac is True and source == "ac") or (on_ac is False and source == "battery")
        print(f"{title} profile: {S.bold(mode.label)}" + (S.dim("  (active power source)") if active else ""))
        assessment = assess(gather_facts())
        if not assessment.supports(mode):
            print(S.yellow(f"Note: {assessment.unsupported_reason(mode)}"))
        return 0
    mode = MODE_ALIASES.get(args[0].lower())
    if mode is None:
        err(f"Unknown mode '{args[0]}'. Use one of: {', '.join(MODES)}")
        return 2
    setattr(config, key, mode.value)
    save_config(config)
    print(f"{title} profile set to {S.bold(mode.label)}.")

    state = load_state()
    on_ac = read_power_supply().on_ac
    target, applied_by = target_mode(config.ac_mode, config.battery_mode, on_ac, state.manual_mode, state.manual_on_ac, config.auto_switch)
    if target is mode and applied_by != "manual" and ((source == "ac") == bool(on_ac)):
        return _apply(mode, applied_by)
    assessment = assess(gather_facts())
    if not assessment.supports(mode):
        print(S.yellow(f"Saved, but not applicable right now: {assessment.unsupported_reason(mode)}"))
    return 0


BATTERY_HELP = f"""\
gpu-s battery — battery information and charge limits

  gpu-s battery                 Battery information (same as 'status')
  gpu-s battery status          Level, health, thresholds, AC and charging state
  gpu-s battery limit N         Stop charging at N % ({charge.MIN_END}–{charge.MAX_END}); 100 removes the limit
  gpu-s battery limit off       Remove the limit (charge to 100 %)
  gpu-s battery start N         Resume charging below N % (only if the battery supports it)
  gpu-s battery preset NAME     care (75→80), balanced (50→80), maximum (20→100)
  gpu-s battery profile [MODE]  Show/set the GPU mode used on battery
  gpu-s battery help            This help

Limits are applied by UPower to the kernel threshold and persist across reboot.
"""


def _charge_rows(info) -> list[tuple[str, str]]:
    rows = [("Battery", f"{info.battery}" + (f"  {S.dim((info.vendor or '') + ' ' + (info.model or ''))}" if info.model else ""))]
    status = {"Full": "Fully charged", "Not charging": "Not charging"}.get(info.status or "", info.status or "—")
    if info.status == "Not charging" and info.limit_active:
        status += S.dim(f"  (held at the {info.kernel_end} % limit)")
    rows += [
        ("Status", status),
        ("Level", _fmt(info.percent, " %")),
        ("Health", f"{info.health_percent:.1f} %" if info.health_percent else "—"),
        ("Energy", f"{_fmt(info.energy_wh, ' Wh', 1)} / {_fmt(info.energy_full_wh, ' Wh', 1)} (design {_fmt(info.energy_design_wh, ' Wh', 1)})"),
        ("Voltage", _fmt(info.voltage_v, " V", 2)),
        ("Cycles", str(info.cycles) if info.cycles else "not reported"),
        ("AC", "Connected" if info.ac_online else "Disconnected" if info.ac_online is False else "—"),
        ("Charging", "Yes" if info.charging else "No"),
    ]
    if info.start_supported:
        rows.append(("Start Threshold", _fmt(info.kernel_start, " %")))
    else:
        rows.append(("Start Threshold", S.dim("not supported by this battery (firmware resumes on its own)")))
    if info.kernel_end is None:
        rows.append(("End Threshold", "not supported"))
    else:
        rows.append(("End Threshold", f"{info.kernel_end} %" + ("" if info.limit_active else S.dim("  (no limit)"))))
    if info.upower_supported:
        state = "enabled" if info.upower_enabled else "disabled"
        configured = f"{'_' if not info.start_supported else info.upower_start},{info.upower_end}"
        rows.append(("Managed by", f"UPower (limit {state}, configured {configured})" + (S.dim("  · GPU-S override") if info.override else "")))
    return rows


def cmd_battery(args: list[str]) -> int:
    sub = args[0].lower() if args else "status"
    rest = args[1:]
    if sub in MODE_ALIASES and sub not in ("on", "off"):
        return cmd_source_profile("battery", args)  # gpu-s battery <gpu-mode>
    if sub in ("help", "-h", "--help"):
        print(BATTERY_HELP, end="")
        return 0
    if sub == "profile":
        return cmd_source_profile("battery", rest)
    info = charge.read_charge()
    if sub in ("status", "info"):
        if info.battery is None:
            print("No battery detected.")
            return 0
        rows = _charge_rows(info)
        width = max(len(k) for k, _ in rows) + 2
        for key, value in rows:
            print(f"{S.cyan(key.ljust(width))}{value}")
        for unit in info.legacy_units_enabled:
            print(S.yellow(f"Warning: {unit} is enabled and also writes the threshold at boot."))
        return 0
    if sub == "limit":
        if len(rest) != 1:
            err("usage: gpu-s battery limit N|off")
            return 2
        value = rest[0].rstrip("%").lower()
        if value in ("off", "none", "100"):
            end = 100
        elif value.isdigit():
            end = int(value)
        else:
            err(f"invalid limit '{rest[0]}': use a number {charge.MIN_END}-{charge.MAX_END} or 'off'")
            return 2
        return _charge_apply(end, None)
    if sub == "start":
        if len(rest) != 1 or not rest[0].rstrip("%").isdigit():
            err("usage: gpu-s battery start N")
            return 2
        end = info.kernel_end if info.limit_active else 80
        return _charge_apply(end, int(rest[0].rstrip("%")))
    if sub == "preset":
        names = {p.key: p for p in charge.PRESETS}
        names.update({"maximum-runtime": names["maximum"], "battery-care": names["care"], "max": names["maximum"]})
        preset = names.get(rest[0].lower()) if len(rest) == 1 else None
        if preset is None:
            err("usage: gpu-s battery preset care|balanced|maximum")
            return 2
        ok, why = charge.preset_status(info, preset)
        if not ok:
            err(f"{preset.label} is not available on this battery: {why}")
            return 1
        return _charge_apply(preset.end, preset.start if info.start_supported else None)
    err(f"unknown battery command '{sub}'. Run 'gpu-s battery help'.")
    return 2


def _charge_apply(end: int, start: int | None) -> int:
    result = charge.apply_sync(end, start)
    if not result.ok:
        err(result.message)
        return 1
    print(result.message + ("" if result.changed else S.dim("  (already set)")))
    print(S.dim("Applied by UPower to the kernel threshold; persists across reboot."))
    return 0


def cmd_apps(_args) -> int:
    snapshot = Monitor().snapshot(None, 2.0, want_supply=False)
    if not snapshot.processes:
        state, _ = gpu_state_label(snapshot.facts)
        print(f"No applications are using the NVIDIA GPU. (GPU: {state})")
        return 0
    if not snapshot.sample_fresh:
        print(S.dim(snapshot.sensor_note or "Live usage not read."))
    print(S.bold(f"{'PID':>7}  {'VRAM':>8}  {'GPU':>5}  {'TYPE':<20} NAME"))
    for p in snapshot.processes:
        vram = _fmt(p.vram_mb, " MB")
        sm = _fmt(p.sm, " %")
        name = p.name + (S.dim(" (gpu-s)") if p.is_self else "")
        print(f"{p.pid:>7}  {vram:>8}  {sm:>5}  {p.kind:<20} {name}")
    return 0


def offload_env(facts: Facts) -> dict[str, str]:
    """PRIME render-offload environment (what Arch's prime-run sets, plus VA-API)."""
    if facts.session_on_nvidia:
        return {}
    env = {
        "__NV_PRIME_RENDER_OFFLOAD": "1",
        "__NV_PRIME_RENDER_OFFLOAD_PROVIDER": "NVIDIA-G0",
        "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
        "__VK_LAYER_NV_optimus": "NVIDIA_only",
        "LIBVA_DRIVER_NAME": "nvidia",
        "NVD_BACKEND": "direct",
    }
    nvidia_egl = Path("/usr/share/glvnd/egl_vendor.d/10_nvidia.json")
    if nvidia_egl.is_file():
        env["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(nvidia_egl)
    return env


def cmd_run(args: list[str]) -> int:
    if not args:
        err("usage: gpu-s run COMMAND [ARGS...]")
        return 2
    facts = gather_facts()
    if not facts.gpu:
        err("No NVIDIA GPU was detected.")
        return 1
    env = os.environ.copy()
    extra = offload_env(facts)
    if extra:
        # An inherited "Intel only" restriction would hide the NVIDIA GPU.
        for var in ("VK_DRIVER_FILES", "VK_ICD_FILENAMES", "__EGL_VENDOR_LIBRARY_FILENAMES", "DRI_PRIME"):
            env.pop(var, None)
    env.update(extra)
    if not extra and sys.stderr.isatty():
        print(S.dim("The session already renders on NVIDIA; running the command as-is."), file=sys.stderr)
    try:
        os.execvpe(args[0], args, env)
    except FileNotFoundError:
        err(f"command not found: {args[0]}")
        return 127
    except OSError as exc:
        err(f"cannot run {args[0]}: {exc.strerror}")
        return 126


def cmd_mux(args: list[str]) -> int:
    facts = gather_facts()
    mux = facts.mux
    if not mux.available:
        err("This system has no ASUS GPU MUX control.")
        return 1
    if not args:
        print(f"Configured MUX mode: {S.bold((mux.configured_mode or 'unknown').capitalize())}  {S.dim('(' + (mux.source or '') + ')')}")
        print(f"Active this boot:    {(facts.effective_mux_mode or 'unknown').capitalize()}")
        if facts.mux_change_pending:
            print(S.yellow("A MUX change is pending and takes effect after the next reboot."))
        return 0
    target = args[0].lower()
    if target not in ("hybrid", "discrete"):
        err("usage: gpu-s mux [hybrid|discrete]")
        return 2
    check = power_manager.mux_check(target)
    if not check.ok:
        err(check.message)
        return 1
    if check.output.startswith("unchanged"):
        print(f"The MUX is already configured for {target.capitalize()}.")
        return 0
    result = power_manager.mux_switch_sync(target)
    if not result.ok:
        err(result.message)
        return 1
    print(f"MUX set to {S.bold(target.capitalize())}. It takes effect after you reboot; GPU-S will not reboot for you.")
    if target == "hybrid" and not session_tuning.is_enabled():
        print(S.dim("Tip: 'gpu-s tuning on' stops Omarchy's defaults from waking the NVIDIA GPU for video decoding in Hybrid mode."))
    return 0


def cmd_tuning(args: list[str]) -> int:
    if not args:
        print(f"Hybrid session tuning: {S.bold('On' if session_tuning.is_enabled() else 'Off')}")
        print(S.dim(f"File: {session_tuning.snippet_path()}"))
        return 0
    if args[0] not in ("on", "off"):
        err("usage: gpu-s tuning [on|off]")
        return 2
    ok, message = session_tuning.enable() if args[0] == "on" else session_tuning.disable()
    (print if ok else err)(message)
    return 0 if ok else 1


def cmd_autostart(args: list[str]) -> int:
    config = load_config()
    if not args:
        print(f"Start at login: {S.bold('On' if autostart.is_enabled() else 'Off')}")
        return 0
    if args[0] not in ("on", "off"):
        err("usage: gpu-s autostart [on|off]")
        return 2
    config.autostart = args[0] == "on"
    save_config(config)
    autostart.set_enabled(config.autostart)
    print(f"Start at login {'enabled' if config.autostart else 'disabled'}.")
    return 0


def cmd_reset(args: list[str]) -> int:
    """Undo everything GPU-S changed (used by the uninstaller)."""
    status = 0
    cmd_quit([])
    ok, message = session_tuning.disable()
    print(("✓ " if ok else "✗ ") + message)
    if autostart.is_enabled():
        autostart.set_enabled(False)
        print("✓ Start at login removed.")
    result = power_manager.restore_default_sync()
    print(("✓ " if result.ok else "✗ ") + result.message)
    status |= 0 if result.ok else 1
    battery = charge.remove_override_sync()
    print(("✓ " if battery.ok else "✗ ") + battery.message)
    status |= 0 if battery.ok else 1

    state = load_state()
    facts = gather_facts()
    baseline = state.baseline_mux
    if facts.mux.available and baseline and facts.mux.configured_mode != baseline:
        if "--keep-mux" in args:
            print(f"! The MUX is configured for {facts.mux.configured_mode}; it was {baseline} before GPU-S. Kept as requested.")
        else:
            result = power_manager.mux_switch_sync(baseline)
            if result.ok:
                print(f"✓ MUX restored to {baseline.capitalize()} (takes effect after reboot).")
            else:
                print(f"✗ Could not restore the MUX to {baseline}: {result.message}")
                status = 1
    else:
        print("✓ GPU MUX unchanged by GPU-S.")
    state.manual_mode = state.manual_on_ac = state.applied_mode = state.applied_by = None
    save_state(state)
    return status


def cmd_quit(_args) -> int:
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            APP_ID, "/" + APP_ID.replace(".", "/"), "org.gtk.Actions", "Activate",
            GLib.Variant("(sava{sv})", ("quit", [], {})), None, Gio.DBusCallFlags.NONE, 3000, None,
        )
    except Exception:  # noqa: BLE001
        print("GPU-S is not running.")
        return 0
    print("GPU-S stopped.")
    return 0


# -- GUI launch ----------------------------------------------------------------


def prefer_igpu_for_self(facts: Facts) -> None:
    """In Hybrid mode, keep GPU-S's own rendering off the NVIDIA GPU so the
    monitor never becomes the reason the dGPU stays awake."""
    if facts.session_on_nvidia or not facts.igpu or not facts.gpu:
        return
    mesa_egl = Path("/usr/share/glvnd/egl_vendor.d/50_mesa.json")
    if mesa_egl.is_file():
        os.environ.setdefault("__EGL_VENDOR_LIBRARY_FILENAMES", str(mesa_egl))
    os.environ.setdefault("__GLX_VENDOR_LIBRARY_NAME", "mesa")
    for icd in ("/usr/share/vulkan/icd.d/intel_icd.json", "/usr/share/vulkan/icd.d/intel_icd.x86_64.json"):
        if Path(icd).is_file():
            os.environ.setdefault("VK_DRIVER_FILES", icd)
            break


def launch_gui(argv: list[str]) -> int:
    background = "--background" in argv
    foreground = "--foreground" in argv or background
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        err("No graphical session found. Use 'gpu-s status' for a terminal summary.")
        return 1
    if not foreground:
        log = paths.state_dir() / "gpu-s.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        root = str(Path(__file__).resolve().parent.parent)
        env = os.environ.copy()
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        with open(log, "ab") as out:
            subprocess.Popen(
                [sys.executable, "-m", "gpu_s", "--foreground"],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True, env=env, cwd="/",
            )
        return 0
    prefer_igpu_for_self(gather_facts())
    from .ui.app import run

    return run(background=background)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("--background", "--foreground"):
        return launch_gui(argv)
    command, args = argv[0], argv[1:]
    if command in ("help", "-h", "--help"):
        print(HELP, end="")
        return 0
    if command in ("--version", "-V", "version"):
        print(f"{APP_NAME} {VERSION}")
        return 0
    if command in ("gui", "open", "show"):
        return launch_gui(args)
    handlers = {
        "status": cmd_status,
        "on": lambda a: cmd_mode(Mode.ALWAYS_ON),
        "off": lambda a: cmd_mode(Mode.POWER_SAVING),
        "auto": lambda a: cmd_mode(Mode.AUTO),
        "profile": cmd_profile,
        "apply": cmd_profile,
        "ac": lambda a: cmd_source_profile("ac", a),
        "battery": cmd_battery,
        "apps": cmd_apps,
        "run": cmd_run,
        "mux": cmd_mux,
        "tuning": cmd_tuning,
        "autostart": cmd_autostart,
        "quit": cmd_quit,
        "reset": cmd_reset,
    }
    handler = handlers.get(command)
    if handler is None:
        err(f"unknown command '{command}'. Run 'gpu-s help'.")
        return 2
    if command != "reset":
        power_manager.record_baseline()
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
