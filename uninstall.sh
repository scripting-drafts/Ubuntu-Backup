#!/usr/bin/env bash
# Remove the Ubuntu Backup program; backups and runtime traces are preserved unless purged.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
WORKSPACES=("$HOME/Ubuntu-Backup/data" "$HOME/ubuntu-backup/data" "$HOME/ubuntu-backup-data" "$HOME/ubuntu-configuration")
FILES=("$BIN/ubuntu-backup.py" "$BIN/ubuntu-backup-askpass" "$BIN/ubackup-core" "$HOME/ubuntu-backup.py" "$HOME/ubuntu-configuration.py"
       "$HOME/ubuntu-backup.zip" "$APPS/ubuntu-backup.desktop" "$AUTOSTART" "$ICON"
       "$CONFIG/systemd/user/ubuntu-configuration-backup.service"
       "$CONFIG/systemd/user/ubuntu-configuration-backup.timer"
       "$CONFIG/systemd/user/ubuntu-backup-tray.service"
       "$CONFIG/systemd/user/ubuntu-backup-mount.service"
       "$CONFIG/systemd/user/ubuntu-backup-mount.timer")
LINK="$BIN/ubuntu-backup"
LINK_REMOVE=0
if [[ -L "$LINK" ]]; then
    link_target="$(readlink -- "$LINK" || true)"
    if [[ "$link_target" == "ubuntu-backup.py" || "$link_target" == "$BIN/ubuntu-backup.py" ]]; then
        LINK_REMOVE=1
    fi
fi
PURGE_FAILED=0
echo "Program files to remove:"
for path in "${FILES[@]}"; do
    [[ -e "$path" || -L "$path" ]] && echo "  $path"
done
if (( LINK_REMOVE )); then
    echo "  $LINK"
fi
if (( PURGE_BACKUPS )); then
    echo "Workspaces to purge:"
else
    echo "Workspaces preserved (backups and runtime traces):"
fi
for path in "${WORKSPACES[@]}"; do
    [[ -d "$path" ]] && echo "  $path"
done
if (( PURGE_BACKUPS )); then
    echo "Backup data will also be purged: local workspaces and configured external timelines."
fi
if (( DRY )) && (( PURGE_BACKUPS )); then
    python3 - "${WORKSPACES[@]}" <<'PY'
import json, sys
from pathlib import Path

def mountpoints():
    points = []
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return points
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        point = fields[4]
        for encoded, char in (("\\040", " "), ("\\011", "\t"),
                              ("\\012", "\n"), ("\\134", "\\")):
            point = point.replace(encoded, char)
        points.append(Path(point))
    return points

def marker_at(base):
    marker = Path(base) / ".ubuntu-configuration-target.json"
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        value = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    return marker if value.get("format") == "ubuntu-configuration-v1" else None

for workspace in map(Path, sys.argv[1:]):
    config = workspace / "config/settings.json"
    if not config.is_file():
        continue
    try:
        value = json.loads(config.read_text()).get("external_backup")
    except (OSError, ValueError, AttributeError):
        continue
    if not isinstance(value, dict):
        continue
    subdirectory = value.get("subdirectory") or ""
    if not isinstance(subdirectory, str):
        continue
    relative = Path(subdirectory)
    if relative.is_absolute() or ".." in relative.parts:
        continue
    legacy = value.get("mount_point")
    bases = [Path(legacy)] if isinstance(legacy, str) and legacy else mountpoints()
    for base in bases:
        marker = marker_at(base / relative if relative.parts else base)
        if marker is not None:
            print("External backup root: " + str(marker.parent))
PY
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
if (( PURGE_BACKUPS )) && (( ! YES )); then
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
    systemctl --user disable --now ubuntu-backup-tray.service 2>/dev/null || true
    systemctl --user disable --now ubuntu-backup-mount.timer 2>/dev/null || true
fi
for path in "${FILES[@]}"; do
    [[ -e "$path" || -L "$path" ]] && rm -rf -- "$path"
done
if (( LINK_REMOVE )); then
    rm -f -- "$LINK"
fi
if [[ -x "$ROOT/extension/uninstall.sh" ]]; then
    "$ROOT/extension/uninstall.sh" || echo "Warning: AppIndicator extension fork was not fully removed." >&2
fi
if (( PURGE_BACKUPS )); then
    if ! python3 - "${WORKSPACES[@]}" <<'PY'
import json, shutil, sys
from pathlib import Path

