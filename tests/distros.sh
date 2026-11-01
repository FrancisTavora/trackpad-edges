#!/bin/sh
# Runs the real installer and the unit tests in a container per distro family.
# Needs docker (or podman: DOCKER=podman). systemd can't run in a plain
# container, so systemctl/modprobe are stubbed; everything else is real:
# the package manager, package names, /bin/sh (dash on Debian/Ubuntu) and Python.
#   sh tests/distros.sh                 all of them
#   sh tests/distros.sh debian:12       just one
set -eu
cd "$(dirname "$0")/.."
DOCKER=${DOCKER:-docker}
IMAGES=${*:-"archlinux:latest fedora:latest debian:12 ubuntu:22.04 ubuntu:24.04 opensuse/tumbleweed opensuse/leap:16.0"}

# What runs inside each container, as root, from a copy of the repo.
# shellcheck disable=SC2016  # expanded inside the container, not here
inner='
set -eu
for cmd in systemctl modprobe; do
    printf "#!/bin/sh\necho \"[stub] %s \$*\"\n[ \"\$1\" = is-active ] && exit 3\nexit 0\n" "$cmd" > /usr/local/sbin/$cmd
    chmod +x /usr/local/sbin/$cmd
done
# a fresh Arch image has no package database; a real system has one
command -v pacman >/dev/null && pacman -Syu --noconfirm >/dev/null
cp -r /src /repo && cd /repo
echo "--- /bin/sh is $(readlink -f /bin/sh)"
sh install.sh
echo "--- config"
cat /etc/trackpad-edges.conf | grep -v "^#" | grep .
echo "--- --check (no touchpad in a container, so exit 1 is expected)"
python3 /usr/local/bin/trackpad_edges.py --check || true
echo "--- python $(python3 -V 2>&1)"
python3 -m unittest discover -s tests > /tmp/unittest.log 2>&1 || { cat /tmp/unittest.log; exit 1; }
tail -3 /tmp/unittest.log
command -v notify-send ddcutil runuser
echo "--- uninstall"
sh install.sh --uninstall --purge
test ! -e /usr/local/bin/trackpad_edges.py && test ! -e /etc/trackpad-edges.conf
echo "PASS"
'

status=0
for image in $IMAGES; do
    echo "=================== $image"
    if $DOCKER run --rm -v "$PWD:/src:ro" "$image" sh -c "$inner"; then
        echo "=================== $image: ok"
    else
        echo "=================== $image: FAILED"
        status=1
    fi
done
exit $status
