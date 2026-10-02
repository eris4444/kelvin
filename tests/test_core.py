"""Unit tests for detection, policy, monitoring gate, parsing and config.

Run: python3 -m unittest discover -s tests -v
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

from fakesys import NV, FakeSystem  # noqa: E402

from kelvin import battery, config, hardware, monitor, nvml, paths, policy, procs  # noqa: E402
from kelvin.policy import Mode  # noqa: E402


class FakeSysTestCase(unittest.TestCase):
    def make(self, **kwargs) -> hardware.Facts:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.fake = FakeSystem(root, **kwargs)
        for attr, value in (("SYSFS", self.fake.sys), ("PROCFS", self.fake.proc)):
            patcher = mock.patch.object(paths, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root / "run"), "HYPRLAND_INSTANCE_SIGNATURE": ""})
        env.start()
        self.addCleanup(env.stop)
        hardware.pci_ids_name.cache_clear()
        return hardware.gather_facts()


class DetectionTests(FakeSysTestCase):
    def test_discrete_wiring(self):
        facts = self.make(mode="discrete")
        self.assertEqual(facts.gpu.address, NV)
        self.assertEqual(facts.gpu.driver, "nvidia")
        self.assertEqual(facts.igpu.vendor_name, "Intel")
        self.assertTrue(facts.panel_on_nvidia)
        self.assertEqual(facts.session.renderer_address, NV)
        self.assertEqual(facts.session.renderer_source, "internal-panel")
        self.assertEqual(facts.effective_mux_mode, "discrete")
        self.assertFalse(facts.mux_change_pending)
        self.assertEqual(facts.driver_version, "610.57.04")
        self.assertEqual(facts.product, "ASUS TUF Gaming F15 FX507ZC4")
        self.assertEqual([s.address for s in facts.siblings], ["0000:01:00.1"])
        self.assertEqual(facts.gpu_nodes, {"/dev/dri/card1", "/dev/dri/renderD128"})
        self.assertTrue(facts.rtd3.fine_grained)

    def test_hybrid_wiring_and_suspend(self):
        facts = self.make(mode="hybrid", runtime_status="suspended")
        self.assertFalse(facts.panel_on_nvidia)
        self.assertFalse(facts.session_on_nvidia)
        self.assertEqual(facts.effective_mux_mode, "hybrid")
        self.assertTrue(facts.power.powered_down)
        self.assertEqual(facts.rtd3.video_memory, "Off")
        self.assertAlmostEqual(facts.power.suspended_fraction, 0.1)

    def test_pending_mux_change(self):
        facts = self.make(mode="discrete", mux="hybrid")
        self.assertEqual(facts.mux.configured_mode, "hybrid")
        self.assertTrue(facts.mux.pending_reboot)
        self.assertTrue(facts.mux_change_pending)

    def test_hyprland_log_renderer(self):
        facts = self.make(mode="hybrid")
        log_dir = Path(self._tmp.name) / "run/hypr/sig"
        log_dir.mkdir(parents=True)
        (log_dir / "hyprland.log").write_text("DEBUG from aquamarine ]: drm: gpu /dev/dri/card1 becomes primary drm\n")
        with mock.patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "sig"}):
            facts = hardware.gather_facts()
        # Panel on Intel but Hyprland chose the NVIDIA card: the log wins.
        self.assertEqual(facts.session.renderer_source, "hyprland-log")
        self.assertTrue(facts.session_on_nvidia)


class PolicyTests(FakeSysTestCase):
    def test_discrete_forces_always_on(self):
        a = policy.assess(self.make(mode="discrete"))
        self.assertTrue(a.supports(Mode.ALWAYS_ON))
        self.assertFalse(a.supports(Mode.AUTO))
        self.assertFalse(a.supports(Mode.POWER_SAVING))
        self.assertIs(a.forced_mode, Mode.ALWAYS_ON)
        codes = {b.code for b in a.blockers}
        self.assertEqual(codes, {"panel-on-nvidia", "session-on-nvidia"})
        self.assertFalse(a.can_power_down_now)

    def test_hybrid_supports_everything(self):
        a = policy.assess(self.make(mode="hybrid"))
        for mode in Mode:
            self.assertTrue(a.supports(mode), mode)
        self.assertIsNone(a.forced_mode)
        self.assertTrue(a.can_power_down_now)

    def test_external_display_blocks_power_down(self):
        a = policy.assess(self.make(mode="hybrid", hdmi_connected=True))
        self.assertTrue(a.dynamic_modes_possible)
        self.assertFalse(a.can_power_down_now)
        self.assertEqual(a.blockers[0].code, "external-display")
        self.assertIn("HDMI-A-1", a.blockers[0].title)

    def test_pinned_audio_function_blocks(self):
        a = policy.assess(self.make(mode="hybrid", audio_control="on"))
        self.assertIn("sibling-pinned", {b.code for b in a.blockers})

    def test_no_rtd3(self):
        a = policy.assess(self.make(mode="hybrid", rtd3="Disabled by default"))
        self.assertFalse(a.runtime_pm_available)
        self.assertFalse(a.supports(Mode.AUTO))
        self.assertIn("not available", a.unsupported.title)

    def test_effective_mode(self):
        facts = self.make(mode="hybrid", control="on")
        a = policy.assess(facts)
        self.assertIs(policy.effective_mode(facts, a, "power-saving"), Mode.ALWAYS_ON)
        facts = self.make(mode="hybrid", control="auto")
        a = policy.assess(facts)
        self.assertIs(policy.effective_mode(facts, a, "power-saving"), Mode.POWER_SAVING)
        self.assertIs(policy.effective_mode(facts, a, "always-on"), Mode.AUTO)
        facts = self.make(mode="discrete", control="auto")
        self.assertIs(policy.effective_mode(facts, policy.assess(facts), "auto"), Mode.ALWAYS_ON)

    def test_plan_writes_only_when_needed(self):
        from kelvin import power_manager

        facts = self.make(mode="hybrid", control="auto")
        a = policy.assess(facts)
        self.assertIsNone(power_manager.plan(Mode.AUTO, facts, a).helper_arg)
        self.assertEqual(power_manager.plan(Mode.ALWAYS_ON, facts, a).helper_arg, "on")
        facts = self.make(mode="hybrid", control="auto", audio_control="on")
        self.assertEqual(power_manager.plan(Mode.POWER_SAVING, facts, policy.assess(facts)).helper_arg, "auto")
        facts = self.make(mode="discrete")
        a = policy.assess(facts)
        p = power_manager.plan(Mode.POWER_SAVING, facts, a)
        self.assertFalse(p.ok)
        self.assertIn("Hybrid", p.message)
        p = power_manager.plan(Mode.ALWAYS_ON, facts, a)
        self.assertTrue(p.ok)
        self.assertIsNone(p.helper_arg)


class TargetModeTests(unittest.TestCase):
    def test_profiles(self):
        t = policy.target_mode
        self.assertEqual(t("always-on", "power-saving", True, None, None, True), (Mode.ALWAYS_ON, "ac-profile"))
        self.assertEqual(t("always-on", "power-saving", False, None, None, True), (Mode.POWER_SAVING, "battery-profile"))

    def test_manual_holds_until_source_changes(self):
        t = policy.target_mode
        self.assertEqual(t("always-on", "power-saving", True, "auto", True, True), (Mode.AUTO, "manual"))
        self.assertEqual(t("always-on", "power-saving", False, "auto", True, True), (Mode.POWER_SAVING, "battery-profile"))

    def test_auto_switch_off(self):
        t = policy.target_mode
        self.assertEqual(t("always-on", "power-saving", False, None, None, False), (None, "none"))
        self.assertEqual(t("always-on", "power-saving", False, "auto", True, False), (Mode.AUTO, "manual"))

    def test_impact_estimate(self):
        self.assertEqual(policy.impact_estimate(8.0, None), "High")
        self.assertEqual(policy.impact_estimate(0.5, None), "Negligible")
        self.assertEqual(policy.impact_estimate(2.0, 20.0), "Low")
        self.assertEqual(policy.impact_estimate(8.0, 20.0), "High")
        self.assertIsNone(policy.impact_estimate(None, 20.0))


class MonitorGateTests(FakeSysTestCase):
    """Kelvin must never be the reason the GPU stays awake."""

    def sample(self, util=0.0):
        return nvml.GpuSample(0, "GPU", "610", 40, 5.0, 80, 95, "P8", util, 0, 400, 4096, 210, 405, "Disabled")

    def test_never_queries_suspended_gpu(self):
        facts = self.make(mode="hybrid", runtime_status="suspended")
        m = monitor.Monitor()
        with mock.patch.object(nvml, "query") as query:
            snap = m.snapshot(None, 2.0, want_supply=False, force=True)
        query.assert_not_called()
        self.assertIn("powered down", snap.sensor_note)
        self.assertIsNone(snap.gpu_power_w)

    def test_backs_off_when_idle_in_auto(self):
        self.make(mode="hybrid", control="auto")
        m = monitor.Monitor()
        with mock.patch.object(nvml, "query", return_value=self.sample(0.0)) as query:
            m.snapshot("auto", 2.0, want_supply=False, want_processes=False)
            m.snapshot("auto", 2.0, want_supply=False, want_processes=False)
        self.assertEqual(query.call_count, 1)  # second call is inside the backoff window
        self.assertGreater(m._next_query - m._awake_since, 2.0)

    def test_power_saving_waits_before_first_read(self):
        self.make(mode="hybrid", control="auto")
        m = monitor.Monitor()
        with mock.patch.object(nvml, "query") as query:
            snap = m.snapshot("power-saving", 2.0, want_supply=False, want_processes=False)
        query.assert_not_called()
        self.assertIn("Power Saving", snap.sensor_note)

    def test_pinned_kelvinamples_every_time(self):
        self.make(mode="discrete")
        m = monitor.Monitor()
        with mock.patch.object(nvml, "query", return_value=self.sample(0.0)) as query:
            for _ in range(3):
                m.snapshot(None, 2.0, want_supply=False, want_processes=False)
        self.assertEqual(query.call_count, 3)

    def test_hidden_window_never_queries(self):
        self.make(mode="discrete")
        m = monitor.Monitor()
        with mock.patch.object(nvml, "query") as query:
            m.snapshot(None, 2.0, want_supply=False, allow_nvml=False)
        query.assert_not_called()


class ParserTests(unittest.TestCase):
    PMON = (
        "# gpu         pid   type     sm    mem    enc    dec    jpg    ofa     fb   ccpm    command \n"
        "# Idx           #    C/G      %      %      %      %      %      %     MB     MB    name \n"
        "    0       1312     G     24      3      -      -      -      -     97      0    Hyprland       \n"
        "    0       1941   C+G      -      -      -      -      -      -      5      0    voxtype-osd-gtk\n"
    )

    def test_pmon(self):
        rows = nvml.parse_pmon(self.PMON)
        self.assertEqual([(r.pid, r.kind, r.sm, r.vram_mb, r.command) for r in rows],
                         [(1312, "G", 24.0, 97.0, "Hyprland"), (1941, "C+G", None, 5.0, "voxtype-osd-gtk")])

    def test_query_line(self):
        line = ("NVIDIA GeForce RTX 3050 Laptop GPU, 610.57.04, 48, 3.16, 80.00, 95.00, P8, 0, 0, 497, 4096, "
                "210, 405, Disabled")
        row = nvml.parse_query(line)
        self.assertEqual(row["pstate"], "P8")
        self.assertEqual(nvml._number(row["power.draw"]), 3.16)
        self.assertIsNone(nvml._number("[N/A]"))
        with self.assertRaises(nvml.NvmlError):
            nvml.parse_query("garbage")

    def test_script_names(self):
        self.assertEqual(procs._script_name(["python3", "-m", "claude_f", "--x"]), "claude_f")
        self.assertEqual(procs._script_name(["python3", "-u", "/usr/bin/kelvin"]), "kelvin")
        self.assertEqual(procs.pretty_name("firefox"), "Firefox")
        self.assertEqual(procs.pretty_name("Xwayland"), "Xwayland")


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": tmp}):
            cfg = config.load_config()  # creates defaults
            self.assertTrue(config.config_path().is_file())
            self.assertEqual((cfg.ac_mode, cfg.battery_mode), ("always-on", "power-saving"))
            config.config_path().write_text('{"ac_mode": "turbo", "refresh_interval": 999, "tray": "yes", "unknown": 1}')
            cfg = config.load_config()
            self.assertEqual(cfg.ac_mode, "always-on")
            self.assertEqual(cfg.refresh_interval, 30.0)
            self.assertIs(cfg.tray, True)
            config.config_path().write_text("{not json")
            self.assertEqual(config.load_config(), config.Config())
            state = config.State(manual_mode="auto", manual_on_ac=True)
            config.save_state(state)
            self.assertEqual(config.load_state(), state)


class BatteryTests(FakeSysTestCase):
    def test_sysfs_ac_detection(self):
        self.make()
        self.assertEqual(battery.ac_online_sysfs(), (True, ["ACAD"]))
        self.fake.set_ac(False)
        self.assertEqual(battery.ac_online_sysfs(), (False, []))


class SessionTuningTests(unittest.TestCase):
    def test_enable_disable_restores_config(self):
        from kelvin import session_tuning

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            hypr = Path(tmp) / "hypr"
            hypr.mkdir()
            original = "require(\"hypr.monitors\")\n-- user stuff\n"
            (hypr / "hyprland.lua").write_text(original)
            ok, _ = session_tuning.enable(validate=False)
            self.assertTrue(ok)
            self.assertTrue(session_tuning.is_enabled())
            self.assertIn('pcall(require, "hypr.kelvin")', (hypr / "hyprland.lua").read_text())
            self.assertTrue(list(hypr.glob("hyprland.lua.bak.kelvin.*")))
            session_tuning.enable(validate=False)  # idempotent
            self.assertEqual((hypr / "hyprland.lua").read_text().count("hypr.kelvin"), 1)
            session_tuning.disable(validate=False)
            self.assertFalse(session_tuning.is_enabled())
            self.assertEqual((hypr / "hyprland.lua").read_text(), original)
            self.assertFalse((hypr / "kelvin.lua").exists())


class TrayTests(unittest.TestCase):
    def test_icon_and_menu(self):
        from kelvin import tray

        status = tray.TrayStatus(state="Active", mode=Mode.ALWAYS_ON, supported={m: m is Mode.ALWAYS_ON for m in Mode}, level="forced")
        data = tray.draw_icon(22, status)
        self.assertEqual(len(data), 22 * 22 * 4)
        self.assertTrue(any(data[i] for i in range(0, len(data), 4)))  # some opaque pixels

        from gi.repository import GLib

        icon = tray.TrayIcon.__new__(tray.TrayIcon)
        icon._status = status
        icon._revision = 1
        items = icon._items()
        layout = GLib.Variant("(u(ia{sv}av))", (1, icon._layout(items, 0, -1)))
        revision, (root, _props, children) = layout.unpack()
        self.assertEqual(root, 0)
        self.assertEqual(len(children), 10)
        radios = {i: items[i][0] for i in (41, 42, 43)}
        self.assertEqual(radios[42]["toggle-state"].unpack(), 1)
        self.assertFalse(radios[41]["enabled"].unpack())


class ThemeTests(unittest.TestCase):
    def test_palette_contrast(self):
        from kelvin.ui import theme

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "theme"
            d.mkdir()
            (d / "colors.toml").write_text('accent = "#BE3F50"\nforeground = "#14B9B5"\nbackground = "#0e091d"\n')
            (Path(tmp) / "theme.name").write_text("aetheria\n")
            p = theme.load_palette(d)
            self.assertEqual(p.name, "aetheria")
            self.assertFalse(p.light)
            self.assertGreaterEqual(theme.contrast(p.text, p.background), 7.0)
            self.assertGreaterEqual(theme.contrast(p.accent_text, p.background), 4.5)
            css = theme.palette_css(p, True, False)
            self.assertIn("--accent-bg-color: #be3f50;", css)


if __name__ == "__main__":
    unittest.main()
