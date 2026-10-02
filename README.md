# Kelvin

**Power & thermal control centre for Omarchy laptops**: NVIDIA GPU power, battery
charge limit, fans and CPU temperatures, and sleep/lock timers in one native
GTK4/libadwaita app. It also has a CLI and a tray icon, and it follows your Omarchy theme live.
*(Formerly **GPU-S**. The `gpu-s` command still works and your settings carry over.)*

Built and tested on an **ASUS TUF Gaming F15 FX507ZC4** (i7-12700H, RTX 3050 Laptop
GPU, NVIDIA open driver 610.57.04, kernel 7.2, Hyprland 0.56, Omarchy 4). The
hardware detection adapts to other NVIDIA Optimus/MUX laptops, but this is the
only model that has been tested.

Everything uses real kernel, firmware or desktop interfaces and is verified
after each change. Nothing is simulated.

| Tab | What it controls | Through |
|---|---|---|
| **GPU** | NVIDIA power-down (RTD3), ASUS MUX, AC/battery GPU profiles, apps on the GPU | PCI runtime PM, `asus-armoury`, `nvidia-smi` |
| **Battery** | Charge limit, battery health and live state | UPower + kernel `charge_control_end_threshold` |
| **Cooling** | CPU package/per-core temperature, per-thread load and frequency; fan RPM; thermal profile; CPU/GPU fan curves | `coretemp`, `/proc/stat`, power-profiles-daemon, `asus_custom_fan_curve` |
| **Sleep** | Screensaver, lock, screen off, sleep on AC / on battery, each with its own timer; stay awake | Omarchy shell idle service + Kelvin's shell plugin |

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/eris4444/kelvin/main/install-remote.sh | bash
```

This clones the repo to `~/.local/share/kelvin`, runs the test suite, builds an Arch
package with `makepkg`, and installs it with `pacman -U`. You'll be asked for your
password at that one step. If GPU-S is installed, the same step replaces it.
Re-run the command any time to update.

## Cooling: CPU and fans

* **CPU**: package temperature with its throttle point, plus temperature per physical
  core (`coretemp`), load per thread (`/proc/stat`) and frequency. Performance and
  efficient cores are shown separately.
* **Fans**: live RPM of the CPU and GPU fans (asus-wmi). The GPU's fan is driven by the
  ASUS EC; NVIDIA reports no fan control on this laptop.
* **Thermal profile**: Silent / Balanced / Performance. This is the ASUS platform
  profile (the same as Fn+F5), and it also sets power limits. It is switched through
  power-profiles-daemon, which owns it on Omarchy.
* **Fan curves**: per fan, **Auto** (the firmware curve), **Quiet**, **Cool**, **Full
  speed**, or a custom 8-point curve from the CLI. A root helper writes the curves and
  enforces these minimums whatever is requested: at least 25 % at 70 °C, 50 % at 80 °C,
  and a last point of at most 95 °C at 50 % or more. If the kernel rejects a curve, the
  firmware curve is restored. The kernel drops custom curves at boot and on every
  thermal-profile change, so Kelvin's agent re-applies yours (it starts at login).

## Sleep: idle timers

| Stage | Run by | Stored in |
|---|---|---|
| Screensaver | Omarchy shell | `~/.config/omarchy/shell.json` → `idle.screensaver` |
| Lock screen | Omarchy shell | `~/.config/omarchy/shell.json` → `idle.lock` |
| Turn off screen (DPMS) | Kelvin shell plugin `kelvin.idle` | `~/.config/kelvin/idle.json` |
| Sleep on AC / on battery | Kelvin shell plugin `kelvin.idle` | `~/.config/kelvin/idle.json` |
| Stay awake (pauses everything) | Omarchy shell | `omarchy-shell idle enable/disable` |

Omarchy has no "never" setting for its own timers, so Kelvin writes a ~23-day
timeout for a disabled stage. Screen off and sleep-on-idle don't exist in Omarchy.
Kelvin adds them as an Omarchy shell service plugin (symlinked into
`~/.config/omarchy/plugins/kelvin.idle` and enabled with `omarchy plugin enable`).
It uses the same `IdleMonitor` as Omarchy, so video playback and other idle
inhibitors hold it off, and it honours stay-awake. Settings are plain files, so they
keep working without Kelvin running. The lid action comes from systemd-logind and is
shown but not changed.

## How it controls the GPU

Kelvin uses only real kernel and firmware interfaces, never fake states:

| Control | Interface | Privilege |
|---|---|---|
| Keep GPU on / allow power-down | PCI runtime PM: `/sys/bus/pci/devices/0000:01:00.0/power/control` (`on` / `auto`) with the NVIDIA driver's fine-grained RTD3 | polkit, no password for the active local user |
| Graphics mode (which GPU drives the panel) | ASUS firmware MUX: `/sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode` (0 Discrete, 1 Hybrid). Applies after reboot. | polkit, admin password |
| Status | sysfs, `/proc/driver/nvidia`, DRM connectors, Hyprland log, UPower, `nvidia-smi` | none |

supergfxctl, envycontrol, switcheroo-control and TLP are not installed, and none is needed for the above.
power-profiles-daemon (CPU profiles) is left untouched.

**Kelvin never wakes a sleeping GPU to read it.** Any NVML query resumes a
runtime-suspended GPU. Kelvin only reads `nvidia-smi` when the kernel says the GPU is already
awake. It backs off while the GPU is idle so the driver can power it down, and it
never reads NVML while the window is hidden.

## Graphics modes (ASUS MUX)

* **Discrete**: the laptop panel is wired to NVIDIA and Hyprland renders on it.
  The NVIDIA GPU *cannot* power down. Only **Always On** is possible. Kelvin says
  so and explains why, rather than pretending otherwise.
* **Hybrid (Optimus)**: the panel is wired to the Intel iGPU and Hyprland renders on
  Intel. The NVIDIA GPU sleeps in D3cold when idle and wakes on demand. Run individual
  apps on it with `kelvin run <app>`. The HDMI port stays wired to NVIDIA.

Switching modes takes a reboot. Kelvin never reboots for you.

### Measured on this laptop (Hybrid mode)

* Hyprland picks the Intel iGPU as its primary GPU (`card2 (i915) becomes primary drm`). NVIDIA is
  only a secondary GPU for the HDMI port.
* After **Allow GPU To Power Down** / Auto, the kernel reports `suspended` + `D3cold` and video memory
  `Off` about 10–15 s after the GPU was last used.
* With the Kelvin window open the GPU still powers down. Kelvin holds no NVIDIA device handles
  (it renders itself on Intel) and never reads sensors from a sleeping GPU.
* `kelvin run <app>` wakes the GPU for that app, and it returns to D3cold ~13 s after the app exits.
* **Limitation:** starting *any* app that initialises Vulkan (including GTK4 apps) wakes the dGPU for
  a few seconds, because the Vulkan loader probes every GPU driver. RTD3 puts it back to sleep on its own.
  Kelvin does not restrict Vulkan session-wide: that would hide NVIDIA from games and could affect
  the NVIDIA-wired HDMI output.

## GPU modes

| Mode | Runtime PM | Behaviour |
|---|---|---|
| Always On | `on` | GPU pinned in D0, instantly available |
| Auto | `auto` | GPU powers down when idle and wakes when an app needs it |
| Power Saving | `auto` | Intel preferred. Same kernel policy as Auto, but Kelvin never wakes the GPU for sensor reads and names apps that keep it awake |

Auto and Power Saving are only offered when the GPU can actually power down (Hybrid mode).


## Battery charge limit

On this laptop the kernel exposes **only an end threshold** for the battery
(`/sys/class/power_supply/BAT1/charge_control_end_threshold`, from the ASUS WMI driver).
There is no start threshold: the firmware resumes charging on its own once the level drops below
the limit. UPower 1.91 already manages that threshold (`ChargeThresholdSettingsSupported = 2`, end
only), so Kelvin works through UPower instead of writing sysfs:

* **Enable/disable** goes through UPower's `EnableChargeThreshold`, which the active user may
  call without a password. UPower persists the setting in `/var/lib/upower` and re-applies it at
  boot and resume.
* **Value** goes in UPower's documented local override, `/etc/udev/hwdb.d/61-kelvin-battery.hwdb`
  (`CHARGE_LIMIT=_,N`). It is written by a polkit-gated root helper, which then restarts UPower so
  the new value is read. Kelvin refuses to write it if another hwdb file already sets
  `CHARGE_LIMIT`.
* Every change is verified against the kernel attribute. Limits from 50 to 100 % are accepted.
  The start-threshold controls and the *Balanced* preset (50→80) are shown as unsupported on this
  battery. *Battery Care* stops at 80 %, and *Maximum Runtime* removes the limit.
* `/etc/systemd/system/battery-charge-limit.service` (an older manual approach, disabled) is left
  untouched. Kelvin warns if it is ever enabled, because it would fight UPower.

```
kelvin battery [status]        kelvin battery limit 80|off
kelvin battery preset care|balanced|maximum
kelvin battery start N         (only on batteries with a start threshold)
kelvin battery profile [MODE]  GPU mode used on battery
```

## CLI

```
kelvin                       open the window          kelvin status [--json]
kelvin on | off | auto       GPU: Always On | Power Saving | Auto
kelvin profile | ac [MODE]   GPU AC/battery profiles  kelvin apps | run CMD
kelvin mux [hybrid|discrete] ASUS MUX (after reboot)  kelvin tuning [on|off]
kelvin battery [status|limit N|off|preset care|maximum|profile [MODE]]
kelvin cpu                   per-core temperature, load, frequency
kelvin fan [status|profile silent|balanced|performance|cpu|gpu auto|quiet|cool|max|curve T:S,...]
kelvin sleep [status|screensaver|lock|screen-off|suspend-ac|suspend-battery TIME|never|stay-awake on|off]
kelvin autostart [on|off]    kelvin reset             kelvin quit | help | --version
```

`kelvin battery help`, `kelvin fan help` and `kelvin sleep help` list every option.
TIME accepts `90`, `5m`, `1h30m` or `"10 min"`.

## Hybrid session tuning (optional)

Omarchy exports `LIBVA_DRIVER_NAME=nvidia` and `__GLX_VENDOR_LIBRARY_NAME=nvidia`
whenever an NVIDIA GPU exists. In Hybrid mode that wakes the dGPU for every video
and every X11 OpenGL app. When enabled, the tuning writes `~/.config/hypr/kelvin.lua` and adds
one `pcall(require, "hypr.kelvin")` line to `hyprland.lua` (a backup is kept). If the panel is on
Intel at login, it routes those two to Intel. In Discrete mode it does nothing.

## Install / uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/eris4444/kelvin/main/install-remote.sh | bash   # install / update
./install.sh                 # same, from an existing clone
./uninstall.sh               # kelvin reset + pacman -R kelvin
./uninstall.sh --purge       # ...and delete ~/.config/kelvin and ~/.local/state/kelvin
./uninstall.sh --keep-mux    # leave the ASUS MUX as it is
```

