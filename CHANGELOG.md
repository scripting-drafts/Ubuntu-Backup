# Changelog

## Unreleased
- Nest package scan under the backup action and modified-package restore under the restore action.

## v1.0.0 - 2026-11-10
- Harden purge handling: complete workspace removal, mount/symlink guards, sudo fallback for root-owned data, clear incomplete-purge failures.
- Verify backup-drive marker identity during selection, backups, and external purge; print exact external roots in uninstall dry runs.
- Fix fresh-workspace tray status fallback, install/uninstall command symlink handling, and root-owned staging cleanup.
- Add regression coverage for purge, marker identity, install round trips, symlink/mount refusal, and staging cleanup.
- Add CI for Ubuntu 22.04/24.04 with shell checks, Python checks, and the full test suite.
- Document testing and prerequisites.
