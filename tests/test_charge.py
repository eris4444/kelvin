"""Battery charge-limit logic: validation, presets, planning, transitions."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpu_s import charge  # noqa: E402
from gpu_s.agent import charge_transition  # noqa: E402


def info(**kw) -> charge.ChargeInfo:
    base = dict(battery="BAT1", upower_path="/x", kernel_end=80, start_supported=False, upower_supported=True,
                upower_enabled=True, upower_start=75, upower_end=80)
    base.update(kw)
    return charge.ChargeInfo(**base)


class ValidationTests(unittest.TestCase):
    def test_range(self):
        i = info()
        self.assertIsNone(charge.validate(i, 80))
        self.assertIsNone(charge.validate(i, 100))
        self.assertIsNotNone(charge.validate(i, 49))
        self.assertIsNotNone(charge.validate(i, 101))

    def test_start_requires_support_and_order(self):
        self.assertIn("end threshold", charge.validate(info(), 80, 75))
        both = info(start_supported=True)
        self.assertIsNone(charge.validate(both, 80, 75))
        self.assertIsNotNone(charge.validate(both, 80, 80))
        self.assertIsNotNone(charge.validate(both, 80, 90))

    def test_unsupported_and_conflicts(self):
        self.assertIsNotNone(charge.validate(info(kernel_end=None), 80))
        self.assertIsNotNone(charge.validate(info(upower_supported=False), 80))
        self.assertIn("will not override", charge.validate(info(foreign_overrides=["/etc/udev/hwdb.d/x.hwdb"]), 80))


class PresetTests(unittest.TestCase):
    def test_end_only_battery(self):
        i = info()
        status = {p.key: charge.preset_status(i, p) for p in charge.PRESETS}
        self.assertTrue(status["care"][0])
        self.assertFalse(status["balanced"][0])  # identical end, needs a start threshold
        self.assertTrue(status["maximum"][0])
        self.assertEqual(charge.matching_preset(i).key, "care")
        self.assertEqual(charge.matching_preset(info(kernel_end=100)).key, "maximum")
        self.assertIsNone(charge.matching_preset(info(kernel_end=85)))

    def test_start_capable_battery(self):
        i = info(start_supported=True, kernel_start=50, kernel_end=80)
        self.assertTrue(charge.preset_status(i, charge.PRESETS[1])[0])
        self.assertEqual(charge.matching_preset(i).key, "balanced")


class PlanTests(unittest.TestCase):
    def test_root_only_when_stored_value_changes(self):
        self.assertEqual(charge._plan(info(upower_end=80), 80, None), (None, True))
        self.assertEqual(charge._plan(info(upower_end=80), 85, None), (["set", "BAT1", "_", "85"], True))
        self.assertEqual(charge._plan(info(), 100, None), (None, False))

    def test_read_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".hwdb") as f:
            f.write("battery:BAT1:A32-K55:dmi:*\n CHARGE_LIMIT=_,85\n")
            f.flush()
            self.assertEqual(charge.read_override(Path(f.name)), ("_", 85))
        self.assertIsNone(charge.read_override(Path("/nonexistent")))

    def test_apply_sync_rejects_without_touching_anything(self):
        with mock.patch.object(charge, "read_charge", return_value=info()), \
             mock.patch.object(charge.privilege, "run_sync") as run:
            result = charge.apply_sync(30)
        self.assertFalse(result.ok)
        run.assert_not_called()


class TransitionTests(unittest.TestCase):
    def test_transitions(self):
        self.assertEqual(charge_transition(1, 5, True), "stopped")
        self.assertEqual(charge_transition(1, 4, True), "stopped")
        self.assertEqual(charge_transition(5, 1, True), "resumed")
        self.assertIsNone(charge_transition(1, 2, False))  # unplugged: not a limit event
        self.assertIsNone(charge_transition(5, 5, True))
        self.assertIsNone(charge_transition(None, 1, True))


if __name__ == "__main__":
    unittest.main()
