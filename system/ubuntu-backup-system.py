#!/usr/bin/python3 -I
"""System-wide CONFIGURATION capture; root reads, owner writes. Never a disk image."""
# Managed by ubuntu-configuration system capture v1
import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

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
PACKAGE_FILE_MAX = 64 * 1024 * 1024
PACKAGE_FILE_TOTAL = 256 * 1024 * 1024
XATTR_MAX = 64 * 1024
XATTR_PREFIXES = ("user.", "system.posix_acl_")
XATTR_EXACT = {"security.capability"}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_dpkg_verify(text):
    records = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("missing"):
            flags, rest = "missing", line[len("missing"):].lstrip()
        else:
            match = re.match(r"^([?.A-Za-z0-9]{9})\s+(.*)$", line)
            if not match:
                continue
            flags, rest = match.group(1), match.group(2)
        conffile = rest.startswith("c ")
        if conffile:
            rest = rest[2:].lstrip()
        note = ""
        if rest.endswith(")") and " (" in rest:
            rest, note = rest.rsplit(" (", 1)
            note = note[:-1]
        path = rest.strip()
        if path.startswith("/"):
            records.append({"path": path, "flags": flags,
                            "conffile": conffile, "note": note})
    return records


def _dpkg_owners(info_dir=Path("/var/lib/dpkg/info")):
    owners = {}
    try:
        lists = sorted(info_dir.glob("*.list"))
    except OSError:
        return owners
    for path in lists:
        package = path.name[:-5].partition(":")[0]
        try:
            for line in path.read_text(errors="replace").splitlines():
                if line.startswith("/"):
                    owners.setdefault(line, package)
        except OSError:
            continue
    return owners


def _dpkg_conffiles(status_path=Path("/var/lib/dpkg/status")):
    paths = set()
    try:
        text = status_path.read_text(errors="replace")
    except OSError:
        return paths
    in_conffiles = False
    for line in text.splitlines():
        if line.startswith("Conffiles:"):
            in_conffiles = True
        elif in_conffiles and line.startswith(" "):
            parts = line.split()
            if parts:
                paths.add(parts[0])
        elif in_conffiles:
            in_conffiles = False
    return paths


def _package_counts(records):
    count = {"total": 0, "modified_files": 0, "modified_packages": 0,
             "conffiles": 0, "captured": 0, "unverified": 0,
             "unreadable": 0, "missing": 0, "metadata_changes": 0}
    packages = set()
    for record in records:
        flags, note = str(record.get("flags", "")), str(record.get("note", ""))
        package, conffile = record.get("package"), bool(record.get("conffile"))
        count["total"] += 1
        if not package:
            continue
        if conffile:
            count["conffiles"] += 1
        elif "Permission denied" in note:
            count["unreadable"] += 1
        elif flags == "?????????":
            count["unverified"] += 1
        elif flags == "missing":
            count["missing"] += 1
            count["modified_files"] += 1
            packages.add(package)
        elif any(flag in flags for flag in "5S"):
            count["modified_files"] += 1
            packages.add(package)
        else:
            count["metadata_changes"] += 1
        if record.get("captured"):
            count["captured"] += 1
    count["modified_packages"] = len(packages)
    return count


def _read_xattrs(path):
    values = {}
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as error:
        return values, f"xattrs unavailable: {error}"
    total = 0
    for name in names:
        if name not in XATTR_EXACT and not any(name.startswith(prefix) for prefix in XATTR_PREFIXES):
            continue
        try:
            value = os.getxattr(path, name, follow_symlinks=False)
        except OSError as error:
            return values, f"xattr {name}: {error}"
        total += len(value)
        if total > XATTR_MAX:
            return values, "xattr size limit reached"
        values[name] = base64.b64encode(value).decode("ascii")
    return values, ""


