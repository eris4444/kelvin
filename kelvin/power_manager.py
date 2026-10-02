"""Apply GPU modes and MUX changes, verifying the result against the kernel."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import paths, privilege
from .config import State, load_state, save_state
from .hardware import Facts, gather_facts, power_state
from .policy import Assessment, Mode, assess


@dataclass
class ApplyResult:
    ok: bool
    changed: bool  # the kernel state or the active mode actually changed
    mode: Mode | None
    message: str


@dataclass
class Plan:
    ok: bool
    helper_arg: str | None  # "on" / "auto" when a kernel change is needed
    message: str


def plan(mode: Mode, facts: Facts, assessment: Assessment) -> Plan:
    if not assessment.gpu_present:
        return Plan(False, None, "No NVIDIA GPU was detected.")
    if not assessment.supports(mode):
        reason = assessment.unsupported
        text = reason.title if reason else "This mode is not supported here."
        if reason and reason.hint:
            text += " " + reason.hint
        return Plan(False, None, text)
    if assessment.forced_mode is mode:
        # e.g. Always On with a Discrete MUX: already true, nothing to write.
        why = assessment.unsupported.title if assessment.unsupported else ""
        return Plan(True, None, f"{mode.label} is already in effect. {why}".strip())
    if not assessment.runtime_pm_available:
        return Plan(False, None, "Runtime GPU power management is not available on this system.")

    wanted = mode.pm_control
    needs_write = facts.power is None or facts.power.control != wanted
    if wanted == "auto":
        needs_write = needs_write or any(p.control == "on" for p in facts.sibling_power.values())
    return Plan(True, wanted if needs_write else None, "")


def _record(state: State, mode: Mode, applied_by: str, on_ac: bool | None, facts: Facts) -> None:
    if state.baseline_pm_control is None and facts.power and facts.power.control:
        state.baseline_pm_control = facts.power.control
    state.applied_mode = mode.value
    state.applied_by = applied_by
    if applied_by == "manual":
        state.manual_mode = mode.value
        state.manual_on_ac = on_ac
    else:
        state.manual_mode = None
        state.manual_on_ac = None


VERIFY_TIMEOUT = 2.0  # waking from D3cold takes a moment


def _check(facts: Facts, wanted: str, final: bool) -> str | None | bool:
    """None = verified, str = failure, True = not settled yet (retry)."""
    if not facts.gpu:
        return "The GPU disappeared."
    power = power_state(facts.gpu)
    if power.control != wanted:
        return f"The kernel reports runtime PM '{power.control}', expected '{wanted}'."
    if wanted == "on" and power.runtime_status != "active":
        return f"Runtime PM is 'on' but the GPU reports '{power.runtime_status}'." if final else True
    return None


def _verify(facts: Facts, wanted: str) -> str | None:
    deadline = time.time() + VERIFY_TIMEOUT
    while True:
        outcome = _check(facts, wanted, time.time() > deadline)
        if outcome is not True:
            return outcome
        time.sleep(0.1)


def _verify_async(facts: Facts, wanted: str, callback: Callable[[str | None], None]) -> None:
    from gi.repository import GLib

    deadline = time.time() + VERIFY_TIMEOUT

    def poll() -> bool:
        outcome = _check(facts, wanted, time.time() > deadline)
        if outcome is True:
            return True
        callback(outcome)
        return False

    if poll():
        GLib.timeout_add(100, poll)


def _describe(mode: Mode, facts_after: Facts) -> str:
    power = facts_after.power
    if mode is Mode.ALWAYS_ON:
        return f"GPU set to Always On (runtime PM 'on', {power.pci_state if power else 'D0'})."
    if power and power.powered_down:
        return f"GPU set to {mode.label}. It is powered down ({power.pci_state})."
    return f"GPU set to {mode.label}. It will power down once idle."


def apply_sync(mode: Mode, applied_by: str, on_ac: bool | None) -> ApplyResult:
    facts = gather_facts()
    assessment = assess(facts)
    p = plan(mode, facts, assessment)
    state = load_state()
    if not p.ok:
        return ApplyResult(False, False, None, p.message)
    previous = state.applied_mode
    if p.helper_arg:
        result = privilege.run_sync(paths.PM_HELPER, p.helper_arg)
        if not result.ok:
            return ApplyResult(False, False, None, result.message)
        problem = _verify(facts, p.helper_arg)
        if problem:
            return ApplyResult(False, True, None, problem)
    _record(state, mode, applied_by, on_ac, facts)
    save_state(state)
    changed = bool(p.helper_arg) or previous != mode.value
    return ApplyResult(True, changed, mode, p.message or _describe(mode, gather_facts()))


def apply_async(mode: Mode, applied_by: str, on_ac: bool | None, callback: Callable[[ApplyResult], None]) -> None:
    from gi.repository import GLib

    facts = gather_facts()
    assessment = assess(facts)
    p = plan(mode, facts, assessment)
    if not p.ok:
        result = ApplyResult(False, False, None, p.message)
        GLib.idle_add(lambda: callback(result) and False)
        return

    def finish(helper_ran: bool) -> None:
        state = load_state()
        previous = state.applied_mode
        _record(state, mode, applied_by, on_ac, facts)
        save_state(state)
        changed = helper_ran or previous != mode.value
        callback(ApplyResult(True, changed, mode, p.message or _describe(mode, gather_facts())))

    if not p.helper_arg:
        GLib.idle_add(lambda: finish(False) and False)
        return

    def verified(problem: str | None) -> None:
        if problem:
            callback(ApplyResult(False, True, None, problem))
        else:
            finish(True)

    def done(result: privilege.HelperResult) -> None:
        if not result.ok:
            callback(ApplyResult(False, False, None, result.message))
            return
        _verify_async(facts, p.helper_arg, verified)

    privilege.run_async(paths.PM_HELPER, [p.helper_arg], done)


def record_baseline() -> None:
    """Remember how the system was before Kelvin changed anything (for `kelvin reset`)."""
    state = load_state()
    if state.baseline_mux is not None and state.baseline_pm_control is not None:
        return
    facts = gather_facts()
    changed = False
    if state.baseline_mux is None and facts.mux.available and not facts.mux_change_pending:
        state.baseline_mux = facts.mux.configured_mode
        changed = True
    if state.baseline_pm_control is None and facts.power and facts.power.control:
        state.baseline_pm_control = facts.power.control
        changed = True
    if changed:
        save_state(state)


def restore_default_sync() -> ApplyResult:
    """Put runtime PM back to the NVIDIA driver default ('auto')."""
    facts = gather_facts()
    if not facts.gpu or not facts.power or facts.power.control is None:
        return ApplyResult(True, False, None, "Nothing to restore.")
    if facts.power.control == "auto":
        return ApplyResult(True, False, None, "Runtime PM is already at the driver default ('auto').")
    result = privilege.run_sync(paths.PM_HELPER, "auto")
    if not result.ok:
        return ApplyResult(False, False, None, result.message)
    return ApplyResult(True, True, None, "Runtime PM restored to the driver default ('auto').")


# -- ASUS MUX ---------------------------------------------------------------


def mux_check(target: str) -> privilege.HelperResult:
    """Validate a MUX switch without root and without writing anything."""
    import subprocess

    helper = paths.MUX_HELPER
    if not helper.is_file():
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "helpers" / "kelvin-mux-helper"
        helper = source if source.is_file() else helper
    try:
        proc = subprocess.run(["/bin/bash", str(helper), f"check-{target}"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return privilege.HelperResult(False, -1, "", str(exc))
    return privilege._interpret(proc.returncode, proc.stdout, proc.stderr)


def mux_switch_sync(target: str) -> privilege.HelperResult:
    return privilege.run_sync(paths.MUX_HELPER, target)


def mux_switch_async(target: str, callback: Callable[[privilege.HelperResult], None]) -> None:
    privilege.run_async(paths.MUX_HELPER, [target], callback)
