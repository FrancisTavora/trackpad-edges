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

Every setting below can be overridden in /etc/trackpad-edges.conf
(see trackpad-edges.conf.example).

Also usable by hand:
  sudo trackpad_edges.py --check       # what it found and how it will act
  sudo trackpad_edges.py --status      # what nits each screen is at right now
  sudo trackpad_edges.py --calibrate   # re-fit the built-in panel's curve
"""
import configparser
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

CONFIG_FILE = os.environ.get("TRACKPAD_EDGES_CONFIG", "/etc/trackpad-edges.conf")
OLD_CONFIG_FILE = "/etc/trackpad-edges/config.toml"   # pre-1.1 TOML config

# ---- Which touchpad ----
# Regex matched against the evdev device name (`sudo libinput list-devices`).
# Empty, or nothing matches = the touchpad found by its capabilities.
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
# "auto" = KDE's own shortcut (shows the OSD) on Plasma, the volume keys on
# GNOME (shows GNOME's OSD), else wpctl, else pactl.
VOLUME_BACKEND = "auto"  # "auto", "kde", "keys", "wpctl" or "pactl"
VOLUME_STEP = 5          # percent per step for wpctl / pactl

# ---- Popups ----
# "auto" = Plasma's OSD on KDE, GNOME's OSD on GNOME, notify-send elsewhere.
POPUP = "auto"           # "auto" or "none"

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
# Steps follow the panel's own brightness curve, so one step is the same
# change on Plasma's slider (and looks even to the eye) at any level.
BRIGHTNESS_STEP = 0.05   # each step = 5% of the slider
MIN_BRIGHTNESS = 0.01    # dimmest allowed = 1% on the slider
DDC_RETRY_BACKOFF = 30.0 # seconds to ignore a monitor whose setvcp keeps failing
DDCUTIL_RESCAN = 300.0   # min seconds between slow `ddcutil detect` fallbacks

# ---- Remembering brightness ----
REMEMBER_BRIGHTNESS = True
STATE_FILE = "/var/lib/trackpad-edges/state.json"
SAVE_DELAY = 2.0          # write the file this long after the last change
POLL_INTERVAL = 2.0       # how often to notice brightness keys / KDE slider moves
# Plasma's PowerDevil (and GNOME) set their own remembered brightness a few
# seconds into a session, so the saved level is put back after that.
LOGIN_RESTORE_DELAYS = (4.0, 10.0)
SETTLE_TIME = 1.5         # KWin fades brightness; ignore readings this long after a change

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

# config keys -> the settings above (same names, lower case)
_CONFIGURABLE = (
    "TOUCHPAD_NAME", "LEFT_EDGE", "RIGHT_EDGE", "EDGE_WIDTH", "TOP_DEAD_ZONE",
    "ACTIVATION", "SENSITIVITY", "VOLUME_BACKEND", "VOLUME_STEP", "POPUP",
    "BRIGHTNESS_STEP", "MIN_BRIGHTNESS", "REMEMBER_BRIGHTNESS",
)
CONFIG_LOADED = None                              # path actually read, for --check


def _convert(name, raw):
    """Config text -> the type of the default it replaces. Raises ValueError."""
    default = globals()[name]
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]                           # `touchpad_name = "ELAN"` is fine too
    if type(default) is bool:
        low = raw.lower()
        if low in ("1", "yes", "true", "on"):
            return True
        if low in ("0", "no", "false", "off"):
            return False
        raise ValueError(f"expected true/false, got {raw!r}")
    return type(default)(raw)


def parse_config(text, source="config"):
    """INI text -> ({SETTING: value}, {monitor: nits}). Bad lines are logged and skipped.

    Settings live in [settings] (or before any section); peak brightness in [nits].
    """
    cp = configparser.ConfigParser(default_section="__none__", interpolation=None,
                                   inline_comment_prefixes=("#", ";"))
    cp.optionxform = str                          # keep monitor names as written
    cp.read_string("[settings]\n" + text, source=source)
    settings, nits = {}, {}
    for key, raw in cp["settings"].items():
        name = key.upper()
        if name not in _CONFIGURABLE:
            log(f"{source}: unknown setting {key!r}")
            continue
        try:
            settings[name] = _convert(name, raw)
        except ValueError as e:
            log(f"{source}: bad value for {key!r}: {e}")
    if cp.has_section("nits"):
        for key, raw in cp["nits"].items():
            name = key.strip().strip("\"'")
            try:
                nits[name] = float(raw.strip().strip("\"'"))
            except ValueError:
                log(f"{source}: bad nits for {key!r}: {raw!r}")
    for section in cp.sections():
        if section not in ("settings", "nits"):
            log(f"{source}: unknown section [{section}]")
    return settings, nits


def parse_old_config(path=OLD_CONFIG_FILE):
    """The pre-1.1 TOML config -> same shape as parse_config(), or None."""
    try:
        import tomllib                            # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            return None
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, ValueError):
        return None
    settings, nits = {}, {}
    for key, value in cfg.items():
        name = key.upper()
        if name == "SCREEN_NITS" and isinstance(value, dict):
            nits = {k: float(v) for k, v in value.items() if isinstance(v, (int, float))}
        elif name in _CONFIGURABLE:
            try:
                settings[name] = _convert(name, str(value))
            except ValueError:
                pass
    return settings, nits


def apply_config(settings, nits):
    g = globals()
    g.update(settings)
    if nits:                                      # a [nits] section replaces the list
        g["SCREEN_NITS"] = {"internal": SCREEN_NITS["internal"], **nits}


def load_config(path=None):
    """Apply /etc/trackpad-edges.conf (or the old TOML file) on top of the defaults."""
    global CONFIG_LOADED
    path = path or CONFIG_FILE
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        old = parse_old_config() if path == CONFIG_FILE else None
        if old:
            apply_config(*old)
            CONFIG_LOADED = OLD_CONFIG_FILE
            log(f"loaded {OLD_CONFIG_FILE} (old format; rerun install.sh to migrate it)")
        return
    except OSError as e:
        log(f"ignoring {path}: {e}")
        return
    try:
        apply_config(*parse_config(text, path))
    except configparser.Error as e:
        log(f"ignoring {path}: {e}")
        return
    CONFIG_LOADED = path
    log(f"loaded {path}")


SEED_NITS = {"internal": 400, "ARZOPA": 300, "G241VC": 250}


def render_config(template, settings, nits):
    """The example config with `settings` filled in and its [nits] list replaced."""
    head, sep, tail = template.partition("\n[nits]\n")
    out = []
    for line in head.splitlines():
        m = re.match(r"([a-z_]+)\s*=\s*(.*?)(\s+#.*)?$", line)
        if m and m.group(1).upper() in settings:
            value = settings[m.group(1).upper()]
            value = str(value).lower() if type(value) is bool else value
            line = f"{m.group(1)} = {value}".ljust(len(line) - len(m.group(3) or "")) \
                + (m.group(3) or "")
        out.append(line.rstrip())
    if sep:
        out.append("\n[nits]")
        out += [l for l in tail.splitlines() if l.startswith("#")]
        out += [f"{k} = {int(v) if float(v).is_integer() else v}" for k, v in nits.items()]
    return "\n".join(out) + "\n"


def migrate_config(template_path, out_path, extra_nits=None):
    """Write out_path from the example, carrying over the old config's values.

    Nits come from the old TOML config and `extra_nits` (e.g. an older script's
    built-in list); the seeds fill in only monitors none of those mention, so
    "Optix G241VC" = 260 replaces the G241VC seed instead of sitting beside it.
    """
    settings, nits = parse_old_config() or ({}, {})
    nits = {**(extra_nits or {}), **nits}
    for key, value in SEED_NITS.items():
        if not any(key.lower() in k.lower() for k in nits):
            nits[key] = value
    nits = {"internal": nits.pop("internal"), **nits}
    text = render_config(Path(template_path).read_text(), settings, nits)
    parse_config(text, out_path)                  # never write something we can't read
    Path(out_path).write_text(text)
    os.chmod(out_path, 0o644)
    return settings, nits


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

    def __init__(self, x_range, y_range, on_step):
        (x0, x1), (y0, y1) = x_range, y_range     # some pads don't start at 0
        w, h = x1 - x0, y1 - y0
        self.left_max = x0 + w * EDGE_WIDTH
        self.right_min = x0 + w * (1 - EDGE_WIDTH)
        self.slack = w * EDGE_WIDTH * 0.3         # sideways drift allowed
        self.top_limit = y0 + h * TOP_DEAD_ZONE
        self.activation = h * ACTIVATION
        self.sensitivity = h * SENSITIVITY
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


def _run(cmd, timeout=5):
    """Run cmd and return its stdout, or None if it failed."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


