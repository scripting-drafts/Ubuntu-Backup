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
sudo apt install ubuntu-backup
# core and system are chained and installed automatically
```

## Key

- Fingerprint: `6A4A51E4A1F0BEB561ECAED0893713F390A5DC44`
- Public key: `ubuntu-backup-archive-key.asc` at the repository root.
- The archive key is separate from the maintainer's personal GPG key. It signs
  only repository metadata; protect the private key and the GitHub account.

## Publishing an update

Run from the repository root.

```bash
# Build the debs, copy them into pool/main/u/ubuntu-backup/, then keep only
# the version being published so superseded debs are not republished.
VERSION=<version>
find pool/main/u/ubuntu-backup -maxdepth 1 -name '*.deb' \
  -not -name "*_${VERSION}_*" -delete

# Index; gzip -n keeps Packages.gz reproducible.
dpkg-scanpackages pool /dev/null > dists/stable/main/binary-amd64/Packages
gzip -9nc dists/stable/main/binary-amd64/Packages > dists/stable/main/binary-amd64/Packages.gz

# Release; drop the old metadata first, and write outside the tree, so it
# does not checksum its own output.
rm -f dists/stable/Release dists/stable/Release.gpg dists/stable/InRelease
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin="Ubuntu Backup" \
  -o APT::FTPArchive::Release::Label="Ubuntu Backup" \
  -o APT::FTPArchive::Release::Suite=stable \
  -o APT::FTPArchive::Release::Codename=stable \
  -o APT::FTPArchive::Release::Architectures=amd64 \
  -o APT::FTPArchive::Release::Components=main \
  -o APT::FTPArchive::Release::Description="Ubuntu Backup APT repository" \
  release dists/stable > /tmp/Release
mv /tmp/Release dists/stable/Release

# Sign both the inline and the detached metadata.
GNUPGHOME=/home/gerard/.gnupg-ubuntu-backup-archive \
  gpg --batch --yes --clearsign -u 6A4A51E4A1F0BEB561ECAED0893713F390A5DC44 \
  -o dists/stable/InRelease dists/stable/Release
GNUPGHOME=/home/gerard/.gnupg-ubuntu-backup-archive \
  gpg --batch --yes --armor --detach-sign -u 6A4A51E4A1F0BEB561ECAED0893713F390A5DC44 \
  -o dists/stable/Release.gpg dists/stable/Release

# Verify before pushing.
GNUPGHOME=/home/gerard/.gnupg-ubuntu-backup-archive gpg --verify dists/stable/InRelease
GNUPGHOME=/home/gerard/.gnupg-ubuntu-backup-archive gpg --verify dists/stable/Release.gpg dists/stable/Release
```

Commit and push the `gh-pages` branch. GitHub Pages rebuilds automatically.
