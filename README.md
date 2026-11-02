# trackpad-edges

Zenbook-style edge swipes for Linux touchpads:

- **Left edge**: slide up/down to change the **volume**
- **Right edge**: slide up/down to change the **brightness** of the laptop screen
  *and* any external monitors (via DDC/CI), matched by real brightness in nits
  so every screen looks equally bright

It is one small Python process that sleeps until you touch the pad. It runs as
a system service, so brightness works even at the login screen, and it
**remembers your last brightness** across reboots, logins and suspend.

Touches that start in the top 20% of the pad are ignored, so it plays nicely
with the [ASUS NumberPad driver](https://github.com/asus-linux-drivers/asus-numberpad-driver).
Two-finger gestures and normal pointer use in the middle of the pad are never
touched.

## Install

```sh
git clone https://github.com/FrancisTavora/trackpad-edges.git
cd trackpad-edges
sudo sh install.sh
```

`install.sh` is plain POSIX `sh`, so the same command works from bash, zsh or
fish. It installs these packages, loads `i2c-dev` and `uinput` at boot,
stops any older copy, installs the service, starts it, and finishes by
running `trackpad_edges.py --check`:

| Distro family | Package manager | Packages installed |
|---|---|---|
| Arch, CachyOS, Manjaro, EndeavourOS | pacman | `python python-evdev ddcutil libnotify` |
| Fedora | dnf | `python3 python3-evdev ddcutil libnotify` |
| Ubuntu, Debian, Mint, Pop!_OS | apt | `python3 python3-evdev ddcutil libnotify-bin` |
| openSUSE | zypper | `python3 python3-evdev ddcutil libnotify-tools` |

On another distro, install the equivalents yourself and run
`sudo sh install.sh --no-deps`. You need systemd, Python 3.8+ (so not
openSUSE Leap 15, whose Python is 3.6) and a touchpad that
reports multitouch positions (nearly all do).

### What each desktop gets

| Desktop | Volume | Brightness | Popup |
|---|---|---|---|
| KDE Plasma | Plasma's volume shortcut | through KWin, so the tray slider follows | Plasma's OSD |
| GNOME | GNOME's volume keys (virtual keyboard) | GNOME's brightness keys; monitors follow the panel | GNOME's OSD |
| niri, Hyprland, Sway, Cinnamon, XFCE, others | `wpctl`, else `pactl` | written to the backlight directly | `notify-send` with a progress bar |
| Login screen (any) | — | written to the backlight directly | — |

GNOME is driven through its own keys because GNOME Shell only lets its own
services show its OSD, and because GNOME owns the backlight while you're
logged in.

Volume up always unmutes, and volume never goes past 100%. For the
`notify-send` popups you need a notification daemon (mako, dunst, swaync...).
Most desktops ship one; on niri, Hyprland or Sway you might have to install
one yourself.

### Update / uninstall

```sh
git pull && sudo sh install.sh              # update; your config is kept
sudo sh install.sh --uninstall              # remove (or: sudo sh uninstall.sh)
sudo sh install.sh --uninstall --purge      # also delete config and saved brightness
```

**Updating from an older version** (the one with
`/etc/trackpad-edges/config.toml`): just `git pull` and run
`sudo sh install.sh` again. It stops the old service, moves your settings and
monitor nits into the new `/etc/trackpad-edges.conf`, and leaves the old file
in place, unused. To check which version is running, run
`trackpad_edges.py --help`. The new version lists `--check`.

## Configure

Edit `/etc/trackpad-edges.conf`, then run
`sudo systemctl restart trackpad-edges`. The installer creates this file once
and never overwrites it. It carries over settings from the old
`/etc/trackpad-edges/config.toml` if you had one. Every option is documented in
[`trackpad-edges.conf.example`](trackpad-edges.conf.example). These are the
ones you're most likely to change:

```ini
touchpad_name = ELAN.*Touchpad    # only if --check picked the wrong device
right_edge = brightness           # or volume / none
sensitivity = 0.018               # smaller = faster
top_dead_zone = 0                 # no NumberPad? use the whole edge
popup = auto                      # or none

[nits]                            # so monitors match the laptop
internal = 400
U2720Q = 350
```

Monitor names under `[nits]` match any part of the name that
`sudo trackpad_edges.py --check` shows. You rarely need to type these lines
yourself. See below.

## External monitors

You don't need to set anything up. Any monitor with DDC/CI follows the laptop's
brightness: your swipes, and also the brightness keys or the desktop's own
slider. The one exception is KDE Plasma, where only swipes move the
monitors, because Plasma drives external monitors itself.

- **Plugging in**: the service hears about it from the kernel straight away,
  with no polling. Within a second or two the monitor is set to the current
  brightness. If its DDC/CI is still waking up, it tries again a few
  seconds later.
- **How bright the monitor really is**: to look *equally* bright, every
  screen is driven to the same nits, so the service needs each monitor's
  peak brightness.
  1. If it's listed under `[nits]`, that number is used.
  2. HDR monitors report their own peak in their EDID, and that's used
     automatically.
  3. Otherwise the monitor is treated as being as bright as the laptop panel.
     The first time such a monitor is plugged in, you get one popup that
     suggests the next step.
- **Matching one by eye** takes about a minute per monitor:

  ```sh
  sudo trackpad_edges.py --match-monitors
  ```

  The laptop screen is set to a fixed level. For each monitor, use the arrow
  keys until it looks as bright as the laptop. Looking at something white on
  both helps. Use up/down for 1% steps and left/right for 5%. Enter saves,
  `s` skips and `q` quits. The result goes into `[nits]` in
  `/etc/trackpad-edges.conf`, and the rest of your file is left alone. The
  service pauses while you do this. Afterwards your brightness is put back
  and the service restarts.

## Troubleshooting

Start here. It shows what the service sees and what it will do:

```sh
sudo trackpad_edges.py --check
```

Example output:

```
config        /etc/trackpad-edges.conf
touchpad      ELAN1203:00 04F3:307A Touchpad (/dev/input/event8)  x 0..3200  y 0..2000
backlight     /sys/class/backlight/amdgpu_bl1  max 255  curve calibrated
monitor       DELL U2720Q (i2c-4)  peak 350 nits
session       alice (uid 1000, session 2, wayland)
desktop       kde (logind says 'KDE')
volume        kde  popup: Plasma OSD
brightness    kde + ddcutil  popup: Plasma OSD
left edge     volume
right edge    brightness
```

- **`--check` is an unknown option**: the old version is still installed. Run
  `git pull`, then `sudo sh install.sh` again from the repo.
- **`touchpad NOT FOUND`**: the list under it shows every input device.
  Set `touchpad_name` to a regex for yours.
- **Swipes do nothing**: check `journalctl -u trackpad-edges -f` while you
  swipe. Start in the bottom 80% of the very edge (the outer 7%).
- **`session none` while logged in**: logind doesn't see a graphical session
  on seat0 (`loginctl list-sessions`). Volume only works for that session.
- **Volume does nothing**: run `wpctl status` as your user. If you use
  PulseAudio, make sure `pactl` is installed.
- **No popup on niri/Hyprland/Sway**: install and start a notification daemon
  such as mako. `--check` says when none is running.
- **A monitor looks brighter or dimmer than the laptop**: run
  `sudo trackpad_edges.py --match-monitors`.
- **External monitor doesn't change**: turn on DDC/CI in the monitor's own
  menu, and check `sudo ddcutil detect`. Some portable/USB-C monitors don't
  support DDC/CI at all. If `--check` says there is no `/dev/i2c-*`, run
  `sudo modprobe i2c-dev`.
- **The panel flashes once on the first brightness swipe**: that's the
  one-time measurement of the panel's brightness curve. It's cached in
  `/var/lib/trackpad-edges/panel.json`.

More tools:

```sh
journalctl -u trackpad-edges -f          # live log (shows monitors as they're plugged in)
sudo trackpad_edges.py --match-monitors  # match external monitors to the laptop by eye
sudo trackpad_edges.py --status          # brightness of every screen + saved level
sudo trackpad_edges.py --calibrate       # re-measure the laptop panel's curve
```

## How light it is

It's one Python process with three threads, and all three sleep until
something happens:
- the touchpad reader, until the pad sends events;
- the hotplug listener, until the kernel reports a display change;
- the brightness worker, which wakes every 2 seconds for a few cheap file
  reads (it remembers brightness and notices the brightness keys).

On the laptop it was measured on, that came to about 30 MB of RAM and under
0.1% CPU while idle. While the touchpad is in use it costs about half a
microsecond per touch event. Commands that have to run in your desktop
session (volume, popups, Plasma's brightness) start directly as your user,
with no `runuser` or `sudo` in between.

## Development

```sh
python3 -m unittest discover -s tests     # unit + kernel tests
sh tests/distros.sh                       # installer on each distro family (docker)
```

- **Unit tests** feed fake touch events, config files, EDIDs, kernel hotplug
  messages, `ddcutil` output and volume/session command output. One test
  replays random swipes through the original detector and the current one,
  to prove the swipe feel hasn't changed.
- **Kernel tests** (`tests/test_e2e_uinput.py`) create a virtual touchpad and
  a virtual touchscreen with uinput, then swipe through the real kernel. They
  check that the touchpad is picked, that its axis ranges are used, and that
  edge, corner and middle touches behave. They also check that the service's
  own event reader sees the same swipe and stops cleanly when the pad is
  unplugged. They need access to `/dev/uinput`
  and `/dev/input`, through root or the `uinput` and `input` groups, and are
  skipped otherwise. The virtual pad is grabbed, so your desktop never sees
  the swipes.
- **`tests/distros.sh`** runs the real `install.sh` (package names, `/bin/sh`
  being dash on Debian/Ubuntu) and the unit tests on Arch, Fedora,
  Debian 12, Ubuntu 22.04/24.04 and openSUSE Tumbleweed/Leap 16 containers.
  `systemctl` and `modprobe` are stubbed, because containers have no
  systemd.

## Windows / macOS

Not supported. This program reads raw finger positions from the Linux kernel
(`evdev`) and changes brightness through `/sys`. Neither exists on Windows or
macOS.

## License

MIT
