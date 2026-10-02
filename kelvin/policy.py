"""GPU modes and what the hardware can do right now.

Pure logic over `hardware.Facts`: no I/O, fully unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .hardware import Facts


class Mode(str, Enum):
    ALWAYS_ON = "always-on"
    AUTO = "auto"
    POWER_SAVING = "power-saving"

    @property
    def label(self) -> str:
        return MODE_LABELS[self]

    @property
    def pm_control(self) -> str:
        """Kernel runtime-PM control value this mode requires."""
        return "on" if self is Mode.ALWAYS_ON else "auto"

    @classmethod
    def parse(cls, value: str | None) -> "Mode | None":
        try:
            return cls(value) if value else None
        except ValueError:
            return None


MODE_LABELS = {
    Mode.ALWAYS_ON: "Always On",
    Mode.AUTO: "Auto",
    Mode.POWER_SAVING: "Power Saving",
}

MODE_ICONS = {
    Mode.ALWAYS_ON: "power-profile-performance-symbolic",
    Mode.AUTO: "power-profile-balanced-symbolic",
    Mode.POWER_SAVING: "power-profile-power-saver-symbolic",
}

MODE_SUMMARIES = {
    Mode.ALWAYS_ON: "The NVIDIA GPU stays powered (D0) and instantly available. "
    "Kernel runtime power management is set to 'on'.",
    Mode.AUTO: "The NVIDIA GPU powers down (D3cold) whenever it is idle and wakes "
    "automatically when an app needs it. Runtime power management is 'auto'.",
    Mode.POWER_SAVING: "Intel iGPU preferred. Same kernel policy as Auto, but Kelvin "
    "never wakes the GPU to read sensors and points out apps that keep it awake.",
}


@dataclass(frozen=True)
class Blocker:
    code: str
    title: str
    detail: str
    hint: str | None = None


@dataclass
class Assessment:
    gpu_present: bool
    runtime_pm_available: bool  # kernel + driver can runtime-suspend the GPU
    dynamic_modes_possible: bool  # Auto / Power Saving can actually power it down
    forced_mode: Mode | None  # hardware dictates this mode (e.g. Discrete MUX)
    unsupported: Blocker | None  # why Auto / Power Saving are unavailable
    blockers: list[Blocker] = field(default_factory=list)  # what keeps it on right now
    notes: list[Blocker] = field(default_factory=list)  # informational

    def supports(self, mode: Mode) -> bool:
        if not self.gpu_present:
            return False
        if mode is Mode.ALWAYS_ON:
            return True
        return self.dynamic_modes_possible

    def unsupported_reason(self, mode: Mode) -> str | None:
        if self.supports(mode):
            return None
        if self.unsupported:
            return self.unsupported.title
        return "Not supported on this system."

    @property
    def can_power_down_now(self) -> bool:
        return self.dynamic_modes_possible and not self.blockers


def _renderer_name(facts: Facts) -> str:
    addr = facts.session.renderer_address
    gpu = next((g for g in facts.gpus if g.address == addr), None)
    return gpu.vendor_name if gpu else "unknown"


def assess(facts: Facts) -> Assessment:
    if facts.gpu is None:
        blocker = Blocker("no-gpu", "No NVIDIA GPU was detected.", "No NVIDIA display device is present on the PCI bus.")
        return Assessment(False, False, False, None, blocker)

    if facts.gpu.driver != "nvidia":
        blocker = Blocker(
            "no-driver",
            "The NVIDIA driver is not bound to the GPU.",
            f"The GPU at {facts.gpu.address} is using '{facts.gpu.driver or 'no driver'}'.",
        )
        return Assessment(True, False, False, None, blocker)

    power = facts.power
    rtd3 = facts.rtd3
    pm_available = bool(power and power.control is not None and power.runtime_status != "unsupported")
    if rtd3 is not None and not rtd3.enabled:
        pm_available = False

    notes: list[Blocker] = []
    if facts.mux_change_pending:
        target = facts.mux.configured_mode or "the new mode"
        notes.append(
            Blocker(
                "mux-pending",
                f"A GPU MUX change to {target.capitalize()} is pending.",
                "The firmware applies it on the next boot.",
                "Reboot when convenient. Kelvin never reboots on its own.",
            )
        )

    if not pm_available:
        detail = (
            f"The NVIDIA driver reports runtime D3 as '{rtd3.status}'."
            if rtd3 and rtd3.status
            else "The kernel exposes no runtime power management for the GPU."
        )
        blocker = Blocker("no-rtd3", "Runtime GPU power management is not available on this system.", detail)
        return Assessment(True, False, False, Mode.ALWAYS_ON, blocker, [blocker], notes)

    # Hard wiring: the session/panel depends on the NVIDIA GPU.
    wiring: list[Blocker] = []
    if facts.panel_on_nvidia:
        mux = " (ASUS GPU MUX: Discrete)" if facts.mux.available else ""
        wiring.append(
            Blocker(
                "panel-on-nvidia",
                "The internal display is wired to the NVIDIA GPU" + mux + ".",
                "The NVIDIA GPU has to stay on to drive the laptop panel.",
                "Switch the GPU MUX to Hybrid (Optimus) and reboot."
                if facts.mux.available
                else "Change the graphics mode to hybrid/Optimus in firmware setup.",
            )
        )
    if facts.session_on_nvidia:
        compositor = facts.session.compositor or "The graphical session"
        wiring.append(
            Blocker(
                "session-on-nvidia",
                f"{compositor} is currently running on the NVIDIA GPU.",
                "The compositor renders every frame on it, so it can never go idle.",
                "Switch the graphical session to the Intel iGPU before attempting GPU power-down.",
            )
        )

    if wiring:
        forced = Mode.ALWAYS_ON
        unsupported = Blocker(
            "session-needs-nvidia",
            "Power saving is unavailable while the current session uses the NVIDIA GPU.",
            " ".join(b.title for b in wiring),
            wiring[0].hint,
        )
        return Assessment(True, True, False, forced, unsupported, wiring, notes)

    blockers: list[Blocker] = []
    external = facts.external_on_nvidia
    if external:
        names = ", ".join(c.name for c in external)
        blockers.append(
            Blocker(
                "external-display",
                f"An external display ({names}) is connected to a port wired to the NVIDIA GPU.",
                "The GPU must stay on while it drives that output.",
                "Disconnect it to let the GPU power down.",
            )
        )
    if power and power.control == "on":
        notes.append(
            Blocker(
                "pm-pinned",
                "Runtime power management is set to 'on'.",
                "The kernel keeps the GPU in D0 until this is set back to 'auto'.",
            )
        )
    for address, sibling in facts.sibling_power.items():
        if sibling.control == "on":
            blockers.append(
                Blocker(
                    "sibling-pinned",
                    f"The GPU's companion device {address} is pinned on.",
                    "Every function of the card must be allowed to suspend for D3cold.",
                    "Use 'Allow GPU To Power Down', which also sets it to 'auto'.",
                )
            )
    if power and power.d3cold_allowed is False:
        notes.append(Blocker("no-d3cold", "D3cold is disallowed for the GPU.", "It can only reach D3hot, which saves less power."))

    return Assessment(True, True, True, None, None, blockers, notes)


def effective_mode(facts: Facts, assessment: Assessment, applied_mode: str | None) -> Mode | None:
    """The mode the system is in right now, derived from real kernel state."""
    if not assessment.gpu_present:
        return None
    if assessment.forced_mode is not None:
        return assessment.forced_mode
    control = facts.power.control if facts.power else None
    if control == "on":
        return Mode.ALWAYS_ON
    if control == "auto":
        applied = Mode.parse(applied_mode)
        return applied if applied in (Mode.AUTO, Mode.POWER_SAVING) else Mode.AUTO
    return None


def target_mode(
    ac_mode: str,
    battery_mode: str,
    on_ac: bool | None,
    manual_mode: str | None,
    manual_on_ac: bool | None,
    auto_switch: bool,
) -> tuple[Mode | None, str]:
    """Pick the mode to enforce. A manual choice holds until the power source changes."""
    manual = Mode.parse(manual_mode)
    if manual is not None and (manual_on_ac == on_ac or not auto_switch):
        return manual, "manual"
    if not auto_switch or on_ac is None:
        return None, "none"
    if on_ac:
        return Mode.parse(ac_mode), "ac-profile"
    return Mode.parse(battery_mode), "battery-profile"


def impact_estimate(gpu_w: float | None, system_w: float | None) -> str | None:
    """Coarse label for the GPU's share of power. Always presented as an estimate."""
    if gpu_w is None:
        return None
    if system_w and system_w > 0:
        share = gpu_w / system_w
        if share < 0.05:
            return "Negligible"
        if share < 0.15:
            return "Low"
        if share < 0.30:
            return "Moderate"
        return "High"
    if gpu_w < 1:
        return "Negligible"
    if gpu_w < 3:
        return "Low"
    if gpu_w < 6:
        return "Moderate"
    return "High"
