# GPU-S

Monitor and control the NVIDIA GPU — and the battery charge limit — on
hybrid-graphics laptops running [Omarchy](https://omarchy.org). Built and
tested on an **ASUS TUF Gaming F15 FX507ZC4** (RTX 3050 Laptop GPU + Intel
Iris Xe, NVIDIA open driver 610.57.04, kernel 7.2, Hyprland 0.56); the
detection code is hardware-adaptive and degrades gracefully on other
NVIDIA Optimus/MUX laptops, but only this model has been tested.

Native GTK4/libadwaita app that follows the current Omarchy theme live, plus a CLI and a tray icon.
Real kernel/firmware controls only — see [How it controls the GPU](#how-it-controls-the-gpu); nothing here is simulated.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/eris4444/gpu-s/main/install-remote.sh | bash
```

This clones the repo to `~/.local/share/gpu-s`, runs its test suite, builds an
Arch package with `makepkg`, and installs it with `pacman -U` (you'll be
asked for your password at that one step; nothing else needs it). Re-run the
same command any time to update. See [Install / uninstall](#install--uninstall)
for what gets installed and how to remove it, and [Layout](#layout) for the
source if you'd rather read it first.

## How it controls the GPU

GPU-S uses only real kernel and firmware interfaces, never fake states:

| Control | Interface | Privilege |
|---|---|---|
| Keep GPU on / allow power-down | PCI runtime PM: `/sys/bus/pci/devices/0000:01:00.0/power/control` (`on` / `auto`) with the NVIDIA driver's fine-grained RTD3 | polkit, no password for the active local user |
| Graphics mode (which GPU drives the panel) | ASUS firmware MUX: `/sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode` (0 Discrete, 1 Hybrid). Applies after reboot. | polkit, admin password |
| Status | sysfs, `/proc/driver/nvidia`, DRM connectors, Hyprland log, UPower, `nvidia-smi` | none |

supergfxctl, envycontrol, switcheroo-control and TLP are not installed, and none is needed for the above.
power-profiles-daemon (CPU profiles) is left untouched.

**GPU-S never wakes a sleeping GPU to read it.** Any NVML query resumes a
runtime-suspended GPU. GPU-S only reads `nvidia-smi` when the kernel says the GPU is already
awake. It backs off while the GPU is idle so the driver can power it down, and it
never reads NVML while the window is hidden.

## Graphics modes (ASUS MUX)

* **Discrete**: the laptop panel is wired to NVIDIA and Hyprland renders on it.
  The NVIDIA GPU *cannot* power down. Only **Always On** is possible. GPU-S says
  so and explains why, rather than pretending otherwise.
* **Hybrid (Optimus)**: the panel is wired to the Intel iGPU and Hyprland renders on
  Intel. The NVIDIA GPU sleeps in D3cold when idle and wakes on demand. Run individual
  apps on it with `gpu-s run <app>`. The HDMI port stays wired to NVIDIA.

Switching modes takes a reboot. GPU-S never reboots for you.

### Measured on this laptop (Hybrid mode)

* Hyprland picks the Intel iGPU as its primary GPU (`card2 (i915) becomes primary drm`). NVIDIA is
  only a secondary GPU for the HDMI port.
* After **Allow GPU To Power Down** / Auto, the kernel reports `suspended` + `D3cold` and video memory
  `Off` about 10–15 s after the GPU was last used.
* With the GPU-S window open the GPU still powers down. GPU-S holds no NVIDIA device handles
  (it renders itself on Intel) and never reads sensors from a sleeping GPU.
* `gpu-s run <app>` wakes the GPU for that app, and it returns to D3cold ~13 s after the app exits.
* **Limitation:** starting *any* app that initialises Vulkan (including GTK4 apps) wakes the dGPU for
  a few seconds, because the Vulkan loader probes every GPU driver. RTD3 puts it back to sleep on its own.
  GPU-S does not restrict Vulkan session-wide: that would hide NVIDIA from games and could affect
  the NVIDIA-wired HDMI output.

## GPU modes

| Mode | Runtime PM | Behaviour |
|---|---|---|
| Always On | `on` | GPU pinned in D0, instantly available |
| Auto | `auto` | GPU powers down when idle and wakes when an app needs it |
| Power Saving | `auto` | Intel preferred. Same kernel policy as Auto, but GPU-S never wakes the GPU for sensor reads and names apps that keep it awake |

Auto and Power Saving are only offered when the GPU can actually power down (Hybrid mode).

## AC / battery profiles

Pick a mode for **Plugged In** and **On Battery**. The GPU-S agent watches UPower
(`OnBattery`) and logind (resume) and applies the matching profile. It sends a
notification only when something actually changed. A manual choice (`gpu-s on/off/auto`,
the mode buttons, the tray) holds until the power source next changes. Profiles are stored
in `~/.config/gpu-s/config.json` and applied again at login through XDG autostart
(`~/.config/autostart/dev.erisrtg.GpuS.desktop`, started by uwsm).

## Battery charge limit

On this laptop the kernel exposes **only an end threshold** for the battery
(`/sys/class/power_supply/BAT1/charge_control_end_threshold`, from the ASUS WMI driver).
There is no start threshold: the firmware resumes charging on its own once the level drops below
the limit. UPower 1.91 already manages that threshold (`ChargeThresholdSettingsSupported = 2`, end
only), so GPU-S works through UPower instead of writing sysfs:

* **Enable/disable** goes through UPower's `EnableChargeThreshold`, which the active user may
  call without a password. UPower persists the setting in `/var/lib/upower` and re-applies it at
  boot and resume.
* **Value** goes in UPower's documented local override, `/etc/udev/hwdb.d/61-gpu-s-battery.hwdb`
  (`CHARGE_LIMIT=_,N`). It is written by a polkit-gated root helper, which then restarts UPower so
  the new value is read. GPU-S refuses to write it if another hwdb file already sets
  `CHARGE_LIMIT`.
* Every change is verified against the kernel attribute. Limits from 50 to 100 % are accepted.
  The start-threshold controls and the *Balanced* preset (50→80) are shown as unsupported on this
  battery. *Battery Care* stops at 80 %, and *Maximum Runtime* removes the limit.
* `/etc/systemd/system/battery-charge-limit.service` (an older manual approach, disabled) is left
  untouched. GPU-S warns if it is ever enabled, because it would fight UPower.

```
gpu-s battery [status]        gpu-s battery limit 80|off
gpu-s battery preset care|balanced|maximum
gpu-s battery start N         (only on batteries with a start threshold)
gpu-s battery profile [MODE]  GPU mode used on battery
```

## CLI

```
gpu-s                  open the window
gpu-s status [--json]  summary (never wakes a sleeping GPU)
gpu-s on | off | auto  Always On | Power Saving | Auto (manual override)
gpu-s profile          re-apply the profile for the current power source
gpu-s ac [MODE]        show/set the plugged-in GPU profile
gpu-s battery [...]    battery info and charge limits (gpu-s battery help)
gpu-s apps             processes using the NVIDIA GPU
gpu-s run CMD...       run one app on NVIDIA (PRIME render offload)
gpu-s mux [hybrid|discrete]
gpu-s tuning [on|off]  Hyprland session tuning for Hybrid mode
gpu-s autostart [on|off]
gpu-s reset            undo everything GPU-S changed
gpu-s quit | help | --version
```

## Hybrid session tuning (optional)

Omarchy exports `LIBVA_DRIVER_NAME=nvidia` and `__GLX_VENDOR_LIBRARY_NAME=nvidia`
whenever an NVIDIA GPU exists. In Hybrid mode that wakes the dGPU for every video
and every X11 OpenGL app. When enabled, the tuning writes `~/.config/hypr/gpu-s.lua` and adds
one `pcall(require, "hypr.gpu-s")` line to `hyprland.lua` (a backup is kept). If the panel is on
Intel at login, it routes those two to Intel. In Discrete mode it does nothing.

## Install / uninstall

```bash
# one-liner (clones to ~/.local/share/gpu-s, see "Install" above) — also used to update
curl -fsSL https://raw.githubusercontent.com/eris4444/gpu-s/main/install-remote.sh | bash

# equivalent, from an existing clone
./install.sh                 # runs the tests, builds a pacman package, installs it (asks for your password)

./uninstall.sh               # gpu-s reset (incl. removing the battery override) + pacman -R gpu-s
./uninstall.sh --purge       # ...and delete ~/.config/gpu-s and ~/.local/state/gpu-s
```

Installed files (all owned by the `gpu-s` package): `/usr/bin/gpu-s`,
`/usr/lib/gpu-s/` (app and the two root helpers),
`/usr/share/polkit-1/actions/dev.erisrtg.gpus.policy`,
`/usr/share/applications/dev.erisrtg.GpuS.desktop`, icons, docs.

## If Hybrid mode misbehaves

The MUX is a firmware setting, so it survives reinstalls. To go back to Discrete from any terminal or TTY
(Ctrl+Alt+F3):

```
gpu-s mux discrete && reboot
# without GPU-S:
echo 0 | sudo tee /sys/class/firmware-attributes/asus-armoury/attributes/gpu_mux_mode/current_value && reboot
```

## Layout

```
bin/gpu-s             launcher
gpu_s/hardware.py     GPU detection: PCI, DRM wiring, MUX, RTD3, session renderer
gpu_s/nvml.py         nvidia-smi sampling      gpu_s/procs.py   /proc fd scan
gpu_s/monitor.py      snapshot + wake-safe sampling policy
gpu_s/policy.py       modes, capability assessment (pure logic)
gpu_s/power_manager.py apply + verify          gpu_s/privilege.py pkexec
gpu_s/agent.py        AC/battery profile agent gpu_s/battery.py UPower
gpu_s/config.py       XDG config/state         gpu_s/cli.py     CLI
gpu_s/tray.py         StatusNotifierItem + dbusmenu
gpu_s/session_tuning.py, autostart.py, notify.py
gpu_s/ui/             GTK4/libadwaita window, settings, Omarchy theme
helpers/              root helpers (whitelisted operations only)
tests/                unittest suite + helper argument tests (make test)
```
