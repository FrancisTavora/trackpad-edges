#!/usr/bin/env bash
# Installs trackpad-edges as a system service. Works on Arch/CachyOS/Manjaro,
# Debian/Ubuntu/Mint/Pop, Fedora, openSUSE and other systemd distros.
#   ./install.sh            install or update
#   ./install.sh --no-deps  skip installing packages
set -euo pipefail
cd "$(dirname "$0")"

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

if ! command -v systemctl >/dev/null; then
    echo "systemd is required." >&2
    exit 1
fi

if [[ "${1:-}" != "--no-deps" ]]; then
    echo "==> Installing dependencies (python evdev, ddcutil)"
    if command -v pacman >/dev/null; then
        pacman -S --needed --noconfirm python python-evdev ddcutil
    elif command -v apt-get >/dev/null; then
        apt-get update
        apt-get install -y python3 python3-evdev python3-tomli ddcutil
    elif command -v dnf >/dev/null; then
        dnf install -y python3 python3-evdev ddcutil
    elif command -v zypper >/dev/null; then
        zypper --non-interactive install python3 python3-evdev ddcutil
    else
        echo "Unknown package manager: install python3, python-evdev and ddcutil yourself," >&2
        echo "then run ./install.sh --no-deps" >&2
        exit 1
    fi
fi

python3 -c "import evdev" || { echo "python evdev is not importable" >&2; exit 1; }

echo "==> Loading i2c-dev (for external monitor brightness)"
echo i2c-dev > /etc/modules-load.d/trackpad-edges.conf
modprobe i2c-dev || echo "   could not load i2c-dev; external monitors won't change"

echo "==> Installing files"
install -Dm755 trackpad_edges.py /usr/local/bin/trackpad_edges.py
install -Dm644 trackpad-edges.service /etc/systemd/system/trackpad-edges.service
if [[ ! -e /etc/trackpad-edges/config.toml ]]; then
    install -Dm644 config.example.toml /etc/trackpad-edges/config.toml
    echo "   created /etc/trackpad-edges/config.toml"
else
    echo "   kept your existing /etc/trackpad-edges/config.toml"
fi

echo "==> Starting the service"
systemctl daemon-reload
systemctl enable trackpad-edges.service
systemctl restart trackpad-edges.service

echo
echo "Done. Edit /etc/trackpad-edges/config.toml, then: sudo systemctl restart trackpad-edges"
echo "Logs: journalctl -u trackpad-edges -f     Status: sudo trackpad_edges.py --status"
