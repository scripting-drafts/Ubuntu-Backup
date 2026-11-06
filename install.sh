#!/usr/bin/env bash
# Install the Ubuntu Backup tool, launcher, icon, autostart, and start the tray now.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if (( $# )); then
    echo "Usage: ./install.sh" >&2
    exit 2
fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || {
    echo "GTK 3 Python bindings are required: sudo apt install python3-gi gir1.2-gtk-3.0" >&2
    exit 1
}
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/scalable/apps"
AUTODIR="$CONFIG/autostart"
SYSTEMD="$CONFIG/systemd/user"
WORKSPACE="$HOME/ubuntu-backup/data"
mkdir -p "$BIN" "$APPS" "$ICONS" "$AUTODIR" "$SYSTEMD"
install -m 700 "$ROOT/ubuntu-backup.py" "$BIN/ubuntu-backup.py"
if [[ ! -x "$ROOT/native/ubackup-core" && -f "$ROOT/native/ubackup-core.c" ]]; then
    make -C "$ROOT/native" >/dev/null 2>&1 || echo "Warning: native capture core build failed; --native-capture will be unavailable." >&2
fi
if [[ -x "$ROOT/native/ubackup-core" ]]; then
    install -m 700 "$ROOT/native/ubackup-core" "$BIN/ubackup-core"
fi
install -m 600 "$ROOT/ubuntu-backup.svg" "$ICONS/ubuntu-backup.svg"
quote() { printf '%s' "$1" | sed 's/[\\"]/\\&/g'; }
# systemd specifier %h keeps the generated units portable between machines/accounts.
portable() { case "$1" in "$HOME"/*) printf '%%h/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac; }
cat > "$APPS/ubuntu-backup.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Ubuntu Backup
Comment=Configuration and full /home backup
Exec="$(quote "$BIN/ubuntu-backup.py")" tray
Icon=ubuntu-backup
Terminal=false
Categories=Utility;
EOF
cat > "$AUTODIR/ubuntu-backup-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Ubuntu Backup tray
Comment=Backup status icon
Exec="$(quote "$BIN/ubuntu-backup.py")" --workspace "$(quote "$WORKSPACE")" tray
Icon=ubuntu-backup
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
rm -f "$SYSTEMD/ubuntu-backup-tray.service"
# The mount units carry no device name or UUID: the saved UUID is read at run time,
# so a replaced or reformatted drive is simply skipped until it is selected again.
cat > "$SYSTEMD/ubuntu-backup-mount.service" <<EOF
[Unit]
Description=Mount the backup drive declared in /etc/fstab
ConditionPathExists=$(portable "$WORKSPACE")/config/settings.json

[Service]
Type=oneshot
ExecStart="$(portable "$BIN/ubuntu-backup.py")" --workspace "$(portable "$WORKSPACE")" mount-saved --quiet
EOF
cat > "$SYSTEMD/ubuntu-backup-mount.timer" <<EOF
[Unit]
Description=Retry mounting the saved Ubuntu Backup drive

[Timer]
OnBootSec=20s
OnUnitActiveSec=2min
AccuracySec=10s
Unit=ubuntu-backup-mount.service

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable --now ubuntu-backup-mount.timer 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true
if [[ "${UBUNTU_BACKUP_SKIP_START:-0}" != 1 ]] && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    python3 - "$BIN/ubuntu-backup.py" <<'PY'
import os, signal, sys, time
target = os.path.realpath(sys.argv[1])
pids = []
for entry in os.listdir('/proc'):
    if not entry.isdigit():
        continue
    try:
        args = open(f'/proc/{entry}/cmdline', 'rb').read().split(b'\0')
        if target.encode() in args and b'tray' in args:
            pids.append(int(entry))
            os.kill(int(entry), signal.SIGTERM)
    except (OSError, ValueError):
        pass
for pid in pids:
    for _ in range(40):
        if not os.path.exists(f'/proc/{pid}'):
            break
        time.sleep(0.05)
PY
    setsid --fork "$BIN/ubuntu-backup.py" --workspace "$WORKSPACE" tray >/dev/null 2>&1
    echo "Ubuntu Backup installed and started."
else
    echo "Ubuntu Backup installed; start the tray with ./refresh after logging in."
fi
if [[ -x "$ROOT/extension/install.sh" ]]; then
    "$ROOT/extension/install.sh" || echo "Warning: AppIndicator extension fork was not installed." >&2
fi
