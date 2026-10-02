"""Combines cheap kernel facts with carefully gated NVML sampling.

Any NVML query (nvidia-smi) resumes a runtime-suspended GPU and resets its
idle timer. Kelvin must never be the reason the GPU stays awake, so:

* it never queries while the kernel reports the GPU runtime-suspended;
* when the GPU is pinned on (Always On, Discrete MUX, external display) it
  samples at the normal refresh interval;
* otherwise (runtime PM 'auto') it samples at the normal rate only while the
  GPU is busy and backs off exponentially while it is idle, so the driver's
  idle timer can expire and the GPU can power down;
* in Power Saving it only samples a GPU that has already stayed awake for
  a while (i.e. something else is holding it), at most every 30 s.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import charge, fans, idle, nvml
from .cpu import CpuInfo, CpuMonitor
from .battery import PowerSupply, read_power_supply
from .hardware import Facts, gather_facts
from .policy import Assessment, Mode, assess, effective_mode
from .procs import DeviceHolder, device_holders, process_identity

# When the GPU looks idle, wait longer than the driver's idle-to-D3cold delay
# (measured ~10 s on the RTX 3050 Laptop) before sampling again, so a sample
# never lands inside that window and keeps resetting it.
IDLE_BACKOFF_MIN = 15.0
IDLE_BACKOFF_MAX = 60.0
POWER_SAVING_GRACE = 30.0
SUPPLY_TTL = 10.0  # battery figures change slowly; AC changes are signalled to the agent


@dataclass
class GpuProcess:
    pid: int
    name: str
    command: str
    kind: str  # Graphics / Compute / Graphics + Compute / Device open
    vram_mb: float | None
    sm: float | None
    from_nvml: bool
    is_self: bool = False


@dataclass
class Snapshot:
    time: float
    facts: Facts
    assessment: Assessment
    mode: Mode | None
    supply: PowerSupply | None
    sample: nvml.GpuSample | None  # most recent sample (may be old)
    sample_fresh: bool  # sample taken this refresh
    sensor_note: str | None  # why sensors weren't read
    nvml_error: str | None
    processes: list[GpuProcess] = field(default_factory=list)
    charge: charge.ChargeInfo | None = None
    cpu: CpuInfo | None = None
    fans: fans.FanInfo | None = None
    idle: idle.IdleSettings | None = None
    history: list[tuple[float, float | None, float | None]] = field(default_factory=list)

    @property
    def sample_age(self) -> float | None:
        return self.time - self.sample.timestamp if self.sample else None

    @property
    def gpu_power_w(self) -> float | None:
        """Measured GPU draw, only if the sample reflects the current state."""
        if self.sample is None or self.facts.power is None or self.facts.power.suspended:
            return None
        age = self.sample_age
        return self.sample.power_draw if age is not None and age < 90 else None


_KIND = {"G": "Graphics", "C": "Compute", "C+G": "Graphics + Compute", "M+C": "Compute", "M": "Compute"}


class Monitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sample: nvml.GpuSample | None = None
        self._next_query = 0.0
        self._idle_streak = 0
        self._awake_since: float | None = None
        self._supply: PowerSupply | None = None
        self._supply_time = 0.0
        self._charge: charge.ChargeInfo | None = None
        self._charge_time = 0.0
        self._units: list[str] = []
        self._units_time = 0.0
        self.cpu_monitor = CpuMonitor()
        self._idle: idle.IdleSettings | None = None
        self._idle_time = 0.0
        self._logind: tuple[str | None, str | None] = (None, None)
        self._logind_time = 0.0
        self.history: deque[tuple[float, float | None, float | None]] = deque(maxlen=120)

    def _power_supply(self, now: float, force: bool) -> PowerSupply:
        if force or self._supply is None or now - self._supply_time >= SUPPLY_TTL:
            self._supply = read_power_supply()
            self._supply_time = now
        return self._supply

    def invalidate_supply(self) -> None:
        self._supply = None
        self._charge = None
        self._idle = None

    def _idle_settings(self, now: float, force: bool) -> idle.IdleSettings:
        if force or self._idle is None or now - self._idle_time >= SUPPLY_TTL / 2:
            with_logind = force or now - self._logind_time >= 60
            settings = idle.read_settings(with_logind=with_logind)
            if with_logind:
                self._logind = (settings.lid_action, settings.lid_action_ac)
                self._logind_time = now
            else:
                settings.lid_action, settings.lid_action_ac = self._logind
            self._idle, self._idle_time = settings, now
        return self._idle

    def _charge_info(self, now: float, force: bool) -> charge.ChargeInfo:
        if force or self._charge is None or now - self._charge_time >= SUPPLY_TTL / 2:
            check_units = force or now - self._units_time >= 60
            info = charge.read_charge(check_units=check_units)
            if check_units:
                self._units_time = now
                self._units = info.legacy_units_enabled
            else:
                info.legacy_units_enabled = self._units
            self._charge, self._charge_time = info, now
        return self._charge

    # -- sampling policy -------------------------------------------------

    def _gate(self, facts: Facts, assessment: Assessment, mode: Mode | None, interval: float, now: float, force: bool):
        power = facts.power
        if facts.gpu is None or facts.gpu.driver != "nvidia" or power is None:
            return False, "No NVIDIA GPU with the NVIDIA driver is available."
        if power.runtime_status != "active":
            self._awake_since = None
            return False, "The GPU is powered down. Sensors are not read, so it can stay asleep."

        if self._awake_since is None:
            self._awake_since = now

        pinned = (
            power.control == "on"
            or assessment.forced_mode is not None
            or bool(facts.external_on_nvidia)
            or not assessment.dynamic_modes_possible
        )
        if pinned or force:
            return True, None

        if mode is Mode.POWER_SAVING:
            awake_for = now - self._awake_since
            if awake_for < POWER_SAVING_GRACE:
                return False, "Power Saving: sensors are not read while the GPU may be going to sleep."
            if now < self._next_query:
                return False, "Power Saving: sensor reads limited to every 30 s."
            return True, None

        if now < self._next_query:
            wait = int(self._next_query - now) + 1
            return False, f"Sensor reads paused so the GPU can power down (next in {wait} s)."
        return True, None

    def _schedule_next(self, sample: nvml.GpuSample | None, mode: Mode | None, pinned: bool, interval: float, now: float):
        if pinned:
            self._idle_streak = 0
            self._next_query = now
        elif mode is Mode.POWER_SAVING:
            self._next_query = now + POWER_SAVING_GRACE
        elif sample is not None and sample.busy:
            self._idle_streak = 0
            self._next_query = now + interval
        else:
            self._idle_streak += 1
            self._next_query = now + min(IDLE_BACKOFF_MAX, max(interval, IDLE_BACKOFF_MIN) * (2 ** (self._idle_streak - 1)))

    # -- snapshot --------------------------------------------------------

    def snapshot(
        self,
        applied_mode: str | None,
        interval: float,
        *,
        want_processes: bool = True,
        want_supply: bool = True,
        allow_nvml: bool = True,
        force: bool = False,
        nvml_processes: bool | None = None,
    ) -> Snapshot:
        """nvml_processes: also run the per-process NVML query (defaults to want_processes)."""
        if nvml_processes is None:
            nvml_processes = want_processes
        with self._lock:
            now = time.time()
            facts = gather_facts()
            assessment = assess(facts)
            mode = effective_mode(facts, assessment, applied_mode)
            supply = self._power_supply(now, force) if want_supply else None
            charge_info = self._charge_info(now, force) if want_supply else None
            cpu_info = self.cpu_monitor.sample() if want_processes else None
            fan_info = fans.read_fans() if want_supply else None
            idle_info = self._idle_settings(now, force) if want_supply else None

            fresh, note, error = False, None, None
            if allow_nvml:
                query, note = self._gate(facts, assessment, mode, interval, now, force)
            else:
                query, note = False, "Live sensors are only read while the window is open."
            if query and facts.gpu:
                try:
                    previous = self.sample
                    self.sample = nvml.query(facts.gpu.address, processes=nvml_processes)
                    if not nvml_processes and previous is not None:
                        # Keep the last per-process figures instead of blanking them.
                        self.sample.processes = previous.processes
                    fresh = True
                except nvml.NvmlError as exc:
                    error = str(exc)
                pinned = (
                    facts.power is not None
                    and (facts.power.control == "on" or assessment.forced_mode is not None or bool(facts.external_on_nvidia))
                )
                self._schedule_next(self.sample if fresh else None, mode, pinned, interval, now)

            power = facts.power
            if fresh and self.sample:
                self.history.append((now, self.sample.power_draw, self.sample.util_gpu))
            elif power is not None and power.powered_down:
                self.history.append((now, None, None))

            processes = self._processes(facts, fresh) if want_processes else []
            return Snapshot(
                time=now,
                facts=facts,
                assessment=assessment,
                mode=mode,
                supply=supply,
                sample=self.sample,
                sample_fresh=fresh,
                sensor_note=None if fresh else note,
                nvml_error=error,
                processes=processes,
                charge=charge_info,
                cpu=cpu_info,
                fans=fan_info,
                idle=idle_info,
                history=list(self.history),
            )

    def _processes(self, facts: Facts, fresh: bool) -> list[GpuProcess]:
        own = os.getpid()
        holders: dict[int, DeviceHolder] = device_holders(facts.gpu_nodes) if facts.gpu else {}
        nvml_procs = {p.pid: p for p in self.sample.processes} if (self.sample and fresh) else {}
        result = []
        for pid in sorted(set(holders) | set(nvml_procs)):
            name, command = process_identity(pid)
            sample = nvml_procs.get(pid)
            if sample is not None:
                kind = _KIND.get(sample.kind, sample.kind)
            else:
                holder = holders[pid]
                kind = "Driver open (idle)" if holder.uses_nvidia_driver else "Display node open"
            if sample is None and not holders[pid].uses_nvidia_driver:
                # Only a DRM card/render handle: not an NVIDIA client.
                continue
            result.append(
                GpuProcess(
                    pid=pid,
                    name=name if name != str(pid) else (sample.command if sample else name),
                    command=command,
                    kind=kind,
                    vram_mb=sample.vram_mb if sample else None,
                    sm=sample.sm if sample else None,
                    from_nvml=sample is not None,
                    is_self=pid == own,
                )
            )
        result.sort(key=lambda p: (-(p.vram_mb or 0), p.name.lower()))
        return result
