#!/usr/bin/env bash
# Purge the Ubuntu Backup program and runtime traces; preserve every backup file.
set -Eeuo pipefail
DRY=0
YES=0
for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY=1 ;;
        --yes) YES=1 ;;
        *) echo "Usage: ./uninstall.sh [--dry-run] [--yes]" >&2; exit 2 ;;
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
echo "Workspaces to clean (backup directories are preserved):"
for path in "${WORKSPACES[@]}"; do
    [[ -d "$path" ]] && echo "  $path"
done
if (( DRY )); then
    echo "Dry run only; nothing removed."
    exit 0
fi
if (( ! YES )); then
    read -r -p "Type PURGE PROGRAM to remove program files and keep backups: " answer
    [[ "$answer" == "PURGE PROGRAM" ]] || { echo "Cancelled."; exit 1; }
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
# Remove program state while keeping backup, snapshot, restore and profile data.
shopt -s nullglob dotglob
for workspace in "${WORKSPACES[@]}"; do
    [[ -d "$workspace" ]] || continue
    for entry in "$workspace"/*; do
        name="$(basename "$entry")"
        case "$name" in
            backups|snapshot|.previous-snapshot|restored|rollback|profiles|privileged-system)
                continue
                ;;
        esac
        rm -rf -- "$entry"
    done
done
# External backup layouts are intentionally left untouched.
if sudo -n true 2>/dev/null; then
    for path in /etc/systemd/system/ubuntu-configuration-system.service \
                /etc/systemd/system/ubuntu-configuration-system.timer; do
        if [[ -f "$path" ]] && grep -q 'Managed by ubuntu-configuration' "$path"; then
            sudo rm -f -- "$path"
        fi
    done
    helper=/usr/local/libexec/ubuntu-configuration-system-capture.py
    if [[ -f "$helper" ]] && grep -q 'Managed by ubuntu-configuration' "$helper"; then
        sudo rm -f -- "$helper"
    fi
    sudo systemctl disable --now ubuntu-configuration-system.timer 2>/dev/null || true
    sudo systemctl daemon-reload 2>/dev/null || true
else
    echo "System-wide timer/helper not removed (sudo required)."
fi
if [[ "${UBUNTU_BACKUP_SKIP_SERVICE:-0}" != 1 ]]; then
    systemctl --user daemon-reload 2>/dev/null || true
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true
echo "Uninstall complete. Backup files were preserved."
