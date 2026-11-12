# Native backup and restore core

- Required for every full `/home` backup and restore; Python is only the front end.
- Build: `make`
- Scan: `./ubackup-core scan --source DIR --objects DIR --entries FILE --report FILE [--stage DIR]`
- Restore: `./ubackup-core restore --entries FILE --objects DIR --destination DIR --report FILE`
- Content-addressed SHA-256 store; sparse files, hardlinks, symlinks, xattrs/ACLs.
- Add `--timeline DIR --run-id ID` to publish a Python-verifiable full-home timeline.
- Per-file omissions are recorded; restore refuses a non-empty destination.
- Exit codes: 0 complete, 3 complete with omissions, 1 fatal.
- Tests: `make test`; benchmark: `python3 benchmark.py --files 5000`
