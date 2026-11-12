#!/usr/bin/env bash
# Restart the tray icon if it is stuck or not visible.
set -Eeuo pipefail
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}/ubuntu-backup.py"
WORKSPACE="${UBUNTU_BACKUP_WORKSPACE:-$HOME/Ubuntu-Backup/data}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
[[ -x "$BIN" ]] || { echo "Install first: ./install.sh" >&2; exit 1; }
python3 - "$BIN" <<'PY'
import os, select, signal, sys, time

target = os.path.realpath(sys.argv[1])

def is_tray(args):
    for index, arg in enumerate(args):
        if not arg or not os.path.isabs(os.fsdecode(arg)):
            continue
        if os.path.realpath(os.fsdecode(arg)) != target:
            continue
        return b'tray' in args[index + 1:]
    return False

def stop(entry):
    try:
        pidfd = os.pidfd_open(int(entry))
    except (AttributeError, OSError):
        pidfd = None
    try:
        args = open(f'/proc/{entry}/cmdline', 'rb').read().split(b'\0')
    except OSError:
        args = []
    if not is_tray(args):
        if pidfd is not None:
            os.close(pidfd)
        return None
    try:
        if pidfd is not None:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        else:
            os.kill(int(entry), signal.SIGTERM)
    except (AttributeError, OSError):
        if pidfd is not None:
            os.close(pidfd)
        return None
    return pidfd, int(entry)

pids = []
for entry in os.listdir('/proc'):
    if not entry.isdigit():
        continue
    result = stop(entry)
    if result is not None:
        pids.append(result)
for pidfd, pid in pids:
    if pidfd is not None:
        try:
            poller = select.poll()
            poller.register(pidfd, select.POLLIN)
            poller.poll(2000)
        finally:
            os.close(pidfd)
    else:
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
