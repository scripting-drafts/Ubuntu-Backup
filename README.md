# Ubuntu Backup APT repository

Signed `stable` repository for the Ubuntu Backup debs.

## Install the archive key

```bash
curl -fsSL https://scripting-drafts.github.io/Ubuntu-Backup/ubuntu-backup-archive-key.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/ubuntu-backup-archive-keyring.gpg
```

## Add the repository

```bash
echo 'deb [signed-by=/usr/share/keyrings/ubuntu-backup-archive-keyring.gpg] https://scripting-drafts.github.io/Ubuntu-Backup/ stable main' \
  | sudo tee /etc/apt/sources.list.d/ubuntu-backup.list
sudo apt update
```

## Install

```bash
sudo apt install ubuntu-backup
```

`ubuntu-backup`, `ubuntu-backup-system` and `ubuntu-backup-core` are chained:
installing or removing any one applies to all three. Do not combine the APT
packages with the strict snap.

## Remove

```bash
# Keep conffiles and the tray autostart entry
sudo apt remove ubuntu-backup

# Also remove conffiles and the tray autostart entry
sudo apt purge ubuntu-backup
```

Backups in `~/Ubuntu-Backup` and on external drives are not removed.

Key fingerprint: `6A4A51E4A1F0BEB561ECAED0893713F390A5DC44`
