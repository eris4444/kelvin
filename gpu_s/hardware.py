"""GPU detection from sysfs/procfs.

Everything here reads cached kernel state only. Nothing touches PCI config
space (no lspci, no config/link attributes) or NVML, so none of it can wake a
runtime-suspended NVIDIA GPU.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import paths
from .sysfs import link_name, read, read_int

NVIDIA = "0x10de"
INTEL = "0x8086"
AMD = "0x1002"
VENDOR_NAMES = {NVIDIA: "NVIDIA", INTEL: "Intel", AMD: "AMD"}

INTERNAL_CONNECTORS = ("eDP", "LVDS", "DSI")


@dataclass(frozen=True)
class PciDevice:
    address: str
    vendor: str
    device: str
    pci_class: str
    driver: str | None
    boot_vga: bool

    @property
    def path(self) -> Path:
        return paths.SYSFS / "bus/pci/devices" / self.address

    @property
    def vendor_name(self) -> str:
        return VENDOR_NAMES.get(self.vendor, self.vendor)

    @property
    def is_display(self) -> bool:
        return self.pci_class.startswith("0x03")

    @property
    def is_nvidia(self) -> bool:
        return self.vendor == NVIDIA

    @property
    def pci_name(self) -> str | None:
        return pci_ids_name(self.vendor, self.device)


def _pci_device(path: Path) -> PciDevice | None:
    vendor = read(path / "vendor")
    device = read(path / "device")
    pci_class = read(path / "class")
    if not (vendor and device and pci_class):
        return None
    return PciDevice(
        address=path.name,
        vendor=vendor,
        device=device,
        pci_class=pci_class,
        driver=link_name(path / "driver"),
        boot_vga=read(path / "boot_vga") == "1",
    )


def pci_devices() -> list[PciDevice]:
    root = paths.SYSFS / "bus/pci/devices"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [dev for dev in map(_pci_device, entries) if dev is not None]


def display_devices() -> list[PciDevice]:
    return [dev for dev in pci_devices() if dev.is_display]


def nvidia_gpu(devices: list[PciDevice] | None = None) -> PciDevice | None:
    for dev in devices if devices is not None else display_devices():
        if dev.is_display and dev.is_nvidia:
            return dev
    return None


def integrated_gpu(devices: list[PciDevice] | None = None) -> PciDevice | None:
    for dev in devices if devices is not None else display_devices():
        if dev.is_display and dev.vendor in (INTEL, AMD):
            return dev
    return None


def sibling_functions(gpu: PciDevice) -> list[PciDevice]:
    """Other PCI functions of the same physical card (HD audio, USB-C...)."""
    slot = gpu.address.rsplit(".", 1)[0]
    return [
        dev
        for dev in pci_devices()
        if dev.address != gpu.address
        and dev.address.rsplit(".", 1)[0] == slot
        and dev.vendor == gpu.vendor
    ]


@lru_cache(maxsize=32)
def pci_ids_name(vendor: str, device: str) -> str | None:
    """Look a device up in the hwdata pci.ids database (no hardware access)."""
    vendor_id = vendor.removeprefix("0x").lower()
    device_id = device.removeprefix("0x").lower()
    for db in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids"):
        try:
            with open(db, encoding="utf-8", errors="replace") as fh:
                in_vendor = False
                for line in fh:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if not line.startswith("\t"):
                        if in_vendor:
                            return None
                        in_vendor = line[:4].lower() == vendor_id
                    elif in_vendor and not line.startswith("\t\t"):
                        if line[1:5].lower() == device_id:
                            return line[5:].strip()
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------
# DRM: cards, connectors, internal panel wiring


@dataclass(frozen=True)
class DrmConnector:
    card: str
    name: str
    status: str
    enabled: bool
    gpu_address: str | None

    @property
    def connected(self) -> bool:
        return self.status == "connected"

    @property
    def internal(self) -> bool:
        return self.name.startswith(INTERNAL_CONNECTORS)

    @property
    def active(self) -> bool:
        return self.connected and self.enabled


def drm_cards() -> dict[str, str]:
    """Map card name ('card1') to the PCI address of its GPU."""
    cards: dict[str, str] = {}
    root = paths.SYSFS / "class/drm"
    try:
        entries = list(root.iterdir())
    except OSError:
        return cards
    for entry in entries:
        if re.fullmatch(r"card\d+", entry.name):
            address = link_name(entry / "device")
            if address:
                cards[entry.name] = address
    return cards


def drm_connectors(cards: dict[str, str] | None = None) -> list[DrmConnector]:
    cards = drm_cards() if cards is None else cards
    connectors = []
    root = paths.SYSFS / "class/drm"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return connectors
    for entry in entries:
        match = re.fullmatch(r"(card\d+)-(.+)", entry.name)
        if not match:
            continue
        card, name = match.groups()
        connectors.append(
            DrmConnector(
                card=card,
                name=name,
                status=read(entry / "status") or "unknown",
                enabled=read(entry / "enabled") == "enabled",
                gpu_address=cards.get(card),
            )
        )
    return connectors


def device_nodes(address: str, cards: dict[str, str] | None = None) -> set[str]:
    """/dev/dri nodes (card and render) that belong to a GPU."""
    nodes: set[str] = set()
    drm_dir = paths.SYSFS / "bus/pci/devices" / address / "drm"
    try:
        for entry in drm_dir.iterdir():
            if re.fullmatch(r"(card|renderD)\d+", entry.name):
                nodes.add(f"/dev/dri/{entry.name}")
    except OSError:
        cards = drm_cards() if cards is None else cards
        nodes.update(f"/dev/dri/{card}" for card, addr in cards.items() if addr == address)
    return nodes


# --------------------------------------------------------------------------
# NVIDIA driver and runtime D3


@dataclass(frozen=True)
class PowerState:
    runtime_status: str  # active | suspended | suspending | resuming | unsupported
    pci_state: str | None  # D0 | D3hot | D3cold
    control: str | None  # on | auto
    d3cold_allowed: bool | None
    active_ms: int | None
    suspended_ms: int | None

    @property
    def suspended(self) -> bool:
        return self.runtime_status == "suspended"

    @property
    def powered_down(self) -> bool:
        """Kernel confirms the device is runtime-suspended in a D3 state."""
        return self.suspended and (self.pci_state or "").startswith("D3")

    @property
    def suspended_fraction(self) -> float | None:
        if self.active_ms is None or self.suspended_ms is None:
            return None
        total = self.active_ms + self.suspended_ms
        return self.suspended_ms / total if total else None


def power_state(dev: PciDevice) -> PowerState:
    base = dev.path
    control = read(base / "power/control")
    allowed = read(base / "d3cold_allowed")
    return PowerState(
        runtime_status=read(base / "power/runtime_status") or "unsupported",
        pci_state=read(base / "power_state"),
        control=control,
        d3cold_allowed=None if allowed is None else allowed == "1",
        active_ms=read_int(base / "power/runtime_active_time"),
        suspended_ms=read_int(base / "power/runtime_suspended_time"),
    )


@dataclass(frozen=True)
class Rtd3Info:
    status: str | None  # e.g. "Enabled (fine-grained)"
    video_memory: str | None  # "Active" / "Off"

    @property
    def enabled(self) -> bool:
        return bool(self.status and self.status.lower().startswith("enabled"))

    @property
    def fine_grained(self) -> bool:
        return bool(self.status and "fine" in self.status.lower())


def rtd3_info(address: str) -> Rtd3Info | None:
    """Parse /proc/driver/nvidia/gpus/<addr>/power (documented by NVIDIA for
    checking runtime D3 status; reading it does not resume the GPU)."""
    text = read(paths.PROCFS / "driver/nvidia/gpus" / address / "power")
    if text is None:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    return Rtd3Info(
        status=fields.get("runtime d3 status"),
        video_memory=fields.get("video memory"),
    )


def nvidia_driver_version() -> str | None:
    return read(paths.SYSFS / "module/nvidia/version")


# --------------------------------------------------------------------------
# ASUS GPU MUX


@dataclass(frozen=True)
class MuxInfo:
    available: bool
    source: str | None = None  # "asus-armoury" | "asus-nb-wmi"
    value: int | None = None  # configured value: 0 discrete, 1 hybrid
    pending_reboot: bool | None = None
    dgpu_disabled: bool | None = None

    @property
    def configured_mode(self) -> str | None:
        return {0: "discrete", 1: "hybrid"}.get(self.value)


def mux_info() -> MuxInfo:
    armoury = paths.SYSFS / "class/firmware-attributes/asus-armoury/attributes"
    legacy = paths.SYSFS / "devices/platform/asus-nb-wmi"
    if (armoury / "gpu_mux_mode/current_value").exists():
        pending = read(armoury / "pending_reboot")
        disabled = read(armoury / "dgpu_disable/current_value")
        return MuxInfo(
            available=True,
            source="asus-armoury",
            value=read_int(armoury / "gpu_mux_mode/current_value"),
            pending_reboot=None if pending is None else pending == "1",
            dgpu_disabled=None if disabled is None else disabled == "1",
        )
    if (legacy / "gpu_mux_mode").exists():
        disabled = read(legacy / "dgpu_disable")
        return MuxInfo(
            available=True,
            source="asus-nb-wmi",
            value=read_int(legacy / "gpu_mux_mode"),
            dgpu_disabled=None if disabled is None else disabled == "1",
        )
    return MuxInfo(available=False)


# --------------------------------------------------------------------------
# Graphical session


@dataclass(frozen=True)
class SessionInfo:
    compositor: str | None
    renderer_address: str | None
    renderer_source: str  # hyprland-log | internal-panel | boot-vga | unknown
    panel_address: str | None
    panel_connector: str | None


def hyprland_log() -> Path | None:
    base = paths.runtime_dir() / "hypr"
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if signature:
        candidate = base / signature / "hyprland.log"
        if candidate.is_file():
            return candidate
    try:
        logs = [d / "hyprland.log" for d in base.iterdir() if (d / "hyprland.log").is_file()]
    except OSError:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime, default=None)


_PRIMARY_RE = re.compile(r"gpu (/dev/dri/card\d+) becomes primary drm")


def hyprland_primary_card(log: Path | None) -> str | None:
    """Hyprland (aquamarine) logs which DRM device it renders on at startup."""
    if log is None:
        return None
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            head = fh.read(1 << 20)
    except OSError:
        return None
    match = _PRIMARY_RE.search(head)
    return match.group(1).rsplit("/", 1)[-1] if match else None


def compositor_running() -> str | None:
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or (paths.runtime_dir() / "hypr").is_dir():
        return "Hyprland"
    desktop = os.environ.get("XDG_CURRENT_DESKTOP")
    return desktop or None


def session_info(
    cards: dict[str, str],
    connectors: list[DrmConnector],
    gpus: list[PciDevice],
) -> SessionInfo:
    panel = next((c for c in connectors if c.internal and c.connected), None)
    compositor = compositor_running()

    renderer, source = None, "unknown"
    if compositor == "Hyprland":
        card = hyprland_primary_card(hyprland_log())
        if card and card in cards:
            renderer, source = cards[card], "hyprland-log"
    if renderer is None and panel and panel.gpu_address:
        renderer, source = panel.gpu_address, "internal-panel"
    if renderer is None:
        boot = next((g for g in gpus if g.boot_vga), None)
        if boot:
            renderer, source = boot.address, "boot-vga"

    return SessionInfo(
        compositor=compositor,
        renderer_address=renderer,
        renderer_source=source,
        panel_address=panel.gpu_address if panel else None,
        panel_connector=panel.name if panel else None,
    )


def product_name() -> str | None:
    name = read(paths.SYSFS / "class/dmi/id/product_name")
    if name and "_" in name:
        # "ASUS TUF Gaming F15 FX507ZC4_FX507ZC4" -> "ASUS TUF Gaming F15 FX507ZC4"
        name = name.split("_", 1)[0]
    return name


# --------------------------------------------------------------------------
# Snapshot of all cheap facts


@dataclass
class Facts:
    gpus: list[PciDevice]
    gpu: PciDevice | None
    igpu: PciDevice | None
    siblings: list[PciDevice]
    driver_version: str | None
    power: PowerState | None
    sibling_power: dict[str, PowerState]
    rtd3: Rtd3Info | None
    mux: MuxInfo
    cards: dict[str, str]
    connectors: list[DrmConnector]
    session: SessionInfo
    product: str | None = None
    nvidia_connectors: list[DrmConnector] = field(default_factory=list)

    @property
    def gpu_nodes(self) -> set[str]:
        return device_nodes(self.gpu.address, self.cards) if self.gpu else set()

    @property
    def panel_on_nvidia(self) -> bool:
        return bool(self.gpu and self.session.panel_address == self.gpu.address)

    @property
    def session_on_nvidia(self) -> bool:
        return bool(self.gpu and self.session.renderer_address == self.gpu.address)

    @property
    def external_on_nvidia(self) -> list[DrmConnector]:
        return [c for c in self.nvidia_connectors if c.active and not c.internal]

    @property
    def effective_mux_mode(self) -> str | None:
        """What the hardware is actually doing this boot, from panel wiring."""
        if not self.mux.available or self.session.panel_address is None or not self.gpu:
            return None
        return "discrete" if self.panel_on_nvidia else "hybrid"

    @property
    def mux_change_pending(self) -> bool:
        effective = self.effective_mux_mode
        configured = self.mux.configured_mode
        return bool(self.mux.pending_reboot) or (
            effective is not None and configured is not None and effective != configured
        )


def gather_facts() -> Facts:
    gpus = display_devices()
    gpu = nvidia_gpu(gpus)
    cards = drm_cards()
    connectors = drm_connectors(cards)
    siblings = sibling_functions(gpu) if gpu else []
    return Facts(
        gpus=gpus,
        gpu=gpu,
        igpu=integrated_gpu(gpus),
        siblings=siblings,
        driver_version=nvidia_driver_version(),
        power=power_state(gpu) if gpu else None,
        sibling_power={s.address: power_state(s) for s in siblings},
        rtd3=rtd3_info(gpu.address) if gpu else None,
        mux=mux_info(),
        cards=cards,
        connectors=connectors,
        session=session_info(cards, connectors, gpus),
        product=product_name(),
        nvidia_connectors=[c for c in connectors if gpu and c.gpu_address == gpu.address],
    )