# ---- who is sitting at the screen ----

class Session(tuple):
    """The logged-in user in front of seat0. Indexes like (uid, name)."""

    def __new__(cls, uid, name, sid="", kind="", desktop=""):
        self = super().__new__(cls, (uid, name))
        self.uid, self.name, self.sid, self.kind = uid, name, sid, kind
        self.desktop = desktop                    # logind's raw Desktop= value
        return self


# logind rewrites these (by rename) whenever sessions come, go or switch, so
# their stat changing is a cheap "ask loginctl again" signal. Their contents
# are private to systemd, so we never parse them.
_LOGIND_STATE = ("/run/systemd/seats/seat0", "/run/systemd/sessions")
_session_cache = {"stamp": None, "at": 0.0, "session": None}


def _logind_stamp():
    stamp = []
    for f in _LOGIND_STATE:
        try:
            st = os.stat(f)
            stamp.append((st.st_ino, st.st_mtime_ns))
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def parse_props(text):
    """`loginctl show-* -p A -p B` output -> {"A": ..., "B": ...}."""
    return dict(line.split("=", 1) for line in (text or "").splitlines() if "=" in line)


def _query_session():
    if not shutil.which("loginctl"):              # no logind: best effort
        for sock in sorted(glob.glob("/run/user/*/bus")):
            uid = int(sock.split("/")[3])
            if uid >= 1000:
                try:
                    return Session(uid, pwd.getpwuid(uid).pw_name)
                except KeyError:
                    pass
        return None
    sid = parse_props(_run(["loginctl", "show-seat", "seat0", "-p", "ActiveSession"])
                      ).get("ActiveSession")
    if not sid:
        return None
    p = parse_props(_run(["loginctl", "show-session", sid, "-p", "User", "-p", "Name",
                          "-p", "Class", "-p", "Type", "-p", "Desktop", "-p", "State"]))
    if p.get("Class") != "user" or p.get("State") not in ("active", "online"):
        return None                               # the login screen (greeter) or nobody
    try:
        uid = int(p["User"])
        name = p.get("Name") or pwd.getpwuid(uid).pw_name
    except (KeyError, ValueError):
        return None
    return Session(uid, name, sid, p.get("Type", ""), p.get("Desktop", ""))


