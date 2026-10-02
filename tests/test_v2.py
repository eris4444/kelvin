"""Kelvin 2.0 modules: CPU, fans, idle."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kelvin import cpu, fans, idle, paths  # noqa: E402


def w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")


class CpuTests(unittest.TestCase):
    def test_hybrid_topology_temps_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sys_, proc = root / "sys", root / "proc"
            hw = sys_ / "class/hwmon/hwmon7"
            w(hw / "name", "coretemp")
            for i, (label, val) in enumerate((("Package id 0", 61000), ("Core 0", 58000), ("Core 24", 52000)), 1):
                w(hw / f"temp{i}_label", label)
                w(hw / f"temp{i}_input", str(val))
            w(hw / "temp1_max", "100000")
            w(hw / "temp1_crit", "100000")
            w(sys_ / "devices/cpu_core/cpus", "0-1")
            w(sys_ / "devices/cpu_atom/cpus", "2")
            for n, core in ((0, 0), (1, 0), (2, 24)):
                w(sys_ / f"devices/system/cpu/cpu{n}/topology/core_id", str(core))
                w(sys_ / f"devices/system/cpu/cpu{n}/cpufreq/scaling_cur_freq", "2000000")
            w(proc / "cpuinfo", "model name\t: 12th Gen Intel(R) Core(TM) i7-12700H")
            stat = proc / "stat"
            w(stat, "cpu 0 0 0 300 0 0 0 0\ncpu0 0 0 0 100 0 0 0 0\ncpu1 0 0 0 100 0 0 0 0\ncpu2 0 0 0 100 0 0 0 0\nintr 0")
            with mock.patch.object(paths, "SYSFS", sys_), mock.patch.object(paths, "PROCFS", proc):
                m = cpu.CpuMonitor()
                w(stat, "cpu 150 0 0 450 0 0 0 0\ncpu0 100 0 0 100 0 0 0 0\ncpu1 50 0 0 150 0 0 0 0\ncpu2 0 0 0 200 0 0 0 0\nintr 0")
                info = m.sample()
        self.assertEqual(info.model, "12th Gen Intel Core i7-12700H")
        self.assertEqual((info.package_temp, info.temp_high), (61.0, 100.0))
        self.assertEqual([(c.kind, c.core_id, c.temp, len(c.threads)) for c in info.cores], [("P", 0, 58.0, 2), ("E", 24, 52.0, 1)])
        self.assertEqual([round(t.usage) for t in info.logical], [100, 50, 0])
        self.assertEqual(info.in_use, 2)
        self.assertEqual(round(info.usage), 50)


class FanTests(unittest.TestCase):
    def test_presets_are_safe(self):
        for name, curve in fans.PRESETS.items():
            self.assertIsNone(fans.validate_curve(curve), name)

    def test_unsafe_curves_rejected(self):
        bad = {
            "too few": [(40, 0)] * 3,
            "temps not increasing": [(40, 0), (40, 10), (50, 20), (60, 30), (70, 64), (75, 100), (80, 128), (85, 200)],
            "speed decreases": [(40, 50), (45, 40), (50, 60), (60, 70), (70, 80), (75, 100), (80, 128), (85, 200)],
            "too slow at 70": [(40, 0), (45, 0), (50, 0), (60, 0), (70, 50), (75, 100), (80, 128), (85, 200)],
            "too slow at 80": [(40, 0), (45, 0), (50, 0), (60, 0), (70, 64), (75, 100), (80, 100), (85, 200)],
            "last too hot": [(40, 0), (50, 0), (60, 0), (70, 64), (75, 100), (80, 128), (90, 200), (99, 255)],
        }
        for name, curve in bad.items():
            self.assertIsNotNone(fans.validate_curve(curve), name)

    def test_parse_curve_percent(self):
        curve = fans.parse_curve("45:0,55:20%,62:30%,68:40%,74:55%,80:70%,86:85%,92:100%")
        self.assertEqual(curve[1], (55, 51))
        self.assertEqual(curve[-1], (92, 255))
        self.assertIsNone(fans.validate_curve(curve))
        with self.assertRaises(ValueError):
            fans.parse_curve("45:x")

    def test_needs_apply(self):
        state = fans.FanState("cpu", 0, [(57, 40)] * 8, False)
        self.assertFalse(fans.needs_apply(state, "auto", None))
        self.assertTrue(fans.needs_apply(state, "quiet", None))
        applied = fans.FanState("cpu", 0, fans.PRESETS["quiet"], True)
        self.assertFalse(fans.needs_apply(applied, "quiet", None))
        self.assertTrue(fans.needs_apply(applied, "cool", None))
        self.assertTrue(fans.needs_apply(applied, "auto", None))


class IdleTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(idle.parse_duration("never"), 0)
        self.assertEqual(idle.parse_duration("10 min"), 600)
        self.assertEqual(idle.parse_duration("1h30m"), 5400)
        for bad in ("-5", "abc", "5x", "", "99999999"):
            with self.assertRaises(ValueError):
                idle.parse_duration(bad)
        self.assertEqual(idle.fmt_duration(150), "2 min 30 s")
        self.assertEqual(idle.fmt_duration(0), "Never")

    def test_stage_writes_preserve_shell_config(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}):
            shell = Path(tmp) / "omarchy/shell.json"
            w(shell, json.dumps({"bar": {"layout": {"left": [1]}}, "plugins": [], "idle": {"lock": 300, "screensaver": 150}}))
            r = idle.set_stage("lock", 0)
            self.assertTrue(r.ok, r.message)
            data = json.loads(shell.read_text())
            self.assertEqual(data["idle"], {"lock": idle.NEVER, "screensaver": 150})
            self.assertEqual(data["bar"], {"layout": {"left": [1]}})
            s = idle.read_settings(with_logind=False)
            self.assertEqual((s.lock, s.screensaver), (0, 150))
            self.assertTrue(idle.set_stage("screensaver", 600).ok)
            self.assertEqual(json.loads(shell.read_text())["idle"]["screensaver"], 600)
            self.assertFalse(idle.set_stage("bogus", 60).ok)

    def test_kelvin_stage_installs_plugin(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}):
            w(Path(tmp) / "omarchy/shell.json", json.dumps({"plugins": []}))

            def fake_enable(cmd, **_kw):
                path = Path(tmp) / "omarchy/shell.json"
                data = json.loads(path.read_text())
                data["plugins"].append({"id": "kelvin.idle"})
                path.write_text(json.dumps(data))
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(idle.shutil, "which", return_value="/usr/bin/omarchy"), \
                 mock.patch.object(idle.subprocess, "run", side_effect=fake_enable):
                r = idle.set_stage("sleep_battery", 900)
            self.assertTrue(r.ok, r.message)
            self.assertEqual(json.loads((Path(tmp) / "kelvin/idle.json").read_text())["sleep_battery"], 900)
            link = Path(tmp) / "omarchy/plugins/kelvin.idle"
            self.assertTrue(link.is_symlink() and (link / "manifest.json").exists())
            s = idle.read_settings(with_logind=False)
            self.assertTrue(s.plugin_enabled and s.plugin_installed)
            self.assertEqual(s.sleep_battery, 900)


if __name__ == "__main__":
    unittest.main()
