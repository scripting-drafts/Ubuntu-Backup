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
sudo apt install ubuntu-backup-system
# Optional GUI package (do not combine with the strict snap):
sudo apt install ubuntu-backup
```

Key fingerprint: `6A4A51E4A1F0BEB561ECAED0893713F390A5DC44`
