#!/usr/bin/env python3
"""
Edge-swipe volume/brightness for Linux touchpads (originally an ASUS Zenbook /
Vivobook NumberPad feature).

Runs as a root system service so it also works at the login screen.
  - left edge : slide up/down = volume      (needs a logged-in user)
  - right edge: slide up/down = brightness  (laptop screen + DDC/CI monitors,
                                             matched by real brightness in nits)
Touches that start in the top corners are ignored, so the NumberPad
corner swipes are never affected.

The last brightness is remembered and put back after a reboot, after login
(KDE and GNOME restore their own value then), after resume and when a monitor
is plugged in.

Every setting below can be overridden in /etc/trackpad-edges/config.toml
(see config.example.toml).

Also usable by hand:
  sudo trackpad_edges.py --status      # what nits each screen is at right now
  sudo trackpad_edges.py --calibrate   # re-fit the built-in panel's curve
"""
import glob
import json
import math
import os
import pwd
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from evdev import InputDevice, ecodes, list_devices


def log(msg):
    print(msg, flush=True)                        # shows up in `journalctl -u trackpad-edges`

CONFIG_FILE = os.environ.get("TRACKPAD_EDGES_CONFIG", "/etc/trackpad-edges/config.toml")

# ---- Which touchpad ----
# Regex matched against the evdev device name (`sudo libinput list-devices`).
# Empty, or nothing matches = the first device with "touchpad" in its name.
TOUCHPAD_NAME = ""

# ---- What each edge does: "volume", "brightness" or "none" ----
LEFT_EDGE = "volume"
RIGHT_EDGE = "brightness"

# ---- Touch tunables (fractions of the touchpad size) ----
EDGE_WIDTH = 0.07        # width of the left/right strips
TOP_DEAD_ZONE = 0.20     # touches starting in the top 20% are ignored
ACTIVATION = 0.03        # vertical travel before the first step
SENSITIVITY = 0.018      # vertical travel per step

# ---- Volume ----
# "auto" = KDE's own shortcut (shows the OSD) on Plasma, else wpctl, else pactl.
VOLUME_BACKEND = "auto"  # "auto", "kde", "wpctl" or "pactl"
VOLUME_STEP = 5          # percent per step for wpctl / pactl

# ---- Brightness matching ----
# Every screen is driven to the SAME nits. The shared range tops out at the
# brightest screen's peak, so a dimmer monitor just saturates (sits at 100%)
# for the top part of the swipe while the brighter panel keeps going.
#   "internal" = the laptop panel's SDR peak. Use the SDR figure, not the HDR
#                one: the sysfs backlight range only drives SDR, so an HDR
#                number would make the panel look dimmer than it is.
#   Other keys = part of an external monitor's name as shown by
#                `sudo ddcutil detect --brief` (the "Monitor:" line).
# A monitor not listed here is assumed to be as bright as the laptop panel.
SCREEN_NITS = {
    "internal": 400,
}
BRIGHTNESS_STEP = 0.05   # each step = 5% of the shared maximum
MIN_NITS_FRACTION = 0.02 # bottom of the shared range = 2% of its ceiling
DDC_RETRY_BACKOFF = 30.0 # seconds to ignore a monitor whose setvcp keeps failing

# ---- Remembering brightness ----
REMEMBER_BRIGHTNESS = True
STATE_FILE = "/var/lib/trackpad-edges/state.json"
SAVE_DELAY = 2.0          # write the file this long after the last change
POLL_INTERVAL = 2.0       # how often to notice brightness keys / KDE slider moves
# Plasma's PowerDevil (and GNOME) set their own remembered brightness a few
# seconds into a session, so the saved level is put back after that.
LOGIN_RESTORE_DELAYS = (4.0, 10.0)

# ---- Built-in panel brightness curve ----
# amdgpu runs the requested `brightness` value through a firmware gamma curve,
# so what comes out is NOT what you asked for: `actual_brightness` is. The
# kernel even says so (`scale` = "non-linear"). Percent maths on the requested
# value therefore misjudges this panel badly. We fit
#     actual = actual_max * (requested / max_brightness) ** gamma
# once, cache it, and use it to hit a nits target exactly.
PANEL_CURVE_FILE = "/var/lib/trackpad-edges/panel.json"
PANEL_CAL_STEPS = (1.0, 0.75, 0.5, 0.25)   # fractions of range to sample
PANEL_SETTLE = 0.06       # amdgpu applies a new backlight value asynchronously

