# Changelog

## v1.3.10 - 2026-11-13
- Tag APT-only recovery phases and show their repository instructions.

## v1.3.9 - 2026-11-13
- Do not flag strict-snap capability omissions (for example root-owned files under $HOME) as tray warnings or exit status 3.
- Keep the full details in capture-report.json and unreadable-files.json.

## v1.3.8 - 2026-11-13
- Link the deb-only information message to the APT instructions in the repository.

## v1.3.7 - 2026-11-13
- Tell end users that system-wide capture and package scanning require the ubuntu-backup-system .deb package.
- Add a disabled tray information row for the same limitation.

## v1.3.6 - 2026-11-13
- Add a guided Snap permissions dialog to the drive chooser when no devices are visible.
- Provide copyable `snap connect` commands and a Retry action.

## v1.3.5 - 2026-11-13
- Explain missing snap interface connections when a mounted drive is unreadable.
- Stage canberra-gtk3-module to silence the cosmetic GTK warning.

## v1.3.4 - 2026-11-13
- Grey out deb-only tray and drive actions in the strict snap and add hover explanations.
- Keep Prepare new drive visible but disabled; formatting remains deb-only.

## v1.3.3 - 2026-11-13
- Strict snap: list unmounted Linux filesystems and mount them via udisks2.
- Remove the Prepare new drive action from the strict GUI; formatting remains deb-only.
- Keep the stock AppIndicator menu without the optional host-extension D-Bus service.

## v1.3.2 - 2026-11-13
- Declare the `org.ubuntu.Backup` session D-Bus slot so the strict snap tray can own its status service; fixes startup on X11/GNOME.

## v1.3.1 - 2026-11-13
- Remove `ubuntu-backup-system@.timer` and its automatic enablement.
- System capture and package scan are now strictly on-demand via `pkexec`,
  the request watcher, the desktop actions, or the helper CLI.
- Remove stale timer enablement symlinks on upgrade.

## v1.3.0 - 2026-11-13
- Add `ubuntu-backup-core` with the shared application and native engine; the GUI and system helper both depend on it.
- Auto-enable `ubuntu-backup-system@<sudo-user>.timer` during normal sudo installs; root-only installs still choose explicitly.
- Add package-file scanning to the helper and run it during system capture.
- Add a fixed-schema request queue (`capture`, `package-scan`), a globally enabled user path unit, and `pkexec` processing.
- Add `home-capture` to the helper and a desktop action; it uses the shared app/native engine.
- Make the strict snap UI configuration-only and remove formatting, package restore, system restore and deploy from its recovery/menu paths (deb-only, explicit).
- Keep `request-system` and `capture-root` as request commands in the snap.

## v1.2.1 - 2026-11-13
- Move tray autostart into the strict snap using snapd's `autostart:` key; the app writes `$SNAP_USER_DATA/.config/autostart/ubuntu-backup.desktop` on first run.
- Remove the deb-side strict-snap tray wrapper and XDG autostart. The GUI deb keeps its own `/etc/xdg/autostart/ubuntu-backup-tray.desktop`.
- Bump the snap and deb version to 1.2.1.

## v1.2.0 - 2026-11-13
- Publish `ubackup` as a strict-confinement snap (no classic request) with `home`, `removable-media`, `udisks2`, and desktop/Mount-Observe plugs.
- Snapshot mode disables every `sudo`/`pkexec` path, host package queries, user systemd timers, fstab edits, and raw-disk formatting.
- Add the `ubuntu-backup-system` deb: root capture helper, `ubuntu-backup-system@.timer`, polkit action, and System Capture desktop entry. The deb owns its own files; the helper never self-installs into `/usr/local/libexec` or `/etc/systemd/system`.
- The helper writes `privileged-system/system-wide.tar.gz` into the user's workspace as the user, using the existing verified cache format consumed by `build_snapshot`.
- The helper captures `/etc`, `/usr/local/etc`, and only the invoking user's home by default; `--include-other-users` is an explicit privacy-sensitive opt-in.
- Declare snapd autostart for `ubackup` and create the per-user file in `$SNAP_USER_DATA/.config/autostart`; the strict snap never writes the host's `~/.config/autostart` or `~/.config/systemd/user`.
- Remove the deb-side strict-snap tray autostart; the GUI deb keeps its own `/etc/xdg/autostart` entry.
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
