# Strict snap and system helper

`ubackup` is published as a **strict** snap. It never uses classic confinement,
`sudo`, `pkexec`, `/etc` writes, fstab edits, or raw-disk formatting.

## Split

| Component | Runs as | Responsibility |
|---|---|---|
| `ubackup` snap | desktop user | tray, user configuration, development environments, external-drive copies, verification |
| `ubuntu-backup-system` deb | root via pkexec/request watcher | `/etc`, package state, full `/home` capture |
| `ubuntu-backup-core` deb | shared | application + native engine used by GUI and system helper |

The deb writes a verified cache into the snap's workspace:

```text
~/Ubuntu-Backup/data/privileged-system/system-wide.tar.gz
```

The snap reads that cache on the next backup. Root reads, owner writes: the
helper drops to the invoking user before creating the archive.

By default the helper captures `/etc`, `/usr/local/etc`, and **only the invoking
user's** home configuration. Other users' homes (including `/root`) are omitted
to avoid copying third-party secrets across a privilege boundary. Add
`--include-other-users` to the helper command if an administrator explicitly
needs them; the option is privacy-sensitive.

Sensitive root-only paths are also excluded from the cache: shadow/gshadow
databases, SSH host private keys, TLS private keys, Wi-Fi/NetworkManager
secrets, VPN secrets, Kerberos keytabs, and APT auth files. The remaining cache
still contains private configuration; keep the workspace and backup drives
secure.

## Install

```bash
snap install ubackup
sudo apt install ./ubuntu-backup-system_1.3.1_all.deb
```

The debs are also available from the signed APT repository documented in
[APT-REPO.md](APT-REPO.md):

```bash
sudo apt install ubuntu-backup-system
```

Install the `ubuntu-backup` GUI deb only when **not** using the snap; installing
both creates two tray processes. Use `ubuntu-backup-system` alongside the snap.

The strict snap declares `autostart: ubuntu-backup.desktop` and creates
`$SNAP_USER_DATA/.config/autostart/ubuntu-backup.desktop` on first run. snapd's
user session agent starts `ubackup tray` at login. It never writes the host's
`~/.config/autostart` and is removed with the snap.

Connect the privileged plugs if the store has not auto-connected them:

```bash
sudo snap connect ubackup:removable-media
sudo snap connect ubackup:udisks2
sudo snap connect ubackup:hardware-observe
sudo snap connect ubackup:mount-observe
```

The last two are required for `lsblk`/`findmnt` enumeration. The strict snap lists
unmounted Linux filesystems and mounts them through udisks2; formatting remains
deb-only.

## Capture

From the application menu: **Ubuntu Backup — System Capture** (uses `pkexec`).

Or from a terminal:

```bash
ubuntu-backup-system capture
```

There is **no scheduled system-capture timer**. Capture and package scan are
on-demand only. From the snap:

```bash
ubackup request-system --action capture
ubackup request-system --action package-scan
```

This writes a fixed request file; the deb's user path unit asks for
administrator approval through `pkexec`, then runs the helper. Full `/home`
capture is an explicit deb action:

```bash
ubuntu-backup-system home-capture
```

or **Ubuntu Backup — Full /home Capture** from the application menu.

The strict snap owns its tray autostart through snapd. The separate
`ubuntu-backup` GUI deb keeps its own
`/etc/xdg/autostart/ubuntu-backup-tray.desktop`; install only one GUI to avoid
two trays.

## Boundaries and limitations

- The strict snap does not capture dotfiles or `/etc` itself; install the deb
  for system-wide coverage. Without it, the snapshot records an explicit
  `SYSTEM-WIDE COVERAGE INCOMPLETE` warning.
- Full `/home` **restore**, package-file restore, system `/etc` restore,
  fstab/boot mounts, and drive formatting are explicit deb-only operations.
  The strict snap UI does not offer them.
- The helper's systemd unit uses `ReadWritePaths=/home/%i`; custom home
  locations outside `/home` need a unit drop-in.
- `ubuntu-backup-system` is a Debian/Ubuntu artifact. Other distributions get
  the user-space snap only.
