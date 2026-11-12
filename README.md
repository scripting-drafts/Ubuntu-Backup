# Ubuntu Backup

Ubuntu Backup is an interactive backup and recovery utility for Ubuntu desktops. It captures system configuration, development environments, and—when explicitly requested—a complete `/home` snapshot. A tray application provides backup and restore workflows, progress reporting, scheduling, and backup-drive management.

The project is designed to make configuration recovery repeatable without blindly restoring old package versions or overwriting a running home directory.

## What it backs up

A standard **Backup** operation brings together three areas:

- **System configuration** — selected configuration from `/etc` and `/usr/local/etc`, with configurable reference-only paths.
- **Development environments** — selected development projects and their recorded package/library references. Recovery reapplies changes in dependency order and resolves package names again instead of pinning historical versions.
- **Full home directory** — an optional, manual capture of `/home`. It is external-drive only, requires explicit privilege approval, and is handled exclusively by the native `ubackup-core` engine.

The tool also provides package scanning as part of the backup workflow and an optional modified-package recovery step during configuration restore.

## Features

- GTK desktop interface and tray indicator.
- Dedicated Backup and Restore process windows with live output, status/history, and cancellation.
- GUI password prompts for privileged operations.
- External backup-drive selection and saved-drive mount management.
- Scheduled backups.
- Shared-baseline external storage with dated timeline entries.
- Content-addressed storage to avoid storing identical file content repeatedly.
- Native full-home capture and restore, including sparse files, hard links, symbolic links, extended attributes, and ACLs.
- Guarded cleanup and purge operations.
- Regression tests and GitHub Actions CI.

## Requirements

The installer expects:

- Ubuntu **22.04 through 26.10** (it warns when run outside this range).
- Python 3.
- GTK 3 and the Python GObject bindings.
- Ayatana AppIndicator support for the tray integration.
- A C compiler, `make`, and OpenSSL development headers to build the native core.
- A graphical desktop session to start and use the tray interface.

On Ubuntu, install the dependencies with:

```bash
sudo apt update
sudo apt install \
  python3 python3-gi gir1.2-gtk-3.0 \
  libayatana-appindicator3-1 gir1.2-ayatanaappindicator3-0.1 \
  build-essential libssl-dev
```

## Installation

Clone the repository and run the installer as your regular desktop user. Do **not** run the installer with `sudo`.

```bash
git clone https://github.com/scripting-drafts/Ubuntu-Backup.git
cd Ubuntu-Backup
./install.sh
```

The installer:

1. Installs the application and its askpass helper under `~/.local/bin`.
2. Builds and installs the required native backup engine.
3. Installs the desktop launcher, icon, and tray autostart entry.
4. Configures a user systemd timer to retry mounting the saved backup drive.
5. Installs the AppIndicator integration when the desktop extension is available.
6. Starts the tray application when a graphical session is active.

The installed command is:

```bash
ubuntu-backup
```

You can also launch the Python entry point directly:

```bash
ubuntu-backup.py
```

## Using the application

### Back up

1. Connect the external drive you intend to use.
2. Open **Ubuntu Backup** from the application menu or tray.
3. Select or prepare the backup drive when prompted.
4. Start **Backup** and follow the progress window.
5. Review the operation status and history when it completes.

A normal backup captures configuration and development data. A full `/home` capture is a separate, manual operation: it requires an external backup destination and explicit sudo approval.

### Restore

1. Connect and mount the drive containing the backup.
2. Open **Restore** from the application.
3. Select the recovery point and the recovery scope offered by the interface.
4. Review the recovery choices and package-install warning before proceeding.
5. Follow the restore process and inspect its final report.

Configuration recovery is staged and uses rollback/recovery safeguards. A full-home restore is performed by the native engine and refuses to restore into a non-empty destination. It does not overwrite the currently active `/home` as an in-place operation.

### Manage the saved drive

The repository includes a helper for checking, mounting, and unmounting the configured backup drive:

```bash
./drive.sh status
./drive.sh mount
./drive.sh unmount
```

After installation, the tray can be restarted with:

```bash
./refresh.sh
```

These repository scripts expect the application to have been installed first.

## Storage model

External backups use the selected drive root and a shared baseline. Dated timeline entries reference that baseline through symlinks, reducing duplicate storage across backup runs.

Dated configuration copies are stored externally by default. The `keep_local_backups` setting can retain local copies as well.

The local workspace is normally created at:

