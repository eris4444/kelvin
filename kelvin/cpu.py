"""CPU temperature, per-core usage and frequency.

Sources: coretemp hwmon (package + per-physical-core temperatures),
/proc/stat (per-logical-CPU usage, computed between two samples),
cpufreq scaling_cur_freq, and /sys/devices/cpu_{core,atom}/cpus for Intel
hybrid (P-core / E-core) topology.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .sysfs import read, read_int

BUSY_THRESHOLD = 5.0  # % usage above which a logical CPU counts as "in use"


@dataclass
class LogicalCpu:
    cpu: int
    core_id: int
    kind: str | None  # "P", "E" or None (non-hybrid CPU)
    usage: float | None  # % over the last interval
    freq_mhz: float | None


@dataclass
class PhysicalCore:
    core_id: int
    kind: str | None
    temp: float | None
    threads: list[LogicalCpu] = field(default_factory=list)

    @property
    def usage(self) -> float | None:
        values = [t.usage for t in self.threads if t.usage is not None]
        return max(values) if values else None

    @property
    def freq_mhz(self) -> float | None:
        values = [t.freq_mhz for t in self.threads if t.freq_mhz]
        return max(values) if values else None


@dataclass
class CpuInfo:
    model: str | None
    package_temp: float | None
    temp_high: float | None  # coretemp "max" (throttle point)
    temp_crit: float | None
    usage: float | None  # whole-CPU %
    cores: list[PhysicalCore]
    logical: list[LogicalCpu]
    timestamp: float

    @property
    def in_use(self) -> int:
        return sum(1 for c in self.logical if c.usage is not None and c.usage >= BUSY_THRESHOLD)

    @property
    def hottest_core(self) -> PhysicalCore | None:
        cores = [c for c in self.cores if c.temp is not None]
        return max(cores, key=lambda c: c.temp) if cores else None

    @property
    def avg_freq_mhz(self) -> float | None:
        values = [c.freq_mhz for c in self.logical if c.freq_mhz]
        return sum(values) / len(values) if values else None


def _cpu_list(text: str | None) -> set[int]:
    cpus: set[int] = set()
    for part in (text or "").split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            if lo.isdigit() and hi.isdigit():
                cpus.update(range(int(lo), int(hi) + 1))
        elif part.strip().isdigit():
            cpus.add(int(part))
    return cpus


def _coretemp() -> Path | None:
    root = paths.SYSFS / "class/hwmon"
    try:
        for hwmon in sorted(root.iterdir()):
            if read(hwmon / "name") in ("coretemp", "k10temp", "zenpower"):
                return hwmon
    except OSError:
        pass
    return None


def read_temps() -> tuple[float | None, float | None, float | None, dict[int, float]]:
    """(package °C, high °C, critical °C, {core_id: °C})."""
    hwmon = _coretemp()
    if hwmon is None:
        return None, None, None, {}
    package = high = crit = None
    cores: dict[int, float] = {}
    for label_file in hwmon.glob("temp*_label"):
        base = str(label_file)[: -len("_label")]
        label = read(label_file) or ""
        value = read_int(base + "_input")
        if value is None:
            continue
        celsius = value / 1000
        match = re.fullmatch(r"Core (\d+)", label)
        if match:
            cores[int(match.group(1))] = celsius
        elif label.startswith(("Package id", "Tctl", "Tdie")):
            package = celsius
            high = (read_int(base + "_max") or 0) / 1000 or None
            crit = (read_int(base + "_crit") or 0) / 1000 or None
    return package, high, crit, cores


def _proc_stat() -> dict[int | None, tuple[int, int]]:
    """{cpu index (None = total): (busy jiffies, total jiffies)}."""
    out: dict[int | None, tuple[int, int]] = {}
    try:
        lines = (paths.PROCFS / "stat").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.startswith("cpu"):
            break
        name, *values = line.split()
        nums = [int(v) for v in values[:8]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        key = None if name == "cpu" else int(name[3:])
        out[key] = (total - idle, total)
    return out


def _model() -> str | None:
    try:
        for line in (paths.PROCFS / "cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                name = line.split(":", 1)[1].strip()
                return re.sub(r"\s+", " ", name.replace("(R)", "").replace("(TM)", ""))
    except OSError:
        pass
    return None


class CpuMonitor:
    """Usage needs two /proc/stat samples; keep the previous one between calls."""

    def __init__(self) -> None:
        self._prev = _proc_stat()
        self._model = _model()
        devices = paths.SYSFS / "devices"
        self._p = _cpu_list(read(devices / "cpu_core/cpus"))
        self._e = _cpu_list(read(devices / "cpu_atom/cpus"))

    def _kind(self, cpu: int) -> str | None:
        if cpu in self._p:
            return "P"
        if cpu in self._e:
            return "E"
        return None

    def sample(self) -> CpuInfo:
        now = _proc_stat()
        prev, self._prev = self._prev, now

        def usage(key) -> float | None:
            if key not in now or key not in prev:
                return None
            busy = now[key][0] - prev[key][0]
            total = now[key][1] - prev[key][1]
            return max(0.0, min(100.0, 100.0 * busy / total)) if total > 0 else None

        package, high, crit, core_temps = read_temps()
        logical: list[LogicalCpu] = []
        cpu_root = paths.SYSFS / "devices/system/cpu"
        for key in sorted(k for k in now if k is not None):
            base = cpu_root / f"cpu{key}"
            core_id = read_int(base / "topology/core_id")
            freq = read_int(base / "cpufreq/scaling_cur_freq")
            logical.append(
                LogicalCpu(
                    cpu=key,
                    core_id=core_id if core_id is not None else key,
                    kind=self._kind(key),
                    usage=usage(key),
                    freq_mhz=freq / 1000 if freq else None,
                )
            )
        cores: dict[int, PhysicalCore] = {}
        for lc in logical:
            core = cores.setdefault(lc.core_id, PhysicalCore(lc.core_id, lc.kind, core_temps.get(lc.core_id)))
            core.threads.append(lc)
        ordered = sorted(cores.values(), key=lambda c: (c.kind != "P", c.core_id))
        return CpuInfo(
            model=self._model,
            package_temp=package,
            temp_high=high,
            temp_crit=crit,
            usage=usage(None),
            cores=ordered,
            logical=logical,
            timestamp=time.time(),
        )
