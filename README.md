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
`sudo sh install.sh --no-deps`. You need systemd, Python 3 and a touchpad that
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
G241VC = 250
```

Monitor names under `[nits]` match any part of the name that
`sudo ddcutil detect --brief` prints.

## Troubleshooting

Start here. It shows what the service sees and what it will do:

```sh
sudo trackpad_edges.py --check
```

Example output:

```
config        /etc/trackpad-edges.conf
touchpad      ASUE140D:00 04F3:31BC Touchpad (/dev/input/event8)  x 0..3880  y 0..2299
backlight     /sys/class/backlight/amdgpu_bl1  max 400000  curve calibrated
monitor       no DDC/CI monitor found
session       ferin (uid 1000, session 2, wayland)
desktop       kde (logind says 'KDE')
volume        kde  popup: Plasma OSD
brightness    kde + ddcutil  popup: Plasma OSD
left edge     volume
right edge    brightness
```

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
- **External monitor doesn't change**: turn on DDC/CI in the monitor's own
  menu, and check `sudo ddcutil detect`. Some portable/USB-C monitors don't
  support DDC/CI at all. If `--check` says there is no `/dev/i2c-*`, run
  `sudo modprobe i2c-dev`.
- **The panel flashes once on the first brightness swipe**: that's the
  one-time measurement of the panel's brightness curve. It's cached in
  `/var/lib/trackpad-edges/panel.json`.

More tools:

```sh
journalctl -u trackpad-edges -f        # live log
sudo trackpad_edges.py --status        # brightness of every screen + saved level
sudo trackpad_edges.py --calibrate     # re-measure the laptop panel's curve
```

## Development

```sh
python3 -m unittest discover -s tests
```

The tests feed fake touch events, config files, `ddcutil` output and
volume/session command output, so they run without a touchpad or root.

## Windows / macOS

Not supported. This program reads raw finger positions from the Linux kernel
(`evdev`) and changes brightness through `/sys`. Neither exists on Windows or
macOS.

## License

MIT