def package_integrity(destination):
    manifest = {"format": "ubuntu-package-integrity-v1", "available": False,
                "captured_at": _now(),
                "caution": "Captured bytes are the modified files at scan time, not vendor originals.",
                "max_file_bytes": PACKAGE_FILE_MAX, "max_total_bytes": PACKAGE_FILE_TOTAL,
                "records": [], "warnings": []}
    dpkg = shutil.which("dpkg") or "/usr/bin/dpkg"
    try:
        result = subprocess.run([dpkg, "--verify", "--verify-format=rpm"],
                                capture_output=True, text=True, errors="replace", timeout=300)
        if result.returncode not in (0, 1):
            result = subprocess.run([dpkg, "--verify"], capture_output=True, text=True,
                                    errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        manifest["warnings"].append(f"dpkg verify failed: {error}")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        (destination / "package-integrity.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest
    verify_output = result.stdout
    if result.returncode not in (0, 1):
        manifest["warnings"].append(f"dpkg verify exited with {result.returncode}")
    owners = _dpkg_owners()
    conffiles = _dpkg_conffiles()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    content_dir = destination / "package-files"
    captured_total, seen = 0, set()
    for item in _parse_dpkg_verify(verify_output):
        path = item["path"]
        if path in seen:
            continue
        seen.add(path)
        package = owners.get(path)
        record = {"path": path, "package": package, "flags": item["flags"],
                  "conffile": bool(item["conffile"] or path in conffiles),
                  "note": item["note"], "captured": False}
        if not package:
            record["reason"] = "not owned by an installed package"
        elif record["conffile"]:
            record["reason"] = "conffile; captured by the configuration backup"
        elif "Permission denied" in item["note"]:
            record["reason"] = "unreadable without root"
        elif item["flags"] == "?????????":
            record["reason"] = "verification unavailable"
        elif item["flags"] == "missing":
            record["reason"] = "missing; reinstall the package"
        elif not any(flag in item["flags"] for flag in "5S"):
            record["reason"] = "metadata-only change"
        else:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise OSError("not a regular file")
                    if before.st_size > PACKAGE_FILE_MAX:
                        raise OSError("larger than the per-file capture limit")
                    if captured_total + before.st_size > PACKAGE_FILE_TOTAL:
                        raise OSError("package-file capture limit reached")
                    data = stream.read(PACKAGE_FILE_MAX + 1)
                    after = os.fstat(stream.fileno())
                if (len(data) > PACKAGE_FILE_MAX or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns):
                    raise OSError("file changed during capture")
            except OSError as error:
                record["reason"] = str(error)
            else:
                sha256 = hashlib.sha256(data).hexdigest()
                content_dir.mkdir(mode=0o700, exist_ok=True)
                target = content_dir / sha256
                if not target.exists():
                    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                captured_total += len(data)
                xattrs, xattr_warning = _read_xattrs(path)
                record.update({"captured": True, "sha256": sha256,
                               "size": len(data), "mode": stat.S_IMODE(before.st_mode),
                               "uid": before.st_uid, "gid": before.st_gid,
                               "xattrs": xattrs})
                if xattr_warning:
                    record["xattr_warning"] = xattr_warning
        manifest["records"].append(record)
    counts = _package_counts(manifest["records"])
    manifest.update({"available": True, "captured_bytes": captured_total, "counts": counts})
    if counts["unreadable"]:
        manifest["warnings"].append(
            f"{counts['unreadable']} package paths could not be read without root.")
    (destination / "package-integrity.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _chown_tree(root, owner_uid, owner_gid):
    for directory, _, files in os.walk(root):
        os.chown(directory, owner_uid, owner_gid)
        for name in files:
            os.chown(Path(directory) / name, owner_uid, owner_gid)


def package_scan(owner_uid, owner_gid, kit):
    """Root-only, read-only package-file scan into the user's workspace."""
    if os.geteuid() != 0:
        raise RuntimeError("The package scan must run as root.")
    kit = Path(kit).absolute()
    if kit.is_symlink() or not kit.is_dir():
        raise RuntimeError("Run a user backup once so the private workspace exists.")
    if kit.stat().st_uid != owner_uid:
        raise RuntimeError("The workspace must be owned by the backup user.")
    staging = Path(tempfile.mkdtemp(prefix=".privileged-package-scan-", dir=kit))
    cache = kit / "privileged-package-scan"
    previous = None
    try:
        manifest = package_integrity(staging)
        _chown_tree(staging, owner_uid, owner_gid)
        if cache.exists():
            if cache.is_symlink() or not cache.is_dir():
                raise RuntimeError("Refusing an invalid package-scan cache.")
            previous = kit / (".privileged-package-scan-old-" + uuid.uuid4().hex)
            cache.rename(previous)
        try:
            staging.rename(cache)
        except BaseException:
            if previous is not None and previous.exists() and not cache.exists():
                previous.rename(cache)
            raise
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
        print(f"Package scan: {manifest['counts']['captured']} captured, "
              f"{manifest['counts']['modified_files']} modified, "
              f"{manifest['counts']['unreadable']} unreadable.")
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _request_owner():
    for key in ("PKEXEC_UID", "SUDO_UID"):
        value = os.environ.get(key)
        if value:
            try:
                return int(value)
            except ValueError:
                continue
    return None


def _validated_workspace(owner, value):
    kit = (Path(value).expanduser().absolute() if value
           else Path(owner.pw_dir) / "Ubuntu-Backup" / "data")
    if ".." in kit.parts or kit.is_symlink():
        raise RuntimeError("Invalid workspace path.")
    if Path(owner.pw_dir) not in kit.parents:
        raise RuntimeError("Workspace must be inside the backup owner's home.")
    return kit


def home_capture(owner, workspace):
    app = Path("/usr/lib/ubuntu-backup/ubuntu-backup.py")
    if not app.is_file():
        raise RuntimeError("Install ubuntu-backup-core to enable full /home capture.")
    env = dict(os.environ, SUDO_UID=str(owner.pw_uid), SUDO_GID=str(owner.pw_gid),
               HOME=owner.pw_dir)
    command = ["/usr/bin/python3", "-I", str(app), "--workspace", str(workspace),
               "_backup-home-worker"]
    os.execve(command[0], command, env)


def _write_result(owner, kit, result):
    base = Path(owner.pw_dir) / "Ubuntu-Backup" / "data" / "system"
    directory = base / "results"
    for path in (base, directory):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(path, owner.pw_uid, owner.pw_gid)
    path = directory / "request.json"
    temporary = directory / (".request-" + uuid.uuid4().hex)
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    info = path.stat()
    if os.geteuid() == 0 and (info.st_uid != owner.pw_uid or info.st_gid != owner.pw_gid):
        os.chown(path, owner.pw_uid, owner.pw_gid)


def process_request(request_path):
    if os.geteuid() != 0:
        raise RuntimeError("The request processor must run as root.")
    owner_uid = _request_owner()
    if owner_uid is None:
        raise RuntimeError("No PKEXEC_UID/SUDO_UID for the request.")
    owner = pwd.getpwuid(owner_uid)
    if owner.pw_uid == 0:
        raise RuntimeError("A non-root request owner is required.")
    path = Path(request_path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Request must be a regular file.")
    info = path.stat()
    if info.st_uid != owner_uid:
        raise RuntimeError("Request is not owned by the invoking user.")
    if info.st_size > 4096:
        raise RuntimeError("Request is too large.")
    data = json.loads(path.read_text())
    if data.get("format") != "ubuntu-backup-request-v1":
        raise RuntimeError("Unrecognized request format.")
    if int(data.get("owner", -1)) != owner_uid:
        raise RuntimeError("Request owner mismatch.")
    action = data.get("action")
    if action not in ("capture", "package-scan"):
        raise RuntimeError("Unsupported request action.")
    kit = _validated_workspace(owner, data.get("workspace"))
    result = {"format": "ubuntu-backup-result-v1", "action": action,
              "created_at": _now(), "owner": owner_uid, "status": "ok"}
    try:
        if action == "capture":
            main(owner_uid, kit, False, True)
        else:
            package_scan(owner_uid, owner.pw_gid, kit)
    except BaseException as error:  # noqa: BLE001 - report the failure to the user
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        _write_result(owner, kit, result)
        path.unlink(missing_ok=True)
        raise
    _write_result(owner, kit, result)
    path.unlink(missing_ok=True)
    print("Request completed: " + action)


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


def main(owner_uid=None, workspace=None, include_other_users=False, run_package_scan=True):
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
    if run_package_scan:
        try:
            package_scan(owner.pw_uid, owner.pw_gid, kit)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"Warning: package scan failed: {type(error).__name__}: {error}", file=sys.stderr)
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


def _option_owner(value):
    if value:
        try:
            return pwd.getpwuid(int(value))
        except ValueError:
            return pwd.getpwnam(str(value))
    identity = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if identity:
        return pwd.getpwuid(int(identity))
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?",
                        choices=("capture", "package-scan", "home-capture", "process-request"),
                        default="capture")
    parser.add_argument("--owner", help="UID or username that owns the backup workspace")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--request", type=Path, help="request file for process-request")
    parser.add_argument("--include-other-users", action="store_true",
                        help="also copy other local users' and root's home configuration "
                             "(privacy-sensitive; off by default)")
    options = parser.parse_args()
    try:
        if options.command == "process-request":
            if options.request is None:
                parser.error("--request is required for process-request")
            process_request(options.request)
        elif options.command in ("package-scan", "home-capture"):
            owner = _option_owner(options.owner)
            if owner is None:
                raise RuntimeError("Owner is required (--owner or PKEXEC_UID/SUDO_UID).")
            kit = _validated_workspace(owner, options.workspace)
            if options.command == "package-scan":
                package_scan(owner.pw_uid, owner.pw_gid, kit)
            else:
                home_capture(owner, kit)
        else:
            main(options.owner, options.workspace, options.include_other_users)
    except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
