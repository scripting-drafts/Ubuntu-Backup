# Testing

- Full regression suite: `python3 -m unittest discover -s tests -v`
- Native core: required; `make -C native` builds it, and tests exercise both scan and restore. CI installs `build-essential` and `libssl-dev`.
- Shell checks: `shellcheck install.sh uninstall.sh refresh.sh drive.sh extension/*.sh`
- Install round trip is covered by `InstallRoundTripTests` using an isolated temporary `HOME`.
- Destructive drills must use temporary workspaces and a scratch drive.
- `--purge-backups` deletes the local workspace and configured external timelines; never point it at real backups.
- CI runs the suite on Ubuntu 22.04 and 24.04 via `.github/workflows/ci.yml`.
