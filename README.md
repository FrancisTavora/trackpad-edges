# trackpad-edges

Zenbook-style edge swipes for Linux touchpads:

- **Left edge** — slide up/down to change **volume**
- **Right edge** — slide up/down to change **brightness** of the laptop screen
  *and* any external monitors (via DDC/CI), matched by real brightness in nits
  so every screen looks equally bright

It runs as a system service, so brightness works even at the login screen, and
it **remembers your last brightness** across reboots, logins and suspend.

Touches that start in the top 20% of the pad are ignored, so it plays nicely
with the [ASUS NumberPad driver](https://github.com/asus-linux-drivers/asus-numberpad-driver).
Two-finger gestures and normal pointer use in the middle of the pad are never
touched.

## Install (Linux)

```bash
git clone https://github.com/FrancisTavora/trackpad-edges.git
cd trackpad-edges
./install.sh
```

The installer asks for your password, installs `python-evdev` and `ddcutil`
with your package manager (pacman, apt, dnf or zypper), and starts the
`trackpad-edges` service.

Requirements: systemd, Python 3.11+ (or 3.8+ with `tomli`), a touchpad that
reports multitouch positions (nearly all modern ones do).

| Desktop | Volume | Brightness OSD |
|---|---|---|
| KDE Plasma | ✅ (Plasma's own shortcut + OSD) | ✅ |
| GNOME, XFCE, Hyprland, Sway, … | ✅ via `wpctl` or `pactl` | — (brightness still changes) |

### Update / uninstall

```bash
git pull && ./install.sh          # update, keeps your config
./uninstall.sh                    # remove (add --purge to delete config too)
```

## Configure

Edit `/etc/trackpad-edges/config.toml`, then
`sudo systemctl restart trackpad-edges`. Every option is documented in
[`config.example.toml`](config.example.toml). The ones you'll most likely touch:

```toml
touchpad_name = "ELAN.*Touchpad"   # if the wrong device is picked
right_edge = "brightness"          # or "volume" / "none"
sensitivity = 0.018                # smaller = faster
top_dead_zone = 0.0                # no NumberPad? use the whole edge
remember_brightness = true

[screen_nits]                      # so monitors match the laptop
internal = 400
"Optix G241VC" = 250
```

Find your touchpad name with `sudo libinput list-devices`, and your monitor
names with `sudo ddcutil detect --brief`.

## Troubleshooting

```bash
journalctl -u trackpad-edges -f        # live log
sudo trackpad_edges.py --status        # brightness of every screen + saved level
sudo trackpad_edges.py --calibrate     # re-measure the laptop panel's curve
```

- **External monitor doesn't change:** turn on DDC/CI in the monitor's own menu.
  Some portable/USB-C monitors don't support DDC/CI at all.
- **Panel flashes once on first run:** that's the one-time calibration of the
  panel's brightness curve. It's cached in `/var/lib/trackpad-edges/panel.json`.

## Windows / macOS

Not supported yet. This program reads raw finger positions from the Linux
kernel (`evdev`) and changes brightness through `/sys`, neither of which exist
on Windows or macOS. A Windows port would need a different touch reader
(Raw Input / HID precision-touchpad reports) and WMI for brightness.
Pull requests welcome.

## License

MIT