def logged_in_user():
    """The active session on seat0 (multi-user and lingering-user safe), else None."""
    stamp, now = _logind_stamp(), time.time()
    c = _session_cache
    if stamp != c["stamp"] or now - c["at"] > 60 or None in stamp:
        c["session"], c["stamp"], c["at"] = _query_session(), stamp, now
    return c["session"]


_proc_cache = {}                                  # {uid: (checked at, {comm, ...})}


def user_processes(uid, max_age=30):
    """Names of the processes `uid` runs (cached; one /proc walk, no subprocess)."""
    at, names = _proc_cache.get(uid, (0.0, set()))
    if time.time() - at > max_age:
        names = set()
        for d in glob.glob("/proc/[0-9]*"):
            try:
                if os.stat(d).st_uid == uid:
                    names.add(Path(d, "comm").read_text().strip())
            except OSError:
                continue
        _proc_cache[uid] = (time.time(), names)
    return names


# process name -> desktop; checked first because logind's Desktop= is the
# session name ("ubuntu", "plasma", "gnome-xorg"...), not always the desktop
_DESKTOP_PROCS = (
    ("plasmashell", "kde"), ("gnome-shell", "gnome"), ("cinnamon", "cinnamon"),
    ("xfce4-session", "xfce"), ("niri", "niri"), ("Hyprland", "hyprland"),
    ("sway", "sway"),
)


def desktop_from_name(name):
    n = (name or "").lower()
    for key, desk in (("kde", "kde"), ("plasma", "kde"), ("gnome", "gnome"),
                      ("ubuntu", "gnome"), ("pop", "gnome"), ("cinnamon", "cinnamon"),
                      ("xfce", "xfce"), ("niri", "niri"), ("hyprland", "hyprland"),
                      ("sway", "sway")):
        if key in n:
            return desk
    return n or "unknown"


def desktop_of(session):
    """'kde', 'gnome', 'niri', ... for a Session; None at the login screen."""
    if not session:
        return None
    procs = user_processes(session.uid)
    for proc, desk in _DESKTOP_PROCS:
        if proc in procs:
            return desk
    return desktop_from_name(session.desktop)


def plasma_running(uid):
    return "plasmashell" in user_processes(uid)


