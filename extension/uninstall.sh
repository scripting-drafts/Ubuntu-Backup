#!/usr/bin/env bash
# Remove the user-level AppIndicator fork and restore the Ubuntu extension.
set -Eeuo pipefail
UUID="ubuntu-backup-appindicators@local"
SYSTEM_UUID="ubuntu-appindicators@ubuntu.com"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
python3 - "$UUID" "$SYSTEM_UUID" <<'PY'
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


write("enabled-extensions", [item for item in read("enabled-extensions") if item != fork])
write("disabled-extensions", [item for item in read("disabled-extensions") if item not in (fork, system)])
PY
if [[ -d "$DEST" ]]; then
    existing="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("uuid",""))' "$DEST/metadata.json" 2>/dev/null || true)"
    if [[ "$existing" == "$UUID" ]]; then
        rm -rf "$DEST"
    fi
fi
if command -v gnome-extensions >/dev/null; then
    gnome-extensions enable "$SYSTEM_UUID" >/dev/null 2>&1 || true
fi
echo "Removed $UUID; the Ubuntu AppIndicators extension is enabled again."
