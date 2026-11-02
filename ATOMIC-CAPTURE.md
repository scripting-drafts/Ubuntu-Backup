# Atomic full-`/home` capture

Status: design only. Chosen approach: snapshot the source filesystem before reading it; do not
try to emulate point-in-time consistency inside the copier.

## Why the current capture is not atomic

`root_home_backup()` streams a live `tar` over `/home` while applications keep writing. GNU tar
trusts the size in each header, so a file that grows or shrinks mid-read is stored torn (padded or
truncated). The timeline is published atomically (temp directory, verify, `rename`), but its
*content* spans the whole capture window rather than one instant. No amount of retrying in the
copier can fix that: the filesystem itself must hold still.

## Layout constraint

`/home` is a directory on the root ext4 filesystem. There is no separate `/home` block device, LVM
volume group, btrfs subvolume, or ZFS dataset. The snapshot unit must therefore be the **root
filesystem**, not `/home` alone. LVM and btrfs both require a re-layout or reinstall; this tool must
never perform that migration automatically.

## Target designs

### A. LVM thin volume (recommended)

Preconditions: root on a thin LV in a VG, `lvm2` installed, VG free space at least the expected
copy-on-write growth for the capture window.

1. Verify source hardware/mount identity exactly as today.
2. `sync`.
3. Freeze the source filesystem only around snapshot creation:
   `fsfreeze --freeze /home`, `lvcreate -s -n <name> -L <size> <root_lv>`, `fsfreeze --unfreeze /home`.
4. Mount the snapshot read-only (`-o ro,nouuid`) under a private root-only directory.
5. Stream the existing `tar` pipeline from the snapshot mountpoint.
6. In `finally`: unmount, then `lvremove -f`. Never leave an orphan snapshot mounted.

Window: seconds for the freeze. Long-lived cost is only thin-pool growth while the snapshot exists.

### B. btrfs

Root on btrfs with `/home` as a subvolume (`@home`):

1. `btrfs subvolume snapshot -r /home <private>/snap`
2. Mount read-only, stream tar from it, unmount.
3. `btrfs subvolume delete <private>/snap` in `finally`.

No freeze is needed. In-place `btrfs-convert` is possible but risky; prefer a planned reinstall or a
maintenance-window migration. `btrfs send` is an option later, but keep tar so the on-drive format
stays unchanged.

### C. ZFS

`zfs snapshot pool/ROOT@<name>`, read through the read-only `.zfs/snapshot/<name>` path (or a clone),
then `zfs destroy`. Same integration points as A and B.

## Code changes

1. **`snapshot_source()` helper** — detect the filesystem type and available tool, create the
   snapshot, return `(mountpoint, metadata, release)`. Unsupported or unavailable must raise, never
   silently fall back to a live read when atomic capture is required.
2. **`root_home_backup()`** — create the snapshot *before* the writer drops privileges; point
   `tar --directory=` at the snapshot mountpoint; keep the existing exclusion list, resolved
   relative to the workspace inside the snapshot. Record a `snapshot` block in the manifest
   (`kind`, `id`, `created_at`, `window_ms`).
3. **Privilege ordering** — snapshot create/teardown need root, but the writer deliberately drops to
   the owner before consuming the stream. Use a small root supervisor process that creates the
   snapshot up front and unmounts/destroys it after the stream ends, or have the worker fork a root
   helper before `setuid`. Teardown must run on failure, SIGINT, and disk-full.
4. **`capture_home_archive()`** — unchanged; it stays a pure stream consumer.
5. **`wizard_home_backup()`** — replace the "capture is not atomic" warning with the actual snapshot
   status. New setting `home_snapshot: "auto" | "required" | "off"`; `required` refuses to start
   without a snapshot.
6. **`install.sh`** — report snapshot capability and the `home_snapshot` default. Do not change the
   filesystem layout.

## Safety

- Mount snapshots read-only; never write to them.
- Destroy snapshots in `finally`; detect and report orphans on startup with an explicit cleanup action.
- Check thin-pool/VG free space before creating; abort below the reserved headroom.
- Record snapshot id and source device in the manifest and status history.
- Snapshots contain secrets and are never published.
- The backup drive and staging area must stay on a different filesystem (already enforced).

## Tests

- PATH shims for `findmnt`, `lvcreate`, `btrfs`: success, missing tool, creation failure, and
  failure mid-stream must all leave no snapshot behind.
- Manifest carries the snapshot metadata; older manifests without it still verify.
- `home_snapshot="off"` reproduces today's behaviour exactly.

## Rollout

1. Land the abstraction with `off` as the default (no behaviour change).
2. Add the LVM path and exercise it on a scratch VM.
3. Default to `auto`; `required` for machines that can snapshot.
4. Measure the freeze window; target under 5 seconds.
5. Run a restore drill from a snapshot-based timeline.

## Out of scope

- Automating ext4 → LVM/btrfs migration; it is destructive and must stay a manual, reviewed step.
- Application-internal consistency. A filesystem snapshot gives a point-in-time file set, but running
  databases, browsers, and VMs may hold unflushed state. Quiesce or use their native backup APIs
  before snapshotting.