def session_bus(uid):
    """The user's D-Bus address: systemd's per-user bus, else whatever their apps use."""
    if os.path.exists(f"/run/user/{uid}/bus"):
        return f"unix:path=/run/user/{uid}/bus"
    for d in glob.glob("/proc/[0-9]*"):          # rare: dbus-launch on X11
        try:
            if os.stat(d).st_uid != uid:
                continue
            env = Path(d, "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for var in env:
            if var.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                return var.split(b"=", 1)[1].decode()
    return f"unix:path=/run/user/{uid}/bus"


def as_user(user, *cmd):
    uid, name = user[0], user[1]
    return ["runuser", "-u", name, "--", "env",
            f"DBUS_SESSION_BUS_ADDRESS={session_bus(uid)}",
            f"XDG_RUNTIME_DIR=/run/user/{uid}", *cmd]


def kde_shortcut(user, component, action):
    _spawn(as_user(
        user, "dbus-send", "--type=method_call", "--dest=org.kde.kglobalaccel",
        f"/component/{component}",
        "org.kde.kglobalaccel.Component.invokeShortcut", f"string:{action}"))


# ---- media keys (GNOME) ----
# GNOME Shell only lets its own daemons call ShowOSD, so on GNOME we press the
# volume/brightness keys on a virtual keyboard instead: GNOME then changes the
# level itself, keeps its sliders right and shows its own OSD.

_keys = {"dev": None, "failed": False}


def keys_possible():
    return os.path.exists("/dev/uinput") and not _keys["failed"]


def press_key(code, times=1):
    """Tap a key on our virtual keyboard. False if uinput is unavailable."""
    if _keys["dev"] is None:
        if _keys["failed"]:
            return False
        try:
            from evdev import UInput
            _keys["dev"] = UInput({ecodes.EV_KEY: [
                ecodes.KEY_VOLUMEUP, ecodes.KEY_VOLUMEDOWN,
                ecodes.KEY_BRIGHTNESSUP, ecodes.KEY_BRIGHTNESSDOWN]},
                name="trackpad-edges keys")
            time.sleep(0.3)                       # let the compositor pick it up
        except Exception as e:
            log(f"virtual keyboard unavailable ({e}); falling back")
            _keys["failed"] = True
            return False
    dev = _keys["dev"]
    for _ in range(times):
        dev.write(ecodes.EV_KEY, code, 1)
        dev.syn()
        dev.write(ecodes.EV_KEY, code, 0)
        dev.syn()
    return True


# ---- popups ----

_notify = {"replace": None, "ids": {}}


def notify_command(kind, percent, muted=False, replace_id=None, can_replace=True):
    """notify-send arguments for a volume/brightness popup with a progress bar.

    The hints make dunst, mako, swaync and GNOME/Cinnamon/XFCE replace the
    previous popup instead of stacking a new one per step.
    """
    if kind == "volume":
        level = "muted" if muted or percent == 0 else \
            "low" if percent < 34 else "medium" if percent < 67 else "high"
        icon, title = f"audio-volume-{level}", "Volume"
    else:
        icon, title = "display-brightness", "Brightness"
    tag = f"trackpad-edges-{kind}"
    cmd = ["notify-send", "-a", "trackpad-edges", "-u", "low", "-t", "1500", "-i", icon,
           "-h", f"int:value:{percent}",
           "-h", f"string:x-canonical-private-synchronous:{tag}",
           "-h", f"string:x-dunst-stack-tag:{tag}",
           "-h", "boolean:transient:true"]
    if can_replace:
        cmd.append("-p")                          # print the id so the next one replaces it
        if replace_id:
            cmd += ["-r", str(replace_id)]
    return cmd + [title, f"{percent}%" + (" (muted)" if muted else "")]


def notify(user, kind, percent, muted=False):
    if not shutil.which("notify-send"):
        return
    if _notify["replace"] is None:                # -p/-r need libnotify 0.7.10+
        _notify["replace"] = "--replace-id" in (_run(["notify-send", "--help"]) or "")
    key = (user[0], kind)
    out = _run(as_user(user, *notify_command(kind, percent, muted, _notify["ids"].get(key),
                                             _notify["replace"])), timeout=3)
    if out and out.strip().isdigit():
        _notify["ids"][key] = int(out.strip())


# ---- volume ----

def volume_method(user):
    """How a volume step is done for this session: kde, keys, wpctl, pactl or None."""
    if not user:
        return None                               # no audio session at the login screen
    if VOLUME_BACKEND != "auto":
        return VOLUME_BACKEND
    desk = desktop_of(user)
    if desk == "kde":
        return "kde"
    if desk == "gnome" and keys_possible():
        return "keys"
    if shutil.which("wpctl"):
        return "wpctl"
    if shutil.which("pactl"):
        return "pactl"
    return None


def volume_popup(method):
    return {"kde": "Plasma OSD", "keys": "GNOME OSD"}.get(method) or \
        ("notify-send" if POPUP != "none" and method else "none")


def parse_wpctl_volume(text):
    """`wpctl get-volume` -> (percent, muted) or None. e.g. 'Volume: 0.45 [MUTED]'."""
    m = re.search(r"Volume:\s*([\d.]+)", text or "")
    return (round(float(m.group(1)) * 100), "MUTED" in text) if m else None


def parse_pactl_volume(volume_text, mute_text=""):
    """`pactl get-sink-volume` (+ get-sink-mute) -> (percent, muted) or None."""
    pcts = [int(p) for p in re.findall(r"(\d+)%", volume_text or "")]
    if not pcts:
        return None
    return round(sum(pcts) / len(pcts)), "yes" in (mute_text or "").lower()


def volume_commands(method, n, current=None):
    """Commands (to run as the user) that move the volume n steps.

    Raising always unmutes; the result never goes past 100%. pactl has no
    limit flag, so it needs the `current` percentage to cap against.
    """
    up = n > 0
    if method == "wpctl":
        sink = "@DEFAULT_AUDIO_SINK@"
        cmds = [["wpctl", "set-mute", sink, "0"]] if up else []
        return cmds + [["wpctl", "set-volume", "-l", "1.0", sink,
                        f"{abs(n) * VOLUME_STEP}%{'+' if up else '-'}"]]
    if method == "pactl":
        sink = "@DEFAULT_SINK@"
        cmds = [["pactl", "set-sink-mute", sink, "0"]] if up else []
        if current is None:
            return cmds + [["pactl", "set-sink-volume", sink,
                            f"{'+' if up else '-'}{abs(n) * VOLUME_STEP}%"]]
        target = max(0, min(100, current + n * VOLUME_STEP))
        return cmds + [["pactl", "set-sink-volume", sink, f"{target}%"]]
    return []


def read_volume(user, method):
    if method == "wpctl":
        return parse_wpctl_volume(_run(as_user(user, "wpctl", "get-volume",
                                               "@DEFAULT_AUDIO_SINK@")))
    if method == "pactl":
        return parse_pactl_volume(
            _run(as_user(user, "pactl", "get-sink-volume", "@DEFAULT_SINK@")),
            _run(as_user(user, "pactl", "get-sink-mute", "@DEFAULT_SINK@")))
    return None


class Volume:
    """Applies volume steps off the touchpad thread, merging quick ones into one change."""

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending = 0
        threading.Thread(target=self._worker, daemon=True).start()

    def step(self, direction):
        with self.lock:
            self.pending += direction
        self.wake.set()

    def _worker(self):
        while True:
            self.wake.wait()                      # sleeps until a swipe
            self.wake.clear()
            with self.lock:
                n, self.pending = self.pending, 0
            if n:
                try:
                    self.apply(n)
                except Exception as e:
                    log(f"volume error: {e!r}")

    def apply(self, n):
        user = logged_in_user()
        method = volume_method(user)
        if method == "kde":
            for _ in range(abs(n)):
                kde_shortcut(user, "kmix", "increase_volume" if n > 0 else "decrease_volume")
            return
        if method == "keys" and press_key(
                ecodes.KEY_VOLUMEUP if n > 0 else ecodes.KEY_VOLUMEDOWN, abs(n)):
            return
        if method == "keys":                      # uinput failed: pick again without it
            method = volume_method(user)
        if method not in ("wpctl", "pactl"):
            return
        current = read_volume(user, method) if method == "pactl" else None
        for cmd in volume_commands(method, n, current and current[0]):
            _run(as_user(user, *cmd))
        if POPUP != "none":
            now = read_volume(user, method)
            if now:
                notify(user, "volume", *now)


_volume = None


def change_volume(direction):
    global _volume
    if _volume is None:
        _volume = Volume()
    _volume.step(direction)


# ---- brightness ----

def brightness_method(user):
    """'kde' (KWin owns the panel), 'keys' (GNOME owns it) or 'sysfs' (we write it)."""
    desk = desktop_of(user)
    if desk == "kde":
        return "kde"
    if desk == "gnome" and keys_possible():
        return "keys"
    return "sysfs"


def brightness_popup(user):
    if not user or POPUP == "none":
        return "none"
    return {"kde": "Plasma OSD", "keys": "GNOME OSD"}.get(brightness_method(user),
                                                           "notify-send")


def show_osd(percent):
    """Brightness popup for the logged-in user (nothing at the login screen)."""
    user = logged_in_user()
    if not user or POPUP == "none":
        return
    if plasma_running(user[0]):
        _spawn(as_user(
            user, "dbus-send", "--type=method_call", "--dest=org.kde.plasmashell",
            "/org/kde/osdService", "org.kde.osdService.brightnessChanged",
            f"int32:{percent}"))
    else:
        notify(user, "brightness", percent)


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


_kde_displays = {}                                # {uid: D-Bus path of the built-in screen}


def sync_kde(fraction):
    """Tell Plasma the panel's brightness (0..1 of the sysfs range).

    KWin keeps its own copy in ~/.config/kwinoutputconfig.json and applies it
    the moment the desktop starts. Writing sysfs directly never updates that
    copy, so without this the desktop opens at KWin's stale (usually darker)
    level until our login restore kicks in.

    True if Plasma took the change (it then drives the panel itself).
    """
    user = logged_in_user()
    if not user or not plasma_running(user[0]):
        return False
    try:
        path = _kde_displays.get(user[0]) or kde_internal_display(user)
        if not path:
            return False
        r = subprocess.run(as_user(                # flags=1: no OSD popup
            user, "dbus-send", "--print-reply", "--dest=org.kde.ScreenBrightness",
            path, "org.kde.ScreenBrightness.Display.SetBrightness",
            f"int32:{round(fraction * 10000)}", "uint32:1"),
            capture_output=True, timeout=5)
        if r.returncode == 0:
            _kde_displays[user[0]] = path
            return True
        _kde_displays.pop(user[0], None)           # display renumbered: look it up again
    except (OSError, subprocess.SubprocessError) as e:
        log(f"could not update Plasma's brightness: {e}")
    return False


def internal_connectors():
    """Connector names of built-in panels, as KWin spells them (e.g. 'eDP-1')."""
    return {re.sub(r"^card\d+-", "", os.path.basename(c))
            for c in glob.glob(f"{DRM_DIR}/card*-*")
            if re.search(r"eDP|LVDS|DSI", os.path.basename(c))}


def write_kwin_brightness(fraction):
    """Set the built-in panel's brightness in ~/.config/kwinoutputconfig.json.

    Only for users without a running Plasma session (e.g. at the login
    screen): KWin reads the file when the desktop starts, so a swipe at the
    login screen carries over instead of the desktop opening at its old level.
    A running KWin would overwrite the file; sync_kde() covers that case.
    """
    names = internal_connectors()
    if not names:
        return
    for user in pwd.getpwall():
        if user.pw_uid < 1000 or plasma_running(user.pw_uid):
            continue
        path = Path(user.pw_dir, ".config", "kwinoutputconfig.json")
        try:
            st = path.stat()
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        changed = False
        for section in doc if isinstance(doc, list) else []:
            if not isinstance(section, dict) or section.get("name") != "outputs":
                continue
            for out in section.get("data", []):
                if (isinstance(out, dict) and out.get("connectorName") in names
                        and "brightness" in out and out["brightness"] != round(fraction, 4)):
                    out["brightness"] = round(fraction, 4)
                    changed = True
        if not changed:
            continue
        tmp = path.with_name(path.name + ".trackpad-edges.tmp")
        try:
            tmp.write_text(json.dumps(doc, indent=4, ensure_ascii=False) + "\n")
            os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, st.st_mode & 0o7777)
            tmp.replace(path)                     # KWin never sees a half-written file
        except OSError as e:
            log(f"could not update {path}: {e}")
            tmp.unlink(missing_ok=True)


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


