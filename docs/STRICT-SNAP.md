# Strict snap and system helper

`ubackup` is published as a **strict** snap. It never uses classic confinement,
`sudo`, `pkexec`, `/etc` writes, fstab edits, or raw-disk formatting.

## Split

| Component | Runs as | Responsibility |
|---|---|---|
| `ubackup` snap | desktop user | tray, user configuration, development environments, external-drive copies, verification |
| `ubuntu-backup-system` deb | root via systemd/pkexec | `/etc`, other users' dotfiles, package state, full `/home`, system restore |

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

## Install

```bash
snap install ubackup
sudo apt install ./ubuntu-backup-system_1.2.0_all.deb
```

Install the `ubuntu-backup` GUI deb only when **not** using the snap; installing
both creates two tray processes. Use `ubuntu-backup-system` alongside the snap.

Connect the privileged plugs if the store has not auto-connected them:

```bash
sudo snap connect ubackup:removable-media
sudo snap connect ubackup:udisks2
```

## Capture

From the application menu: **Ubuntu Backup — System Capture** (uses `pkexec`).

Or from a terminal:

```bash
ubuntu-backup-system capture
```

Daily system capture (opt-in, per user):

```bash
sudo systemctl enable --now ubuntu-backup-system@$(id -un).timer
```

Use the username, not the numeric UID: the unit's `ReadWritePaths=/home/%i`
resolves a username. Custom home locations need a drop-in.

The GUI tray autostart is owned by the deb
(`/etc/xdg/autostart/ubuntu-backup-tray.desktop`). A snap-only installation does
not write `~/.config/autostart`; start the tray from the application menu.

## Boundaries and limitations

- The strict snap does not capture dotfiles or `/etc` itself; install the deb
  for system-wide coverage. Without it, the snapshot records an explicit
  `SYSTEM-WIDE COVERAGE INCOMPLETE` warning.
- Full `/home` capture/restore, package-file restore, fstab/boot mounts, and
  drive formatting are deb-only operations. The snap reports this rather than
  invoking a privileged command.
- The helper's systemd unit uses `ReadWritePaths=/home`; custom home locations
  outside `/home` need a unit drop-in.
- `ubuntu-backup-system` is a Debian/Ubuntu artifact. Other distributions get
  the user-space snap only.
