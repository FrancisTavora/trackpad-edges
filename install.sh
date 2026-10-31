#!/bin/sh
# Installs trackpad-edges as a system service. POSIX sh, so it runs from any
# shell (bash, zsh, fish...) on Arch/CachyOS/Manjaro, Fedora,
# Debian/Ubuntu/Mint/Pop!_OS and openSUSE.
#   sudo sh install.sh                    install or update (keeps your config)
#   sudo sh install.sh --no-deps          same, without installing packages
#   sudo sh install.sh --uninstall        remove it
#   sudo sh install.sh --uninstall --purge   remove it with config and saved state
set -eu

if [ "$(id -u)" -ne 0 ]; then
    exec sudo sh "$0" "$@"
fi
cd "$(dirname "$0")"

BIN=/usr/local/bin/trackpad_edges.py
UNIT=/etc/systemd/system/trackpad-edges.service
CONF=/etc/trackpad-edges.conf
MODULES=/etc/modules-load.d/trackpad-edges.conf

deps=1 uninstall=0 purge=0
for arg in "$@"; do
    case "$arg" in
        --no-deps) deps=0 ;;
        --uninstall) uninstall=1 ;;
        --purge) purge=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd is required." >&2
    exit 1
fi

stop_old_copies() {
    systemctl disable --now trackpad-edges.service >/dev/null 2>&1 || true
    # copies started by hand or from a desktop's autostart
    pkill -f 'trackpad_edges\.py' 2>/dev/null || true
    for f in /home/*/.config/autostart/*trackpad*edges*.desktop \
             /root/.config/autostart/*trackpad*edges*.desktop; do
        [ -e "$f" ] || continue
        mv "$f" "$f.disabled"
        echo "   disabled autostart entry $f"
    done
    for f in /home/*/.config/systemd/user/*.wants/*trackpad*edges*; do
        [ -e "$f" ] || [ -L "$f" ] || continue
        rm -f "$f"
        echo "   disabled user service $f"
    done
}

if [ "$uninstall" -eq 1 ]; then
    echo "==> Removing trackpad-edges"
    stop_old_copies
    rm -f "$UNIT" "$BIN" "$MODULES"
    systemctl daemon-reload
    if [ "$purge" -eq 1 ]; then
        rm -rf "$CONF" /etc/trackpad-edges /var/lib/trackpad-edges
        echo "   deleted config and saved brightness"
    else
        echo "   kept $CONF (add --purge to delete it)"
    fi
    echo "trackpad-edges removed."
    exit 0
fi

if [ "$deps" -eq 1 ]; then
    echo "==> Installing dependencies (python evdev, ddcutil, notify-send)"
    if command -v pacman >/dev/null 2>&1; then
        pacman -S --needed --noconfirm python python-evdev ddcutil libnotify
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update
        apt-get install -y python3 python3-evdev ddcutil libnotify-bin
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-evdev ddcutil libnotify
    elif command -v zypper >/dev/null 2>&1; then
        zypper --non-interactive install python3 python3-evdev ddcutil libnotify-tools
    else
        echo "Unknown package manager: install python3, python evdev, ddcutil and" >&2
        echo "notify-send yourself, then run: sudo sh install.sh --no-deps" >&2
        exit 1
    fi
fi

python3 -c "import evdev" || { echo "python evdev is not importable" >&2; exit 1; }

echo "==> Loading i2c-dev (monitor brightness) and uinput (GNOME media keys)"
printf 'i2c-dev\nuinput\n' > "$MODULES"
modprobe i2c-dev || echo "   could not load i2c-dev; external monitors won't change"
modprobe uinput || echo "   could not load uinput; GNOME falls back to wpctl and notify-send"

echo "==> Stopping any old copy"
stop_old_copies

echo "==> Installing files"
if [ -e "$CONF" ]; then
    echo "   kept your existing $CONF"
else
    # carries over /etc/trackpad-edges/config.toml and the old script's nits
    python3 trackpad_edges.py --migrate-config trackpad-edges.conf.example "$CONF" "$BIN"
    echo "   created $CONF"
fi
install -Dm755 trackpad_edges.py "$BIN"
install -Dm644 trackpad-edges.service "$UNIT"

echo "==> Starting the service"
systemctl daemon-reload
systemctl enable trackpad-edges.service
systemctl restart trackpad-edges.service

echo
echo "==> What it found"
python3 "$BIN" --check || true
echo
echo "Done. Settings: $CONF, then: sudo systemctl restart trackpad-edges"
echo "Logs: journalctl -u trackpad-edges -f"
