#!/usr/bin/env bash
# Mount/unmount the backup drive declared in /etc/fstab, verified by the on-disk
# marker. No device name, mount path or UUID is stored in the workspace: the
# filesystem owns its UUID, the marker owns its identity, fstab owns the path.
set -Eeuo pipefail
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}/ubuntu-backup.py"
WORKSPACE="${UBUNTU_BACKUP_WORKSPACE:-$HOME/Ubuntu-Backup/data}"
[[ -x "$BIN" ]] || { echo "Install first: ./install.sh" >&2; exit 1; }
case "${1:-status}" in
    mount)
        exec "$BIN" --workspace "$WORKSPACE" mount-saved
        ;;
    unmount)
        exec "$BIN" --workspace "$WORKSPACE" unmount-saved
        ;;
    status)
        exec "$BIN" --workspace "$WORKSPACE" mount-saved --check
        ;;
    *)
        echo "Usage: ./drive.sh {mount|unmount|status}" >&2
        exit 2
        ;;
esac
