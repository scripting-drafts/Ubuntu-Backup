#!/usr/bin/env bash
# Install a patched copy of Ubuntu AppIndicators in a user-controlled system
# data directory. It keeps the stock UUID, so only one watcher can ever exist.
set -Eeuo pipefail
SYSTEM_UUID="ubuntu-appindicators@ubuntu.com"
OLD_FORK_UUID="ubuntu-backup-appindicators@local"
SRC="${UBUNTU_APPINDICATOR_SRC:-/usr/share/gnome-shell/extensions/$SYSTEM_UUID}"
DATA_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/ubuntu-backup-system"
DEST="$DATA_ROOT/gnome-shell/extensions/$SYSTEM_UUID"
OLD_FORK_DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$OLD_FORK_UUID"
[[ -f "$SRC/metadata.json" ]] || { echo "AppIndicator extension not found: $SRC" >&2; exit 1; }
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ubuntu-backup-extension.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
cp -a "$SRC/." "$TMP/"
python3 - "$TMP" "$SYSTEM_UUID" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
uuid = sys.argv[2]
metadata_path = root / "metadata.json"
metadata = json.loads(metadata_path.read_text())
metadata.update({
    "uuid": uuid,
    "name": "Ubuntu AppIndicators (Ubuntu Backup)",
    "description": "Patched copy: left-click opens Ubuntu Backup status, right-click opens the menu.",
})
metadata_path.write_text(json.dumps(metadata, indent=4) + "\n")

import re

app_path = root / "appIndicator.js"
icon_path = root / "indicatorStatusIcon.js"
patched = False
if app_path.is_file():
    app = app_path.read_text()
    marker = re.search(r"^([ \t]*)secondaryActivate\([^\n]*\)[ \t]*\{",
                       app, re.MULTILINE)
    if marker:
        indent = marker.group(1)
        method = """showQuitMenu(x, y) {
    Gio.DBus.session.call(
        'org.ubuntu.Backup', '/org/ubuntu/Backup', 'org.ubuntu.Backup',
        'ShowQuitMenu', null, null, Gio.DBusCallFlags.NONE, 1000,
        null, (_connection, result) => {
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
        method = "".join(indent + line if line.strip() else line
                         for line in method.splitlines(True))
        app = app[:marker.start()] + method + app[marker.start():]
        app_path.write_text(app)
        patched = True
    else:
        print("Warning: appIndicator.js has no secondaryActivate marker; "
              "installing the stock copy.", file=sys.stderr)

if patched and icon_path.is_file():
    icon = icon_path.read_text()
    marker = re.search(r"^([ \t]*)vfunc_button_press_event\([^\n]*\)[ \t]*\{",
                       icon, re.MULTILINE)
    if marker:
        indent = marker.group(1)
        press = """vfunc_event(event) {
    if (event.type() === Clutter.EventType.BUTTON_PRESS &&
        event.get_button() === 3 && this._indicator.id === 'ubuntu-backup') {
        let [x, y] = event.get_coords();
        if (Main.panel.menuManager.activeMenu && Main.panel.menuManager._closeMenu)
            Main.panel.menuManager._closeMenu(true, Main.panel.menuManager.activeMenu);
        this._indicator.showQuitMenu(x, y);
        return Clutter.EVENT_STOP;
    }
    return super.vfunc_event(event);
}

"""
        press = "".join(indent + line if line.strip() else line
                        for line in press.splitlines(True))
        icon = icon[:marker.start()] + press + icon[marker.start():]
        icon_path.write_text(icon)
    else:
        print("Warning: indicatorStatusIcon.js has no button-press marker; "
              "right-click quit is disabled.", file=sys.stderr)
elif not patched:
    print("Warning: installing the stock AppIndicator extension; "
          "backup-specific right-click is unavailable.", file=sys.stderr)
PY
if command -v node >/dev/null; then
    node --check "$TMP/appIndicator.js" >/dev/null 2>&1 || \
        echo "Warning: appIndicator.js syntax check failed; verify the extension manually." >&2
    node --check "$TMP/indicatorStatusIcon.js" >/dev/null 2>&1 || \
        echo "Warning: indicatorStatusIcon.js syntax check failed; verify the extension manually." >&2
fi
if [[ -d "$DEST" ]]; then
    existing="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("uuid",""))' "$DEST/metadata.json" 2>/dev/null || true)"
    [[ "$existing" == "$SYSTEM_UUID" ]] || { echo "Refusing to replace unexpected extension at $DEST" >&2; exit 1; }
    rm -rf "$DEST"
fi
mkdir -p "$(dirname "$DEST")"
mv "$TMP" "$DEST"
trap - EXIT
rm -rf "$OLD_FORK_DEST"

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


write("enabled-extensions", [item for item in read("enabled-extensions") if item != fork])
write("disabled-extensions", [item for item in read("disabled-extensions") if item not in (fork, system)])
PY

CURRENT="$(systemctl --user show-environment 2>/dev/null | sed -n 's/^XDG_DATA_DIRS=//p' | head -1)"
if [[ -z "$CURRENT" ]]; then
    CURRENT="/usr/local/share:/usr/share"
fi
case ":$CURRENT:" in
    *":$DATA_ROOT:"*) DATA_DIRS="$CURRENT" ;;
    *) DATA_DIRS="$DATA_ROOT:$CURRENT" ;;
esac
for unit in org.gnome.Shell@x11.service org.gnome.Shell@wayland.service; do
    directory="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit.d"
    mkdir -p "$directory"
    cat > "$directory/90-ubuntu-backup.conf" <<EOF
[Service]
Environment=XDG_DATA_DIRS=$DATA_DIRS
EOF
done
systemctl --user daemon-reload
if grep -q 'showQuitMenu' "$DEST/appIndicator.js"; then
    echo "Installed the patched AppIndicator copy. Restart GNOME Shell after the first install or a system extension update."
else
    echo "Installed the stock AppIndicator copy; backup-specific right-click is unavailable."
fi
