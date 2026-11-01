#!/usr/bin/env bash
# Remove the Ubuntu Backup program; backups and runtime traces are preserved unless purged.
set -Eeuo pipefail
DRY=0
YES=0
PURGE_BACKUPS=0
for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY=1 ;;
        --yes) YES=1 ;;
        --purge-backups) PURGE_BACKUPS=1 ;;
        *) echo "Usage: ./uninstall.sh [--dry-run] [--yes] [--purge-backups]" >&2; exit 2 ;;
    esac
done
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
APPS="$DATA/applications"
ICON="$DATA/icons/hicolor/scalable/apps/ubuntu-backup.svg"
AUTOSTART="$CONFIG/autostart/ubuntu-backup-tray.desktop"
WORKSPACES=("$HOME/ubuntu-backup-data" "$HOME/ubuntu-configuration")
FILES=("$BIN/ubuntu-backup.py" "$HOME/ubuntu-backup.py" "$HOME/ubuntu-configuration.py"
       "$HOME/ubuntu-backup.zip" "$APPS/ubuntu-backup.desktop" "$AUTOSTART" "$ICON"
       "$CONFIG/systemd/user/ubuntu-configuration-backup.service"
       "$CONFIG/systemd/user/ubuntu-configuration-backup.timer")
echo "Program files to remove:"
for path in "${FILES[@]}"; do
    [[ -e "$path" || -L "$path" ]] && echo "  $path"
done
echo "Workspaces preserved (backups and runtime traces):"
for path in "${WORKSPACES[@]}"; do
    [[ -d "$path" ]] && echo "  $path"
done
if (( PURGE_BACKUPS )); then
    echo "Backup data will also be purged: local backups/snapshots/profiles and configured external timelines."
fi
if (( DRY )); then
    echo "Dry run only; nothing removed."
    exit 0
fi
confirm() {
    local answer default="${2:-no}" label
    [[ "$default" == yes ]] && label="[Y/n]" || label="[y/N]"
    read -r -p "$1 $label: " answer || return 1
    case "${answer,,}" in
        y|yes) return 0 ;;
        n|no) return 1 ;;
        "") [[ "$default" == yes ]] ;;
        *) return 1 ;;
    esac
}
if (( ! YES )); then
    confirm "Remove Ubuntu Backup program files?" yes || { echo "Cancelled."; exit 1; }
fi
if (( PURGE_BACKUPS )); then
    confirm "Permanently delete all backup data?" no || { echo "Cancelled; backups preserved."; exit 1; }
fi
python3 - "$BIN/ubuntu-backup.py" <<'PY'
import os, signal, sys
target = os.path.realpath(sys.argv[1])
for entry in os.listdir('/proc'):
    if not entry.isdigit():
        continue
    try:
        args = open(f'/proc/{entry}/cmdline', 'rb').read().split(b'\0')
        if target.encode() in args and b'tray' in args:
            os.kill(int(entry), signal.SIGTERM)
    except (OSError, ValueError):
        pass
PY
if [[ "${UBUNTU_BACKUP_SKIP_SERVICE:-0}" != 1 ]]; then
    systemctl --user disable --now ubuntu-configuration-backup.timer 2>/dev/null || true
fi
for path in "${FILES[@]}"; do
    [[ -e "$path" || -L "$path" ]] && rm -rf -- "$path"
done
if (( PURGE_BACKUPS )); then
    python3 - "${WORKSPACES[@]}" <<'PY'
import json, shutil, sys
from pathlib import Path
for workspace in map(Path, sys.argv[1:]):
    for name in ("backups", "snapshot", ".previous-snapshot", "profiles", "privileged-system"):
        shutil.rmtree(workspace / name, ignore_errors=True)
    try:
        value = json.loads((workspace / "config/settings.json").read_text()).get("external_backup")
    except (OSError, ValueError):
        value = None
    if not isinstance(value, dict) or not value.get("mount_point"):
        continue
    mount = Path(value["mount_point"])
    marker = mount / (value.get("subdirectory") or "") / ".ubuntu-configuration-target.json"
    try:
        marker_value = json.loads(marker.read_text())
    except (OSError, ValueError):
        continue
    if marker_value.get("format") != "ubuntu-configuration-v1":
        continue
    root = marker.parent
    for name in ("baseline", "timeline", "ubuntu-configuration-backups"):
        shutil.rmtree(root / name, ignore_errors=True)
    marker.unlink(missing_ok=True)
    try:
        root.rmdir()
    except OSError:
        pass
    print("Purged backup layout:", root)
PY
fi
SYSTEM_PRESENT=0
for path in /etc/systemd/system/ubuntu-configuration-system.service \
            /etc/systemd/system/ubuntu-configuration-system.timer \
            /usr/local/libexec/ubuntu-configuration-system-capture.py; do
    [[ -e "$path" ]] && SYSTEM_PRESENT=1
done
if (( SYSTEM_PRESENT )) && [[ "${UBUNTU_BACKUP_SKIP_SERVICE:-0}" != 1 ]]; then
    echo "Removing system-wide timer/helper (sudo required)."
    sudo systemctl disable --now ubuntu-configuration-system.timer 2>/dev/null || true
    for path in /etc/systemd/system/ubuntu-configuration-system.service \
                /etc/systemd/system/ubuntu-configuration-system.timer; do
        if [[ -f "$path" ]] && grep -q 'Managed by ubuntu-configuration' "$path"; then
            sudo rm -f -- "$path" || echo "Could not remove: $path"
        fi
    done
    helper=/usr/local/libexec/ubuntu-configuration-system-capture.py
    if [[ -f "$helper" ]] && grep -q 'Managed by ubuntu-configuration' "$helper"; then
        sudo rm -f -- "$helper" || echo "Could not remove: $helper"
    fi
    sudo systemctl daemon-reload 2>/dev/null || true
fi
if [[ "${UBUNTU_BACKUP_SKIP_SERVICE:-0}" != 1 ]]; then
    systemctl --user daemon-reload 2>/dev/null || true
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true
if (( PURGE_BACKUPS )); then
    echo "Uninstall complete. Program traces removed; backups purged; runtime traces preserved."
else
    echo "Uninstall complete. Backups and runtime traces were preserved."
fi
