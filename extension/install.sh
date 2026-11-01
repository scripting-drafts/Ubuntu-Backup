#!/usr/bin/env bash
# Install a user-level fork of Ubuntu AppIndicators that sends this app's
# left-click to org.ubuntu.Backup.ShowStatus and keeps right-click for the menu.
set -Eeuo pipefail
SRC="${UBUNTU_APPINDICATOR_SRC:-/usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com}"
UUID="ubuntu-backup-appindicators@local"
SYSTEM_UUID="ubuntu-appindicators@ubuntu.com"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"
[[ -f "$SRC/metadata.json" ]] || { echo "AppIndicator extension not found: $SRC" >&2; exit 1; }
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ubuntu-backup-extension.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
cp -a "$SRC/." "$TMP/"
python3 - "$TMP" "$UUID" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
uuid = sys.argv[2]
metadata_path = root / "metadata.json"
metadata = json.loads(metadata_path.read_text())
metadata.update({
    "uuid": uuid,
    "name": "Ubuntu Backup AppIndicators",
    "description": "User-level AppIndicator fork; left-click opens Ubuntu Backup status, right-click opens the menu.",
})
metadata_path.write_text(json.dumps(metadata, indent=4) + "\n")

app_path = root / "appIndicator.js"
app = app_path.read_text()
marker = "    secondaryActivate(timestamp, x, y) {"
method = """    showStatus(x, y) {
        Gio.DBus.session.call(
            'org.ubuntu.Backup', '/org/ubuntu/Backup', 'org.ubuntu.Backup',
            'ShowStatus', null, null, Gio.DBusCallFlags.NONE, 1000,
            this._cancellable, (_connection, result) => {
                try {
                    Gio.DBus.session.call_finish(result);
                } catch (e) {
                    if (e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        return;
                    this.open(x, y);
                }
            });
    }

"""
if app.count(marker) != 1:
    raise SystemExit("appIndicator.js marker changed; refusing to patch")
app_path.write_text(app.replace(marker, method + marker, 1))

icon_path = root / "indicatorStatusIcon.js"
icon = icon_path.read_text()
press_marker = "    vfunc_button_press_event(buttonEvent) {\n"
press = """        if (buttonEvent.button === 1 && this._indicator.id === 'ubuntu-backup') {
            Main.panel.menuManager._closeMenu(true, Main.panel.menuManager.activeMenu);
            this._indicator.showStatus(buttonEvent.x, buttonEvent.y);
            return Clutter.EVENT_STOP;
        }

"""
release_marker = "    vfunc_scroll_event(scrollEvent) {\n"
release = """    vfunc_button_release_event(buttonEvent) {
        if (buttonEvent.button === 1 && this._indicator.id === 'ubuntu-backup')
            return Clutter.EVENT_STOP;
        return Clutter.EVENT_PROPAGATE;
    }

"""
if icon.count(press_marker) != 1 or icon.count(release_marker) != 1:
    raise SystemExit("indicatorStatusIcon.js markers changed; refusing to patch")
icon = icon.replace(press_marker, press_marker + press, 1)
icon = icon.replace(release_marker, release + release_marker, 1)
icon_path.write_text(icon)
PY
if command -v node >/dev/null; then
    node --check "$TMP/appIndicator.js" >/dev/null
    node --check "$TMP/indicatorStatusIcon.js" >/dev/null
fi
if [[ -d "$DEST" ]]; then
    existing="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("uuid",""))' "$DEST/metadata.json" 2>/dev/null || true)"
    [[ "$existing" == "$UUID" ]] || { echo "Refusing to replace unexpected extension at $DEST" >&2; exit 1; }
    rm -rf "$DEST"
fi
mkdir -p "$(dirname "$DEST")"
mv "$TMP" "$DEST"
trap - EXIT

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


enabled = [item for item in read("enabled-extensions") if item != system]
if fork not in enabled:
    enabled.append(fork)
write("enabled-extensions", enabled)
disabled = [item for item in read("disabled-extensions") if item != fork]
if system not in disabled:
    disabled.append(system)
write("disabled-extensions", disabled)
PY

if command -v gnome-extensions >/dev/null; then
    gnome-extensions enable "$UUID" >/dev/null 2>&1 || true
fi
echo "Installed $UUID. Restart GNOME Shell after the first install or a system extension update."
