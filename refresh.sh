#!/usr/bin/env bash
# Restart the tray icon if it is stuck or not visible.
set -Eeuo pipefail
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}/ubuntu-backup.py"
WORKSPACE="${UBUNTU_BACKUP_WORKSPACE:-$HOME/ubuntu-backup/data}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
[[ -x "$BIN" ]] || { echo "Install first: ./install.sh" >&2; exit 1; }
python3 - "$BIN" <<'PY'
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
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    setsid --fork "$BIN" --workspace "$WORKSPACE" tray >/dev/null 2>&1
    echo "Ubuntu Backup tray refreshed."
else
    echo "No graphical session detected; tray will start at login." >&2
    exit 1
fi
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$DATA/applications" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true
fi
