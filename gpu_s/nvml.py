"""Live NVIDIA sensor data through nvidia-smi.

Any NVML query resumes a runtime-suspended GPU, so callers must decide
*whether* to query (see monitor.py); this module only knows *how*.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field

NVIDIA_SMI = shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"

_FIELDS = (
    "name",
    "driver_version",
    "temperature.gpu",
    "power.draw",
    "enforced.power.limit",
    "power.max_limit",
    "pstate",
    "utilization.gpu",
    "utilization.memory",
    "memory.used",
    "memory.total",
    "clocks.gr",
    "clocks.mem",
    "persistence_mode",
)


class NvmlError(RuntimeError):
    pass


@dataclass
class ProcessSample:
    pid: int
    kind: str  # G, C, C+G
    sm: float | None  # % SM utilisation over the sample
    vram_mb: float | None
    command: str


@dataclass
class GpuSample:
    timestamp: float
    name: str | None
    driver: str | None
    temperature: float | None
    power_draw: float | None
    power_limit: float | None
    power_max: float | None
    pstate: str | None
    util_gpu: float | None
    util_mem: float | None
    mem_used: float | None
    mem_total: float | None
    clock_gr: float | None
    clock_mem: float | None
    persistence: str | None
    processes: list[ProcessSample] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        if self.util_gpu and self.util_gpu > 0:
            return True
        return any(p.sm and p.sm > 0 for p in self.processes)


def _value(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.startswith("[") or raw in ("N/A", "Not Supported"):
        return None
    return raw


def _number(raw: str) -> float | None:
    value = _value(raw)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _run(args: list[str], timeout: float) -> str:
    try:
        proc = subprocess.run(
            [NVIDIA_SMI, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NvmlError("nvidia-smi is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise NvmlError("nvidia-smi did not respond") from exc
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip().splitlines()
        raise NvmlError(message[0] if message else f"nvidia-smi exited with {proc.returncode}")
    return proc.stdout


def parse_query(line: str) -> dict[str, str]:
    parts = [p.strip() for p in line.split(", ")]
    if len(parts) != len(_FIELDS):
        raise NvmlError(f"unexpected nvidia-smi output: {line!r}")
    return dict(zip(_FIELDS, parts))


def parse_pmon(text: str) -> list[ProcessSample]:
    """Parse `nvidia-smi pmon -c 1 -s um`, locating columns by header name."""
    header: list[str] | None = None
    samples = []
    for line in text.splitlines():
        if line.startswith("#"):
            if header is None:
                header = line.lstrip("#").split()
            continue
        if not header or not line.strip():
            continue
        cols = line.split()
        if len(cols) < len(header):
            continue
        # The command name is last and may itself contain spaces.
        row = dict(zip(header[:-1], cols[: len(header) - 1]))
        row[header[-1]] = " ".join(cols[len(header) - 1 :])
        try:
            pid = int(row.get("pid", "-"))
        except ValueError:
            continue
        samples.append(
            ProcessSample(
                pid=pid,
                kind=row.get("type", "?"),
                sm=_number(row.get("sm", "-").replace("-", "")),
                vram_mb=_number(row.get("fb", "-").replace("-", "")),
                command=row.get("command", ""),
            )
        )
    return samples


def query(address: str | None = None, *, processes: bool = True, timeout: float = 5.0) -> GpuSample:
    select = ["-i", address] if address else []
    out = _run(
        [*select, f"--query-gpu={','.join(_FIELDS)}", "--format=csv,noheader,nounits"],
        timeout,
    )
    line = out.strip().splitlines()[0] if out.strip() else ""
    row = parse_query(line)
    sample = GpuSample(
        timestamp=time.time(),
        name=_value(row["name"]),
        driver=_value(row["driver_version"]),
        temperature=_number(row["temperature.gpu"]),
        power_draw=_number(row["power.draw"]),
        power_limit=_number(row["enforced.power.limit"]),
        power_max=_number(row["power.max_limit"]),
        pstate=_value(row["pstate"]),
        util_gpu=_number(row["utilization.gpu"]),
        util_mem=_number(row["utilization.memory"]),
        mem_used=_number(row["memory.used"]),
        mem_total=_number(row["memory.total"]),
        clock_gr=_number(row["clocks.gr"]),
        clock_mem=_number(row["clocks.mem"]),
        persistence=_value(row["persistence_mode"]),
    )
    if processes:
        try:
            # pmon only accepts -i after the subcommand.
            sample.processes = parse_pmon(_run(["pmon", *select, "-c", "1", "-s", "um"], timeout))
        except NvmlError:
            sample.processes = []
    return sample