def parse_ddcutil_detect(text):
    """`ddcutil detect --brief` -> [(i2c bus, monitor name)] of external monitors."""
    found = []
    for block in re.split(r"\n(?=Display \d|Invalid display)", "\n" + (text or "")):
        block = block.strip()
        if not block.startswith("Display "):      # skips "Invalid display" entries
            continue
        bus = re.search(r"/dev/i2c-(\d+)", block)
        if not bus or re.search(r"eDP|LVDS|DSI", block):   # skip the built-in panel
            continue
        mon = re.search(r"Monitor:\s*(.+)", block)
        found.append((bus.group(1), mon.group(1).strip() if mon else ""))
    return found


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
        self.last_ddcutil = -DDCUTIL_RESCAN
        self.ddc_dead = {}            # {bus: timestamp to retry at}
        self.synced = set()           # buses already sent the current level
        self.ddc_fails = {}           # {bus: failed setvcp in a row}
        self.curve = None             # PanelCurve, loaded on first use
        self.restore_at = []          # times to put the saved level back
        self.saved = load_state() if REMEMBER_BRIGHTNESS else None
        self.known = None             # panel nits as last set or seen
        self.save_at = None
        self.session_uid = None
        self.driven_at = 0.0          # when we last set the panel
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
            self._sync_kwin_files()

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
        if t is not None and now - self.driven_at > SETTLE_TIME:
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

    def _sync_kwin_files(self):
        """Saved level -> KWin's config of every user not in a Plasma session."""
        bl = backlight()
        if bl is None or self.saved is None:
            return
        curve = self._get_curve(bl)
        t_min, t_max, _ = self._range(bl)
        t = min(t_max, max(t_min, self.saved))
        write_kwin_brightness(curve.requested(t, SCREEN_NITS["internal"]) / curve.max)

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
        # Fall back to asking ddcutil, but rarely: `ddcutil detect` can take
        # seconds, and with no monitor plugged in it would otherwise run on
        # every swipe and hold up the brightness change.
        if not found and self.last_scan - self.last_ddcutil > DDCUTIL_RESCAN:
            self.last_ddcutil = self.last_scan
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
        try:
            r = subprocess.run(["ddcutil", "detect", "--brief"], capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"ddcutil detect could not run: {e}")
            return []
        found = [(bus, self._peak_for(name)) for bus, name in parse_ddcutil_detect(r.stdout)]
        if not found:
            log(f"ddcutil detect saw no monitors. stdout={r.stdout[:300]!r} stderr={r.stderr[:300]!r}")
        return found

    def _range(self, bl):
        """(dimmest nits, brightest nits, gamma) of the shared range."""
        peak_int = SCREEN_NITS["internal"]
        peaks = [peak_int] + [p for _, p in self.externals if p]
        t_max = max(peaks)                 # brightest screen sets the ceiling;
        gamma = self._get_curve(bl).gamma  # a dimmer monitor just saturates
        return t_max * MIN_BRIGHTNESS ** gamma, t_max, gamma

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
        if brightness_method(logged_in_user()) == "keys" and press_key(
                ecodes.KEY_BRIGHTNESSUP if n > 0 else ecodes.KEY_BRIGHTNESSDOWN, abs(n)):
            # GNOME moves the panel (and its slider and OSD); monitors follow it
            time.sleep(0.4)
            t = self._panel_nits(bl)
            if t is not None:
                self.known, self.driven_at = t, time.time()
                self._push_all(t)
                self._remember(t)
            return
        if self.known is not None and time.time() - self.driven_at < SETTLE_TIME:
            t = self.known                  # mid-swipe: the panel may still be fading
        else:
            t = self._panel_nits(bl)        # follows KDE's own brightness keys too
        if t is None:
            return                          # panel vanished; nothing to drive
        _, t_max, gamma = self._range(bl)
        level = (max(t, 0.0) / t_max) ** (1 / gamma)   # nits -> slider position
        level = min(1.0, max(MIN_BRIGHTNESS, level + n * BRIGHTNESS_STEP))
        t = t_max * level ** gamma
        fraction = self._drive(bl, t)
        show_osd(round(fraction * 100))     # the same % as Plasma's brightness slider
        self._remember(t)

    def restore(self):
        """Put every screen back at the remembered brightness."""
        if self.saved is None:
            return
        bl = backlight()
        if bl is None:
            return
        self._maybe_rescan()
        t_min, t_max, _ = self._range(bl)
        t = min(t_max, max(t_min, self.saved))    # keeps self.saved for a bigger monitor
        if brightness_method(logged_in_user()) == "keys":
            t = self._panel_nits(bl)              # GNOME restores its own level: match it
            if t is None:
                return
        log(f"restoring brightness: {t:.0f} nits")
        self.synced.clear()                       # a resumed monitor may have reset
        self._drive(bl, t)

    def _drive(self, bl, t):
        curve = self._get_curve(bl)
        want = max(1, round(curve.requested(t, SCREEN_NITS["internal"])))
        # On Plasma, KWin owns the backlight: hand it the level so the tray
        # slider and KWin's saved brightness follow. Writing sysfs as well
        # would fight KWin's fade. GNOME owns it too (we only follow it there).
        # Elsewhere, and at the login screen, write it ourselves.
        method = brightness_method(logged_in_user())
        if method == "keys":
            pass
        elif not (method == "kde" and sync_kde(want / curve.max)):
            try:
                cur = int(Path(bl, "brightness").read_text())
                if want != cur:                # skip a pointless sysfs write
                    Path(bl, "brightness").write_text(str(want))
            except OSError as e:
                log(f"panel brightness write failed: {e}")
        self.known = t
        self.driven_at = time.time()
        self._push_all(t)
        return want / curve.max

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

