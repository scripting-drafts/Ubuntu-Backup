# APT publishing

Ubuntu Backup is published from the Launchpad PPA:

```text
ppa:toomanypatterns/ubuntu-backup
https://ppa.launchpadcontent.net/toomanypatterns/ubuntu-backup/ubuntu/ jammy main
```

Launchpad signs the archive with `DD690D4A9810E29DE3824FF2FBD3F68940827DC3`;
each upload is signed with the maintainer key registered on Launchpad.

## Install

```bash
sudo add-apt-repository ppa:toomanypatterns/ubuntu-backup
sudo apt update
sudo apt install ubuntu-backup
```

`ubuntu-backup`, `ubuntu-backup-core` and `ubuntu-backup-system` are chained and
install or remove as one unit.

## Publish an update

`debian/source/format` is `3.0 (native)`, so the version carries no Debian
revision and `debian/changelog` must target `jammy`.

```bash
# 1. Add a debian/changelog entry; the tree must be clean.
export XDG_RUNTIME_DIR=/run/user/$(id -u)
dpkg-buildpackage -S -sa -kF104A3F1CE9A23540AEB8F24F31978E49E85DDCA

# 2. Upload; the target comes from ~/.dput.cf.
dput ubuntu-backup ../ubuntu-backup_<version>_source.changes
```

Launchpad builds the three binary packages for jammy, then publishes them. Watch
the queue before announcing the release:

```bash
curl -fsS "https://api.launchpad.net/1.0/~toomanypatterns/+archive/ubuntu/ubuntu-backup?ws.op=getPublishedSources&source_name=ubuntu-backup"
```

Verify on a client that the candidate resolves from the PPA:

```bash
apt-cache policy ubuntu-backup
```

## Retired origin

The GitHub Pages repository at `scripting-drafts.github.io/Ubuntu-Backup` is no
longer updated. Its `gh-pages` branch is kept as a historical archive only; do
not add it to new systems and do not publish new debs there.
