# Signed APT repository

The debs are published from a signed `stable` repository on GitHub Pages:

```text
https://scripting-drafts.github.io/Ubuntu-Backup/
```

## Add the key and repository

```bash
curl -fsSL https://scripting-drafts.github.io/Ubuntu-Backup/ubuntu-backup-archive-key.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/ubuntu-backup-archive-keyring.gpg

echo 'deb [signed-by=/usr/share/keyrings/ubuntu-backup-archive-keyring.gpg] https://scripting-drafts.github.io/Ubuntu-Backup/ stable main' \
  | sudo tee /etc/apt/sources.list.d/ubuntu-backup.list

sudo apt update
```

## Install

```bash
sudo apt install ubuntu-backup-system
# GUI package, only when not using the strict snap:
sudo apt install ubuntu-backup
```

## Key

- Fingerprint: `D924602B5792EAFD32146E903DDA7A6E98513F34`
- Public key: `ubuntu-backup-archive-key.asc` at the repository root.
- The archive key is separate from the maintainer's personal GPG key. It signs
  only repository metadata; protect the private key and the GitHub account.

## Publishing an update

```bash
# Build the debs, copy them into pool/main/u/ubuntu-backup/, then:
dpkg-scanpackages pool /dev/null > dists/stable/main/binary-amd64/Packages
gzip -9c dists/stable/main/binary-amd64/Packages > dists/stable/main/binary-amd64/Packages.gz
apt-ftparchive release dists/stable > /tmp/Release && mv /tmp/Release dists/stable/Release
GNUPGHOME=/home/gerard/.gnupg-ubuntu-backup-archive \
  gpg --batch --yes --clearsign -u D924602B5792EAFD32146E903DDA7A6E98513F34 \
  -o dists/stable/InRelease dists/stable/Release
```

Commit and push the `gh-pages` branch. GitHub Pages rebuilds automatically.