def is_touchpad(caps, props):
    """Multitouch X/Y plus finger tool, and not a touchscreen (INPUT_PROP_DIRECT)."""
    axes = {code for code, _ in caps.get(ecodes.EV_ABS, [])}
    return (ecodes.ABS_MT_POSITION_X in axes and ecodes.ABS_MT_POSITION_Y in axes
            and ecodes.BTN_TOOL_FINGER in caps.get(ecodes.EV_KEY, [])
            and ecodes.INPUT_PROP_DIRECT not in props)


def touchpad_rank(name):
    """Lower is better: the configured name, then anything called a touchpad."""
    if TOUCHPAD_NAME and re.search(TOUCHPAD_NAME, name, re.IGNORECASE):
        return 0
    return 1 if "touchpad" in name.lower() else 2


def find_touchpad():
    """(device, {abs code: AbsInfo}) of the best touchpad, or None."""
    best = None
    for path in list_devices():
        try:
            dev = InputDevice(path)
            caps = dev.capabilities()
            ok = is_touchpad(caps, dev.input_props())
        except OSError:
            continue
        rank = touchpad_rank(dev.name)
        if ok and (best is None or rank < best[0]):
            if best:
                best[1].close()
            best = (rank, dev, dict(caps[ecodes.EV_ABS]))
        else:
            dev.close()
    return best[1:] if best else None


