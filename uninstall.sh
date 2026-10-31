#!/bin/sh
# Removes trackpad-edges. Pass --purge to also delete config and saved state.
# Same as: sudo sh install.sh --uninstall [--purge]
exec sh "$(dirname "$0")/install.sh" --uninstall "$@"
