#!/usr/bin/python3 -I
"""System-wide CONFIGURATION capture; root reads, owner writes. Never a disk image."""
# Managed by ubuntu-configuration system capture v1
import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tarfile
import tempfile

MARKER = "# Managed by ubuntu-configuration system capture v1"
UNIT = "ubuntu-configuration-system"
HELPER = Path("/usr/libexec/ubuntu-backup/ubuntu-backup-system")
ROOTS = ("/etc", "/usr/local/etc")
MAX_FILE, MAX_TOTAL = 64 * 1024 * 1024, 1024 * 1024 * 1024
DATA_DIRS = {"cache", "caches", "gpucache", "code cache", "cacheddata", "cachedextensions", "cachedextensionvsixs",
             "crash reports", "crashpad", "logs", "history", "workspacestorage", "globalstorage",
             "indexeddb", "local storage", "session storage", "service worker", "blob_storage", "__pycache__"}
HOME_PATHS = (".config", ".local/share/applications", ".local/share/fonts", ".local/share/themes", ".local/share/icons",
              ".local/share/gnome-shell/extensions", ".fonts", ".themes", ".icons", ".ssh/config", ".kube/config",
              ".gnupg/gpg.conf", ".gnupg/gpg-agent.conf", ".docker/config.json", ".aws/config")
HISTORIES = {".bash_history", ".python_history", ".scala_history", ".dotty_history", ".lesshst", ".wget-hsts",
             ".sudo_as_admin_successful", ".Xauthority", ".ICEauthority", ".DS_Store"}
SENSITIVE_PATHS = {
    "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-",
    "/etc/security/opasswd", "/etc/krb5.keytab", "/etc/ipsec.secrets",
    "/etc/apt/auth.conf",
}
SENSITIVE_PREFIXES = (
    "/etc/ssl/private", "/etc/pki/private", "/etc/letsencrypt/archive",
    "/etc/letsencrypt/live", "/etc/ssh/ssh_host_",
    "/etc/NetworkManager/system-connections", "/etc/wpa_supplicant",
    "/etc/openvpn", "/etc/ceph", "/etc/libvirt/secrets",
)