def axis_range(info):
    return info.min, info.max


def main():
    global screens
    load_config()
    screens = Screens()
    waited = 0
    while True:
        found = find_touchpad()
        if not found:                             # not up yet: keep looking, slower over time
            if waited in (0, 30):
                log("no touchpad found yet; `sudo trackpad_edges.py --check` explains why")
            time.sleep(1 if waited < 30 else 5)
            waited += 1
            continue
        waited = 0
        dev, axes = found
        log(f"using touchpad {dev.name!r} ({dev.path})")
        detector = EdgeSwipe(axis_range(axes[ecodes.ABS_MT_POSITION_X]),
                             axis_range(axes[ecodes.ABS_MT_POSITION_Y]), on_step)
        try:
            for ev in dev.read_loop():
                detector.feed(ev)
        except OSError:
            pass                                  # device vanished (suspend/replug)
        finally:
            dev.close()
        time.sleep(1)


def check():
    """Print what was detected and how each edge will act. Changes nothing."""
    def row(label, value):
        print(f"{label:<14}{value}")

    row("config", CONFIG_LOADED or f"none (defaults; expected {CONFIG_FILE})")
    found = find_touchpad()
    if found:
        dev, axes = found
        x, y = axes[ecodes.ABS_MT_POSITION_X], axes[ecodes.ABS_MT_POSITION_Y]
        row("touchpad", f"{dev.name} ({dev.path})  x {x.min}..{x.max}  y {y.min}..{y.max}")
        dev.close()
    else:
        row("touchpad", "NOT FOUND (no device with multitouch X/Y and a finger tool)")
        for path in list_devices():
            try:
                d = InputDevice(path)
                row("", f"seen: {d.name} ({path})")
                d.close()
            except OSError:
                pass

    bl = backlight()
    if bl:
        max_b = int(Path(bl, "max_brightness").read_text())
        curve = PanelCurve(max_b)
        cal = "calibrated" if curve.load() else "not calibrated yet (one flash on first swipe)"
        row("backlight", f"{bl}  max {max_b}  curve {cal}")
    else:
        row("backlight", "none (no laptop panel found; external monitors only)")

    mons = Screens(start=False)
    mons._scan()
    if mons.externals:
        for bus, peak in mons.externals:
            row("monitor", f"i2c-{bus}  peak {f'{peak:g} nits' if peak else 'unlisted (as bright as the panel)'}")
    else:
        row("monitor", "no DDC/CI monitor found" + ("" if shutil.which("ddcutil") else " (ddcutil not installed)"))
    if not glob.glob("/dev/i2c-*"):
        row("", "no /dev/i2c-* (load i2c-dev: sudo modprobe i2c-dev)")

    user = logged_in_user()
    if user:
        row("session", f"{user.name} (uid {user.uid}, session {user.sid}, {user.kind or '?'})")
        row("desktop", f"{desktop_of(user)} (logind says {user.desktop or 'nothing'!r})")
    else:
        row("session", "none (login screen): brightness works, volume waits for a login")
    vm = volume_method(user)
    row("volume", f"{vm or 'none'}  popup: {volume_popup(vm)}" if user else "after login")
    row("brightness", f"{brightness_method(user)} + ddcutil  popup: {brightness_popup(user)}")
    for edge, action in (("left edge", LEFT_EDGE), ("right edge", RIGHT_EDGE)):
        row(edge, action)
    if user and "notify-send" in (volume_popup(vm), brightness_popup(user)):
        if not shutil.which("notify-send"):
            row("", "notify-send missing: install libnotify (libnotify-bin on Debian/Ubuntu)")
        elif not notification_daemon(user):
            row("", "no notification daemon (e.g. mako, dunst, swaync): no popups")
    return 0 if found else 1


