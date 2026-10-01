"""End-to-end profile switching: UPower signal -> agent -> apply -> verify -> notify.

Runs the real ProfileAgent in a GLib main loop against a fake Hybrid-mode
sysfs. Only the root helper (simulated by writing the fake power/control)
and the notification daemon are mocked.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gi.repository import GLib  # noqa: E402

from fakesys import NV, FakeSystem  # noqa: E402

from gpu_s import agent, config, hardware, paths, privilege  # noqa: E402
from gpu_s.battery import PowerSupply  # noqa: E402
from gpu_s.policy import Mode  # noqa: E402


class AgentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.fake = FakeSystem(root, mode="hybrid", control="on")
        self.control = self.fake.sys / "bus/pci/devices" / NV / "power/control"
        for target, attr, value in (
            (paths, "SYSFS", self.fake.sys),
            (paths, "PROCFS", self.fake.proc),
            (paths, "PM_HELPER", root / "helper"),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"), "XDG_RUNTIME_DIR": str(root / "run")})
        env.start()
        self.addCleanup(env.stop)
        hardware.pci_ids_name.cache_clear()

        self.helper_calls: list[list[str]] = []

        def fake_helper(helper, args, callback):
            self.helper_calls.append(args)
            self.control.write_text(args[0] + "\n")
            GLib.idle_add(lambda: callback(privilege.HelperResult(True, 0, "ok", "")) and False)

        p = mock.patch.object(privilege, "run_async", side_effect=fake_helper)
        p.start()
        self.addCleanup(p.stop)

        self.notes: list[str] = []
        p = mock.patch.object(agent.notify, "send", side_effect=lambda s, b="", **k: self.notes.append(b))
        p.start()
        self.addCleanup(p.stop)

        self.on_ac = True
        p = mock.patch.object(agent, "read_power_supply", side_effect=lambda: PowerSupply(self.on_ac, [], None, "upower"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(agent, "DEBOUNCE_MS", 50)
        p.start()
        self.addCleanup(p.stop)

        self.cfg = config.Config(ac_mode="always-on", battery_mode="power-saving", notifications=True)
        self.loop = GLib.MainLoop()
        self.changes: list[str | None] = []

        def on_change(message):
            self.changes.append(message)
            self.loop.quit()

        self.agent = agent.ProfileAgent(lambda: self.cfg, on_change)
        self.agent.on_ac = True

    def run_loop(self):
        GLib.timeout_add(3000, self.loop.quit)
        self.loop.run()

    def upower_signal(self, on_battery: bool):
        self.on_ac = not on_battery
        params = GLib.Variant("(sa{sv}as)", ("org.freedesktop.UPower", {"OnBattery": GLib.Variant("b", on_battery)}, []))
        self.agent._on_upower(None, None, None, None, None, params)
        self.run_loop()

    def test_unplug_and_replug(self):
        self.upower_signal(on_battery=True)
        self.assertEqual(self.control.read_text().strip(), "auto")
        self.assertEqual(self.helper_calls, [["auto"]])
        self.assertEqual(self.notes, ["Running on battery.\nSwitched to Battery Power Saving mode."])
        state = config.load_state()
        self.assertEqual((state.applied_mode, state.applied_by), ("power-saving", "battery-profile"))

        self.upower_signal(on_battery=False)
        self.assertEqual(self.control.read_text().strip(), "on")
        self.assertEqual(self.notes[-1], "AC power detected.\nGPU switched to Always On.")

    def test_no_notification_without_change(self):
        # Already Always On while on AC: re-evaluating must not write or notify.
        state = config.load_state()
        state.applied_mode, state.applied_by = "always-on", "ac-profile"
        config.save_state(state)
        self.agent.evaluate("startup")
        self.run_loop()
        self.assertEqual(self.helper_calls, [])
        self.assertEqual(self.notes, [])

    def test_manual_override_cleared_by_source_change(self):
        state = config.load_state()
        state.manual_mode, state.manual_on_ac, state.applied_mode, state.applied_by = "auto", True, "auto", "manual"
        config.save_state(state)
        self.control.write_text("auto\n")
        self.agent.evaluate("startup")  # manual choice holds on the same source
        self.run_loop()
        self.assertEqual(self.helper_calls, [])
        self.upower_signal(on_battery=True)
        self.assertIsNone(config.load_state().manual_mode)
        self.assertEqual(config.load_state().applied_mode, Mode.POWER_SAVING.value)

    def test_auto_switch_disabled(self):
        self.cfg.auto_switch = False
        self.upower_signal(on_battery=True)
        self.assertEqual(self.helper_calls, [])
        self.assertEqual(self.control.read_text().strip(), "on")


if __name__ == "__main__":
    unittest.main()
