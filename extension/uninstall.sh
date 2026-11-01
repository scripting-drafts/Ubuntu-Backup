#!/usr/bin/env bash
# Remove the patched AppIndicator copy and restore the stock system extension.
set -Eeuo pipefail
SYSTEM_UUID="ubuntu-appindicators@ubuntu.com"
OLD_FORK_UUID="ubuntu-backup-appindicators@local"
DATA_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/ubuntu-backup-system"
OLD_FORK_DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$OLD_FORK_UUID"
for unit in org.gnome.Shell@x11.service org.gnome.Shell@wayland.service; do
    directory="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit.d"
    rm -f "$directory/90-ubuntu-backup.conf"
    rmdir "$directory" 2>/dev/null || true
done
systemctl --user daemon-reload 2>/dev/null || true
rm -rf "$DATA_ROOT" "$OLD_FORK_DEST"
python3 - "$OLD_FORK_UUID" "$SYSTEM_UUID" <<'PY'
import ast
import subprocess
import sys

fork, system = sys.argv[1], sys.argv[2]


def read(key):
    value = subprocess.run(["gsettings", "get", "org.gnome.shell", key],
                           capture_output=True, text=True, check=True).stdout.strip()
    if value.startswith("@as"):
        value = value[3:].strip()
    return list(ast.literal_eval(value))


def write(key, values):
    subprocess.run(["gsettings", "set", "org.gnome.shell", key, repr(values)], check=True)


write("enabled-extensions", [item for item in read("enabled-extensions") if item not in (fork, system)])
write("disabled-extensions", [item for item in read("disabled-extensions") if item not in (fork, system)])
PY
echo "Removed the patched AppIndicator copy. Restart GNOME Shell to restore the stock extension."