def notification_daemon(user):
    """True if something answers (or would be started for) org.freedesktop.Notifications."""
    def bus(method, *args):
        return _run(as_user(user, "dbus-send", "--session", "--print-reply",
                            "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus",
                            f"org.freedesktop.DBus.{method}", *args)) or ""
    name = "org.freedesktop.Notifications"
    return "true" in bus("NameHasOwner", f"string:{name}") or \
        f'"{name}"' in bus("ListActivatableNames")


def script_nits(path):
    """SCREEN_NITS from an older installed copy of this script, or {}."""
    import ast
    try:
        tree = ast.parse(Path(path).read_text())
    except (OSError, SyntaxError, ValueError):
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "SCREEN_NITS" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return {}
            return {k: float(v) for k, v in value.items() if isinstance(v, (int, float))}
    return {}


USAGE = """usage: trackpad_edges.py [--check | --status | --calibrate]
  --check      show the touchpad, screens, session and methods it will use
  --status     show each screen's brightness in nits
  --calibrate  re-measure the laptop panel's brightness curve"""


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--migrate-config":                # used by install.sh
        # --migrate-config TEMPLATE OUT [OLD_SCRIPT]
        extra = script_nits(sys.argv[4]) if len(sys.argv) > 4 else {}
        settings, nits = migrate_config(sys.argv[2], sys.argv[3], extra)
        print(f"   carried over: {', '.join(k.lower() for k in settings) or 'no settings'}; "
              f"nits: {', '.join(f'{k}={v:g}' for k, v in nits.items())}")
        sys.exit(0)
    if mode:
        load_config()
    if mode in ("--check", "-c"):
        sys.exit(check())
    if mode in ("--status", "-s"):
        sys.exit(status())
    if mode == "--calibrate":
        sys.exit(recalibrate())
    if mode in ("--help", "-h"):
        print(USAGE)
        sys.exit(0)
    if mode:
        print(f"unknown option {mode!r}\n{USAGE}")
        sys.exit(2)
    main()