`kelvin reset` (run by the uninstaller) does the following:
* puts NVIDIA runtime PM back to `auto`
* hands both fans back to the firmware curve
* removes the battery hwdb override, the idle plugin, the Hyprland tuning and the autostart entry
* switches the MUX back to what it was when Kelvin first ran (takes effect after reboot)

Your Omarchy screensaver and lock timers stay as you set them. The UPower charge limit
stays on; turn it off with `kelvin battery limit off` before uninstalling if you want
100 % charging.

Installed files, all owned by the `kelvin` package:
* `/usr/bin/kelvin` (and the `gpu-s` alias)
* `/usr/lib/kelvin/` (the app and its four root helpers: runtime PM, MUX, battery, fans)
* `/usr/share/polkit-1/actions/dev.erisrtg.kelvin.policy`
* the desktop entry, icons, the Omarchy plugin and the docs

## If Hybrid mode misbehaves

The MUX is a firmware setting, so it survives reinstalls. To go back to Discrete from any terminal or TTY
(Ctrl+Alt+F3):

```
kelvin mux discrete && reboot
# without Kelvin:
echo 0 | sudo tee /sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode/current_value && reboot
```

## Layout

```
bin/kelvin              launcher
kelvin/hardware.py      GPU detection (PCI, DRM wiring, MUX, RTD3, session renderer)
kelvin/monitor.py       snapshot + wake-safe NVML sampling   kelvin/nvml.py, procs.py
kelvin/policy.py        GPU modes (pure logic)                kelvin/power_manager.py
kelvin/charge.py        battery charge limit (UPower)         kelvin/battery.py
kelvin/cpu.py           CPU temperatures, per-core load/freq
kelvin/fans.py          fan RPM, thermal profile, fan curves
kelvin/idle.py          screensaver/lock/screen-off/sleep, stay awake
kelvin/agent.py         AC/battery profiles, charge notifications, fan-curve re-apply
kelvin/tray.py          StatusNotifierItem + dbusmenu         kelvin/cli.py
kelvin/ui/              GTK4/libadwaita window, pages, Omarchy theme
helpers/                root helpers (whitelisted operations, strict validation)
data/omarchy-plugin/    kelvin.idle Omarchy shell service plugin
tests/                  unittest suite + helper argument tests (make test)
```
