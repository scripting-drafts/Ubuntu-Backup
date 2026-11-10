# Ubuntu Backup
- Run `./install.sh`; it installs the patched AppIndicator copy and the `ubuntu-backup` command symlink; `./refresh.sh` restarts the tray; `./drive.sh mount|unmount` manages the saved drive.
- `./uninstall.sh` keeps backups/runtime; `./uninstall.sh --yes --purge-backups` removes the program, entire local workspace, and configured external timelines without prompting.
- Install requires `python3-gi`, GTK 3, and `gir1.2-ayatanaappindicator3-0.1`; supports Ubuntu 22.04 through 26.10, and warns outside that range.
- Run `./ubuntu-backup.py` or `ubuntu-backup` for interactive backup/restore; no personal data is bundled.
- Never publish snapshots or exports; they contain secrets and are not encrypted.
- External backups use the selected drive root and symlink timelines over a shared baseline; dated configuration copies are external-only by default (`keep_local_backups` keeps local).
- One backup action captures configuration, development code and full `/home`; developments reapply in dependency order and package names are re-resolved without version pins.
- The tray and the Backup and Restore GUI cover drive choice, backup, package scan, recovery, scheduling, history, and guarded data purge.
- Full `/home` snapshots are manual, external-only, and require explicit sudo approval.
