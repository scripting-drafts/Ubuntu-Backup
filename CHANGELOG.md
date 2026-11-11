# Changelog

## Unreleased
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
