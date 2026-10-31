"""Tests with fake inputs: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import trackpad_edges as te                       # noqa: E402
from evdev import ecodes as e                     # noqa: E402

Ev = namedtuple("Ev", "type code value")
AbsInfo = namedtuple("AbsInfo", "value min max fuzz flat resolution")
DEFAULTS = {name: getattr(te, name) for name in te._CONFIGURABLE + ("SCREEN_NITS",)}


def reset_settings():
    for name, value in DEFAULTS.items():
        setattr(te, name, value)


class Pad:
    """Drives an EdgeSwipe with one finger, like a kernel multitouch pad."""

    def __init__(self, x_range=(0, 1000), y_range=(0, 1000)):
        self.steps = []
        self.det = te.EdgeSwipe(x_range, y_range,
                                lambda zone, d: self.steps.append((zone, d)))

    def frame(self, x, y, down=None, fingers=None):
        evs = [Ev(e.EV_ABS, e.ABS_MT_POSITION_X, x), Ev(e.EV_ABS, e.ABS_MT_POSITION_Y, y)]
        if down is not None:
            evs.append(Ev(e.EV_KEY, e.BTN_TOUCH, 1 if down else 0))
        if fingers is not None:
            evs.append(Ev(e.EV_KEY, e.BTN_TOOL_DOUBLETAP, fingers))
        evs.append(Ev(e.EV_SYN, e.SYN_REPORT, 0))
        for ev in evs:
            self.det.feed(ev)

    def swipe(self, x, y0, y1, steps=50):
        self.frame(x, y0, down=True)
        for i in range(1, steps + 1):
            self.frame(x, round(y0 + (y1 - y0) * i / steps))
        self.frame(x, y1, down=False)
        return self.steps


class SwipeTest(unittest.TestCase):
    def setUp(self):
        reset_settings()

    def test_left_edge_up_is_volume_up(self):
        # 0.6 of the pad = 0.03 activation + 0.57 / 0.018 per step = 31 steps
        steps = Pad().swipe(30, 900, 300, steps=600)
        self.assertEqual(set(steps), {("LEFT", +1)})
        self.assertEqual(len(steps), 31)

    def test_right_edge_down(self):
        steps = Pad().swipe(970, 300, 900)
        self.assertEqual(set(steps), {("RIGHT", -1)})

    def test_feel_unchanged(self):
        p = Pad()
        self.assertAlmostEqual(p.det.left_max, 70)                         # 7% strips
        self.assertAlmostEqual(p.det.right_min, 930)
        self.assertEqual(p.det.top_limit, 200)                             # 20% dead zone
        self.assertEqual((p.det.activation, p.det.sensitivity), (30, 18))  # 3% and 1.8%

    def test_short_travel_below_activation_does_nothing(self):
        self.assertEqual(Pad().swipe(30, 600, 575), [])

    def test_numberpad_corner_touch_is_ignored(self):
        # NumberPad corner swipes start in the top 20%, on either side
        self.assertEqual(Pad().swipe(30, 100, 900), [])
        self.assertEqual(Pad().swipe(970, 50, 800), [])

    def test_middle_of_pad_is_ignored(self):
        self.assertEqual(Pad().swipe(500, 900, 300), [])

    def test_drifting_into_workspace_stops(self):
        p = Pad()
        p.frame(30, 900, down=True)
        for y in range(890, 700, -10):
            p.frame(30, y)
        n = len(p.steps)
        for y in range(700, 300, -10):
            p.frame(300, y)                        # slid sideways into the pad
        self.assertEqual(len(p.steps), n)

    def test_two_fingers_are_ignored(self):
        p = Pad()
        p.frame(30, 900, down=True, fingers=1)
        for y in range(890, 300, -10):
            p.frame(30, y)
        self.assertEqual(p.steps, [])

    def test_axis_not_starting_at_zero(self):
        # e.g. Synaptics: x 1266..5676, y 1096..4758
        p = Pad((1266, 5676), (1096, 4758))
        h = 4758 - 1096
        self.assertAlmostEqual(p.det.left_max, 1266 + 4410 * 0.07)
        self.assertAlmostEqual(p.det.top_limit, 1096 + h * 0.20)
        steps = p.swipe(1300, 1096 + int(h * 0.9), 1096 + int(h * 0.3), steps=2197)
        self.assertEqual(len(steps), 31)
        self.assertEqual(Pad((1266, 5676), (1096, 4758)).swipe(1300, 1096 + int(h * 0.1), 4500), [])


class SameAsOldVersionTest(unittest.TestCase):
    """The committed 1.0 detector and this one give identical steps on a 0-based pad."""

    def test_identical_steps(self):
        import random
        import subprocess
        import importlib.util
        root = Path(te.__file__).parent
        try:
            src = subprocess.run(["git", "-C", str(root), "show", "09cb474:trackpad_edges.py"],
                                 capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git history not available")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "old_te.py")
            path.write_text(src)
            spec = importlib.util.spec_from_file_location("old_te", path)
            old = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old)
        rng = random.Random(1)
        for trial in range(300):
            got = {}
            for name, mod in (("old", old), ("new", te)):
                out = []
                if name == "old":
                    det = mod.EdgeSwipe(1000, 1000, lambda z, d: out.append((z, d)))
                else:
                    det = mod.EdgeSwipe((0, 1000), (0, 1000), lambda z, d: out.append((z, d)))
                r = random.Random(trial)
                x, y = r.choice([r.randint(0, 80), r.randint(920, 1000), r.randint(0, 1000)]), \
                    r.randint(0, 1000)
                evs = [Ev(e.EV_ABS, e.ABS_MT_POSITION_X, x), Ev(e.EV_ABS, e.ABS_MT_POSITION_Y, y),
                       Ev(e.EV_KEY, e.BTN_TOUCH, 1), Ev(e.EV_SYN, e.SYN_REPORT, 0)]
                for _ in range(r.randint(5, 120)):
                    x = max(0, min(1000, x + r.randint(-8, 8)))
                    y = max(0, min(1000, y + r.randint(-25, 25)))
                    evs += [Ev(e.EV_ABS, e.ABS_MT_POSITION_X, x),
                            Ev(e.EV_ABS, e.ABS_MT_POSITION_Y, y), Ev(e.EV_SYN, e.SYN_REPORT, 0)]
                evs += [Ev(e.EV_KEY, e.BTN_TOUCH, 0), Ev(e.EV_SYN, e.SYN_REPORT, 0)]
                for ev in evs:
                    det.feed(ev)
                got[name] = out
            self.assertEqual(got["old"], got["new"], f"trial {trial}")


class TouchpadPickTest(unittest.TestCase):
    def setUp(self):
        reset_settings()

    def caps(self, finger=True, mt=True):
        abs_ = [(e.ABS_X, None), (e.ABS_Y, None)]
        if mt:
            abs_ += [(e.ABS_MT_POSITION_X, None), (e.ABS_MT_POSITION_Y, None)]
        keys = [e.BTN_TOUCH] + ([e.BTN_TOOL_FINGER] if finger else [])
        return {e.EV_ABS: abs_, e.EV_KEY: keys}

    def test_touchpad_by_capability(self):
        self.assertTrue(te.is_touchpad(self.caps(), [e.INPUT_PROP_POINTER]))

    def test_touchscreen_excluded(self):
        self.assertFalse(te.is_touchpad(self.caps(), [e.INPUT_PROP_DIRECT]))

    def test_needs_finger_tool_and_multitouch(self):
        self.assertFalse(te.is_touchpad(self.caps(finger=False), []))
        self.assertFalse(te.is_touchpad(self.caps(mt=False), []))

    def test_rank_prefers_config_then_touchpad_name(self):
        te.TOUCHPAD_NAME = "ASUE140D:00.*Touchpad"
        self.assertEqual(te.touchpad_rank("ASUE140D:00 04F3:31B9 Touchpad"), 0)
        self.assertEqual(te.touchpad_rank("ELAN0001:00 04F3:3140 Touchpad"), 1)
        self.assertEqual(te.touchpad_rank("SYNA7DB5:01 06CB:CD41 Mouse"), 2)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        reset_settings()
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()
        reset_settings()

    def test_example_config_is_the_defaults(self):
        text = (Path(te.__file__).parent / "trackpad-edges.conf.example").read_text()
        settings, nits = te.parse_config(text)
        for name, value in settings.items():
            self.assertEqual(value, DEFAULTS[name], name)
            self.assertIs(type(value), type(DEFAULTS[name]), name)
        self.assertEqual(nits, {"internal": 400, "ARZOPA": 300, "G241VC": 250})

    def test_types_comments_quotes(self):
        settings, nits = te.parse_config(
            'edge_width = 0.1   # wider\nvolume_step = 3\nremember_brightness = no\n'
            'touchpad_name = "ELAN.*Touchpad"\n[nits]\ninternal = 500\n"Optix G241VC" = 260\n')
        self.assertEqual(settings, {"EDGE_WIDTH": 0.1, "VOLUME_STEP": 3,
                                    "REMEMBER_BRIGHTNESS": False,
                                    "TOUCHPAD_NAME": "ELAN.*Touchpad"})
        self.assertEqual(nits, {"internal": 500.0, "Optix G241VC": 260.0})

    def test_bad_values_are_skipped(self):
        with mock.patch.object(te, "log") as log:
            settings, _ = te.parse_config("edge_width = wide\nbogus = 1\nvolume_step = 4\n")
        self.assertEqual(settings, {"VOLUME_STEP": 4})
        self.assertEqual(log.call_count, 2)

    def test_load_config_applies_and_nits_replace(self):
        path = self.tmp / "t.conf"
        path.write_text("sensitivity = 0.02\n[nits]\nARZOPA = 280\n")
        with mock.patch.object(te, "log"):
            te.load_config(str(path))
        self.assertEqual(te.SENSITIVITY, 0.02)
        self.assertEqual(te.SCREEN_NITS, {"internal": 400, "ARZOPA": 280})

    def migrate(self, old_toml=None, old_nits=None):
        old = self.tmp / "config.toml"
        if old_toml is not None:
            old.write_text(old_toml)
        out = self.tmp / "trackpad-edges.conf"
        example = Path(te.__file__).parent / "trackpad-edges.conf.example"
        with mock.patch.object(te, "OLD_CONFIG_FILE", str(old)), \
                mock.patch.object(te.parse_old_config, "__defaults__", (str(old),)):
            te.migrate_config(example, out, old_nits)
        return te.parse_config(out.read_text()), out.read_text()

    def test_migrate_fresh_install_seeds_nits(self):
        (settings, nits), _ = self.migrate()
        self.assertEqual(nits, {"internal": 400, "ARZOPA": 300, "G241VC": 250})

    def test_migrate_carries_old_toml_values(self):
        (settings, nits), text = self.migrate(
            'touchpad_name = "ASUE140D:00.*Touchpad"\nsensitivity = 0.02\n'
            '[screen_nits]\ninternal = 400\n"Optix G241VC" = 260\n"ARZOPA" = 300\n')
        self.assertEqual(settings["TOUCHPAD_NAME"], "ASUE140D:00.*Touchpad")
        self.assertEqual(settings["SENSITIVITY"], 0.02)
        # the old longer name replaces the G241VC seed rather than duplicating it
        self.assertEqual(nits, {"internal": 400, "Optix G241VC": 260, "ARZOPA": 300})
        self.assertIn("sensitivity = 0.02", text)

    def test_migrate_carries_old_script_nits(self):
        (_, nits), _ = self.migrate(old_nits={"internal": 450, "ARZOPA": 320})
        self.assertEqual(nits, {"internal": 450, "ARZOPA": 320, "G241VC": 250})

    def test_script_nits_reads_installed_copy(self):
        script = self.tmp / "old.py"
        script.write_text('X = 1\nSCREEN_NITS = {\n    "internal": 400,\n    "ARZOPA": 300,\n}\n')
        self.assertEqual(te.script_nits(script), {"internal": 400, "ARZOPA": 300})
        self.assertEqual(te.script_nits(self.tmp / "missing.py"), {})


DDCUTIL_DETECT = """Display 1
   I2C bus:  /dev/i2c-4
   DRM connector:           card1-DP-2
   Monitor:                 MSI:Optix G241VC:
