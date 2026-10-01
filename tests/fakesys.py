"""Build fake /sys and /proc trees that mimic an ASUS hybrid laptop."""

from __future__ import annotations

import os
from pathlib import Path

NV = "0000:01:00.0"
NV_AUDIO = "0000:01:00.1"
INTEL = "0000:00:02.0"


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(target, link.parent), link)


class FakeSystem:
    """mode: 'discrete' (panel on NVIDIA) or 'hybrid' (panel on Intel)."""

    def __init__(
        self,
        root: Path,
        mode: str = "discrete",
        runtime_status: str = "active",
        control: str = "auto",
        rtd3: str = "Enabled (fine-grained)",
        hdmi_connected: bool = False,
        mux: str | None = None,
        audio_control: str = "auto",
    ):
        self.sys = root / "sys"
        self.proc = root / "proc"
        devices = self.sys / "devices/pci0000:00"
        drivers = self.sys / "bus/pci/drivers"
        mux_value = {"discrete": "0", "hybrid": "1"}[mux or mode]

        def pci(address, vendor, device, cls, driver, boot_vga=None, parent=devices):
            dev = parent / address
            _write(dev / "vendor", vendor)
            _write(dev / "device", device)
            _write(dev / "class", cls)
            if boot_vga is not None:
                _write(dev / "boot_vga", "1" if boot_vga else "0")
            (drivers / driver).mkdir(parents=True, exist_ok=True)
            _link(dev / "driver", drivers / driver)
            _link(self.sys / "bus/pci/devices" / address, dev)
            return dev

        intel = pci(INTEL, "0x8086", "0x46a6", "0x038000", "i915")
        bridge = devices / "0000:00:01.0"
        nv = pci(NV, "0x10de", "0x25a2", "0x030000", "nvidia", boot_vga=(mode == "discrete"), parent=bridge)
        audio = pci(NV_AUDIO, "0x10de", "0x2291", "0x040300", "snd_hda_intel", parent=bridge)

        suspended = runtime_status == "suspended"
        _write(nv / "power/control", control)
        _write(nv / "power/runtime_status", runtime_status)
        _write(nv / "power_state", "D3cold" if suspended else "D0")
        _write(nv / "d3cold_allowed", "1")
        _write(nv / "power/runtime_active_time", "9000")
        _write(nv / "power/runtime_suspended_time", "1000")
        _write(audio / "power/control", audio_control)
        _write(audio / "power/runtime_status", "suspended")

        def card(dev, name, render, connectors):
            base = dev / "drm" / name
            base.mkdir(parents=True)
            (dev / "drm" / render).mkdir(parents=True)
            _link(base / "device", dev)
            _link(self.sys / "class/drm" / name, base)
            for conn, status in connectors.items():
                cdir = base / f"{name}-{conn}"
                _write(cdir / "status", status)
                _write(cdir / "enabled", "enabled" if status == "connected" else "disabled")
                _link(self.sys / "class/drm" / f"{name}-{conn}", cdir)

        panel = "connected"
        hdmi = "connected" if hdmi_connected else "disconnected"
        if mode == "discrete":
            card(intel, "card0", "renderD129", {"DP-2": "disconnected"})
            card(nv, "card1", "renderD128", {"eDP-1": panel, "HDMI-A-1": hdmi})
        else:
            card(intel, "card0", "renderD129", {"eDP-1": panel, "DP-2": "disconnected"})
            card(nv, "card1", "renderD128", {"HDMI-A-1": hdmi})

        armoury = self.sys / "class/firmware-attributes/asus-armoury/attributes"
        _write(armoury / "gpu_mux_mode/current_value", mux_value)
        _write(armoury / "gpu_mux_mode/possible_values", "0;1")
        _write(armoury / "dgpu_disable/current_value", "0")
        _write(armoury / "pending_reboot", "0" if mux is None or mux == mode else "1")
        _write(self.sys / "module/nvidia/version", "610.57.04")
        _write(self.sys / "class/dmi/id/product_name", "ASUS TUF Gaming F15 FX507ZC4_FX507ZC4")

        _write(self.sys / "class/power_supply/ACAD/type", "Mains")
        _write(self.sys / "class/power_supply/ACAD/online", "1")
        _write(self.sys / "class/power_supply/BAT1/type", "Battery")
        _write(self.sys / "class/power_supply/BAT1/present", "1")
        _write(self.sys / "class/power_supply/BAT1/capacity", "80")
        _write(self.sys / "class/power_supply/BAT1/status", "Discharging")

        _write(
            self.proc / "driver/nvidia/gpus" / NV / "power",
            f"Runtime D3 status:          {rtd3}\nVideo Memory:               {'Off' if suspended else 'Active'}\n",
        )

    def set_ac(self, online: bool) -> None:
        _write(self.sys / "class/power_supply/ACAD/online", "1" if online else "0")
