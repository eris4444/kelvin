// Kelvin idle service for the Omarchy shell.
//
// Omarchy's own idle service runs the screensaver and lock timers. This adds
// the two stages Omarchy does not have, each with its own timeout, read from
// ~/.config/kelvin/idle.json (hot-reloaded; 0 = never):
//
//   screen_off     turn the displays off (Hyprland DPMS) after N idle seconds
//   sleep_ac       suspend after N idle seconds while on AC power
//   sleep_battery  suspend after N idle seconds while on battery
//
// Like Omarchy's idle service it uses Quickshell's IdleMonitor with
// respectInhibitors, so video playback and other idle inhibitors hold it off,
// and it does nothing while Omarchy's "stay awake" switch is on.
import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import Quickshell.Services.UPower

Item {
  id: root

  // Injected by the shell's service loader (unused).
  property var shell: null

  readonly property string home: Quickshell.env("HOME")
  readonly property string configPath: home + "/.config/kelvin/idle.json"
  readonly property string stayAwakeCheck: "[[ -f \"$HOME/.local/state/omarchy/indicators/stay-awake\" ]]"

  property int screenOffSeconds: 0
  property int sleepAcSeconds: 0
  property int sleepBatterySeconds: 0
  readonly property bool onBattery: UPower.onBattery
  readonly property int sleepSeconds: onBattery ? sleepBatterySeconds : sleepAcSeconds
  property bool screenOffByUs: false
  property string lastEvent: "starting"

  function seconds(value) {
    var n = Math.floor(Number(value))
    return isFinite(n) && n > 0 ? n : 0
  }

  function parse(text) {
    var cfg = {}
    try {
      cfg = JSON.parse(text || "{}") || {}
    } catch (error) {
      cfg = {}
    }
    root.screenOffSeconds = seconds(cfg.screen_off)
    root.sleepAcSeconds = seconds(cfg.sleep_ac)
    root.sleepBatterySeconds = seconds(cfg.sleep_battery)
    log("config", "screen_off=" + root.screenOffSeconds + " sleep_ac=" + root.sleepAcSeconds + " sleep_battery=" + root.sleepBatterySeconds)
  }

  function log(event, details) {
    root.lastEvent = event + (details ? ": " + details : "")
    console.log("kelvin idle " + root.lastEvent)
  }

  function run(process, command) {
    if (process.running) return
    process.command = ["bash", "-lc", command]
    process.running = true
  }

  FileView {
    id: configFile
    path: root.configPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.parse(text())
    onLoadFailed: root.parse("{}")
  }

  IdleMonitor {
    id: screenOffMonitor
    enabled: root.screenOffSeconds > 0
    respectInhibitors: true
    timeout: Math.max(1, root.screenOffSeconds)
    onIsIdleChanged: {
      if (isIdle) {
        root.log("screen-off", root.screenOffSeconds + "s idle")
        root.screenOffByUs = true
        root.run(screenOffProcess, root.stayAwakeCheck + " || omarchy-brightness-display off")
      } else if (root.screenOffByUs) {
        root.screenOffByUs = false
        root.log("screen-on", "activity")
        root.run(screenOnProcess, "omarchy-system-wake")
      }
    }
  }

  IdleMonitor {
    id: sleepMonitor
    enabled: root.sleepSeconds > 0
    respectInhibitors: true
    timeout: Math.max(1, root.sleepSeconds)
    onIsIdleChanged: {
      if (!isIdle) return
      root.log("sleep", root.sleepSeconds + "s idle on " + (root.onBattery ? "battery" : "AC"))
      // Omarchy's sleep hook locks the session before the machine suspends.
      root.run(sleepProcess, root.stayAwakeCheck + " || systemctl suspend")
    }
  }

  Process { id: screenOffProcess }
  Process { id: screenOnProcess }
  Process { id: sleepProcess }

  IpcHandler {
    target: "kelvin-idle"

    function status(): string {
      return JSON.stringify({
        screenOff: root.screenOffSeconds,
        sleepAc: root.sleepAcSeconds,
        sleepBattery: root.sleepBatterySeconds,
        onBattery: root.onBattery,
        activeSleep: root.sleepSeconds,
        screenOffIdle: screenOffMonitor.isIdle,
        sleepIdle: sleepMonitor.isIdle,
        lastEvent: root.lastEvent
      })
    }
  }
}
