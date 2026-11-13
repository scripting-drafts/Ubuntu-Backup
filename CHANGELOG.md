# Changelog

## v1.2.0 - 2026-11-13
- Publish `ubackup` as a strict-confinement snap (no classic request) with `home`, `removable-media`, `udisks2`, and desktop/Mount-Observe plugs.
- Snapshot mode disables every `sudo`/`pkexec` path, host package queries, user systemd timers, fstab edits, and raw-disk formatting.
- Add the `ubuntu-backup-system` deb: root capture helper, `ubuntu-backup-system@.timer`, polkit action, and System Capture desktop entry. The deb owns its own files; the helper never self-installs into `/usr/local/libexec` or `/etc/systemd/system`.
- The helper writes `privileged-system/system-wide.tar.gz` into the user's workspace as the user, using the existing verified cache format consumed by `build_snapshot`.
- The helper captures `/etc`, `/usr/local/etc`, and only the invoking user's home by default; `--include-other-users` is an explicit privacy-sensitive opt-in.
- Keep the GUI tray autostart in the deb (`/etc/xdg/autostart`); the strict snap does not write `~/.config/autostart` or `~/.config/systemd/user`.
- The system deb adds an XDG autostart entry that launches the strict snap tray only when `ubackup` is installed.
- Add the `install` target to `native/Makefile` for the Snapcraft make plugin.
- Publish a signed APT repository for the debs at `https://scripting-drafts.github.io/Ubuntu-Backup/`.

## v1.1.0 - 2026-11-12
- Enforce a per-session tray singleton in `$XDG_RUNTIME_DIR/ubuntu-backup/tray.lock` (independent of the workspace), make loss of `org.ubuntu.Backup` fatal, align the desktop launcher with the autostart workspace, and log the selected tray backend.
- Harden tray shutdown with `pidfd` signals, validate private lock directories and lock files, fail closed on a tampered lock path, refuse symlinked extension destinations, and publish extension updates atomically with `mv -T`.
- Autoscroll the Backup/Restore log window to new output; scrolling up pauses follow mode and returning to the bottom resumes it.
- Make the tray summary status clickable: dismiss finished/failed/cancelled messages, remove the status row, and restore the default icon; running operations open Live status.
- Add cooperative cancellation for backup and restore: GUI Cancel button, workspace cancel file for sudo workers, and SIGINT/SIGTERM handling in `ubackup-core`.
- Make `ubackup-core` the only full `/home` backup and restore engine; remove the Python tar fallback and the Python restore copy loop.
- Require the native core at install time and remove the `--native-capture` switch.
- Nest package scan under the backup action and modified-package restore under the restore action.
- Include package scan status/refresh in the backup flow and optional modified-package restore in the configuration recovery flow.
- Hide package scan and modified-package restore from menus; they are now steps inside the Backup and Restore flows.
- Embed Live status in the Backup and Restore flows and remove the standalone Live status menu entry.
- Run Backup, Restore, and Prepare Drive in GTK process windows; sudo passwords use an askpass pop-up.
- Refresh the privileged package scan by default, once per backup, and keep GUI-worker status from being marked stale.
- Present Restore recovery choices as a selectable GUI menu, with a small-print package-install warning.
- Remove Cancel from the Restore recovery dropdown; dialog cancellation remains available.

## v1.0.0 - 2026-11-10
- Harden purge handling: complete workspace removal, mount/symlink guards, sudo fallback for root-owned data, clear incomplete-purge failures.
- Verify backup-drive marker identity during selection, backups, and external purge; print exact external roots in uninstall dry runs.
- Fix fresh-workspace tray status fallback, install/uninstall command symlink handling, and root-owned staging cleanup.
- Add regression coverage for purge, marker identity, install round trips, symlink/mount refusal, and staging cleanup.
- Add CI for Ubuntu 22.04/24.04 with shell checks, Python checks, and the full test suite.
- Document testing and prerequisites.
