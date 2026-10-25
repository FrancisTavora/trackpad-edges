#!/usr/bin/env bash
# Removes trackpad-edges. Pass --purge to also delete config and saved state.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi
systemctl disable --now trackpad-edges.service 2>/dev/null || true
rm -f /etc/systemd/system/trackpad-edges.service /usr/local/bin/trackpad_edges.py \
      /etc/modules-load.d/trackpad-edges.conf
systemctl daemon-reload
if [[ "${1:-}" == "--purge" ]]; then
    rm -rf /etc/trackpad-edges /var/lib/trackpad-edges
fi
echo "trackpad-edges removed."
