"""Run the root helpers through pkexec/polkit.

The GUI never runs as root and never sees a password: polkit authorises each
helper invocation (runtime PM is allowed for the active local user; the MUX
switch requires administrator authentication through the desktop's polkit
agent).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths

PKEXEC = shutil.which("pkexec") or "/usr/bin/pkexec"


@dataclass
class HelperResult:
    ok: bool
    code: int
    output: str
    error: str

    @property
    def message(self) -> str:
        if self.ok:
            return self.output.strip()
        return self.error.strip() or f"failed with exit code {self.code}"


def _missing(helper: Path) -> HelperResult | None:
    if not helper.is_file():
        return HelperResult(False, -1, "", f"The GPU-S helper {helper} is not installed. Run the GPU-S installer.")
    if not paths.POLKIT_POLICY.is_file():
        return HelperResult(False, -1, "", "The GPU-S polkit policy is not installed. Run the GPU-S installer.")
    return None


def _interpret(code: int, out: str, err: str) -> HelperResult:
    if code in (126, 127) and "gpu-s-" not in err:
        # pkexec: 126 = dialog dismissed, 127 = not authorised / no agent
        reason = "Authorization was cancelled." if code == 126 else "Authorization was denied or no polkit agent is available."
        return HelperResult(False, code, out, reason)
    # Strip the helper's name prefix from its own error messages.
    err = "\n".join(line.split(": ", 1)[1] if line.startswith("gpu-s-") and ": " in line else line for line in err.splitlines())
    return HelperResult(code == 0, code, out, err)


def run_sync(helper: Path, *args: str, timeout: float = 120) -> HelperResult:
    missing = _missing(helper)
    if missing:
        return missing
    try:
        proc = subprocess.run([PKEXEC, str(helper), *args], capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return HelperResult(False, -1, "", "pkexec (polkit) is not installed.")
    except subprocess.TimeoutExpired:
        return HelperResult(False, -1, "", "Timed out waiting for authorization.")
    return _interpret(proc.returncode, proc.stdout, proc.stderr)


def run_async(helper: Path, args: list[str], callback: Callable[[HelperResult], None]) -> None:
    """GLib-friendly variant: never blocks the UI while polkit asks for auth."""
    from gi.repository import Gio, GLib

    missing = _missing(helper)
    if missing:
        GLib.idle_add(lambda: callback(missing) and False)
        return
    try:
        proc = Gio.Subprocess.new(
            [PKEXEC, str(helper), *args],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        )
    except GLib.Error as exc:
        result = HelperResult(False, -1, "", exc.message)
        GLib.idle_add(lambda: callback(result) and False)
        return

    def done(p, res):
        try:
            _, out, err = p.communicate_utf8_finish(res)
        except GLib.Error as exc:
            callback(HelperResult(False, -1, "", exc.message))
            return
        callback(_interpret(p.get_exit_status(), out or "", err or ""))

    proc.communicate_utf8_async(None, None, done)