def collect(owner_uid, accounts=None, system_roots=ROOTS, include_other_users=False):
    """Pinned descriptors, unlimited depth, no symlinks or nested mounts followed.

    Other users' home directories (including /root) are excluded by default: the
    capture is written into the invoking user's workspace, so including them
    would copy third-party secrets across a privilege boundary.
    """
    files, metadata, links, omissions, excluded = {}, {}, [], [], []
    total = 0

    def scan(parent_fd, name, prefix, device=None):
        nonlocal total
        if name.lower() in DATA_DIRS:
            excluded.append(prefix)
            return
        live = "/" + prefix.removeprefix("system/") if prefix.startswith("system/") else ""
        if live in SENSITIVE_PATHS or (live and live.startswith(SENSITIVE_PREFIXES)):
            excluded.append(prefix + " (sensitive; not captured)")
            return
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                links.append({"path": prefix, "target": os.readlink(name, dir_fd=parent_fd)})
                return
            if stat.S_ISDIR(info.st_mode):
                if device is not None and info.st_dev != device:
                    excluded.append(prefix + " (nested mount)")
                    return
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    with os.scandir(fd) as entries:
                        names = sorted(entry.name for entry in entries)
                    for child in names:
                        scan(fd, child, prefix + "/" + child, info.st_dev)
                finally:
                    os.close(fd)
                return
            if not stat.S_ISREG(info.st_mode):
                excluded.append(prefix + " (runtime/special file)")
                return
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            with os.fdopen(fd, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise RuntimeError("Configuration changed type: " + prefix)
                data = source.read(MAX_FILE + 1)
                after = os.fstat(source.fileno())
            if len(data) > MAX_FILE:
                raise RuntimeError("Configuration exceeds 64 MiB; capture aborted: " + prefix)
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("Configuration changed during read; retry capture: " + prefix)
            total += len(data)
            if total > MAX_TOTAL:
                raise RuntimeError("Configuration exceeds 1 GiB; review excluded application data before retrying.")
            files[prefix] = data
            metadata[prefix] = {"mode": stat.S_IMODE(before.st_mode), "uid": before.st_uid, "gid": before.st_gid}
        except FileNotFoundError:
            return
        except PermissionError:
            omissions.append(prefix + " (unreadable even with current privileges)")

    for root in system_roots:
        path = Path(root)
        try:
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            scan(parent, path.name, "system/" + path.as_posix().lstrip("/"))
        finally:
            os.close(parent)
    chosen = []
    for account in accounts if accounts is not None else pwd.getpwall():
        if account.pw_uid != owner_uid:
            if not include_other_users:
                continue
            if account.pw_uid not in (0,) and not (
                    1000 <= account.pw_uid < 65534
                    and account.pw_shell not in ("/bin/false", "/usr/sbin/nologin", "/sbin/nologin")):
                continue
        home = Path(account.pw_dir)
        if not home.is_dir() or home.is_symlink():
            omissions.append(f"Account {account.pw_uid}: home missing or symlinked")
            continue
        chosen.append({"uid": account.pw_uid, "name": account.pw_name, "home": str(home)})
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            prefix = "home" if account.pw_uid == owner_uid else f"accounts/{account.pw_uid}/home"
            paths = set(HOME_PATHS)
            with os.scandir(fd) as entries:
                paths.update(entry.name for entry in entries if entry.name.startswith(".") and entry.is_file(follow_symlinks=False)
                             and entry.name not in HISTORIES and not entry.name.endswith((".swp", ".swo", "~"))
                             and not entry.name.startswith(".xsession-errors"))
            for relative in sorted(paths):
                parent = os.dup(fd)
                try:
                    parts = Path(relative).parts
                    for part in parts[:-1]:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        os.close(parent)
                        parent = child
                    scan(parent, parts[-1], prefix + "/" + relative)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    omissions.append(prefix + "/" + relative + " (ancestor unavailable: " + exc.strerror + ")")
                finally:
                    os.close(parent)
        finally:
            os.close(fd)
    files["inventory/privileged-links.json"] = (json.dumps(links, indent=2) + "\n").encode()
    files["inventory/privileged-source-metadata.json"] = (json.dumps(metadata, indent=2) + "\n").encode()
    files["inventory/privileged-exclusions.json"] = (json.dumps(excluded, indent=2) + "\n").encode()
    return files, {"accounts": chosen, "roots": list(system_roots), "omissions": omissions}


def main(owner_uid=None, workspace=None, include_other_users=False):
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError("Run this helper through pkexec or the ubuntu-backup-system systemd timer.")
    identity = owner_uid if owner_uid is not None else (
        os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID"))
    if identity is None:
        raise RuntimeError("A non-root backup owner (SUDO_UID/PKEXEC_UID or --owner) is required.")
    try:
        owner = pwd.getpwuid(int(identity))
    except ValueError:
        owner = pwd.getpwnam(str(identity))
    if owner.pw_uid == 0:
        raise RuntimeError("A non-root backup owner is required.")
    kit = (Path(workspace).expanduser().absolute() if workspace
           else Path(owner.pw_dir) / "Ubuntu-Backup" / "data")
    if Path(owner.pw_dir) not in kit.parents or ".." in kit.parts or kit.is_symlink():
        raise RuntimeError("The private workspace must be inside the backup owner's real home directory.")
    files, details = collect(owner.pw_uid, include_other_users=include_other_users)
    captured = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest = {"format": "ubuntu-configuration-v1", "kind": "system-wide-configuration-cache", "created_at": captured,
                "captured_at": captured, "scope": ("system+owner" if not include_other_users
                                                   else "system+owner+other-users"), **details,
                "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                           "executable": False} for name, data in sorted(files.items())]}
    if sum(len(data) for data in files.values()) > MAX_TOTAL or len(json.dumps(manifest).encode()) > MAX_FILE:
        raise RuntimeError("Complete configuration/metadata exceeds safety limits; old cache preserved.")
    os.setgroups([])
    os.setgid(owner.pw_gid)
    os.setuid(owner.pw_uid)
    os.environ.update(HOME=owner.pw_dir, XDG_RUNTIME_DIR=f"/run/user/{owner.pw_uid}",
                      DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{owner.pw_uid}/bus")
    destination = kit / "privileged-system"
    if destination.is_symlink():
        raise RuntimeError("Refusing a symlinked capture directory.")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination, prefix=".system-wide-", delete=False) as output:
            temporary = Path(output.name)
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                payload = dict(files)
                payload["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
                for name, data in sorted(payload.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), 0o600
                    archive.addfile(info, io.BytesIO(data))
            output.flush()
            os.fsync(output.fileno())
        with tarfile.open(temporary, "r:gz") as archive:
            for record in manifest["files"]:
                if hashlib.sha256(archive.extractfile(record["path"]).read()).hexdigest() != record["sha256"]:
                    raise RuntimeError("Privileged cache verification failed; old cache preserved.")
        os.replace(temporary, destination / "system-wide.tar.gz")
        temporary = None
        fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Privately captured {len(files)} configuration files across "
          f"{len(details['accounts'])} account(s), scope {manifest['scope']}; "
          f"omissions: {len(details['omissions'])}.")
    print("Cache: " + str(destination / "system-wide.tar.gz"))
    print("The strict snap will include this cache on its next configuration backup. It is NOT encrypted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("capture",), default="capture")
    parser.add_argument("--owner", help="UID or username that owns the backup workspace")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--include-other-users", action="store_true",
                        help="also copy other local users' and root's home configuration "
                             "(privacy-sensitive; off by default)")
    options = parser.parse_args()
    try:
        main(options.owner, options.workspace, options.include_other_users)
    except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