# config.toml keys -> the settings above (same names, lower case)
_CONFIGURABLE = (
    "TOUCHPAD_NAME", "LEFT_EDGE", "RIGHT_EDGE", "EDGE_WIDTH", "TOP_DEAD_ZONE",
    "ACTIVATION", "SENSITIVITY", "VOLUME_BACKEND", "VOLUME_STEP",
    "BRIGHTNESS_STEP", "MIN_NITS_FRACTION", "REMEMBER_BRIGHTNESS",
)


def load_config(path=CONFIG_FILE):
    """Apply /etc/trackpad-edges/config.toml on top of the defaults above."""
    try:
        import tomllib                            # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib               # older Pythons: pip/apt python3-tomli
        except ImportError:
            log(f"Python < 3.11 without tomli: ignoring {path}, using defaults")
            return
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        return
    except (OSError, tomllib.TOMLDecodeError) as e:
        log(f"ignoring {path}: {e}")
        return
    g = globals()
    for key, value in cfg.items():
        name = key.upper()
        if name == "SCREEN_NITS" and isinstance(value, dict):
            g["SCREEN_NITS"] = {"internal": SCREEN_NITS["internal"], **value}
        elif name in _CONFIGURABLE and (type(value) is type(g[name]) or
                                        (type(g[name]) is float and type(value) is int)):
            g[name] = type(g[name])(value)        # `edge_width = 1` is fine for a float
        else:
            log(f"{path}: unknown or wrongly typed setting {key!r}")
    log(f"loaded {path}")


class EdgeSwipe:
    """Turns raw touchpad events into on_step(zone, direction) calls.

    zone is "LEFT" or "RIGHT"; direction is +1 (finger moved up) or -1 (down).
    """

    MULTI_KEYS = (
        ecodes.BTN_TOOL_DOUBLETAP,
        ecodes.BTN_TOOL_TRIPLETAP,
        ecodes.BTN_TOOL_QUADTAP,
        ecodes.BTN_TOOL_QUINTTAP,
    )

    def __init__(self, max_x, max_y, on_step):
        self.left_max = max_x * EDGE_WIDTH
        self.right_min = max_x * (1 - EDGE_WIDTH)
        self.slack = self.left_max * 0.3          # sideways drift allowed
        self.top_limit = max_y * TOP_DEAD_ZONE
        self.activation = max_y * ACTIVATION
        self.sensitivity = max_y * SENSITIVITY
        self.on_step = on_step
        self.reset()

    def reset(self):
        self.x = self.y = None
        self.touching = False
        self.new_touch = False
        self.multi_keys = set()
        self.blocked = False
        self.gesture = None

    def feed(self, ev):
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_MT_POSITION_X:
                self.x = ev.value
            elif ev.code == ecodes.ABS_MT_POSITION_Y:
                self.y = ev.value
        elif ev.type == ecodes.EV_KEY:
            if ev.code == ecodes.BTN_TOUCH:
                if ev.value:
                    self.touching = True
                    self.new_touch = True
                else:
                    self.reset()
            elif ev.code in self.MULTI_KEYS:
                if ev.value:
                    self.multi_keys.add(ev.code)
                else:
                    self.multi_keys.discard(ev.code)
        elif ev.type == ecodes.EV_SYN:
            if ev.code == ecodes.SYN_DROPPED:
                self.reset()
            elif ev.code == ecodes.SYN_REPORT:
                self._frame()

    def _frame(self):
        if self.new_touch:                        # a fresh touch starts clean
            self.gesture = None
            self.blocked = False
            self.new_touch = False
        if not self.touching:
            return
        if self.multi_keys:                       # two+ fingers = not ours
            self.blocked = True
            self.gesture = None
        if self.blocked or self.x is None or self.y is None:
            return

        x, y = self.x, self.y
        g = self.gesture

        if g is None:                             # decide where the touch began
            if y < self.top_limit:                # top corners belong to NumberPad
                self.blocked = True
                return
            if x <= self.left_max:
                zone = "LEFT"
            elif x >= self.right_min:
                zone = "RIGHT"
            else:
                self.blocked = True               # middle of the pad: normal use
                return
            self.gesture = {"zone": zone, "y0": y, "last": y, "acc": 0, "armed": False}
            return

        if (g["zone"] == "LEFT" and x > self.left_max + self.slack) or \
           (g["zone"] == "RIGHT" and x < self.right_min - self.slack):
            self.blocked = True                   # drifted into the workspace
            self.gesture = None
            return

        if y < self.top_limit:                    # pause while over the corner icons
            g["last"] = y
            return

        if not g["armed"]:                        # deliberate-swipe gate
            if abs(y - g["y0"]) >= self.activation:
                g["armed"] = True
                g["acc"] = 0
            g["last"] = y
            return

        g["acc"] += y - g["last"]
        g["last"] = y
        if g["acc"] >= self.sensitivity:          # finger moved down
            g["acc"] = 0
            self.on_step(g["zone"], -1)
        elif g["acc"] <= -self.sensitivity:       # finger moved up
            g["acc"] = 0
            self.on_step(g["zone"], +1)


