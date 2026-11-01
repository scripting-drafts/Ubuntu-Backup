#!/usr/bin/env bash
# Mount or safely unmount the backup drive without the automount timer racing.
set -Eeuo pipefail
DEVICE="${1:-/dev/sda1}"
ACTION="${2:-status}"
status() {
    if mountpoint="$(findmnt -S "$DEVICE" -no TARGET 2>/dev/null)"; then
        echo "Mounted: $mountpoint"
    else
        echo "Not mounted: $DEVICE"
    fi
    systemctl --user is-enabled ubuntu-backup-mount.timer 2>/dev/null \
        | sed 's/^/Automount timer: /' || true
}
case "$ACTION" in
    mount)
        systemctl --user enable --now ubuntu-backup-mount.timer >/dev/null
        systemctl --user start ubuntu-backup-mount.service
        status
        ;;
    unmount)
        systemctl --user disable --now ubuntu-backup-mount.timer >/dev/null || true
        sync
        if findmnt -S "$DEVICE" -no TARGET >/dev/null 2>&1; then
            udisksctl unmount -b "$DEVICE"
        fi
        status
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: ./drive.sh [DEVICE] {mount|unmount|status}" >&2
        exit 2
        ;;
esac
