# Ubuntu Backup

- Run `./install.sh`; `./uninstall.sh` removes the program but keeps backups/runtime; `--purge-backups` asks for explicit yes/no confirmation.
- Run `./ubuntu-backup.py` for interactive backup/restore; no personal data is bundled.
- Never publish snapshots or exports; they contain secrets and are not encrypted.
- External backups use the selected drive root and symlink timelines over a shared baseline.
- Dated configuration copies are external-only by default; set `keep_local_backups` to keep local.
- One backup action captures configuration and full `/home`; the tray handles drive choice, status, history, and Run backup now.
- Menu 1 offers privileged capture; menu 5 schedules both timers.
- Full `/home` snapshots are manual, external-only, and require explicit sudo approval.