EXTERNAL_DATA = ("baseline", "timeline", "ubuntu-configuration-backups")
MARKER = ".ubuntu-configuration-target.json"
failed_any = False

def mountpoints():
    points = []
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return points
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        point = fields[4]
        for encoded, char in (("\\040", " "), ("\\011", "\t"),
                              ("\\012", "\n"), ("\\134", "\\")):
            point = point.replace(encoded, char)
        points.append(Path(point))
    return points

def marker_at(base):
    marker = Path(base) / MARKER
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        value = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    if value.get("format") != "ubuntu-configuration-v1":
        return None
    return marker

def remove_target(target):
    if target.is_symlink():
        raise OSError("refusing symlinked data")
    if target.is_mount():
        raise OSError("refusing mounted data")
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()

for workspace in map(Path, sys.argv[1:]):
    config = workspace / "config/settings.json"
    if not config.is_file():
        continue
    try:
        settings = json.loads(config.read_text())
        value = settings.get("external_backup")
    except (OSError, ValueError, AttributeError) as exc:
        print("Could not read " + str(config) + ": " + str(exc), file=sys.stderr)
        failed_any = True
        continue
    if value is None:
        continue
    if not isinstance(value, dict):
        print("Invalid external backup settings in " + str(config), file=sys.stderr)
        failed_any = True
        continue

    legacy = value.get("mount_point")
    raw_subdirectory = value.get("subdirectory") or ""
    expected_marker = value.get("marker_id")
    if not isinstance(raw_subdirectory, str) or (expected_marker is not None
                                                 and not isinstance(expected_marker, str)):
        print("Invalid external backup settings in " + str(config), file=sys.stderr)
        failed_any = True
        continue
    subdirectory = Path(raw_subdirectory)
    if subdirectory.is_absolute() or ".." in subdirectory.parts:
        print("Unsafe external backup subdirectory in " + str(config) + ": " + raw_subdirectory,
              file=sys.stderr)
        failed_any = True
        continue
    bases = [Path(legacy)] if isinstance(legacy, str) and legacy else mountpoints()
    matches = []
    for base in bases:
        marker = marker_at(base / subdirectory if subdirectory.parts else base)
        if marker is not None:
            matches.append(marker)
    roots = {marker.parent for marker in matches}
    if not roots:
        print("External backup drive not found for " + str(workspace) +
              "; external data was not purged.", file=sys.stderr)
        failed_any = True
        continue
    if len(roots) > 1:
        print("Multiple backup markers found; external purge skipped: "
              + ", ".join(str(root) for root in sorted(roots)), file=sys.stderr)
        failed_any = True
        continue
    root = next(iter(roots))
    try:
        marker_value = json.loads((root / MARKER).read_text())
    except (OSError, ValueError) as exc:
        print("Could not read external marker " + str(root / MARKER) + ": " + str(exc),
              file=sys.stderr)
        failed_any = True
        continue
    if expected_marker and marker_value.get("id") != expected_marker:
        print("Backup marker identity changed at " + str(root) +
              "; external purge skipped.", file=sys.stderr)
        failed_any = True
        continue
    failed = False
    for name in EXTERNAL_DATA:
        target = root / name
        try:
            remove_target(target)
        except OSError as exc:
            print("Could not purge " + str(target) + ": " + str(exc), file=sys.stderr)
            failed = True
    if failed:
        print("Could not fully purge external backup data at " + str(root), file=sys.stderr)
        failed_any = True
        continue
    try:
        (root / MARKER).unlink(missing_ok=True)
    except OSError as exc:
        print("Could not remove external marker " + str(root / MARKER) + ": " + str(exc),
              file=sys.stderr)
        failed_any = True
        continue
    try:
        root.rmdir()
    except OSError:
        print("External backup data purged; root retained (non-backup files remain):", root)
    else:
        print("Purged backup layout:", root)

if failed_any:
    sys.exit(1)
