# AppIndicator patch

- `install.sh` places a patched Ubuntu AppIndicators copy in a user-controlled system data directory.
- A scoped systemd drop-in adds that directory to GNOME Shell's `XDG_DATA_DIRS`.
- The copy keeps the stock UUID, so there is only one StatusNotifierWatcher.
- Left-click opens the native menu; right-click opens the quit-only popup.
- `uninstall.sh` removes the copy and drop-in; restart GNOME Shell to restore stock.
