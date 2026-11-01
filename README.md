# Ubuntu Backup

- Run `./ubuntu-backup.py` for interactive backup and restore; no personal data is bundled.
- Snapshots contain sensitive data and are not encrypted.
- External dates are symlink timelines over a shared content baseline.
- Backups use the selected drive root directly; no host/user subfolder.
- Dated configuration copies are external-only by default; set `keep_local_backups` to keep local.
- One backup action captures configuration and full `/home`; identical files are stored once.
- Menu 1 also offers privileged capture; menu 5 schedules both timers.
- Full `/home` snapshots are manual, external-only, and require explicit sudo approval.
- Never publish generated snapshots, exports, settings, or credentials.
