# APT publishing

Ubuntu Backup is published from the Launchpad PPA:

```text
ppa:toomanypatterns/ubuntu-backup
https://ppa.launchpadcontent.net/toomanypatterns/ubuntu-backup/ubuntu/ jammy main
https://ppa.launchpadcontent.net/toomanypatterns/ubuntu-backup/ubuntu/ noble main
https://ppa.launchpadcontent.net/toomanypatterns/ubuntu-backup/ubuntu/ resolute main
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

`debian/source/format` is `3.0 (native)`. Jammy keeps the native version
(`1.3.31`); newer series get a series-suffixed version
(`1.3.31-0ubuntu1~noble1`, `1.3.31-0ubuntu1~resolute1`) so pockets cannot
collide. The release script builds each variant in a temporary tree; do not
edit `debian/changelog` to a non-jammy series by hand.

```bash
# Add a debian/changelog entry and commit it; the tree must be clean.
scripts/release.sh --check
scripts/release.sh
SERIES="noble resolute" scripts/release.sh
```

Launchpad builds the three binary packages for every uploaded series, then
publishes them. Watch the queue before announcing the release:

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
