"""Find processes holding NVIDIA device nodes by scanning /proc/*/fd.

This never talks to the GPU, so it works (and stays accurate) while the GPU
is powered down. Only processes of the current user are visible, which covers
the compositor and desktop apps.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import paths
from .sysfs import read

_NVIDIA_NODE = re.compile(r"^/dev/nvidia(\d+|ctl|-modeset|-uvm|-uvm-tools)$")


@dataclass
class DeviceHolder:
    pid: int
    nodes: set[str]

    @property
    def uses_nvidia_driver(self) -> bool:
        """Has the NVIDIA userspace driver open (GL/Vulkan/CUDA/NVML client)."""
        return any(re.fullmatch(r"/dev/nvidia\d+", n) for n in self.nodes)


def device_holders(dri_nodes: set[str], exclude: set[int] | None = None) -> dict[int, DeviceHolder]:
    exclude = exclude or set()
    holders: dict[int, DeviceHolder] = {}
    proc = str(paths.PROCFS)
    try:
        pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return holders
    for pid in pids:
        if pid in exclude:
            continue
        fd_dir = f"{proc}/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        found: set[str] = set()
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target.startswith("/dev/") and (target in dri_nodes or _NVIDIA_NODE.match(target)):
                found.add(target)
        if found:
            holders[pid] = DeviceHolder(pid, found)
    return holders


_INTERPRETERS = re.compile(r"^(python[\d.]*|node|perl|ruby|java|bash|sh|gjs)$")


def _script_name(argv: list[str]) -> str | None:
    """For `python3 -m pkg` / `node app.js`, the module or script is the real app."""
    args = argv[1:]
    while args:
        arg = args.pop(0)
        if arg == "-m" and args:
            return args[0].split(".")[0]
        if not arg.startswith("-"):
            return os.path.splitext(os.path.basename(arg))[0]
    return None


def process_identity(pid: int) -> tuple[str, str]:
    """Return (display name, command line) for a pid."""
    comm = read(paths.PROCFS / str(pid) / "comm") or ""
    argv: list[str] = []
    try:
        raw = (paths.PROCFS / str(pid) / "cmdline").read_bytes()
        argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    except OSError:
        pass
    exe = argv[0] if argv else ""
    base = os.path.basename(exe) if exe else ""
    # comm is truncated to 15 chars; prefer the executable's basename.
    name = base if base and (not comm or base.startswith(comm[:15])) else comm or base or str(pid)
    if _INTERPRETERS.match(name):
        name = _script_name(argv) or name
    return pretty_name(name), " ".join(argv) or comm


def pretty_name(name: str) -> str:
    if name and name.islower() and name.isalpha():
        return name[0].upper() + name[1:]
    return name
