#!/usr/bin/env bash
# Install the single-file Ubuntu Backup tool, launcher, icon, and tray autostart.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART=1
if [[ "${1:-}" == "--no-autostart" ]]; then
    AUTOSTART=0
elif [[ $# -gt 0 ]]; then
    echo "Usage: ./install.sh [--no-autostart]" >&2
    exit 2
fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || {
    echo "GTK 3 Python bindings are required: sudo apt install python3-gi gir1.2-gtk-3.0" >&2
    exit 1
}
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
AUTODIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
WORKSPACE="$HOME/ubuntu-backup-data"
mkdir -p "$BIN" "$APPS" "$ICONS" "$AUTODIR"
install -m 700 "$ROOT/ubuntu-backup.py" "$BIN/ubuntu-backup.py"
install -m 600 "$ROOT/ubuntu-backup.svg" "$ICONS/ubuntu-backup.svg"
quote() { printf '%s' "$1" | sed 's/[\\"]/\\&/g'; }
cat > "$APPS/ubuntu-backup.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Ubuntu Backup
Comment=Configuration and full /home backup
Exec="$(quote "$BIN/ubuntu-backup.py")"
Icon=ubuntu-backup
Terminal=true
Categories=Utility;
EOF
if (( AUTOSTART )); then
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
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
echo "Installed $BIN/ubuntu-backup.py"
echo
echo "Next steps:"
echo "1. Open Ubuntu Backup from the application menu."
echo "2. Choose 4 and select the mounted backup drive."
echo "3. Choose 1 to run the first backup."
if (( AUTOSTART )); then
    echo "The tray icon starts at next login; run $BIN/ubuntu-backup.py tray to start it now."
else
    echo "Run $BIN/ubuntu-backup.py tray when you want the status icon."
fi