# ---------------- helpers ----------------

_children = []
_children_lock = threading.Lock()


def _spawn(cmd):
    with _children_lock:
        _children[:] = [p for p in _children if p.poll() is None]   # reap finished
        _children.append(subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))


def logged_in_user():
    """(uid, name) of a real user with a running session bus, else None."""
    for sock in glob.glob("/run/user/*/bus"):
        uid = int(sock.split("/")[3])
        if uid >= 1000:
            try:
                return uid, pwd.getpwuid(uid).pw_name
            except KeyError:
                pass
    return None


def as_user(user, *cmd):
    uid, name = user
    return ["runuser", "-u", name, "--", "env",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
            f"XDG_RUNTIME_DIR=/run/user/{uid}", *cmd]


def kde_shortcut(user, component, action):
    _spawn(as_user(
        user, "dbus-send", "--type=method_call", "--dest=org.kde.kglobalaccel",
        f"/component/{component}",
        "org.kde.kglobalaccel.Component.invokeShortcut", f"string:{action}"))


_plasma_cache = {}                                # {uid: (checked at, running?)}


def plasma_running(uid):
    at, running = _plasma_cache.get(uid, (0.0, False))
    if time.time() - at > 30:
        running = subprocess.run(["pgrep", "-u", str(uid), "-x", "plasmashell"],
                                 stdout=subprocess.DEVNULL).returncode == 0
        _plasma_cache[uid] = (time.time(), running)
    return running


def change_volume(direction):
    user = logged_in_user()
    if not user:
        return                                    # no audio session at the login screen
    backend = VOLUME_BACKEND
    if backend == "auto":
        if plasma_running(user[0]):
            backend = "kde"
        elif shutil.which("wpctl"):
            backend = "wpctl"
        else:
            backend = "pactl"
    if backend == "kde":
        kde_shortcut(user, "kmix", "increase_volume" if direction > 0 else "decrease_volume")
    elif backend == "wpctl":
        _spawn(as_user(user, "wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@",
                       f"{VOLUME_STEP}%{'+' if direction > 0 else '-'}"))
    else:
        _spawn(as_user(user, "pactl", "set-sink-volume", "@DEFAULT_SINK@",
                       f"{'+' if direction > 0 else '-'}{VOLUME_STEP}%"))


def show_osd(percent):
    """Best-effort KDE on-screen display (does nothing at the login screen)."""
    user = logged_in_user()
    if user and plasma_running(user[0]):
        _spawn(as_user(
            user, "dbus-send", "--type=method_call", "--dest=org.kde.plasmashell",
            "/org/kde/osdService", "org.kde.osdService.brightnessChanged",
            f"int32:{percent}"))


