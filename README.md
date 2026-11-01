# Ubuntu Backup

- Run `./ubuntu-backup.py` for interactive backup and restore; no personal data is bundled.
- Never publish snapshots or exports; they contain secrets and are not encrypted.
- External timelines use symlinks over a shared content baseline.
- Backups use the selected drive root directly; no host/user subfolder.
- Dated configuration copies are external-only by default; set `keep_local_backups` to keep local.
- One backup action captures configuration and full `/home`; the tray icon shows live status and history.
- Menu 1 offers privileged capture; menu 5 schedules both timers.
- Full `/home` snapshots are manual, external-only, and require explicit sudo approval.