Display 2
   I2C bus:  /dev/i2c-7
   DRM connector:           card1-HDMI-A-1
   Monitor:                 ARZ:ARZOPA:

Invalid display
   I2C bus:  /dev/i2c-9
   DRM connector:           card1-DP-3
   EDID synopsis:
      Mfg id:               XXX

Display 3
   I2C bus:  /dev/i2c-2
   DRM connector:           card1-eDP-1
   Monitor:                 SDC:ATNA40YK:
"""


class DdcutilTest(unittest.TestCase):
    def test_parse_detect(self):
        self.assertEqual(te.parse_ddcutil_detect(DDCUTIL_DETECT),
                         [("4", "MSI:Optix G241VC:"), ("7", "ARZ:ARZOPA:")])

    def test_parse_empty(self):
        self.assertEqual(te.parse_ddcutil_detect("No displays found.\n"), [])
        self.assertEqual(te.parse_ddcutil_detect(None), [])

    def test_peak_matching(self):
        reset_settings()
        te.SCREEN_NITS = {"internal": 400, "ARZOPA": 300, "G241VC": 250}
        s = te.Screens(start=False)
        self.assertEqual(s._peak_for("MSI:Optix G241VC:"), 250)
        self.assertEqual(s._peak_for("ARZ:arzopa:"), 300)
        self.assertIsNone(s._peak_for("DEL:U2720Q:"))


class VolumeTest(unittest.TestCase):
    def setUp(self):
        reset_settings()

    def test_wpctl_up_unmutes_and_caps(self):
        self.assertEqual(te.volume_commands("wpctl", 2), [
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
            ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "10%+"]])

    def test_wpctl_down(self):
        self.assertEqual(te.volume_commands("wpctl", -1), [
            ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%-"]])

    def test_pactl_caps_at_100(self):
        self.assertEqual(te.volume_commands("pactl", 3, current=93), [
            ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"]])
        self.assertEqual(te.volume_commands("pactl", -4, current=10)[-1][-1], "0%")

    def test_parse_wpctl(self):
        self.assertEqual(te.parse_wpctl_volume("Volume: 0.45\n"), (45, False))
        self.assertEqual(te.parse_wpctl_volume("Volume: 1.00 [MUTED]\n"), (100, True))
        self.assertIsNone(te.parse_wpctl_volume(None))

    def test_parse_pactl(self):
        vol = ("Volume: front-left: 29491 /  45% / -20.81 dB,   "
               "front-right: 29491 /  45% / -20.81 dB\n        balance 0.00\n")
        self.assertEqual(te.parse_pactl_volume(vol, "Mute: no\n"), (45, False))
        self.assertEqual(te.parse_pactl_volume(vol, "Mute: yes\n"), (45, True))

    def user(self, desktop_proc=None, desktop=""):
        u = te.Session(1000, "ferin", "2", "wayland", desktop)
        te._proc_cache[1000] = (1e18, {desktop_proc} if desktop_proc else set())
        return u

    def which(self, *have):
        return mock.patch.object(te.shutil, "which",
                                 lambda c: f"/usr/bin/{c}" if c in have else None)

    def test_method_per_desktop(self):
        with self.which("wpctl", "pactl"), mock.patch.object(te, "keys_possible", lambda: True):
            self.assertEqual(te.volume_method(self.user("plasmashell")), "kde")
            self.assertEqual(te.volume_method(self.user("gnome-shell")), "keys")
            self.assertEqual(te.volume_method(self.user("niri")), "wpctl")
            self.assertEqual(te.volume_method(self.user(None, "Hyprland")), "wpctl")
            self.assertIsNone(te.volume_method(None))
        with self.which("pactl"):
            self.assertEqual(te.volume_method(self.user("sway")), "pactl")
        with self.which("wpctl"), mock.patch.object(te, "keys_possible", lambda: False):
            self.assertEqual(te.volume_method(self.user("gnome-shell")), "wpctl")

    def test_desktop_names(self):
        for raw, want in (("KDE", "kde"), ("plasma", "kde"), ("ubuntu", "gnome"),
                          ("gnome-xorg", "gnome"), ("X-Cinnamon", "cinnamon"),
                          ("xfce", "xfce"), ("niri", "niri"), ("Hyprland", "hyprland"),
                          ("sway", "sway"), ("", "unknown")):
            self.assertEqual(te.desktop_from_name(raw), want, raw)

    def test_runs_as_session_user_with_env(self):
        with mock.patch.object(te.os.path, "exists", lambda p: True):
            cmd = te.as_user(self.user(), "wpctl", "get-volume")
        self.assertEqual(cmd[:4], ["runuser", "-u", "ferin", "--"])
        self.assertIn("XDG_RUNTIME_DIR=/run/user/1000", cmd)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", cmd)

    def test_apply_on_niri_runs_wpctl_then_popup(self):
        ran = []

        def fake_run(cmd, timeout=5):
            ran.append(cmd[cmd.index("--") + 4:])   # strip runuser/env prefix
            return "Volume: 0.55\n" if "get-volume" in cmd else ""

        u = self.user("niri")
        vol = te.Volume.__new__(te.Volume)
        with self.which("wpctl", "notify-send"), \
                mock.patch.object(te, "logged_in_user", lambda: u), \
                mock.patch.object(te, "_run", fake_run), \
                mock.patch.object(te, "notify") as notify:
            vol.apply(+2)
        self.assertEqual(ran[0], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
        self.assertEqual(ran[1][-1], "10%+")
        notify.assert_called_once_with(u, "volume", 55, False)

    def test_session_from_loginctl(self):
        outputs = {
            "show-seat": "ActiveSession=4\n",
            "show-session": "User=1001\nName=friend\nClass=user\nType=wayland\n"
                            "Desktop=niri\nState=active\n",
        }
        with self.which("loginctl"), mock.patch.object(
                te, "_run", lambda cmd, timeout=5: outputs[cmd[1]]):
            s = te._query_session()
        self.assertEqual((s.uid, s.name, s.sid, s.desktop), (1001, "friend", "4", "niri"))

    def test_greeter_is_login_screen(self):
        outputs = {"show-seat": "ActiveSession=c1\n",
                   "show-session": "User=964\nName=sddm\nClass=greeter\nState=active\n"}
        with self.which("loginctl"), mock.patch.object(
                te, "_run", lambda cmd, timeout=5: outputs[cmd[1]]):
            self.assertIsNone(te._query_session())


class PopupTest(unittest.TestCase):
    def test_notify_command_replaces_and_has_progress(self):
        cmd = te.notify_command("volume", 55, replace_id=17)
        self.assertIn("int:value:55", cmd)
        self.assertIn("string:x-canonical-private-synchronous:trackpad-edges-volume", cmd)
        self.assertIn("string:x-dunst-stack-tag:trackpad-edges-volume", cmd)
        self.assertEqual(cmd[cmd.index("-r") + 1], "17")
        self.assertIn("-p", cmd)
        self.assertEqual(cmd[-2:], ["Volume", "55%"])
        self.assertIn("audio-volume-medium", cmd)

    def test_old_libnotify_gets_no_replace_flags(self):
        cmd = te.notify_command("brightness", 30, replace_id=17, can_replace=False)
        self.assertNotIn("-r", cmd)
        self.assertNotIn("-p", cmd)
        self.assertEqual(cmd[-2:], ["Brightness", "30%"])

    def test_muted_icon(self):
        cmd = te.notify_command("volume", 40, muted=True)
        self.assertIn("audio-volume-muted", cmd)
        self.assertEqual(cmd[-1], "40% (muted)")

    def test_notify_remembers_id_per_user_and_kind(self):
        te._notify.update(replace=True, ids={})
        u = te.Session(1000, "ferin")
        with mock.patch.object(te.shutil, "which", lambda c: "/usr/bin/notify-send"), \
                mock.patch.object(te, "_run", return_value="42\n") as run:
            te.notify(u, "volume", 10)
            te.notify(u, "volume", 15)
        self.assertNotIn("-r", run.call_args_list[0].args[0])
        second = run.call_args_list[1].args[0]
        self.assertEqual(second[second.index("-r") + 1], "42")

    def test_popup_per_desktop(self):
        u = te.Session(1000, "ferin")
        with mock.patch.object(te, "keys_possible", lambda: True):
            for proc, want in (("plasmashell", "Plasma OSD"), ("gnome-shell", "GNOME OSD"),
                               ("niri", "notify-send"), ("cinnamon", "notify-send")):
                te._proc_cache[1000] = (1e18, {proc})
                self.assertEqual(te.brightness_popup(u), want, proc)
        self.assertEqual(te.brightness_popup(None), "none")
        self.assertEqual(te.volume_popup("wpctl"), "notify-send")
        self.assertEqual(te.volume_popup("kde"), "Plasma OSD")


if __name__ == "__main__":
    unittest.main()
