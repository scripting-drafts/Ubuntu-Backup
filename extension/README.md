# AppIndicator fork

- `install.sh` copies the system Ubuntu AppIndicators extension into a user-level fork.
- The fork sends this app's left-click to `org.ubuntu.Backup.ShowStatus`; right-click keeps the menu.
- `uninstall.sh` removes the fork and restores `ubuntu-appindicators@ubuntu.com`.
- Re-run `../install.sh` after system extension updates.