def kde_internal_display(user):
    """D-Bus path of Plasma's built-in screen (org.kde.ScreenBrightness), or None."""
    def call(*args):
        r = subprocess.run(as_user(user, "dbus-send", "--print-reply",
                                   "--dest=org.kde.ScreenBrightness", *args),
                           capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""

    out = call("/org/kde/ScreenBrightness", "org.freedesktop.DBus.Properties.Get",
               "string:org.kde.ScreenBrightness", "string:DisplaysDBusNames")
    for name in re.findall(r'string "([^"]+)"', out):
        path = f"/org/kde/ScreenBrightness/{name}"
        if "boolean true" in call(path, "org.freedesktop.DBus.Properties.Get",
                                  "string:org.kde.ScreenBrightness.Display",
                                  "string:IsInternal"):
            return path
    return None


def sync_kde(fraction):
    """Tell Plasma the panel's brightness (0..1 of the sysfs range).

    KWin keeps its own copy in ~/.config/kwinoutputconfig.json and applies it
    the moment the desktop starts. Writing sysfs directly never updates that
    copy, so without this the desktop opens at KWin's stale (usually darker)
    level until our login restore kicks in.
    """
    user = logged_in_user()
    if not user or not plasma_running(user[0]):
        return
    try:
        path = kde_internal_display(user)
        if path:                                  # flags=1: no OSD popup
            subprocess.run(as_user(
                user, "dbus-send", "--type=method_call", "--dest=org.kde.ScreenBrightness",
                path, "org.kde.ScreenBrightness.Display.SetBrightness",
                f"int32:{round(fraction * 10000)}", "uint32:1"),
                capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"could not update Plasma's brightness: {e}")


def backlight():
    """Path of the best backlight device for the built-in panel, or None."""
    best = None
    for bl in glob.glob("/sys/class/backlight/*"):
        try:
            kind = Path(bl, "type").read_text().strip()
        except OSError:
            continue
        rank = {"raw": 0, "platform": 1, "firmware": 2}.get(kind, 3)
        if best is None or rank < best[0]:
            best = (rank, Path(bl))
    return best[1] if best else None


DRM_DIR = "/sys/class/drm"


def edid_name(path):
    """'MFG:Model' read from a monitor's EDID (same shape ddcutil prints)."""
    try:
        edid = Path(path).read_bytes()
    except OSError:
        return ""
    if len(edid) < 128:
        return ""
    m = (edid[8] << 8) | edid[9]
    mfg = "".join(chr(((m >> sh) & 0x1F) + 64) for sh in (10, 5, 0))
    model = ""
    for off in (54, 72, 90, 108):
        d = edid[off:off + 18]
        if d[0:3] == b"\x00\x00\x00" and d[3] == 0xFC:
            model = d[5:18].decode("ascii", "ignore").split("\n")[0].strip()
    return f"{mfg}:{model}"


class PanelCurve:
    """Converts between the built-in panel's sysfs values and real nits.

    Falls back to a straight line (gamma 1, actual_max = max_brightness) when
    no fit is cached or the panel does not expose `actual_brightness`, so the
    script still works on hardware without the amdgpu brightness curve.
    """

    def __init__(self, max_brightness):
        self.max = max_brightness
        self.actual_max = float(max_brightness)
        self.gamma = 1.0
        self.samples = []
        self.calibrated = False

    def load(self):
        """Adopt a cached fit. False if there is none, or if it is stale."""
        try:
            d = json.loads(Path(PANEL_CURVE_FILE).read_text())
            if int(d["max_brightness"]) != self.max:      # mode or HDR changed
                return False
            gamma, actual_max = float(d["gamma"]), float(d["actual_max"])
            samples = [[int(a), int(b)] for a, b in d.get("samples", [])]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if not self._sane(gamma, actual_max):
            return False
        self.gamma, self.actual_max = gamma, actual_max
        self.samples, self.calibrated = samples, True
        return True

    def _sane(self, gamma, actual_max):
        return 0.3 < gamma < 6.0 and 0 < actual_max <= self.max * 1.2

    def save(self):
        try:
            Path(PANEL_CURVE_FILE).parent.mkdir(parents=True, exist_ok=True)
            Path(PANEL_CURVE_FILE).write_text(json.dumps({
                "max_brightness": self.max,
                "actual_max": round(self.actual_max, 1),
                "gamma": round(self.gamma, 4),
                "samples": self.samples,
            }, indent=2) + "\n")
            return True
        except OSError as e:
            log(f"could not save panel curve: {e}")
            return False

    def nits(self, actual, peak):
        """`actual_brightness` -> nits on a panel rated `peak`."""
        return peak * actual / self.actual_max

    def requested(self, nits, peak):
        """sysfs `brightness` value that should produce `nits`."""
        frac = min(1.0, max(0.0, nits / peak))
        return self.max * frac ** (1.0 / self.gamma)


def panel_actual(bl):
    """Light level the panel is really emitting, or None."""
    for name in ("actual_brightness", "brightness"):
        try:
            return int(Path(bl, name).read_text())
        except OSError:
            continue
    return None


def fit_curve(max_b, samples):
    """Least-squares fit of actual = A * (requested/max)^gamma. -> (A, gamma)."""
    pts = [(math.log(r / max_b), math.log(a))
           for r, a in samples if a > 0 and r > 0]
    n = len(pts)
    sx = sum(x for x, _ in pts)
    sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts)
    sxy = sum(x * y for x, y in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    # worst-case how far a sample sits off the fitted line, in log space
    worst = max(abs(y - (intercept + slope * x)) for x, y in pts)
    return math.exp(intercept), slope, worst


def calibrate_panel(bl, quiet=False):
    """Sample the panel's brightness curve and cache it. True if it fit."""
    def say(msg):
        if not quiet:
            log(msg)

    max_b = int(Path(bl, "max_brightness").read_text())
    curve = PanelCurve(max_b)
    if curve.load():
        say(f"panel curve already fitted: gamma={curve.gamma:.3f} "
            f"actual_max={curve.actual_max:.0f}")
        return True
    if not Path(bl, "actual_brightness").exists():
        say("panel does not report actual_brightness; staying linear")
        return False
    try:
        if int(Path(bl, "bl_power").read_text()) != 0:
            say("panel is powered down; will calibrate on the next swipe")
            return False
        original = int(Path(bl, "brightness").read_text())
    except OSError as e:
        say(f"calibration skipped: {e}")
        return False

    samples = []
    try:
        for frac in PANEL_CAL_STEPS:
            Path(bl, "brightness").write_text(str(max(1, round(max_b * frac))))
            time.sleep(PANEL_SETTLE)
            actual = int(Path(bl, "actual_brightness").read_text())
            samples.append([round(max_b * frac), actual])
    except OSError as e:
        say(f"calibration failed mid-way: {e}")
        return False
    finally:
        try:
            Path(bl, "brightness").write_text(str(original))
        except OSError:
            pass

    # Samples run from full brightness downwards, so `actual_brightness` has to
    # fall with them. Anything else (flat, stuck, non-monotonic) means the
    # panel is not reporting real output and there is nothing to fit.
    by_request = sorted(samples, key=lambda s: s[0], reverse=True)
    actuals = [a for _, a in by_request]
    if any(a <= b for a, b in zip(actuals, actuals[1:])):
        say(f"actual_brightness does not fall with the request {samples}; "
            "staying linear")
        return False
    fit = fit_curve(max_b, samples)
    if not fit:
        say(f"could not fit a curve from {samples}; staying linear")
        return False
    actual_max, gamma, worst = fit
    if not curve._sane(gamma, actual_max) or worst > 0.15:
        say(f"bad panel fit (gamma={gamma:.3f} actual_max={actual_max:.0f} "
            f"residual={worst:.3f}); staying linear")
        return False

    curve.gamma, curve.actual_max = gamma, actual_max
    curve.samples, curve.calibrated = samples, True
    curve.save()
    say(f"panel curve fitted: gamma={gamma:.3f} actual_max={actual_max:.0f} "
        f"(worst residual {worst:.1%})")
    return True


def load_state():
    """Saved brightness in nits, or None."""
    try:
        return float(json.loads(Path(STATE_FILE).read_text())["nits"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_state(nits):
    try:
        path = Path(STATE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"nits": round(nits, 1)}) + "\n")
        tmp.replace(path)                         # never leave a half-written file
    except OSError as e:
        log(f"could not save brightness: {e}")


class Screens:
    """Keeps the laptop panel and DDC/CI monitors at the same real brightness."""

    def __init__(self, start=True):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending = 0
        self.externals = []          # [(i2c bus number, peak nits or None)]
        self.scan_how = None
        self.last_scan = 0.0
        self.ddc_dead = {}            # {bus: timestamp to retry at}
        self.synced = set()           # buses already sent the current level
        self.ddc_fails = {}           # {bus: failed setvcp in a row}
        self.curve = None             # PanelCurve, loaded on first use
        self.restore_at = []          # times to put the saved level back
        self.saved = load_state() if REMEMBER_BRIGHTNESS else None
        self.known = None             # panel nits as last set or seen
        self.save_at = None
        self.session_uid = None
        self.asleep_for = self._suspended()
        if start:
            threading.Thread(target=self._worker, daemon=True).start()

    def step(self, direction):
        with self.lock:
            self.pending += direction
        self.wake.set()

    def request_restore(self, delays=(0.0,)):
        if not REMEMBER_BRIGHTNESS:
            return
        now = time.time()
        with self.lock:
            self.restore_at += [now + d for d in delays]
        self.wake.set()

    def _get_curve(self, bl):
        """The panel's brightness curve, fitted on first use and then cached."""
        if self.curve is None:
            curve = PanelCurve(int(Path(bl, "max_brightness").read_text()))
            if not curve.load():
                calibrate_panel(bl)                 # brief flash, once ever
                curve.load()
            self.curve = curve
        return self.curve

    def _worker(self):
        self._scan()
        if self.saved is not None:
            # put back the level from last boot, then again in case
            # systemd-backlight restores its own value after we start
            self.request_restore((0.0, 5.0))
        while True:
            self.wake.wait(timeout=POLL_INTERVAL)
            self.wake.clear()
            now = time.time()
            with self.lock:
                n, self.pending = self.pending, 0
                due = any(t <= now for t in self.restore_at)
                self.restore_at = [t for t in self.restore_at if t > now]
            try:
                if n:
                    self.apply(n)
                elif due:
                    self.restore()
                elif REMEMBER_BRIGHTNESS:
                    self._housekeeping(now)
            except Exception as e:
                log(f"brightness error: {e!r}")   # never let the worker die

    @staticmethod
    def _suspended():
        """Seconds spent in suspend since boot (BOOTTIME counts it, MONOTONIC doesn't)."""
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()

    def _housekeeping(self, now):
        """Runs every POLL_INTERVAL while nothing else is happening."""
        asleep = self._suspended()
        if asleep - self.asleep_for > 5:          # just woke up: monitors may have reset
            self.asleep_for = asleep
            if self.saved is not None:
                self.request_restore((1.0,) + LOGIN_RESTORE_DELAYS)
                return
        if self.save_at and now >= self.save_at:
            self.save_at = None
            save_state(self.saved)
            self._sync_kde()

        user = logged_in_user()                   # someone just logged in:
        uid = user[0] if user else None           # wait out the desktop's own restore
        if uid != self.session_uid:
            self.session_uid = uid
            if uid is not None and self.saved is not None:
                self.request_restore(LOGIN_RESTORE_DELAYS)
                return
        with self.lock:
            if self.restore_at:
                return                            # don't learn the desktop's value

        t = self._panel_nits()                    # brightness keys / desktop slider
        if t is not None:
            if self.known is None:
                self.known = t
            elif abs(t - self.known) > SCREEN_NITS["internal"] * 0.01:
                self.known = t
                self._remember(t)

        if now - self.last_scan > 15 and self.scan_how != "ddcutil detect":
            found = self._scan_sysfs()            # cheap; catches a monitor plugged in
            if found != self.externals:
                self._set_externals(found, "sysfs")
            self.last_scan = now
        if self.known is not None:
            self._push_all(self.known, only_unsynced=True)

    def _remember(self, t):
        if REMEMBER_BRIGHTNESS:
            self.saved = t
            self.save_at = time.time() + SAVE_DELAY

    def _peak_for(self, name):
        name = name.lower()
        return next((n for key, n in SCREEN_NITS.items()
                     if key != "internal" and key.lower() in name), None)

    def _scan(self):
        self.last_scan = time.time()
        found, how = self._scan_sysfs(), "sysfs"
        if not found:                             # fall back to asking ddcutil
            found, how = self._scan_ddcutil(), "ddcutil detect"
        self._set_externals(found, how)

    def _set_externals(self, found, how):
        if found != self.externals or how != self.scan_how:
            log(f"external monitors found via {how}: {found}")
        self.externals, self.scan_how = found, how
        buses = {bus for bus, _ in found}
        self.synced &= buses                      # an unplugged one needs a resync
        self.ddc_fails = {b: n for b, n in self.ddc_fails.items() if b in buses}

    def _scan_sysfs(self):
        """Find connected external screens straight from the kernel (no ddcutil needed)."""
        found = []
        for conn in sorted(glob.glob(f"{DRM_DIR}/card*-*")):
            if re.search(r"eDP|LVDS|DSI", os.path.basename(conn)):
                continue                          # the built-in panel
            try:
                if Path(conn, "status").read_text().strip() != "connected":
                    continue
            except OSError:
                continue
            bus = None
            ddc = Path(conn, "ddc")
            if ddc.exists():
                m = re.search(r"i2c-(\d+)$", os.path.realpath(ddc))
                bus = m.group(1) if m else None
            if bus is None:
                for d in glob.glob(f"{conn}/i2c-*"):
                    m = re.search(r"i2c-(\d+)$", d)
                    if m:
                        bus = m.group(1)
                        break
            if bus is None:
                continue
            found.append((bus, self._peak_for(edid_name(Path(conn, "edid")))))
        return found

    def _scan_ddcutil(self):
        found = []
        try:
            r = subprocess.run(["ddcutil", "detect", "--brief"], capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"ddcutil detect could not run: {e}")
            return found
        for block in re.split(r"\n(?=Display \d|Invalid display)", "\n" + r.stdout):
            block = block.strip()
            if not block.startswith("Display "):  # skips "Invalid display" entries
                continue
            bus = re.search(r"/dev/i2c-(\d+)", block)
            if not bus or "eDP" in block:         # skip the built-in panel
                continue
            mon = re.search(r"Monitor:\s*(.+)", block)
            found.append((bus.group(1), self._peak_for(mon.group(1) if mon else "")))
        if not found:
            log(f"ddcutil detect saw no monitors. stdout={r.stdout[:300]!r} stderr={r.stderr[:300]!r}")
        return found

    def _range(self):
        peak_int = SCREEN_NITS["internal"]
        peaks = [peak_int] + [p for _, p in self.externals if p]
        t_max = max(peaks)                 # brightest screen sets the ceiling;
        return t_max * MIN_NITS_FRACTION, t_max   # a dimmer monitor just saturates

    def _panel_nits(self, bl=None):
        bl = bl or backlight()
        if bl is None:
            return None
        actual = panel_actual(bl)          # follows KDE's own brightness keys too
        if actual is None:
            return None
        return self._get_curve(bl).nits(actual, SCREEN_NITS["internal"])

    def _maybe_rescan(self):
        now = time.time()
        if now - self.last_scan > 60 or (not self.externals and now - self.last_scan > 10):
            self._scan()

    def apply(self, n):
        bl = backlight()
        if bl is None:
            return
        self._maybe_rescan()
        t = self._panel_nits(bl)
        if t is None:
            return                          # panel vanished; nothing to drive
        t_min, t_max = self._range()
        t = min(t_max, max(t_min, t + n * BRIGHTNESS_STEP * t_max))
        self._drive(bl, t)
        show_osd(round(t / t_max * 100))
        self._remember(t)

    def restore(self):
        """Put every screen back at the remembered brightness."""
        if self.saved is None:
            return
        bl = backlight()
        if bl is None:
            return
        self._maybe_rescan()
        t_min, t_max = self._range()
        t = min(t_max, max(t_min, self.saved))    # keeps self.saved for a bigger monitor
        log(f"restoring brightness: {t:.0f} nits")
        self.synced.clear()                       # a resumed monitor may have reset
        self._drive(bl, t)
        self._sync_kde()

    def _sync_kde(self):
        bl = backlight()
        if bl is None or self.known is None:
            return
        curve = self._get_curve(bl)
        sync_kde(curve.requested(self.known, SCREEN_NITS["internal"]) / curve.max)

    def _drive(self, bl, t):
        want = max(1, round(self._get_curve(bl).requested(t, SCREEN_NITS["internal"])))
        try:
            cur = int(Path(bl, "brightness").read_text())
            if want != cur:                # skip a pointless sysfs write
                Path(bl, "brightness").write_text(str(want))
        except OSError as e:
            log(f"panel brightness write failed: {e}")
        self.known = t
        self._push_all(t)

    def _push_all(self, t, only_unsynced=False):
        now = time.time()
        live = [(bus, peak) for bus, peak in self.externals
                if self.ddc_dead.get(bus, 0) <= now
                and not (only_unsynced and (bus in self.synced or
                                            self.ddc_fails.get(bus, 0) >= 3))]
        if len(live) > 1:                  # one round-trip's worth, not N
            workers = [threading.Thread(target=self._push, args=(bus, peak, t))
                       for bus, peak in live]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
        else:
            for bus, peak in live:
                self._push(bus, peak, t)

    def _push(self, bus, peak, t):
        # an unlisted monitor is assumed as bright as the panel (see SCREEN_NITS)
        pct = max(0, min(100, round(t / (peak or SCREEN_NITS["internal"]) * 100)))
        if self._set_ddc(bus, pct):
            self.ddc_dead.pop(bus, None)
            self.ddc_fails.pop(bus, None)
            self.synced.add(bus)
        else:                              # don't retry a dead bus every swipe
            self.ddc_dead[bus] = time.time() + DDC_RETRY_BACKOFF
            self.ddc_fails[bus] = self.ddc_fails.get(bus, 0) + 1   # no DDC/CI: stop resyncing
            log(f"ignoring bus {bus} for {DDC_RETRY_BACKOFF:.0f}s")

    def _set_ddc(self, bus, pct):
        for extra in ([], ["--noverify"]):   # retry without verify if rejected
            try:
                r = subprocess.run(["ddcutil", "--bus", bus, *extra, "setvcp", "10", str(pct)],
                                   capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.SubprocessError) as e:
                log(f"ddcutil could not run: {e}")
                return False
            if r.returncode == 0:
                return True
            log(f"ddcutil bus {bus} {extra}: exit {r.returncode}: {(r.stderr or r.stdout).strip()}")
        return False


screens = None


def on_step(zone, direction):
    action = LEFT_EDGE if zone == "LEFT" else RIGHT_EDGE
    if action == "brightness":
        screens.step(direction)
    elif action == "volume":                      # needs the user's audio session
        change_volume(direction)


# ---------------- manual / diagnostic modes ----------------

def _get_vcp(bus):
    """Current VCP 10 (brightness) percentage on a DDC/CI monitor, or None."""
    try:
        r = subprocess.run(["ddcutil", "--bus", bus, "getvcp", "10"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"ddcutil getvcp bus {bus} failed: {e}")
        return None
    m = re.search(r"current value\s*=\s*(\d+)", r.stdout)
    return int(m.group(1)) if m else None


def status():
    """Print what nits every connected screen is at, and how far apart they are."""
    bl = backlight()
    if bl is None:
        print("no backlight device found")
        return 1
    max_b = int(Path(bl, "max_brightness").read_text())
    cur = int(Path(bl, "brightness").read_text())
    actual = panel_actual(bl)
    peak_int = SCREEN_NITS["internal"]

    curve = PanelCurve(max_b)
    if curve.load():
        print(f"panel curve: gamma={curve.gamma:.3f} "
              f"actual_max={curve.actual_max:.0f}  (cached in {PANEL_CURVE_FILE})")
    else:
        print("panel curve: NOT calibrated - linear fallback in use")
        print("             run: sudo trackpad_edges.py --calibrate")

    readings = []
    panel_nits = curve.nits(actual if actual is not None else cur, peak_int)
    readings.append(panel_nits)
    src = "actual_brightness" if actual is not None else "brightness (linear)"
    print(f"panel  : requested {cur}/{max_b}  {src} {actual}  ->  "
          f"{panel_nits:.0f} nits  (peak {peak_int})")

    monitors = Screens(start=False)
    monitors._scan()
    for bus, peak in monitors.externals:
        pct = _get_vcp(bus)
        if pct is None:
            print(f"bus {bus}: could not read VCP 10 -- DDC/CI is not working")
            continue
        rated = peak or peak_int
        readings.append(pct / 100 * rated)
        note = "  <- saturated, dimmer than the panel" if pct >= 100 and rated < peak_int else ""
        print(f"bus {bus}: VCP10 {pct:>3}%  ->  {pct / 100 * rated:.0f} nits"
              f"  (peak {peak or 'unknown'}){note}")

    if len(readings) > 1:
        print(f"match  : {max(readings) - min(readings):.0f} nits apart "
              f"({min(readings):.0f} - {max(readings):.0f})")
    saved = load_state()
    print(f"saved  : {f'{saved:.0f} nits' if saved is not None else 'nothing yet'}"
          f"  ({STATE_FILE}, remember_brightness={REMEMBER_BRIGHTNESS})")
    return 0


def recalibrate():
    bl = backlight()
    if bl is None:
        print("no backlight device found")
        return 1
    try:
        Path(PANEL_CURVE_FILE).unlink()
        print(f"removed {PANEL_CURVE_FILE}")
    except OSError:
        pass
    print("watch the panel flash for about a second...")
    ok = calibrate_panel(bl)
    curve = PanelCurve(int(Path(bl, "max_brightness").read_text()))
    curve.load()
    if not ok:
        print("calibration failed; the service will stay on the linear fallback")
        return 1
    for requested, act in curve.samples:
        pct = requested / curve.max * 100
        print(f"  requested {pct:5.1f}%  actual {act:>7}  -> "
              f"{curve.nits(act, SCREEN_NITS['internal']):5.0f} nits")
    print(f"gamma={curve.gamma:.3f} actual_max={curve.actual_max:.0f}")
    return 0


# ---------------- device handling ----------------

def find_touchpad():
    fallback = None
    for path in list_devices():
        try:
            dev = InputDevice(path)
            axes = dict(dev.capabilities().get(ecodes.EV_ABS, []))
        except OSError:
            continue
        if ecodes.ABS_MT_POSITION_X not in axes or ecodes.ABS_MT_POSITION_Y not in axes:
            dev.close()
            continue
        if TOUCHPAD_NAME and re.search(TOUCHPAD_NAME, dev.name, re.IGNORECASE):
            return dev, axes
        if "touchpad" in dev.name.lower() and fallback is None:
            fallback = (dev, axes)
        else:
            dev.close()
    return fallback


def main():
    global screens
    load_config()
    screens = Screens()
    while True:
        found = find_touchpad()
        if not found:                             # not up yet: just keep looking
            time.sleep(1)
            continue
        dev, axes = found
        detector = EdgeSwipe(axes[ecodes.ABS_MT_POSITION_X].max,
                             axes[ecodes.ABS_MT_POSITION_Y].max, on_step)
        try:
            for ev in dev.read_loop():
                detector.feed(ev)
        except OSError:
            pass                                  # device vanished (suspend/replug)
        finally:
            dev.close()
        time.sleep(1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode:
        load_config()
    if mode in ("--status", "-s"):
        sys.exit(status())
    if mode == "--calibrate":
        sys.exit(recalibrate())
    if mode:
        print(f"unknown option {mode!r}; try --status or --calibrate")
        sys.exit(2)
    main()