PY
    then
        PURGE_FAILED=1
    fi

    for workspace in "${WORKSPACES[@]}"; do
        if [[ ! -e "$workspace" && ! -L "$workspace" ]]; then
            continue
        fi
        if [[ -L "$workspace" ]]; then
            echo "Refusing to purge symlinked workspace: $workspace" >&2
            PURGE_FAILED=1
            continue
        fi
        if command -v mountpoint >/dev/null && mountpoint -q -- "$workspace"; then
            echo "Refusing to purge mounted workspace: $workspace" >&2
            PURGE_FAILED=1
            continue
        fi
        if rm -rf --one-file-system -- "$workspace" 2>/dev/null; then
            echo "Purged workspace: $workspace"
        elif sudo -n rm -rf --one-file-system -- "$workspace" 2>/dev/null; then
            echo "Purged workspace with sudo: $workspace"
        elif [[ -t 0 ]]; then
            if sudo rm -rf --one-file-system -- "$workspace"; then
                echo "Purged workspace with sudo: $workspace"
            else
                echo "Could not fully purge: $workspace" >&2
                PURGE_FAILED=1
            fi
        else
            echo "Could not fully purge: $workspace (root-owned data; run: sudo rm -rf -- '$workspace')" >&2
            PURGE_FAILED=1
        fi
    done
fi
SYSTEM_PRESENT=0
for path in /etc/systemd/system/ubuntu-configuration-system.service \
            /etc/systemd/system/ubuntu-configuration-system.timer \
            /usr/local/libexec/ubuntu-configuration-system-capture.py; do
    [[ -e "$path" ]] && SYSTEM_PRESENT=1
done
PRESERVED=0
if (( SYSTEM_PRESENT )) && [[ "${UBUNTU_BACKUP_SKIP_SERVICE:-0}" != 1 ]]; then
    BUNDLE="${WORKSPACES[0]}/system-integration-recovery"
    PRESERVED=1
    echo "Preserving system-wide timer/helper for recovery (sudo required)."
    sudo mkdir -p "$BUNDLE" || PRESERVED=0
    for pair in "service:/etc/systemd/system/ubuntu-configuration-system.service" \
                "timer:/etc/systemd/system/ubuntu-configuration-system.timer" \
                "helper:/usr/local/libexec/ubuntu-configuration-system-capture.py"; do
        role="${pair%%:*}"
        path="${pair#*:}"
        if [[ -e "$path" ]]; then
            mode=600
            [[ "$role" == helper ]] && mode=700
            sudo install -m "$mode" "$path" "$BUNDLE/$role" || PRESERVED=0
        fi
    done
    if (( PRESERVED )); then
        sudo chown -R "$(id -u):$(id -g)" "$BUNDLE" || PRESERVED=0
        chmod 700 "$BUNDLE" || PRESERVED=0
    fi
    if (( PRESERVED )); then
        python3 - "$BUNDLE" <<'PY' || PRESERVED=0
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
bundle = Path(sys.argv[1])
paths = {"service": Path("/etc/systemd/system/ubuntu-configuration-system.service"),
         "timer": Path("/etc/systemd/system/ubuntu-configuration-system.timer"),
         "helper": Path("/usr/local/libexec/ubuntu-configuration-system-capture.py")}
items = []
for role, source in paths.items():
    stored = bundle / role
    if not stored.is_file():
        continue
    item = {"role": role, "path": str(source), "present": True, "stored": role,
            "mode": stored.stat().st_mode & 0o777, "uid": 0, "gid": 0,
            "sha256": hashlib.sha256(stored.read_bytes()).hexdigest()}
    if role in ("service", "timer"):
        unit = source.name
        for key, command in (("enabled", ["systemctl", "is-enabled", unit]),
                             ("active", ["systemctl", "is-active", unit])):
            result = subprocess.run(command, capture_output=True, text=True)
            item[key] = result.stdout.strip() or "unknown"
    items.append(item)
(bundle / "system-integration.json").write_text(json.dumps(
    {"format": "ubuntu-system-integration-v1",
     "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(), "items": items}, indent=2) + "\n")
(bundle / "system-integration.json").chmod(0o600)
PY
    fi
    if (( PRESERVED )); then
        echo "Recovery copy: $BUNDLE"
    else
        echo "System integration not removed: recovery copy could not be created."
    fi
fi
if (( SYSTEM_PRESENT )) && [[ "${UBUNTU_BACKUP_SKIP_SERVICE:-0}" != 1 ]] && (( PRESERVED )); then
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
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$APPS" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true
fi
if (( PURGE_BACKUPS )); then
    if (( PURGE_FAILED )); then
        echo "Uninstall complete, but purge was incomplete; see errors above." >&2
        exit 1
    elif (( PRESERVED )); then
        echo "Uninstall complete. Backup data purged; system-integration recovery copy retained at $BUNDLE."
    else
        echo "Uninstall complete. Program and backup data removed."
    fi
else
    echo "Uninstall complete. Backups and runtime traces were preserved."
fi