```text
~/Ubuntu-Backup/data/
```

Keep the workspace's settings, snapshots/timelines, and rollback journals together. Do not manually move or edit individual timeline components unless you understand the storage layout.

## Security and data handling

**Backups are private data. They are not encrypted by this application.**

Configuration files, development workspaces, and home-directory snapshots can contain credentials, private keys, tokens, browser data, personal documents, and other sensitive information.

- Keep backup drives physically secure and restrict access to them.
- Do not commit, upload, publish, or share backup snapshots or exported configuration archives.
- Use a trusted external drive and verify that it is the intended destination before starting a backup.
- Treat restore and purge operations as destructive operations; read the selected scope and confirmation prompts carefully.
- Full `/home` capture is deliberately manual and external-only.
- Cancellation stops the native worker at a safe point and discards unpublished staging/timeline data.

### Uninstalling

To remove the application while preserving backups and runtime data:

```bash
./uninstall.sh
```

Preview the files and backup locations that would be affected:

```bash
./uninstall.sh --dry-run
```

To remove the program and permanently purge local workspaces and configured external backup timelines:

```bash
./uninstall.sh --yes --purge-backups
```

**Warning:** `--purge-backups` is destructive. It removes backup data, not just the application. Review the dry-run output first. Without `--purge-backups`, the uninstaller preserves backups and runtime traces.

## Project layout

| Path | Purpose |
|---|---|
| `ubuntu-backup.py` | Main Python application, orchestration, and desktop interface |
| `ubuntu-backup-askpass.py` | GUI password prompt helper for privileged commands |
| `native/ubackup-core.c` | Native full-home scan and restore engine |
| `native/Makefile` | Native engine build |
| `extension/` | AppIndicator integration patch and installer |
| `install.sh` | Application installation |
| `refresh.sh` | Tray restart helper |
| `drive.sh` | Saved backup-drive management |
| `uninstall.sh` | Application removal and optional backup purge |
| `tests/` | Python and native regression tests |
| `docs/TESTING.md` | Test instructions |
| `ATOMIC-CAPTURE.md` | Full-home snapshot design and consistency notes |
| `CHANGELOG.md` | Project changes |

## Development and testing

Build the native engine:

```bash
make -C native
```

Run the complete regression suite:

```bash
python3 -m unittest discover -s tests -v
```

Run the shell checks used by CI:

```bash
shellcheck install.sh uninstall.sh refresh.sh drive.sh extension/*.sh
bash -n install.sh uninstall.sh refresh.sh drive.sh extension/*.sh
```

The native engine can also be tested independently:

```bash
make -C native test
```

CI runs on Ubuntu 22.04 and 24.04 and checks shell scripts, Python compilation, and the regression suite.

For detailed testing guidance—including safe use of temporary workspaces and scratch drives—see [docs/TESTING.md](docs/TESTING.md).

## Full-home capture consistency

A live filesystem scan is not automatically a point-in-time snapshot. The native copier detects files that change while being read and retries them; persistently changing paths are reported as omissions rather than silently stored as torn content. The timeline itself is published atomically, but that alone does not make a live capture atomic.

Optional filesystem snapshot support is documented in [ATOMIC-CAPTURE.md](ATOMIC-CAPTURE.md). The `home_snapshot` setting accepts `off`, `auto`, or `required`; the default is `off`. Atomic capture depends on the underlying filesystem and its layout. The tool does not automatically migrate or repartition a system.

Even a filesystem snapshot does not guarantee application-level consistency for running databases, virtual machines, or other applications with unflushed state. Use application-native backup procedures or quiesce those workloads when required.

## Troubleshooting

- **Tray icon is missing or stuck:** run `./refresh.sh` from the repository, or restart the installed tray process.
- **Native core build fails:** verify that `build-essential` and `libssl-dev` are installed, then run `make -C native` to see the compiler output.
- **Drive is not available:** connect it and run `./drive.sh status`; use `mount` or `unmount` as needed.
- **No tray appears:** confirm that you are in a graphical desktop session and that GTK 3 / Ayatana AppIndicator dependencies are installed.
- **Restore reports omissions:** inspect the operation report; a completed operation with omissions is distinct from a fatal failure.

## License

No license is currently specified in this repository. Unless a license is added, the source should not be assumed to be available for unrestricted reuse or redistribution.
](https://github.com/scripting-drafts/Ubuntu-Backup/blob/main/README.md?utm_source=chatgpt.com)
