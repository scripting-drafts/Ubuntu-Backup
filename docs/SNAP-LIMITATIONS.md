# Strict snap limitations

The strict snap sets `SNAP_MODE` from the snapd `SNAP` environment variable.
The APT-installed application does not set it, so APT paths remain fully
functional.

## Not available in the strict snap

- `sudo`, `pkexec`, `/etc` writes and package-managed file writes.
- System-wide configuration capture and package scanning; these require the
  `ubuntu-backup-system` .deb helper.
- Full `/home` capture; this requires the .deb helper/core.
- Live full-`/home` restore; restores are staged only.
- Package-file restore, system timer/helper restore, package/snap/flatpak/
  desktop/build deployment.
- Drive formatting and fstab/boot mounts.

APT-only operations show the repository link:
https://github.com/scripting-drafts/Ubuntu-Backup#apt-repository

## Available in the strict snap

- User configuration backup and staging.
- Selective user-file restore and undo.
- Snapshot verification, history and purge.
- Mounted-drive selection and unmounted Linux filesystem mounting through
  udisks2.
- Tray and snap-native autostart.

## Helper boundary

The deb-owned helper is optional for the snap. Without it the snap records
incomplete system coverage and package-scan warnings. The snap never executes
the helper directly; requests are processed by the deb's watcher with polkit
approval.
