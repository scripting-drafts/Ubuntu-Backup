#!/usr/bin/python3 -I
# Ubuntu configuration: self-contained interactive kit
# Contains personal configuration data. PRIVATE; encoded, NOT encrypted.
"""Capture and redeploy a private, upgrade-conscious Ubuntu configuration kit."""

import argparse
import base64
from contextlib import contextmanager
import contextlib
import datetime as dt
import fcntl
import fnmatch
import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


KIT = Path(__file__).resolve().parents[1]
HOME = Path.home()
FORMAT = "ubuntu-configuration-v1"
UNIT = "ubuntu-configuration-backup"
MARKER = "# Managed by ubuntu-configuration v1"
WORKSPACE_README = '# Ubuntu configuration and backup\n\n- Live tool: `~/ubuntu-backup.py` (private, self-contained, not encrypted).\n- Workspace: keep its settings, snapshots/timelines and rollback journals.\n- Run `./ubuntu-backup.py` for the menu; `./ubuntu-backup.py --help` shows full docs.\n- One backup action captures configuration, development code and full `/home`; identical files are stored once.\n- Each development change records its time, content version and package/library references; reapply follows dependency order and never pins versions.\n- Full recovery stages configuration or full `/home`; live `/home` is never overwritten.\n- External copies use dated symlink timelines over shared baselines.\n- Full `/home` capture is manual, external-drive only, requires sudo approval, and is not auto-pruned.\n- Snapshots contain secrets and are not encrypted. Do not publish them.\n'
ARCHIVE_RE = re.compile(r"ubuntu-config-\d{8}T\d{6}\.\d{6}Z\.tar\.gz\Z")
PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?\Z")
PACKAGE_FILE_MAX = 64 * 1024 * 1024
PACKAGE_FILE_TOTAL = 256 * 1024 * 1024
XATTR_MAX = 64 * 1024
XATTR_PREFIXES = ("user.", "system.posix_acl_")
XATTR_EXACT = {"security.capability"}
MAX_IN_MEMORY = 64 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024 * 1024
STAGING_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
SYSTEM_CONFIG_ROOTS = ("/etc", "/usr/local/etc")
DISTRO_METAPACKAGES = ("ubuntu-desktop", "ubuntu-desktop-minimal", "ubuntu-minimal",
                       "ubuntu-standard", "ubuntu-restricted-addons")
APT_HISTORY_GLOB = "/var/log/apt/history.log*"
SUPPORTED_RELEASES = {
    "22.04": {"codename": "jammy", "name": "Jammy Jellyfish",
              "standard_support": "2027-06", "eol": "2037-04"},
    "24.04": {"codename": "noble", "name": "Noble Numbat",
              "standard_support": "2029-06", "eol": "2039-04"},
    "24.10": {"codename": "oracular", "name": "Oracular Oriole",
              "standard_support": "2025-07", "eol": "2025-07"},
    "25.04": {"codename": "plucky", "name": "Plucky Puffin",
              "standard_support": "2026-01", "eol": "2026-01"},
    "25.10": {"codename": "questing", "name": "Questing Quokka",
              "standard_support": "2026-07", "eol": "2026-07"},
    "26.04": {"codename": "resolute", "name": "Resolute Raccoon",
              "standard_support": "2031-05", "eol": "2041-04"},
    "26.10": {"codename": "stonking", "name": "Stonking Stingray",
              "standard_support": "2027-07", "eol": "2027-07"},
}
MIN_SUPPORTED_RELEASE = "22.04"
MAX_SUPPORTED_RELEASE = "26.10"
CONFIG_DATA_DIRS = {"cache", "caches", "gpucache", "code cache", "cacheddata", "cachedextensions", "cachedextensionvsixs",
                    "crash reports", "crashpad", "logs", "history", "workspacestorage", "globalstorage",
                    "indexeddb", "local storage", "session storage", "service worker", "blob_storage"}
KIT_FILES = ("README.md", ".gitignore", "backup.sh", "deploy.sh", "restore.sh",
             "install-backup-timer.sh", "configure-backup-drive.sh", "capture-root-configs.sh",
             "ubuntu-backup.py", "config", "scripts", "tests")


class ConfigError(Exception):
    pass


def timestamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def mkdir(path):
    if path.is_symlink():
        raise ConfigError(f"Refusing directory symlink: {path}")
    if path.exists():
        if not path.is_dir():
            raise ConfigError(f"Not a directory: {path}")
        return
    mkdir(path.parent)
    path.mkdir(mode=0o700)


def write(path, data, executable=False):
    mkdir(path.parent)
    if isinstance(data, str):
        data = data.encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-write-", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o700 if executable else 0o600)
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_stream(source, path, executable=False):
    """Write a file-like object atomically without buffering it in memory."""
    mkdir(path.parent)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".config-write-", delete=False) as output:
        temporary = Path(output.name)
        try:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fchmod(output.fileno(), 0o700 if executable else 0o600)
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_path(source, path, executable=False):
    with Path(source).open("rb") as stream:
        write_stream(stream, path, executable)


def write_json(path, value):
    write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def settings():
    value = json.loads((KIT / "config/settings.json").read_text())
    if not isinstance(value, dict):
        raise ConfigError("settings.json must contain an object.")
    defaults = globals().get("EMBEDDED_DEFAULT_SETTINGS")
    if isinstance(defaults, dict):
        value = {**defaults, **value}
    value.setdefault("home_snapshot", "off")
    if value["home_snapshot"] not in ("off", "auto", "required"):
        raise ConfigError("home_snapshot must be off, auto, or required.")
    if type(value.get("keep_snapshots")) is not int or value["keep_snapshots"] < 1:
        raise ConfigError("keep_snapshots must be a positive integer.")
    if not isinstance(value.get("keep_local_backups", False), bool):
        raise ConfigError("keep_local_backups must be true or false.")
    if not isinstance(value.get("coverage"), str):
        raise ConfigError("coverage must be a string.")
    if value.get("backup_calendar", "daily") not in ("daily", "weekly"):
        raise ConfigError("backup_calendar must be daily or weekly.")
    for key in ("development_include", "home_include", "home_reference_only",
                "system_reference_only", "exclude", "apt_review_only"):
        items = value.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ConfigError(key + " must be a list of strings.")
    excluded = value.get("exclude_cpus", [])
    if not isinstance(excluded, list) or not all(type(cpu) is int and cpu >= 0 for cpu in excluded):
        raise ConfigError("exclude_cpus must be a list of CPU numbers.")
    external = value.get("external_backup")
    if external is not None:
        if not isinstance(external, dict):
            raise ConfigError("external_backup must be an object or null.")
        subdirectory = external.get("subdirectory", "")
        if not isinstance(subdirectory, str):
            raise ConfigError("external_backup.subdirectory must be a string.")
        relative = PurePosixPath(subdirectory)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigError("external_backup.subdirectory must stay below the drive root.")
        marker_id = external.get("marker_id")
        if marker_id is not None and not isinstance(marker_id, str):
            raise ConfigError("external_backup.marker_id must be a string.")
    return value


def cpu_siblings(cpu):
    """Logical CPUs sharing a physical core with cpu."""
    try:
        value = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list").read_text().strip()
    except OSError:
        return {cpu}
    result = set()
    try:
        for part in value.split(","):
            bounds = [int(number) for number in part.split("-")]
            if len(bounds) == 1:
                result.add(bounds[0])
            elif len(bounds) == 2 and 0 <= bounds[0] <= bounds[1]:
                result.update(range(bounds[0], bounds[1] + 1))
            else:
                return {cpu}
    except ValueError:
        return {cpu}
    return result or {cpu}


def allowed_backup_cpus():
    """Current affinity minus excluded CPUs and their SMT siblings."""
    blocked = set()
    for cpu in settings().get("exclude_cpus", []):
        blocked |= cpu_siblings(cpu)
    allowed = set(os.sched_getaffinity(0)) - blocked
    if not allowed:
        raise ConfigError("exclude_cpus leaves no CPU available for the backup worker.")
    return allowed


def restrict_backup_cpus():
    allowed = allowed_backup_cpus()
    os.sched_setaffinity(0, allowed)
    return allowed


def safe_name(name):
    path = PurePosixPath(name)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts and str(path) == name


def checked_path(root, name):
    if not safe_name(name):
        raise ConfigError(f"Unsafe relative path: {name}")
    target = root
    for part in PurePosixPath(name).parts:
        target = target / part
        if target.is_symlink():
            raise ConfigError(f"Refusing symlink in destination: {target}")
    return target


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def regular_files(root):
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ConfigError(f"Unexpected symlink in snapshot: {path}")
        for name in files:
            path = Path(directory) / name
            if not path.is_file():
                raise ConfigError(f"Unexpected special file: {path}")
            result.append(path)
    return sorted(result)


def read_os_release():
    result = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value.strip('"')
    return result


def release_tuple(value):
    """Return a comparable (major, minor) tuple for an Ubuntu release string."""
    match = re.match(r"^(\d+)\.(\d+)", str(value or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def release_support(os_release=None):
    """Report whether the running Ubuntu release is in the tested range."""
    data = read_os_release() if os_release is None else os_release
    version = str(data.get("VERSION_ID") or "")
    key = release_tuple(version)
    minimum, maximum = release_tuple(MIN_SUPPORTED_RELEASE), release_tuple(MAX_SUPPORTED_RELEASE)
    supported = bool(key and minimum <= key <= maximum)
    release = SUPPORTED_RELEASES.get(version, {})
    return {
        "version": version,
        "codename": data.get("UBUNTU_CODENAME") or data.get("VERSION_CODENAME") or release.get("codename"),
        "name": release.get("name"),
        "supported": supported,
        "tested_from": MIN_SUPPORTED_RELEASE,
        "tested_through": MAX_SUPPORTED_RELEASE,
        "standard_support": release.get("standard_support"),
        "eol": release.get("eol"),
        "note": ("Release is inside the tested Ubuntu range."
                 if supported else
                 f"Release {version or 'unknown'} is outside the tested range "
                 f"{MIN_SUPPORTED_RELEASE}-{MAX_SUPPORTED_RELEASE}; review results manually."),
    }


def hardware_inventory():
    """Read-only hardware identity; no device probing, mounting, or driver execution."""
    def text(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None
    result = {"architecture": os.uname().machine,
              "vendor": text("/sys/class/dmi/id/sys_vendor"), "model": text("/sys/class/dmi/id/product_name"),
              "gpus": [], "network": [], "disks": [], "complete": True}
    try:
        for device in sorted(Path("/sys/bus/pci/devices").iterdir()):
            if (text(device / "class") or "").startswith("0x03"):
                result["gpus"].append({"vendor": text(device / "vendor"), "device": text(device / "device")})
        for device in sorted(Path("/sys/class/net").iterdir()):
            if (device / "device").exists():
                result["network"].append({"name": device.name, "address": text(device / "address")})
        output = subprocess.run(["lsblk", "--json", "--bytes", "--output", "TYPE,SIZE,FSTYPE,UUID,PARTUUID,MODEL"],
                                capture_output=True, text=True, timeout=15, check=True)
        def flatten(devices):
            for device in devices:
                if device.get("type") not in ("loop", "rom"):
                    result["disks"].append({key: device.get(key) for key in ("type", "size", "fstype", "uuid", "partuuid", "model")})
                flatten(device.get("children", []))
        flatten(json.loads(output.stdout)["blockdevices"])
        result["disks"].sort(key=lambda device: json.dumps(device, sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        result["complete"] = False
    return result


def configuration_home_paths(home):
    """Configuration roots, not a whole-home/data scan. Each directory recurses without a depth cap."""
    paths = [".config", ".local/share/applications", ".local/share/fonts", ".local/share/themes",
             ".local/share/icons", ".local/share/gnome-shell/extensions", ".fonts", ".themes", ".icons",
             ".ssh/config", ".gnupg/gpg.conf", ".gnupg/gpg-agent.conf", ".kube/config", ".docker/config.json",
             ".aws/config", ".mozilla/firefox/profiles.ini", ".mozilla/firefox/installs.ini"]
    histories = {".bash_history", ".python_history", ".scala_history", ".dotty_history", ".lesshst",
                 ".wget-hsts", ".sudo_as_admin_successful", ".Xauthority", ".ICEauthority", ".DS_Store"}
    try:
        for path in home.iterdir():
            if path.name.startswith(".") and path.is_file() and not path.is_symlink() and path.name not in histories:
                if not path.name.endswith((".swp", ".swo", "~")) and not path.name.startswith(".xsession-errors"):
                    paths.append(path.name)
    except PermissionError:
        pass
    for pattern in (".mozilla/firefox/*/prefs.js", ".mozilla/firefox/*/user.js", ".mozilla/firefox/*/chrome/*.css",
                    "snap/firefox/common/.mozilla/firefox/*/prefs.js", "snap/firefox/common/.mozilla/firefox/*/user.js"):
        try:
            paths.extend(path.relative_to(home).as_posix() for path in home.glob(pattern) if path.is_file())
        except PermissionError:
            pass
    return sorted(set(paths))


def capture_command(args, report, timeout=45, empty_codes=()):
    if shutil.which(args[0]) is None:
        report["unavailable_commands"].append(args[0])
        return ""
    environment = dict(os.environ, LC_ALL="C.UTF-8", COLUMNS="1000")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env=environment, errors="replace")
    except subprocess.TimeoutExpired:
        report["warnings"].append(f"{args[0]} inventory timed out")
        return ""
    if result.returncode in empty_codes:
        return ""
    if result.returncode:
        report["warnings"].append(f"{args[0]} inventory exited with code {result.returncode}")
        return ""
    return result.stdout


def vscode_extensions(report):
    # Read metadata instead of launching the Snap GUI wrapper inside a hardened timer.
    found = set()
    for relative in (".vscode/extensions/extensions.json", ".vscode-oss/extensions/extensions.json",
                     "snap/code/common/.vscode/extensions/extensions.json"):
        path = HOME / relative
        if not path.is_file():
            continue
        try:
            for extension in json.loads(path.read_text()):
                identity, version = extension["identifier"]["id"], extension["version"]
                if re.fullmatch(r"[A-Za-z0-9_.-]+", identity) and re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
                    found.add(identity + "@" + version)
        except (OSError, ValueError, TypeError, KeyError):
            report["warnings"].append(f"Cannot read VS Code extension metadata: {path}")
    return "".join(name + "\n" for name in sorted(found))


def package_name(value):
    """Normalize an APT package name by dropping an architecture suffix."""
    return value.strip().partition(":")[0]


def split_apt_items(value):
    """Split apt history lists on commas that are outside version parentheses."""
    items, current, depth = [], [], 0
    for character in value:
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
        if character == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if current:
        items.append("".join(current).strip())
    return [item for item in items if item]


def parse_apt_history(paths):
    """Read apt's rotated history logs; newest entries win for upgrades."""
    installed, removed, purged, upgraded = set(), set(), set(), {}
    events = 0
    ordered = []
    for path in paths:
        try:
            ordered.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(ordered):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", errors="replace") as stream:
                for line in stream:
                    line = line.rstrip("\n")
                    if line.startswith("Start-Date:"):
                        events += 1
                        continue
                    for label in ("Install", "Upgrade", "Remove", "Purge"):
                        prefix = label + ":"
                        if not line.startswith(prefix):
                            continue
                        for item in split_apt_items(line[len(prefix):]):
                            item = item.strip()
                            if not item:
                                continue
                            name = package_name(item.split(" (", 1)[0])
                            if not name or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
                                continue
                            if label == "Install":
                                installed.add(name)
                            elif label == "Remove":
                                removed.add(name)
                            elif label == "Purge":
                                purged.add(name)
                            else:
                                detail = item.split("(", 1)[1].rstrip(")") if "(" in item else ""
                                parts = [part.strip() for part in detail.split(",")]
                                entry = {"package": name}
                                if len(parts) >= 2:
                                    entry["from"], entry["to"] = parts[0], parts[1]
                                upgraded[name] = entry
        except OSError:
            continue
    return {"events": events, "installed": sorted(installed),
            "upgraded": [upgraded[name] for name in sorted(upgraded)],
            "removed": sorted(removed), "purged": sorted(purged),
            "removed_all": sorted(removed | purged)}


def distro_package_closure(installed, report):
    """Approximate a stock install from the distro metapackages' dependency closure."""
    metapackages = [name for name in DISTRO_METAPACKAGES if name in installed]
    if not metapackages:
        report["warnings"].append(
            "No Ubuntu metapackage found; the package delta cannot separate default packages.")
        return set(), []
    names = set(metapackages)
    command = ["apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests",
               "--no-conflicts", "--no-breaks", "--no-replaces", "--no-enhances"]
    for meta in metapackages:
        try:
            result = subprocess.run(command + [meta], capture_output=True, text=True,
                                    timeout=60, errors="replace")
            if result.returncode:
                result = subprocess.run(["apt-cache", "depends", "--recurse", meta],
                                        capture_output=True, text=True, timeout=60,
                                        errors="replace")
        except (OSError, subprocess.TimeoutExpired) as error:
            report["warnings"].append(f"Default-package closure incomplete for {meta}: {error}")
            continue
        if result.returncode:
            report["warnings"].append(f"Default-package closure failed for {meta}")
            continue
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("Depends:", "PreDepends:")):
                continue
            name = stripped.split(":", 1)[1].strip()
            if name.startswith("<") and name.endswith(">"):
                continue
            name = name.split()[0]
            if re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?", name):
                names.add(package_name(name))
    return names, metapackages


def package_delta(installed, manual, statuses, closure, metapackages, held, history, removed):
    """Classify current packages against the distro baseline and apt history."""
    installed, manual, closure = set(installed), set(manual), set(closure)
    default_installed = installed & closure
    added_manual = manual - closure
    default_manual = manual & closure
    added_dependencies = installed - manual - closure
    held_installed = held & installed
    removed_current = set(removed) - installed
    return {
        "format": "ubuntu-package-delta-v1",
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "baseline": {
            "method": "installed distro metapackage dependency closure",
            "metapackages": metapackages,
            "packages_in_closure": len(closure),
            "note": "Heuristic. 'added' means manually installed outside the metapackage closure; "
                    "'removed' comes from dpkg traces and rotated apt history, so purged packages "
                    "older than the retained logs may be absent.",
        },
        "counts": {
            "installed": len(installed), "manual": len(manual),
            "default_installed": len(default_installed),
            "added_manual": len(added_manual), "default_manual": len(default_manual),
            "added_dependencies": len(added_dependencies),
            "held_installed": len(held_installed),
            "held_not_installed": len(held - installed),
            "removed": len(removed_current),
        },
        "added_manual": sorted(added_manual),
        "default_manual": sorted(default_manual),
        "added_dependencies": sorted(added_dependencies),
        "held_installed": sorted(held_installed),
        "removed": sorted(removed_current),
        "removed_from_default": sorted(removed_current & closure),
        "apt_history": history,
        "removed_traces": sorted(name for name, status in statuses.items()
                                 if status[:2] in ("rc", "hc")),
    }


def parse_dpkg_verify(text):
    """Parse `dpkg --verify --verify-format=rpm` output."""
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


def dpkg_owners(info_dir=Path("/var/lib/dpkg/info")):
    """Map absolute package-owned paths to their package names."""
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


def dpkg_conffiles(status_path=Path("/var/lib/dpkg/status")):
    """Return the set of conffile paths from dpkg's status database."""
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


def conffile_drift(status_path=Path("/var/lib/dpkg/status")):
    """Compare installed conffiles with the MD5 values recorded by dpkg."""
    try:
        text = status_path.read_text(errors="replace")
    except OSError as error:
        return {"checked": 0, "modified": [], "modified_packages": [],
                "unreadable": [{"path": str(status_path), "reason": str(error)}],
                "missing": [], "obsolete": 0}
    package, installed, in_conffiles = None, False, False
    entries = []
    for line in text.splitlines():
        if line.startswith("Package: "):
            package, installed, in_conffiles = line.split(":", 1)[1].strip(), False, False
        elif line.startswith("Status: "):
            fields = line.split()
            installed = len(fields) >= 4 and fields[3] == "installed"
        elif line.startswith("Conffiles:"):
            in_conffiles = True
        elif in_conffiles and line.startswith(" "):
            parts = line.split()
            if package and installed and len(parts) >= 2:
                entries.append((package, parts[0], parts[1],
                                len(parts) > 2 and parts[2] == "obsolete"))
        elif in_conffiles:
            in_conffiles = False
    checked, obsolete = 0, 0
    modified, unreadable, missing = [], [], []
    for package, path, expected, is_obsolete in entries:
        if is_obsolete:
            obsolete += 1
            continue
        try:
            with open(path, "rb") as stream:
                data = stream.read()
        except FileNotFoundError:
            missing.append({"package": package, "path": path})
            continue
        except OSError as error:
            unreadable.append({"package": package, "path": path,
                               "reason": str(error)})
            continue
        checked += 1
        if hashlib.md5(data).hexdigest() != expected:
            modified.append({"package": package, "path": path})
    return {"checked": checked, "modified": modified,
            "modified_packages": sorted({item["package"] for item in modified}),
            "unreadable": unreadable, "missing": missing, "obsolete": obsolete}


def read_xattrs(path):
    """Capture file xattrs as base64 with a bounded total size."""
    values = {}
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as error:
        return values, f"xattrs unavailable: {error}"
    total = 0
    for name in names:
        try:
            value = os.getxattr(path, name, follow_symlinks=False)
        except OSError as error:
            return values, f"xattr {name}: {error}"
        total += len(value)
        if total > XATTR_MAX:
            return values, "xattr size limit reached"
        values[name] = base64.b64encode(value).decode("ascii")
    return values, ""


def xattr_allowed(name):
    return name in XATTR_EXACT or any(name.startswith(prefix) for prefix in XATTR_PREFIXES)


def apply_xattrs(path, mapping):
    """Apply supported xattrs; capabilities and POSIX ACLs require root."""
    errors = []
    for name, encoded in sorted((mapping or {}).items()):
        if not isinstance(name, str) or not isinstance(encoded, str) or not xattr_allowed(name):
            errors.append(f"unsupported xattr {name}")
            continue
        try:
            os.setxattr(path, name, base64.b64decode(encoded), follow_symlinks=False)
        except (OSError, ValueError) as error:
            errors.append(f"{name}: {error}")
    return errors


def package_integrity_counts(records):
    count = {"total": 0, "modified_files": 0, "modified_packages": 0,
             "conffiles": 0, "captured": 0, "unverified": 0,
             "unreadable": 0, "missing": 0, "metadata_changes": 0}
    packages = set()
    for record in records:
        if not isinstance(record, dict):
            continue
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


def package_integrity(destination, report, verify_output=None, owners=None, conffiles=None):
    """Scan package files, capture modified non-conffile contents, and report."""
    manifest = {"format": "ubuntu-package-integrity-v1", "available": False,
                "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "caution": "Captured bytes are the modified files at scan time, not vendor originals.",
                "max_file_bytes": PACKAGE_FILE_MAX, "max_total_bytes": PACKAGE_FILE_TOTAL,
                "records": [], "warnings": []}
    if verify_output is None:
        dpkg = shutil.which("dpkg")
        if not dpkg:
            manifest["warnings"].append("dpkg is unavailable")
            write_json(destination / "package-integrity.json", manifest)
            return manifest
        try:
            result = subprocess.run([dpkg, "--verify", "--verify-format=rpm"],
                                    capture_output=True, text=True, errors="replace",
                                    timeout=300)
            if result.returncode not in (0, 1):
                result = subprocess.run([dpkg, "--verify"], capture_output=True, text=True,
                                        errors="replace", timeout=300)
        except (OSError, subprocess.TimeoutExpired) as error:
            manifest["warnings"].append(f"dpkg verify failed: {error}")
            write_json(destination / "package-integrity.json", manifest)
            return manifest
        verify_output = result.stdout
        if result.returncode not in (0, 1):
            manifest["warnings"].append(f"dpkg verify exited with {result.returncode}")
    owners = dpkg_owners() if owners is None else owners
    conffiles = dpkg_conffiles() if conffiles is None else conffiles
    captured_total, seen = 0, set()
    for item in parse_dpkg_verify(verify_output):
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
                write(destination / "package-files" / sha256, data)
                captured_total += len(data)
                xattrs, xattr_warning = read_xattrs(path)
                record.update({"captured": True, "sha256": sha256,
                               "size": len(data), "mode": stat.S_IMODE(before.st_mode),
                               "uid": before.st_uid, "gid": before.st_gid,
                               "xattrs": xattrs})
                if xattr_warning:
                    record["xattr_warning"] = xattr_warning
        manifest["records"].append(record)
    count = package_integrity_counts(manifest["records"])
    manifest.update({"available": True, "captured_bytes": captured_total,
                     "counts": count})
    if count["unreadable"]:
        manifest["warnings"].append(
            f"{count['unreadable']} package paths could not be read without root.")
    report["package_files"] = count
    write_json(destination / "package-integrity.json", manifest)
    return manifest


def package_file_plan(source, records, packages=None, paths=None, force=False):
    """Decide which captured modified package files can be restored in place."""
    packages = {value.partition(":")[0] for value in (packages or [])}
    paths = {str(Path(value).expanduser()) for value in (paths or [])}
    plan = []
    for record in records:
        if not isinstance(record, dict) or not record.get("captured"):
            continue
        if record.get("conffile"):
            continue
        package = record.get("package")
        destination = Path(record.get("path", "")).expanduser()
        if packages and package not in packages:
            continue
        if paths and str(destination) not in paths:
            continue
        if not destination.is_absolute() or ".." in destination.parts:
            plan.append({"package": package, "destination": destination,
                         "action": "conflict", "reason": "unsafe destination path"})
            continue
        sha256 = record.get("sha256")
        source_file = source / "inventory/package-files" / str(sha256)
        item = {"package": package, "destination": destination,
                "source": source_file, "sha256": sha256,
                "size": record.get("size"), "mode": record.get("mode"),
                "uid": record.get("uid"), "gid": record.get("gid"),
                "xattrs": record.get("xattrs"),
                "current_sha": None, "action": "conflict", "reason": ""}
        if not sha256 or not source_file.is_file() or digest(source_file) != sha256:
            item["reason"] = "captured content is missing or corrupt"
            plan.append(item)
            continue
        try:
            info = destination.lstat()
        except FileNotFoundError:
            item["action"] = "restore"
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                item["reason"] = "destination is not a regular file"
            else:
                current = digest(destination)
                item["current_sha"] = current
                if current == sha256:
                    item["action"] = "already"
                elif force:
                    item["action"] = "restore"
                else:
                    item["reason"] = "destination changed since capture; use --force"
        plan.append(item)
    return plan


def latest_config_snapshot():
    """Pick the newest configuration snapshot that contains a package-file scan."""
    candidates = []
    local = KIT / "snapshot"
    if (local / "manifest.json").is_file():
        candidates.append(local)
    config = settings()
    if config.get("external_backup"):
        try:
            directory = external_directory(config)
            candidates.extend((directory / "timeline/config").glob("ubuntu-config-*"))
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
    usable = [path for path in candidates
              if (path / "inventory/package-integrity.json").is_file()]
    pool = usable or candidates
    if not pool:
        raise ConfigError("No configuration snapshot with a package-file scan was found.")
    return max(pool, key=lambda path: path.stat().st_mtime)


def wizard_restore_package_files(source=None):
    """Interactive frontend for the package-file restore review."""
    source = Path(source).expanduser().resolve() if source else latest_config_snapshot()
    print("Package-file restore source: " + str(source))
    packages = [item.strip() for item in ask(
        "Limit to packages (comma-separated; blank for all)", "").split(",") if item.strip()]
    paths = [item.strip() for item in ask(
        "Limit to paths (comma-separated; blank for all)", "").split(",") if item.strip()]
    plan = restore_package_files(source, apply=False, packages=packages, paths=paths)
    restorable = [item for item in plan if item["action"] == "restore"]
    conflicts = [item for item in plan if item["action"] == "conflict"]
    if not restorable:
        print("No package files can be restored from this snapshot.")
        return
    force = False
    if conflicts:
        force = confirm(f"{len(conflicts)} file(s) differ from the capture; "
                        "allow replacing them?", False)
    if not confirm(f"Restore {len(restorable)} package file(s) in place with sudo?", False):
        print("Restore cancelled; no changes.")
        return
    restore_package_files(source, apply=True, packages=packages, paths=paths, force=force)


def restore_package_files(snapshot=None, apply=False, packages=None, paths=None, force=False):
    """Review or restore captured modified package files in place."""
    source = Path(snapshot).expanduser().resolve() if snapshot else latest_config_snapshot()
    verify_path(source)
    manifest_path = source / "inventory/package-integrity.json"
    if not manifest_path.is_file():
        raise ConfigError("This snapshot has no package-file scan. Run a configuration backup first.")
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"Unreadable package-file inventory: {error}") from error
    records = data.get("records", []) if isinstance(data, dict) else []
    if isinstance(data, dict) and data.get("counts", {}).get("conffiles"):
        print("Conffiles are reported separately; restore them through the configuration snapshot.")
    unreadable_packages = sorted({record.get("package") for record in records
                                  if isinstance(record, dict) and record.get("package")
                                  and "Permission denied" in str(record.get("note", ""))})
    missing_packages = sorted({record.get("package") for record in records
                               if isinstance(record, dict) and record.get("package")
                               and record.get("flags") == "missing"
                               and not record.get("conffile")
                               and "Permission denied" not in str(record.get("note", ""))})
    if unreadable_packages:
        print("Unreadable without root: " + ", ".join(unreadable_packages)
              + "; run capture-package-files.")
    if missing_packages:
        print("Missing package files require reinstall: " + ", ".join(missing_packages))
        print("Vendor files: sudo apt-get install --reinstall " + " ".join(missing_packages))
    plan = package_file_plan(source, records, packages, paths, force)
    if not plan:
        print("No captured modified package files matched.")
        return plan
    for item in plan:
        if item["action"] == "already":
            continue
        label = item["action"].upper()
        detail = f" ({item['reason']})" if item.get("reason") else ""
        print(f"{label:8} {item.get('package') or '?':20} {item['destination']}{detail}")
    already = sum(1 for item in plan if item["action"] == "already")
    if already:
        print(f"Already current: {already} file(s).")
    restorable = [item for item in plan if item["action"] == "restore"]
    if not apply:
        print(f"Dry run: {len(restorable)} file(s) can be restored in place. "
              "Re-run with --apply to write them.")
        return plan
    if not restorable:
        print("Nothing to restore.")
        return plan
    plan_path = KIT / "plans/package-files-restore.json"
    write_json(plan_path, {
        "format": "ubuntu-package-restore-plan-v1", "created_at": timestamp(),
        "snapshot": str(source), "force": bool(force),
        "records": [{key: (str(value) if key in ("source", "destination") else value)
                     for key, value in item.items() if key != "action"}
                    for item in restorable],
    })
    command = ["sudo", "/usr/bin/python3", "-I", str(SCRIPT_PATH), "--workspace", str(KIT),
               "_restore-package-files-worker", "--plan", str(plan_path)]
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise ConfigError("Package-file restore worker failed; inspect the result file.")
    return plan


def _dpkg_owner(path):
    result = subprocess.run(["/usr/bin/dpkg-query", "-S", str(path)],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        return set()
    owners = set()
    for line in result.stdout.splitlines():
        if ": " not in line:
            continue
        for name in line.split(": ", 1)[0].split(","):
            owners.add(name.strip().partition(":")[0])
    return owners


def restore_package_files_worker(plan_path, workspace):
    """Root-only worker: restore reviewed package files with rollback copies."""
    if os.geteuid() != 0:
        raise ConfigError("The package-file restore worker must run as root.")
    workspace = Path(workspace)
    if workspace.is_symlink() or not workspace.is_dir():
        raise ConfigError("Invalid workspace for package-file restore.")
    owner_uid, owner_gid = workspace.stat().st_uid, workspace.stat().st_gid
    if owner_uid == 0:
        raise ConfigError("Refusing a root-owned workspace.")
    plan_path = Path(plan_path).resolve()
    plans_dir = (workspace / "plans").resolve()
    if plan_path.parent != plans_dir or plan_path.is_symlink() or not plan_path.is_file():
        raise ConfigError("Refusing an unmanaged restore plan.")
    try:
        plan = json.loads(plan_path.read_text())
    except (OSError, ValueError) as error:
        raise ConfigError(f"Unreadable restore plan: {error}") from error
    if plan.get("format") != "ubuntu-package-restore-plan-v1":
        raise ConfigError("Unrecognized package-file restore plan.")
    records = plan.get("records", [])
    if not isinstance(records, list):
        raise ConfigError("Invalid package-file restore records.")
    source_root = Path(plan.get("snapshot", "")).expanduser()
    if not source_root.is_absolute() or not source_root.is_dir():
        raise ConfigError("Invalid snapshot path in the restore plan.")
    rollback_root = workspace / "rollback" / ("package-files-" + timestamp())
    restored, skipped, failed = [], [], []
    for record in records:
        if not isinstance(record, dict):
            failed.append({"reason": "invalid record"}); continue
        package = record.get("package")
        destination = Path(record.get("destination", ""))
        source = Path(record.get("source", ""))
        sha256 = record.get("sha256")
        try:
            if not destination.is_absolute() or ".." in destination.parts:
                raise ConfigError("unsafe destination path")
            if not package or package not in _dpkg_owner(destination):
                raise ConfigError("destination is no longer package-owned")
            source_dir = (source_root / "inventory/package-files").absolute()
            if not str(source.absolute()).startswith(str(source_dir) + "/"):
                raise ConfigError("source is outside the snapshot")
            if not sha256 or not source.is_file() or digest(source) != sha256:
                raise ConfigError("source content is missing or corrupt")
            try:
                info = destination.lstat()
            except FileNotFoundError:
                info = None
            if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
                raise ConfigError("destination is not a regular file")
            current_sha = digest(destination) if info is not None else None
            if current_sha != record.get("current_sha"):
                raise ConfigError("destination changed after review; re-run the restore")
            if info is not None:
                rollback = rollback_root / destination.relative_to("/")
                mkdir(rollback.parent)
                current_xattrs, _ = read_xattrs(destination)
                shutil.copy2(destination, rollback)
                os.chown(rollback, owner_uid, owner_gid)
                apply_xattrs(rollback, current_xattrs)
            with tempfile.NamedTemporaryFile(dir=destination.parent,
                                             prefix=".package-restore-", delete=False) as output:
                temporary = Path(output.name)
                try:
                    with source.open("rb") as stream:
                        shutil.copyfileobj(stream, output, length=1024 * 1024)
                    output.flush()
                    os.fchmod(output.fileno(), int(record.get("mode") or 0o644) & 0o7777)
                    os.fchown(output.fileno(), int(record.get("uid", 0)),
                              int(record.get("gid", 0)))
                    os.fsync(output.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            if digest(destination) != sha256:
                raise ConfigError("restored content failed verification")
            xattr_errors = apply_xattrs(destination, record.get("xattrs"))
            if xattr_errors:
                print(f"Warning: xattrs not fully restored for {destination}: "
                      + "; ".join(xattr_errors), file=sys.stderr)
        except (ConfigError, OSError, ValueError) as error:
            failed.append({"package": package, "path": str(destination),
                           "reason": str(error)})
        else:
            restored.append({"package": package, "path": str(destination),
                             "sha256": sha256, "xattr_errors": xattr_errors})
    for directory, _, _ in os.walk(rollback_root, topdown=False):
        try:
            os.chown(directory, owner_uid, owner_gid)
        except OSError:
            pass
    result = {"format": "ubuntu-package-restore-result-v1", "created_at": timestamp(),
              "rollback": str(rollback_root) if rollback_root.exists() else None,
              "restored": restored, "skipped": skipped, "failed": failed,
              "note": "File capabilities, ACLs and xattrs are not restored."}
    result_path = workspace / "plans/package-files-restore-result.json"
    write_json(result_path, result)
    os.chown(result_path, owner_uid, owner_gid)
    print(f"Package files restored: {len(restored)}; failed: {len(failed)}.")
    if result["rollback"]:
        print("Rollback copies: " + result["rollback"])
    for item in failed:
        print(f"FAILED {item.get('package') or '?'} {item.get('path')}: {item.get('reason')}",
              file=sys.stderr)
    if failed:
        raise ConfigError(f"{len(failed)} package file(s) failed to restore.")


def capture_package_files_worker(workspace):
    """Root-only worker: scan every package file and cache modified contents."""
    if os.geteuid() != 0:
        raise ConfigError("The package-file capture worker must run as root.")
    workspace = Path(workspace)
    if workspace.is_symlink() or not workspace.is_dir():
        raise ConfigError("Invalid workspace for package-file capture.")
    owner_uid, owner_gid = workspace.stat().st_uid, workspace.stat().st_gid
    if owner_uid == 0:
        raise ConfigError("Refusing a root-owned workspace.")
    staging = workspace / (".privileged-package-scan-" + uuid.uuid4().hex)
    cache = workspace / "privileged-package-scan"
    report = {"warnings": []}
    try:
        manifest = package_integrity(staging, report)
        write_json(staging / "package-conffiles.json", conffile_drift())
        for warning in report["warnings"]:
            print("Warning: " + warning, file=sys.stderr)
        for directory, _, files in os.walk(staging):
            os.chown(directory, owner_uid, owner_gid)
            for name in files:
                os.chown(Path(directory) / name, owner_uid, owner_gid)
        previous = workspace / (".privileged-package-scan-old-" + uuid.uuid4().hex)
        if cache.exists():
            if cache.is_symlink() or not cache.is_dir():
                raise ConfigError("Refusing an invalid package-scan cache.")
            cache.rename(previous)
        published = False
        try:
            staging.rename(cache)
            published = True
        except BaseException:
            if previous.exists() and not cache.exists():
                previous.rename(cache)
            raise
        finally:
            if published:
                shutil.rmtree(previous, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    counts = manifest.get("counts", {})
    print(f"Privately scanned {counts.get('total', 0)} package paths; "
          f"captured {counts.get('captured', 0)} modified file(s), "
          f"{manifest.get('captured_bytes', 0)} bytes.")


def merge_privileged_package_scan(destination, report):
    """Merge the last root package scan into the snapshot inventory."""
    cache = KIT / "privileged-package-scan"
    manifest_path = cache / "package-integrity.json"
    if not manifest_path.is_file():
        report["warnings"].append(
            "No privileged package scan cache; root-only package files are not captured. "
            "Run capture-package-files.")
        return False
    try:
        privileged = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as error:
        report["warnings"].append(f"Privileged package scan is unreadable: {error}")
        return False
    if not isinstance(privileged, dict):
        return False
    captured_at = privileged.get("captured_at")
    age = None
    if captured_at:
        try:
            age = round((dt.datetime.now(dt.timezone.utc)
                         - dt.datetime.fromisoformat(captured_at)).total_seconds() / 3600, 1)
        except ValueError:
            age = None
    if age is not None and age > 26:
        report["warnings"].append(
            f"Privileged package scan cache is {age} hours old; run capture-package-files.")
    inventory = destination / "inventory"
    user_path = inventory / "package-integrity.json"
    try:
        user = json.loads(user_path.read_text()) if user_path.is_file() else {}
    except (OSError, ValueError):
        user = {}
    records = user.get("records", []) if isinstance(user, dict) else []
    records = [record for record in records if isinstance(record, dict)]
    privileged_records = [record for record in privileged.get("records", [])
                          if isinstance(record, dict) and record.get("path")]
    privileged_paths = {record["path"] for record in privileged_records}
    if age is None or age <= 26:
        records = [record for record in records
                   if not ("Permission denied" in str(record.get("note", ""))
                           and record.get("path") not in privileged_paths)]
    by_path = {record.get("path"): index for index, record in enumerate(records)}
    content_dir = inventory / "package-files"
    for record in privileged_records:
        if record.get("captured") and record.get("sha256"):
            source = cache / "package-files" / record["sha256"]
            target = content_dir / record["sha256"]
            if source.is_file() and not target.exists():
                write_path(source, target)
        path = record["path"]
        if path not in by_path:
            merged = dict(record, source="privileged-scan")
            by_path[path] = len(records)
            records.append(merged)
        elif not records[by_path[path]].get("captured"):
            merged = dict(records[by_path[path]])
            for key in ("captured", "sha256", "size", "mode", "uid", "gid", "xattrs",
                        "flags", "note", "package", "conffile", "reason", "xattr_warning"):
                if key in record:
                    merged[key] = record[key]
            if record.get("captured"):
                merged["note"] = record.get("note", "")
                merged.pop("reason", None)
            merged["source"] = "privileged-scan"
            records[by_path[path]] = merged
        elif record.get("captured") and record.get("xattrs") is not None:
            existing = records[by_path[path]]
            if existing.get("sha256") == record.get("sha256"):
                merged = dict(existing, xattrs=record["xattrs"],
                              source="privileged-scan")
                merged.pop("xattr_warning", None)
                records[by_path[path]] = merged
    counts = package_integrity_counts(records)
    user.update({"format": "ubuntu-package-integrity-v1", "available": True,
                 "records": records, "counts": counts,
                 "privileged_scan_age_hours": age})
    write_json(user_path, user)
    conf_path = cache / "package-conffiles.json"
    if conf_path.is_file():
        try:
            root_conf = json.loads(conf_path.read_text())
            user_conf_path = inventory / "package-conffiles.json"
            user_conf = json.loads(user_conf_path.read_text()) if user_conf_path.is_file() else {}
            modified = {(item.get("package"), item.get("path")): item
                        for item in user_conf.get("modified", []) if isinstance(item, dict)}
            for item in root_conf.get("modified", []):
                if isinstance(item, dict):
                    modified[(item.get("package"), item.get("path"))] = item
            user_conf["modified"] = sorted(modified.values(),
                                           key=lambda item: (item.get("package") or "", item.get("path") or ""))
            user_conf["modified_packages"] = sorted({item.get("package") for item in user_conf["modified"]
                                                     if item.get("package")})
            user_conf["unreadable"] = root_conf.get("unreadable", [])
            user_conf["missing"] = root_conf.get("missing", [])
            user_conf["obsolete"] = root_conf.get("obsolete", 0)
            user_conf["checked"] = max(user_conf.get("checked", 0), root_conf.get("checked", 0))
            user_conf["source"] = "privileged-scan"
            write_json(user_conf_path, user_conf)
        except (OSError, ValueError, TypeError) as error:
            report["warnings"].append(f"Privileged conffile scan merge failed: {error}")
    delta_path = inventory / "package-delta.json"
    if delta_path.is_file():
        try:
            delta = json.loads(delta_path.read_text())
            delta.setdefault("modified", {})
            delta["modified"].update({
                "package_files": counts["modified_files"],
                "package_file_packages": counts["modified_packages"],
                "package_files_captured": counts["captured"],
            })
            delta.setdefault("counts", {})
            delta["counts"]["modified_package_files"] = counts["modified_files"]
            conf_path = inventory / "package-conffiles.json"
            if conf_path.is_file():
                conf = json.loads(conf_path.read_text())
                delta["modified"]["conffiles"] = len(conf.get("modified", []))
                delta["modified"]["conffile_packages"] = conf.get("modified_packages", [])
                delta["modified"]["conffiles_unreadable"] = len(conf.get("unreadable", []))
                delta["counts"]["modified_conffiles"] = len(conf.get("modified", []))
            write_json(delta_path, delta)
        except (OSError, ValueError) as error:
            report["warnings"].append(f"Package delta merge failed: {error}")
    report["privileged_package_scan"] = {"captured_at": captured_at, "age_hours": age}
    return True


def privileged_package_scan_age():
    """Return the age in hours of the last successful privileged package scan."""
    path = KIT / "privileged-package-scan/package-integrity.json"
    try:
        data = json.loads(path.read_text())
        captured = dt.datetime.fromisoformat(data["captured_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (dt.datetime.now(dt.timezone.utc) - captured).total_seconds() / 3600


def package_scan_summary():
    """Short tray/status summary of the privileged package scan cache."""
    path = KIT / "privileged-package-scan/package-integrity.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return "Package scan: no privileged cache"
    counts = data.get("counts", {}) if isinstance(data, dict) else {}
    age = privileged_package_scan_age()
    age_text = f"{age:.1f}h" if age is not None else "unknown"
    return (f"Package scan: {counts.get('captured', 0)} captured, "
            f"{counts.get('modified_files', 0)} modified, cache {age_text}")


def capture_root_package_scan(non_interactive=False, force=False):
    """Run the privileged package-file scan through sudo."""
    age = privileged_package_scan_age()
    if not force and age is not None and age <= (10 / 60):
        print("Using the privileged package scan from the last 10 minutes.")
        return True
    command = ["sudo"] + (["-n"] if non_interactive else [])
    command += ["/usr/bin/python3", "-I", str(SCRIPT_PATH), "--workspace", str(KIT),
                "_capture-package-files-worker"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    for line in result.stdout.splitlines():
        print(line)
    for line in result.stderr.splitlines():
        print(line, file=sys.stderr)
    if result.returncode:
        print("Warning: privileged package-file scan failed; "
              "run capture-package-files with sudo.", file=sys.stderr)
        return False
    return True


def collect_inventory(destination, config, report):
    mkdir(destination)
    write_json(destination / "hardware.json", hardware_inventory())
    native_arch = capture_command(["dpkg", "--print-architecture"], report).strip()
    commands = {
        "apt-manual.txt": ["apt-mark", "showmanual"],
        "apt-held.txt": ["apt-mark", "showhold"],
        "dpkg-packages.tsv": ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n"],
        "dpkg-selections.txt": ["dpkg", "--get-selections"],
        "apt-policy.txt": ["apt-cache", "policy"],
        "snap-list.txt": ["snap", "list"],
        "flatpak-apps.tsv": ["flatpak", "list", "--app", "--columns=application,origin,branch,installation"],
        "flatpak-remotes.tsv": ["flatpak", "remotes", "--columns=name,url,options"],
        "python-user-packages.txt": ["python3", "-m", "pip", "freeze", "--user"],
        "gnome-extensions-enabled.txt": ["gnome-extensions", "list", "--enabled"],
        "dconf.ini": ["dconf", "dump", "/"],
        "system-services-enabled.txt": ["systemctl", "list-unit-files", "--state=enabled", "--no-legend", "--no-pager"],
        "user-services-enabled.txt": ["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend", "--no-pager"],
        "groups.txt": ["id"],
        "kernel.txt": ["uname", "-a"],
    }
    outputs = {}
    for name, command in commands.items():
        outputs[name] = capture_command(command, report)
        write(destination / name, outputs[name])
    write(destination / "vscode-extensions.txt", vscode_extensions(report))
    write(destination / "user-crontab.txt", capture_command(["crontab", "-l"], report, empty_codes=(1,)))
    installed = {}
    statuses = {}
    for line in outputs["dpkg-packages.tsv"].splitlines():
        columns = line.split("\t")
        if len(columns) == 4:
            statuses[package_name(columns[0])] = columns[3].strip()
            if columns[3][1:2] == "i":
                installed[columns[0]] = columns[1]
    manual = set(outputs["apt-manual.txt"].splitlines())
    installed_names = {package_name(name) for name in installed}
    manual_names = {package_name(name) for name in manual}
    held_names = {package_name(name) for name in outputs["apt-held.txt"].splitlines() if name.strip()}
    portable, review = [], []
    for name in sorted(installed):
        if name not in manual and name.split(":")[0] not in manual:
            continue
        portable_name = name.removesuffix(":" + native_arch) if native_arch else name
        target = review if any(fnmatch.fnmatch(portable_name, pattern) for pattern in config["apt_review_only"]) else portable
        target.append(portable_name)
    write(destination / "apt-portable.txt", "\n".join(portable) + "\n")
    write(destination / "apt-review-only.txt", "\n".join(review) + "\n")
    package_baseline(destination, installed, portable + review, native_arch)
    history_paths = list(Path("/var/log/apt").glob("history.log*"))
    if history_paths:
        history = parse_apt_history(history_paths)
    else:
        history = {"events": 0, "installed": [], "upgraded": [], "removed": [],
                   "purged": [], "removed_all": []}
        report["warnings"].append("APT history is unavailable; removal history is incomplete.")
    pending_removals = set()
    for line in outputs["dpkg-selections.txt"].splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[1] in ("deinstall", "purge"):
            pending_removals.add(package_name(columns[0]))
    removed = set(history["removed_all"]) | pending_removals | {
        name for name, status in statuses.items() if status[:2] in ("rc", "hc")}
    closure, metapackages = distro_package_closure(installed_names, report)
    delta = package_delta(installed_names, manual_names, statuses, closure, metapackages,
                          held_names, history, removed)
    conffiles = conffile_drift()
    write_json(destination / "package-conffiles.json", conffiles)
    if conffiles["unreadable"]:
        report["warnings"].append(
            f"{len(conffiles['unreadable'])} conffiles could not be read; "
            "package modification coverage is partial.")
    integrity = package_integrity(destination, report)
    for warning in integrity.get("warnings", []):
        report["warnings"].append(warning)
    counts = integrity.get("counts", {})
    delta["modified"] = {
        "conffiles": len(conffiles["modified"]),
        "conffile_packages": conffiles["modified_packages"],
        "conffiles_unreadable": len(conffiles["unreadable"]),
        "package_files": counts.get("modified_files", 0),
        "package_file_packages": counts.get("modified_packages", 0),
        "package_files_captured": counts.get("captured", 0),
    }
    delta["counts"]["modified_conffiles"] = len(conffiles["modified"])
    delta["counts"]["modified_package_files"] = counts.get("modified_files", 0)
    held_installed = sorted(held_names & installed_names)
    write(destination / "apt-held-installed.txt",
          "\n".join(held_installed) + ("\n" if held_installed else ""))
    write(destination / "apt-removed.txt",
          "\n".join(sorted(removed)) + ("\n" if removed else ""))
    write_json(destination / "apt-history.json", history)
    write_json(destination / "package-delta.json", delta)
    phantom = len(held_names - installed_names)
    if phantom > 100:
        report["warnings"].append(
            f"{len(held_names)} package holds are recorded but only "
            f"{len(held_names & installed_names)} are installed; audit apt-mark showhold.")
    snaps = []
    for line in outputs["snap-list.txt"].splitlines()[1:]:
        fields = line.split()
        if len(fields) < 6:
            continue
        name, _, _, channel, _, notes = fields[:6]
        if name in ("bare", "snapd", "gtk-common-themes") or re.match(
                r"^(core\d*|gnome-.*|mesa-.*|kf[56]-.*)$", name):
            continue
        channel = channel.removesuffix("/…").removesuffix("/...")
        snaps.append({"name": name, "channel": channel, "classic": "classic" in notes.split(",")})
    write_json(destination / "snap-apps.json", snaps)
    tools = ("docker", "kubectl", "kind", "clusterctl", "helm", "python3", "node", "npm",
             "dotnet", "java", "gcc", "code", "opencode", "tailscale", "wg", "nvidia-smi")
    paths = {tool: shutil.which(tool) for tool in tools}
    for name in ("kind", "clusterctl"):
        if (HOME / name).is_file():
            paths["home/" + name] = str(HOME / name)
    write_json(destination / "tool-paths.json", paths)
    os_info = read_os_release()
    support = release_support(os_info)
    if not support["supported"]:
        report["warnings"].append(support["note"])
    write_json(destination / "source-system.json", {
        "os": os_info, "release_support": support,
        "hostname": socket.gethostname(), "home": str(HOME),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
        "architecture": native_arch,
    })
    if not installed or not manual:
        report["warnings"].append("APT package inventory incomplete; code and configuration capture continues.")


def copy_large_selection(source, destination, info, report, executable):
    """Copy a large configuration file with bounded memory."""
    if info.st_size > MAX_FILE:
        report["warnings"].append(
            f"Skipped oversized file (>{MAX_FILE // (1024 * 1024)} MiB): {source}")
        report.setdefault("oversized_files", []).append(str(source))
        return
    mkdir(destination.parent)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".config-write-", delete=False) as output:
        temporary = Path(output.name)
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                total = 0
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_FILE:
                        temporary.unlink(missing_ok=True)
                        report["warnings"].append(
                            f"Skipped file that grew past the {MAX_FILE // (1024 * 1024)} MiB limit: {source}")
                        report.setdefault("oversized_files", []).append(str(source))
                        return
                    output.write(block)
                after = os.fstat(stream.fileno())
            output.flush()
            os.fchmod(output.fileno(), 0o700 if executable else 0o600)
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    if report["bytes_captured"] + total > MAX_TOTAL:
        temporary.unlink(missing_ok=True)
        raise ConfigError(
            f"Selected configurations exceed the {MAX_TOTAL // (1024 ** 3)} GiB safety limit; "
            "review excluded application data.")
    os.replace(temporary, destination)
    report["bytes_captured"] += total
    report.setdefault("large_files", []).append(str(source))
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        report["warnings"].append(f"Large configuration changed while being read: {source}")


def copy_selection(source, destination, relative, config, report, system=False, skip_kit=False, root_device=None):
    if skip_kit and (source == KIT or KIT in source.parents):
        return
    if any(fnmatch.fnmatch(relative, pattern) for pattern in config["exclude"]):
        return
    if config.get("coverage") == "system-wide-configuration" and any(part.lower() in CONFIG_DATA_DIRS for part in PurePosixPath(relative).parts):
        report.setdefault("excluded_cache_data", []).append(str(source))
        return
    info = None
    try:
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode):
            report["symlinks_not_followed"].append({"path": str(source), "target": os.readlink(source)})
            return
        if stat.S_ISDIR(info.st_mode):
            if root_device is not None and info.st_dev != root_device:
                report.setdefault("mounts_not_crossed", []).append(str(source))
                return
            root_device = info.st_dev if root_device is None else root_device
            for child in sorted(source.iterdir()):
                copy_selection(child, destination / child.name, relative + "/" + child.name,
                               config, report, system, skip_kit, root_device)
            return
        if not stat.S_ISREG(info.st_mode):
            report["warnings"].append(f"Skipped special file: {source}")
            return
        if info.st_size > MAX_FILE:
            report["warnings"].append(
                "Omitted from configuration backup because it exceeds the "
                f"{MAX_FILE // (1024 * 1024)} MiB per-file limit: {source}")
            report.setdefault("oversized_files", []).append(str(source))
            return
        if info.st_size > MAX_IN_MEMORY:
            copy_large_selection(source, destination, info, report,
                                 executable=bool(info.st_mode & 0o111))
            return
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ConfigError(f"Source changed type: {source}")
            data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            report["warnings"].append(
                "Omitted from configuration backup because it grew past the "
                f"{MAX_FILE // (1024 * 1024)} MiB per-file limit: {source}")
            report.setdefault("oversized_files", []).append(str(source))
            return
        report["bytes_captured"] += len(data)
        if report["bytes_captured"] > MAX_TOTAL:
            raise ConfigError("Selected configurations exceed the 1 GiB safety limit; review the cache/data exclusions.")
        write(destination, data, executable=bool(info.st_mode & 0o111))
    except FileNotFoundError:
        return
    except PermissionError as exc:
        if exc.filename is not None and Path(exc.filename) != source:
            raise
        if system:
            report["warnings"].append(f"Root-only system reference not captured: {source}")
            return
        entry = {"path": str(source), "reason": "permission denied"}
        if info is not None:
            entry.update({"kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                          "mode": stat.S_IMODE(info.st_mode),
                          "uid": info.st_uid, "gid": info.st_gid})
        else:
            entry["kind"] = "unknown"
        report.setdefault("unreadable_files", []).append(entry)


CODE_SUFFIXES = {".sh": "shell", ".bash": "shell", ".zsh": "shell", ".dash": "shell",
                 ".py": "python", ".pyw": "python", ".js": "javascript", ".mjs": "javascript",
                 ".cjs": "javascript", ".ts": "typescript", ".c": "c", ".h": "c-header",
                 ".cc": "c++", ".cpp": "c++", ".cxx": "c++", ".hpp": "c++-header",
                 ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby", ".pl": "perl",
                 ".php": "php", ".lua": "lua"}
SHEBANG_KINDS = (("bash", "shell"), ("zsh", "shell"), ("dash", "shell"), ("sh", "shell"),
                 ("python", "python"), ("node", "javascript"), ("ruby", "ruby"),
                 ("perl", "perl"), ("lua", "lua"), ("php", "php"))
DEVELOPMENT_MARKERS = (".git", "Makefile", "makefile", "CMakeLists.txt", "pyproject.toml",
                       "setup.py", "setup.cfg", "package.json", "Cargo.toml", "go.mod",
                       "pom.xml", "build.gradle", "build.gradle.kts")
DEVELOPMENT_SKIP_DIRS = ("node_modules", ".venv", "venv", "env", "target", "build", "dist", "out",
                         ".tox", ".mypy_cache", ".pytest_cache", ".next", ".nuxt", "coverage",
                         ".nyc_output", ".gradle", "toolchain")
HEADER_LIBRARIES = {"X11/": ["-lX11"], "Xrandr": ["-lXrandr"], "Xinerama": ["-lXinerama"],
                    "Xext": ["-lXext"], "Xfixes": ["-lXfixes"], "GL/": ["-lGL"],
                    "pthread.h": ["-lpthread"], "math.h": ["-lm"], "dlfcn.h": ["-ldl"],
                    "zlib.h": ["-lz"], "sqlite3.h": ["-lsqlite3"], "curl/": ["-lcurl"],
                    "openssl/": ["-lssl", "-lcrypto"]}
DEVELOPMENT_FORMATS = ("ubuntu-developments-v1", "ubuntu-developments-v2")
SHELL_KEYWORDS = frozenset("""
if then else elif fi for while until do done case esac function select in time
true false return exit break continue shift set unset export readonly local declare
typeset alias unalias source . eval exec trap wait jobs bg fg echo printf cd pwd
read pushd popd umask ulimit command type hash getopts sudo env nohup nice ionice
timeout xargs
""".split())


def iso_timestamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="seconds")


def shell_commands(text):
    """Command-shaped tokens in shell text; only names present in PATH are kept later."""
    commands = set()
    pattern = (r"(?m)(?:^|[|;&(){}]|\$\(|\n)\s*"
               r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
               r"(?:sudo\s+|env\s+(?:\S+\s+)*)?([A-Za-z0-9_./+-]+)")
    for match in re.finditer(pattern, text):
        token = match.group(1)
        if token in SHELL_KEYWORDS or token.startswith("-") or token.isdigit():
            continue
        commands.add(token)
    return commands


def shebang_commands(data):
    if not data.startswith(b"#!"):
        return set()
    tokens = data.split(b"\n", 1)[0].decode("utf-8", "replace")[2:].split()
    commands = set()
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        name = PurePosixPath(token).name
        if name in ("env", "S"):
            continue
        commands.add(name)
    return commands


def c_includes(text):
    return sorted(set(re.findall(r'(?m)^\s*#\s*include\s*[<"]([^>"]+)[>"]', text)))


def python_modules(text):
    modules = set()
    pattern = r"(?m)^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))"
    for match in re.finditer(pattern, text):
        module = (match.group(1) or match.group(2)).split(".")[0]
        modules.add(module)
    return sorted(modules)


def dpkg_path_aliases(path):
    """usrmerge systems install files under /bin but PATH resolves to /usr/bin."""
    aliases = [str(path)]
    for prefix, alternate in (("/usr/bin/", "/bin/"), ("/bin/", "/usr/bin/"),
                              ("/usr/sbin/", "/sbin/"), ("/sbin/", "/usr/sbin/"),
                              ("/usr/lib/", "/lib/"), ("/lib/", "/usr/lib/"),
                              ("/usr/libexec/", "/libexec/"), ("/libexec/", "/usr/libexec/")):
        if str(path).startswith(prefix):
            candidate = alternate + str(path)[len(prefix):]
            if candidate not in aliases:
                aliases.append(candidate)
    return aliases


def installed_module_paths(module):
    paths = []
    for base in sys.path:
        directory = Path(base or os.getcwd())
        for candidate in (directory / (module + ".py"), directory / module / "__init__.py"):
            if candidate.is_file():
                paths.append(candidate)
    return paths


class ReferenceResolver:
    """Read-only identification of packages and libraries; captured code is never run."""

    def __init__(self, home=None):
        self.home = Path(home) if home else HOME
        self.owners_cache = {}
        self.command_cache = {}
        self.library_cache = None
        self.distributions = None
        self.python_cache = {}
        self.git_cache = {}
        self.arch = None

    def _run(self, command, timeout=30):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None

    def _native_arch(self):
        if self.arch is None:
            result = self._run(["dpkg", "--print-architecture"])
            self.arch = result.stdout.strip() if result and result.returncode == 0 else ""
        return self.arch

    def package_owners(self, paths):
        present = [str(Path(path)) for path in paths if Path(path).is_file()]
        aliases = {}
        for path in present:
            for alias in dpkg_path_aliases(path):
                aliases.setdefault(alias, set()).add(path)
        pending = sorted(alias for alias in aliases if alias not in self.owners_cache)
        if pending:
            result = self._run(["dpkg", "-S", *pending], timeout=60)
            found = {}
            if result:
                for line in result.stdout.splitlines():
                    if ": " not in line or line.startswith("diversion by"):
                        continue
                    package, path = line.split(": ", 1)
                    found[path] = package.split(",")[0].strip()
            arch = self._native_arch()
            for path in pending:
                package = found.get(path)
                if package and arch:
                    package = package.removesuffix(":" + arch)
                self.owners_cache[path] = package
        owners = set()
        for path in present:
            for alias in dpkg_path_aliases(path):
                if self.owners_cache.get(alias):
                    owners.add(self.owners_cache[alias])
        return sorted(owners)

    def command(self, name):
        if name in self.command_cache:
            return self.command_cache[name]
        path = shutil.which(name)
        reference = None
        if path:
            reference = {"name": name, "path": path, "packages": self.package_owners([path])}
        self.command_cache[name] = reference
        return reference

    def header_references(self, headers):
        references = []
        for header in headers:
            path = Path("/usr/include") / header
            references.append({"name": header, "path": str(path),
                               "packages": self.package_owners([path]) if path.is_file() else []})
        return references

    def python(self, module):
        if module in self.python_cache:
            return self.python_cache[module]
        if self.distributions is None:
            try:
                self.distributions = importlib.metadata.packages_distributions()
            except Exception:
                self.distributions = {}
        distributions = []
        for name in self.distributions.get(module, []):
            try:
                version = importlib.metadata.version(name)
            except Exception:
                version = ""
            distributions.append({"name": name, "version": version})
        reference = {"module": module, "distributions": distributions,
                     "packages": self.package_owners(installed_module_paths(module))}
        self.python_cache[module] = reference
        return reference

    def elf_needed(self, path):
        readelf = shutil.which("readelf")
        if not readelf:
            return []
        result = self._run([readelf, "-d", str(path)], timeout=20)
        if not result or result.returncode:
            return []
        return sorted(set(re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", result.stdout)))

    def library_references(self, sonames):
        if self.library_cache is None:
            self.library_cache = {}
            result = self._run(["ldconfig", "-p"], timeout=30)
            if result:
                for line in result.stdout.splitlines():
                    if "=>" not in line:
                        continue
                    left, right = line.split("=>", 1)
                    fields = left.strip().split()
                    if len(fields) >= 2 and fields[1].startswith("("):
                        self.library_cache.setdefault(fields[0], right.strip())
        references = []
        for name in sonames:
            path = self.library_cache.get(name)
            references.append({"name": name, "path": path,
                               "packages": self.package_owners([path]) if path else []})
        return references

    def git(self, source_path):
        source_path = Path(source_path)
        if not source_path.is_file() or not self.home.is_dir():
            return None
        root = None
        for candidate in [source_path.parent, *source_path.parent.parents]:
            if candidate != self.home and self.home not in candidate.parents:
                break
            if (candidate / ".git").exists():
                root = candidate
                break
        if root is None:
            return None
        key = str(root)
        if key in self.git_cache:
            return self.git_cache[key]
        git = shutil.which("git")
        if not git:
            self.git_cache[key] = None
            return None

        def run(*args):
            result = self._run([git, "--no-optional-locks", "-c", "core.fsmonitor=false",
                                "-c", "diff.external=", "-C", str(root), *args], timeout=15)
            return result.stdout.strip() if result and result.returncode == 0 else ""

        self.git_cache[key] = {
            "root": "." if root == self.home else root.relative_to(self.home).as_posix(),
            "commit": run("rev-parse", "HEAD"),
            "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "describe": run("describe", "--tags", "--always", "--dirty"),
        }
        return self.git_cache[key]


def build_references(relative, text, data, kind, dependencies, source_path, resolver):
    """Per-file references: code links, commands, headers, libraries, Python distributions."""
    references = {"code": sorted(dependencies), "commands": [], "packages": [],
                  "libraries": [], "library_packages": [], "headers": [],
                  "header_packages": [], "python": []}
    packages = set()
    commands = set()
    if kind in ("shell", "python"):
        commands.update(shell_commands(text))
    commands.update(shebang_commands(data))
    command_details = []
    for command in sorted(commands):
        detail = resolver.command(command)
        if detail:
            command_details.append(detail)
            packages.update(detail["packages"])
    references["commands"] = command_details
    if kind == "python":
        for module in python_modules(text):
            detail = resolver.python(module)
            references["python"].append(detail)
            packages.update(detail["packages"])
    if kind in ("c", "c++", "c-header", "c++-header"):
        references["headers"] = c_includes(text)
        references["header_packages"] = resolver.header_references(references["headers"])
        for reference in references["header_packages"]:
            packages.update(reference["packages"])
        references["libraries"] = infer_libraries(text)
    if kind == "binary":
        references["libraries"] = resolver.elf_needed(source_path)
        references["library_packages"] = resolver.library_references(references["libraries"])
        for reference in references["library_packages"]:
            packages.update(reference["packages"])
    references["packages"] = sorted(packages)
    return references


def development_exclusions():
    """Build caches and vendored dependency trees are never development sources."""
    result = []
    for name in DEVELOPMENT_SKIP_DIRS:
        result.extend((f"**/{name}", f"**/{name}/*"))
    return result


def detect_development_roots(home, config):
    """Explicit roots plus top-level checkouts identified by build/version markers."""
    roots = list(config.get("development_include", []))
    if config.get("coverage") != "system-wide-configuration":
        return sorted(set(roots))
    try:
        children = sorted(home.iterdir())
    except OSError:
        return sorted(set(roots))
    for path in children:
        if path.is_symlink() or not path.is_dir() or path.name.startswith("."):
            continue
        if path == KIT or KIT in path.parents:
            continue
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in config.get("exclude", [])):
            continue
        try:
            if not any((path / marker).exists() for marker in DEVELOPMENT_MARKERS):
                continue
        except OSError:
            continue
        roots.append(path.name)
    return sorted(set(roots))


def code_kind(relative, data):
    """Classify a captured file by binary signature, shebang, or source suffix."""
    if data.startswith(b"\x7fELF"):
        return "binary"
    if data.startswith(b"#!"):
        tokens = data.split(b"\n", 1)[0].decode("utf-8", "replace")[2:].split()
        for token in tokens[1:]:
            name = PurePosixPath(token).name
            for marker, kind in SHEBANG_KINDS:
                if marker in name:
                    return kind
    suffix = PurePosixPath(relative).suffix.lower()
    return CODE_SUFFIXES.get(suffix)


def text_content(data):
    if data.startswith(b"\x7fELF") or b"\x00" in data[:4096]:
        return ""
    return data.decode("utf-8", "replace")


def infer_libraries(text):
    libraries = []
    for marker, values in HEADER_LIBRARIES.items():
        if marker in text:
            for value in values:
                if value not in libraries:
                    libraries.append(value)
    return libraries


def extract_references(relative, text, captured, executables, kind):
    """Resolve backed-up code dependencies without executing any captured file."""
    dependencies = set()
    directory = PurePosixPath(relative).parent
    if str(directory) == ".":
        directory = PurePosixPath("")
    if not text:
        return dependencies

    def resolve(reference):
        reference = reference.strip().strip("'\"")
        reference = reference.replace("${HOME}", str(HOME)).replace("$HOME", str(HOME))
        if reference.startswith("~"):
            reference = str(HOME) + reference[1:]
        prefix = str(HOME).rstrip("/") + "/"
        if reference.startswith(prefix):
            candidate = reference[len(prefix):]
        elif reference.startswith("/"):
            return None
        else:
            candidate = posixpath.normpath(posixpath.join(str(directory), reference))
        if candidate.startswith("../") or candidate == relative:
            return None
        return candidate if candidate in captured else None

    for match in re.finditer(r"(?:\$\{HOME\}|\$HOME|~)/([A-Za-z0-9_.@%+,\-/]+)", text):
        candidate = resolve("~/" + match.group(1).rstrip(".,:;)]}\"'"))
        if candidate:
            dependencies.add(candidate)
    prefix = str(HOME).rstrip("/") + "/"
    for match in re.finditer(re.escape(prefix) + r"([A-Za-z0-9_.@%+,\-/]+)", text):
        candidate = resolve(prefix + match.group(1).rstrip(".,:;)]}\"'"))
        if candidate:
            dependencies.add(candidate)
    if kind in ("shell", "python"):
        for match in re.finditer(r"(?m)^\s*(?:source|\.)\s+([^\s;]+)", text):
            candidate = resolve(match.group(1))
            if candidate:
                dependencies.add(candidate)
        words = set(re.findall(r"[A-Za-z0-9_.@+-]+", text))
        for candidate in captured:
            if candidate == relative or candidate not in executables:
                continue
            if PurePosixPath(candidate).parent == directory and PurePosixPath(candidate).name in words:
                dependencies.add(candidate)
    if kind == "python":
        by_stem = {}
        for name in captured:
            by_stem.setdefault(PurePosixPath(name).stem, []).append(name)
        for match in re.finditer(r"(?m)^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", text):
            module = match.group(1) or match.group(2)
            stem = module.split(".")[-1]
            for candidate in by_stem.get(stem, []):
                if candidate == relative:
                    continue
                parent = PurePosixPath(candidate).parent
                if parent == directory or (str(directory) not in ("", ".") and
                                           candidate.startswith(str(directory) + "/")):
                    dependencies.add(candidate)
    return dependencies


def topological_order(nodes, edges):
    """Dependency-first order with deterministic lexical tie-breaking."""
    nodes = sorted(set(nodes))
    remaining = {name: set(edges.get(name, ())) & set(nodes) for name in nodes}
    order, cycles = [], []
    while remaining:
        ready = sorted(name for name, dependencies in remaining.items() if not dependencies)
        if not ready:
            stuck = sorted(remaining)
            cycles.append(stuck)
            order.extend(stuck)
            break
        order.extend(ready)
        for name in ready:
            del remaining[name]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return order, cycles


def previous_development_records(previous):
    if not previous:
        return {}, None
    path = Path(previous) / "inventory/developments.json"
    if not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, None
    if data.get("format") not in DEVELOPMENT_FORMATS:
        return {}, None
    records = {item["path"]: item for item in data.get("files", [])
               if isinstance(item, dict) and isinstance(item.get("path"), str)}
    return records, data.get("captured_at")


def capture_developments(destination, previous=None, source_home=None):
    """Inventory backed-up code as time/version-stamped changes with package references."""
    root = destination / "home"
    if not root.is_dir():
        return None
    source_home = Path(source_home) if source_home else HOME
    resolver = ReferenceResolver(source_home)
    captured_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    paths = regular_files(root)
    captured = [path.relative_to(root).as_posix() for path in paths]
    captured_set = set(captured)
    kinds, contents, executables, sizes = {}, {}, set(), {}
    for path in paths:
        name = path.relative_to(root).as_posix()
        info = path.stat()
        sizes[name] = info.st_size
        if info.st_mode & 0o111:
            executables.add(name)
        suffix = PurePosixPath(name).suffix.lower()
        data = b""
        if name in executables or suffix in CODE_SUFFIXES:
            try:
                with path.open("rb") as stream:
                    data = stream.read(2 * 1024 * 1024)
            except OSError:
                data = b""
        contents[name] = data
        kind = code_kind(name, data)
        if kind:
            kinds[name] = kind
    code_names = sorted(kinds)
    previous_files, previous_captured = previous_development_records(previous)
    records, edges, changes = [], {}, []
    for name in code_names:
        staged_path = root / name
        source_path = source_home / name
        text = text_content(contents[name])
        dependencies = extract_references(name, text, captured_set, executables, kinds[name])
        sha256 = digest(staged_path)
        version = "sha256:" + sha256[:12]
        try:
            modified = source_path.stat().st_mtime
        except OSError:
            modified = staged_path.stat().st_mtime
        modified_at = iso_timestamp(modified)
        prior = previous_files.get(name)
        prior_revision = prior.get("revision", 1) if prior else 0
        if not isinstance(prior_revision, int) or prior_revision < 0:
            prior_revision = 0
        if prior is None:
            change, revision = "added", 1
            changed_at = modified_at
        elif prior.get("sha256") != sha256:
            change, revision = "modified", prior_revision + 1
            changed_at = modified_at
        else:
            change, revision = "unchanged", max(prior_revision, 1)
            changed_at = prior.get("changed_at") or modified_at
        references = build_references(name, text, contents[name], kinds[name],
                                      dependencies, staged_path, resolver)
        record = {"path": name, "kind": kinds[name], "executable": name in executables,
                  "size": sizes[name], "sha256": sha256, "version": version,
                  "revision": revision,
                  "modified_at": modified_at, "changed_at": changed_at,
                  "captured_at": captured_at, "change": change,
                  "previous_version": prior.get("version") if prior else None,
                  "previous_revision": prior.get("revision") if prior else None,
                  "depends_on": sorted(dependencies), "references": references}
        git = resolver.git(source_path)
        if git:
            record["git"] = git
        if kinds[name] in ("c", "c++"):
            output = str(PurePosixPath(name).with_suffix(""))
            if kinds.get(output) == "binary":
                record["build"] = {"language": kinds[name], "source": name, "output": output,
                                   "compiler": "c++" if kinds[name] == "c++" else "cc",
                                   "libraries": infer_libraries(text)}
                edges.setdefault(output, set()).add(name)
        if change in ("added", "modified"):
            entry = {"path": name, "change": change, "changed_at": changed_at,
                     "detected_at": captured_at, "version": version,
                     "revision": revision,
                     "previous_version": record["previous_version"],
                     "previous_revision": record["previous_revision"],
                     "references": references}
            if record.get("git"):
                entry["git"] = record["git"]
            changes.append(entry)
        edges.setdefault(name, set()).update(dependencies)
        records.append(record)
    removed = []
    for name in sorted(set(previous_files) - set(code_names)):
        prior = previous_files[name]
        removed.append(name)
        changes.append({"path": name, "change": "removed",
                        "changed_at": prior.get("modified_at") or previous_captured,
                        "detected_at": captured_at, "version": None,
                        "revision": prior.get("revision"),
                        "previous_version": prior.get("version"),
                        "previous_revision": prior.get("revision"),
                        "references": prior.get("references", {})})
    order, cycles = topological_order(code_names, edges)
    packages = sorted({package for record in records
                       for package in record["references"]["packages"]})
    libraries = sorted({library for record in records
                        for library in record["references"]["libraries"]})
    distributions = {}
    for record in records:
        for entry in record["references"]["python"]:
            for distribution in entry["distributions"]:
                distributions[distribution["name"]] = distribution["version"]
    result = {"format": "ubuntu-developments-v2", "captured_at": captured_at,
              "source_home": str(source_home), "previous_capture_at": previous_captured,
              "files": records, "order": order, "cycles": cycles,
              "changes": sorted(changes, key=lambda item: (item.get("changed_at") or "", item["path"])),
              "removed": removed,
              "references": {"packages": packages, "libraries": libraries,
                             "python_distributions": [{"name": name, "version": version}
                                                      for name, version in sorted(distributions.items())]}}
    write_json(destination / "inventory/developments.json", result)
    return result


def load_developments(snapshot):
    path = Path(snapshot) / "inventory/developments.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") not in DEVELOPMENT_FORMATS:
        return None
    return data


def deploy_order(snapshot, paths):
    """Configuration first; backed-up code in dependency order, then leftovers."""
    data = load_developments(snapshot)
    order = data.get("order", []) if data else []
    known = {name: index for index, name in enumerate(order)}
    targets = list(paths)
    configuration = sorted(name for name in targets if name not in known)
    developments = [name for name in order if name in set(targets)]
    unseen = sorted(name for name in targets if name in known and name not in set(developments))
    return configuration + developments + unseen


def reapply_builds(snapshot, changed):
    """Optional source rebuild after files are re-applied; failures never discard backups."""
    data = load_developments(snapshot)
    if not data:
        print("No development build metadata in this snapshot.")
        return
    rebuilt = 0
    for item in data.get("files", []):
        build = item.get("build")
        if not build:
            continue
        source_name, output_name = build.get("source"), build.get("output")
        if not source_name or not output_name:
            continue
        if source_name not in changed and output_name not in changed:
            continue
        compiler = shutil.which(build.get("compiler", "cc"))
        if not compiler:
            print(f"Skipping build (compiler not found): {output_name}")
            continue
        libraries = [value for value in build.get("libraries", [])
                     if re.fullmatch(r"-l[A-Za-z0-9_.+-]+", value)]
        command = [compiler, "-O2", "-o", str(checked_path(HOME, output_name)),
                   str(checked_path(snapshot, "home/" + source_name)), *libraries]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            print(f"Build failed for {output_name}; the captured binary remains in place.")
        else:
            rebuilt += 1
            print(f"Rebuilt development: {output_name}")
    if not rebuilt:
        print("No development artifacts needed rebuilding.")


def package_baseline(destination, installed, names, native_arch):
    """Versions are informational: reapply resolves names and accepts newer packages."""
    def version_for(name):
        if name in installed:
            return installed[name]
        for key, value in installed.items():
            if key.removesuffix(":" + native_arch) == name:
                return value
        return ""
    write_json(destination / "package-baseline.json", {
        "format": "ubuntu-package-baseline-v1",
        "pin_versions": False,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_architecture": native_arch,
        "note": "Versions are informational only. Reapply resolves package names against the new "
                "system and accepts newer versions; missing packages are reported, not forced.",
        "packages": [{"name": name, "version": version_for(name)} for name in sorted(set(names))],
    })


def saved_package_names(source):
    """Read name-based package inventory, falling back to older snapshot layouts."""
    for name in ("apt-portable.txt", "apt-manual.txt"):
        path = Path(source) / "inventory" / name
        if path.is_file():
            return sorted(set(path.read_text().split()))
    path = Path(source) / "inventory/package-baseline.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            return sorted({item["name"] for item in data.get("packages", [])
                           if isinstance(item, dict) and isinstance(item.get("name"), str)})
        except (OSError, ValueError, KeyError):
            return []
    return []


def build_snapshot(destination, config):
    config = dict(config)
    config["exclude"] = sorted(set(config.get("exclude", []) + development_exclusions()))
    if config.get("coverage") == "system-wide-configuration":
        config["home_include"] = sorted(set(config["home_include"] + configuration_home_paths(HOME)))
    # Development checkouts are captured as code, not inferred from installed packages.
    config["home_include"] = sorted(set(config["home_include"] + detect_development_roots(HOME, config)))
    if config.get("coverage") == "system-wide-configuration":
        # Children of a selected directory are already recursive. Avoid duplicate reads/copies.
        config["home_include"] = [name for name in config["home_include"] if not any(
            name.startswith(parent + "/") for parent in config["home_include"] if parent != name and "*" not in parent)]
        config["system_reference_only"] = list(SYSTEM_CONFIG_ROOTS)
    report = {"warnings": [], "unavailable_commands": [], "symlinks_not_followed": [],
              "unreadable_files": [], "bytes_captured": 0}
    collect_inventory(destination / "inventory", config, report)
    merge_privileged_package_scan(destination, report)
    for section, folder in (("home_include", "home"), ("home_reference_only", "reference-home")):
        for pattern in config[section]:
            if not safe_name(pattern) or "**" in pattern:
                raise ConfigError(f"Use relative home patterns without '..' or '**'; directories recurse automatically: {pattern}")
            for source in sorted(HOME.glob(pattern)):
                relative = source.relative_to(HOME).as_posix()
                copy_selection(source, destination / folder / relative, relative, config, report, skip_kit=True)
    for name in config["system_reference_only"]:
        source = Path(name)
        if not source.is_absolute() or ".." in source.parts or not any(source == Path(root) or Path(root) in source.parents for root in SYSTEM_CONFIG_ROOTS):
            raise ConfigError("System references must be below /etc or /usr/local/etc.")
        copy_selection(source, destination / "system" / source.relative_to("/"),
                       source.relative_to("/").as_posix(), config, report, system=True)
    wide_cache = KIT / "privileged-system/system-wide.tar.gz"
    if config.get("coverage") == "system-wide-configuration" and wide_cache.exists():
        cached = verify_archive(wide_cache)
        if cached.get("kind") != "system-wide-configuration-cache":
            raise ConfigError("Unrecognized system-wide cache.")
        captured = dt.datetime.fromisoformat(cached["captured_at"])
        age = (dt.datetime.now(dt.timezone.utc) - captured).total_seconds() / 3600
        with tarfile.open(wide_cache, "r:gz") as archive:
            for record in cached["files"]:
                name = record["path"]
                if not name.startswith(("system/", "accounts/", "home/", "inventory/privileged-")):
                    raise ConfigError("Unexpected privileged-cache path.")
                target = checked_path(destination, name)
                if target.exists():
                    continue  # Live readable configuration is newer than a cached copy.
                with archive.extractfile(name) as stream:
                    write(target, stream.read(), bool(record["executable"]))
        report["system_wide_coverage"] = {"captured_at": cached["captured_at"], "cache_age_hours": round(age, 2),
                                          "accounts": cached.get("accounts", []), "roots": cached.get("roots", []),
                                          "complete": age <= 26 and not cached.get("omissions"), "omissions": cached.get("omissions", [])}
        report["warnings"] = [warning for warning in report["warnings"] if not warning.startswith("Root-only system reference not captured:")]
        report["warnings"].append(f"Privileged configuration cache age: {age:.1f} hours; refresh after root/other-user changes.")
        if age > 26 or cached.get("omissions"):
            report["warnings"].append("SYSTEM-WIDE COVERAGE INCOMPLETE: cache is stale or records omissions.")
    elif config.get("coverage") == "system-wide-configuration":
        report["system_wide_coverage"] = {"complete": False, "reason": "No privileged system-wide capture; other users/root-only files are missing."}
        report["warnings"].append("SYSTEM-WIDE COVERAGE INCOMPLETE: use the confirmed sudo system-wide capture/scheduler.")
    cache_manifest = KIT / "privileged-system/manifest.json"
    if cache_manifest.exists() and not wide_cache.exists():
        cached = json.loads(cache_manifest.read_text())
        if cached["format"] != "ubuntu-configuration-root-reference-v1":
            raise ConfigError("Unrecognized privileged-system cache format.")
        used = []
        allowed = {"etc/sysctl.d/100-k8s.conf", "etc/containerd/config.toml"}
        for record in cached["files"]:
            name = record["path"]
            if name not in allowed:
                raise ConfigError("Unexpected file in privileged-system cache.")
            live = Path("/") / name
            if not any(live == Path(item) or Path(item) in live.parents for item in config["system_reference_only"]):
                continue
            target = destination / "system" / name
            if target.exists():
                continue
            source = checked_path(KIT / "privileged-system", name)
            if source.stat().st_size != record["size"] or digest(source) != record["sha256"]:
                raise ConfigError("Privileged-system cache checksum mismatch; recapture it.")
            copy_selection(source, target, name, config, report)
            used.append(name)
            omission = f"Root-only system reference not captured: /{name}"
            if omission in report["warnings"]:
                report["warnings"].remove(omission)
        report["privileged_reference_cache"] = {"captured_at": cached["captured_at"], "files_used": used}
        if used:
            report["warnings"].append("Using cached root-only references from " + cached["captured_at"] +
                                      "; rerun capture-root-configs.sh after root configuration changes.")
    for name in KIT_FILES:
        source = KIT / name
        if source.exists():
            copy_selection(source, destination / "kit" / name, name, config, report)
    previous = KIT / "snapshot"
    developments = capture_developments(
        destination,
        previous=previous if (previous / "inventory/developments.json").is_file() else None,
        source_home=HOME)
    if developments:
        counts = {}
        for item in developments.get("changes", []):
            counts[item.get("change", "unknown")] = counts.get(item.get("change", "unknown"), 0) + 1
        report["developments"] = {"files": len(developments.get("files", [])),
                                  "changes": counts,
                                  "removed": developments.get("removed", []),
                                  "previous_capture_at": developments.get("previous_capture_at"),
                                  "packages": developments.get("references", {}).get("packages", []),
                                  "libraries": developments.get("references", {}).get("libraries", [])}
    unreadable = sorted(report.get("unreadable_files", []), key=lambda entry: entry["path"])
    report["unreadable_files"] = unreadable
    report["unreadable_count"] = len(unreadable)
    report["coverage_complete"] = not unreadable
    if unreadable:
        report["warnings"].append(
            f"{len(unreadable)} unreadable path(s) recorded in inventory/unreadable-files.json")
    write_json(destination / "inventory/unreadable-files.json", {
        "format": "ubuntu-unreadable-files-v1",
        "captured_at": timestamp(),
        "files": unreadable,
    })
    write_json(destination / "inventory/capture-report.json", report)
    records = []
    for path in regular_files(destination):
        records.append({"path": path.relative_to(destination).as_posix(),
                        "size": path.stat().st_size, "sha256": digest(path),
                        "executable": bool(path.stat().st_mode & 0o111)})
    write_json(destination / "manifest.json", {"format": FORMAT, "created_at": timestamp(), "files": records})
    return report


def validate_manifest(manifest, names):
    try:
        records = manifest["files"]
        paths = [record["path"] for record in records]
        if manifest["format"] != FORMAT or not paths or len(paths) != len(set(paths)):
            raise ValueError
        if not all(safe_name(name) for name in paths) or "manifest.json" in paths:
            raise ValueError
        if set(paths) | {"manifest.json"} != set(names):
            raise ValueError
        if any(str(parent) in paths for name in paths for parent in PurePosixPath(name).parents):
            raise ValueError
        if any(not isinstance(record["size"], int) or not 0 <= record["size"] <= MAX_FILE for record in records):
            raise ValueError
        if sum(record["size"] for record in records) > MAX_TOTAL:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ConfigError("Invalid or unsafe snapshot manifest.") from None
    return records


def verify_tree(directory):
    if directory.is_symlink():
        raise ConfigError("Snapshot directory must not be a symlink.")
    files = regular_files(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    records = validate_manifest(manifest, [path.relative_to(directory).as_posix() for path in files])
    for record in records:
        path = checked_path(directory, record["path"])
        if path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
            raise ConfigError(f"Checksum mismatch: {record['path']}")
    return manifest


def verify_archive(path):
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ConfigError("Archive has duplicate entries or no manifest.")
        if any(not member.isfile() or not safe_name(member.name) or not 0 <= member.size <= MAX_FILE for member in members):
            raise ConfigError("Unsafe archive entries.")
        if sum(member.size for member in members) > MAX_TOTAL + MAX_FILE:
            raise ConfigError("Archive exceeds the safety limit.")
        manifest = json.loads(archive.extractfile("manifest.json").read())
        records = validate_manifest(manifest, names)
        for record in records:
            member = archive.getmember(record["path"])
            if member.size != record["size"]:
                raise ConfigError(f"Size mismatch: {record['path']}")
            value = hashlib.sha256()
            with archive.extractfile(member) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            if value.hexdigest() != record["sha256"]:
                raise ConfigError(f"Checksum mismatch: {record['path']}")
    return manifest


def archives(directory):
    if directory.is_symlink():
        raise ConfigError(f"Archive directory must not be a symlink: {directory}")
    return sorted(path for path in directory.glob("ubuntu-config-*.tar.gz")
                  if ARCHIVE_RE.fullmatch(path.name) and not path.is_symlink() and path.is_file())


def prune(directory, keep):
    for path in archives(directory)[:-keep]:
        path.unlink()


DATED_RE = re.compile(r"ubuntu-config-\d{8}T\d{6}\.\d{6}Z\Z")
RUN_ID_RE = re.compile(r"\d{8}T\d{6}\.\d{6}Z\Z")


def run_id_value(value=None):
    value = value or timestamp()
    if not RUN_ID_RE.fullmatch(value):
        raise ConfigError("Backup run identifiers must be UTC timestamps.")
    return value


def dated_config_name(run_id=None):
    return "ubuntu-config-" + run_id_value(run_id)


def dated_snapshots(directory):
    if directory.is_symlink():
        raise ConfigError("Backup directory must not be a symlink.")
    if not directory.exists():
        return []
    return sorted(path for path in directory.iterdir()
                  if DATED_RE.fullmatch(path.name) and not path.is_symlink() and path.is_dir())


def create_dated_snapshot(stage, directory):
    """Independent snapshot tree; share identical, verified bytes with prior dated snapshot."""
    manifest = verify_tree(stage)
    previous = dated_snapshots(directory)
    prior = previous[-1] if previous else None
    prior_records = {}
    if prior:
        prior_manifest = verify_tree(prior)
        prior_records = {(r['sha256'], r['size'], bool(r['executable'])): r['path']
                         for r in prior_manifest['files']}
    destination = Path(tempfile.mkdtemp(prefix=".dated-", dir=directory))
    shared = 0
    try:
        for record in manifest['files']:
            name = record['path']
            target = checked_path(destination, name)
            mkdir(target.parent)
            key = (record['sha256'], record['size'], bool(record['executable']))
            if key in prior_records:
                existing = checked_path(prior, prior_records[key])
                os.link(existing, target, follow_symlinks=False)
                shared += 1
            else:
                source = checked_path(stage, name)
                write_path(source, target, bool(record['executable']))
        write(destination / 'manifest.json', (stage / 'manifest.json').read_bytes())
        verify_tree(destination)
        for parent, _, _ in os.walk(destination, topdown=False):
            fsync_directory(Path(parent))
        result = directory / ('ubuntu-config-' + timestamp())
        if result.exists() or result.is_symlink():
            raise ConfigError('Dated snapshot already exists; refusing overwrite.')
        destination.rename(result)
        fsync_directory(directory)
        return result, shared
    finally:
        if destination.exists():
            shutil.rmtree(destination)


def prune_dated(directory, keep):
    candidates = dated_snapshots(directory)
    # Never delete unfamiliar, incomplete, or modified snapshots.
    for path in candidates[:-keep]:
        try:
            verify_tree(path)
        except (ConfigError, OSError, ValueError, KeyError, TypeError) as exc:
            print(f'Warning: preserving unverified old snapshot {path}: {exc}', file=sys.stderr)
            continue
        shutil.rmtree(path)
    fsync_directory(directory)


def archive_snapshot(snapshot, directory):
    """Portable archive used for an explicitly configured external backup."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.ubuntu-export-', delete=False) as output:
            temporary = Path(output.name)
            with tarfile.open(fileobj=output, mode='w:gz') as archive:
                for path in regular_files(snapshot):
                    info = tarfile.TarInfo(path.relative_to(snapshot).as_posix())
                    info.size, info.mode, info.mtime = path.stat().st_size, stat.S_IMODE(path.stat().st_mode), int(path.stat().st_mtime)
                    with path.open('rb') as stream:
                        archive.addfile(info, stream)
            output.flush()
            os.fsync(output.fileno())
        verify_archive(temporary)
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


LINKED_STORAGE = "ubuntu-linked-symlink-v1"
HOME_TIMELINE_RE = re.compile(r"home-full-\d{8}T\d{6}\.\d{6}Z\Z")
OBJECT_RE = re.compile(r"[0-9a-f]{64}\Z")


def safe_join(root, name, allow_final_symlink=False):
    """Join a trusted relative name, rejecting every symlinked parent."""
    if not safe_name(name):
        raise ConfigError(f"Unsafe relative path: {name}")
    parts = PurePosixPath(name).parts
    target = root
    for index, part in enumerate(parts):
        target = target / part
        if target.is_symlink() and not (allow_final_symlink and index == len(parts) - 1):
            raise ConfigError(f"Refusing symlink in backup path: {target}")
    return target


def ensure_private_directory(root, *parts):
    """Create nested private directories without following an existing symlink."""
    root = Path(root)
    # /proc/self/fd/N pins an already-open mount even if its mountpoint is removed.
    pinned = root.parent == Path("/proc/self/fd") and root.name.isdigit()
    if not (root.is_dir() and (pinned or not root.is_symlink())):
        raise ConfigError(f"Backup root is not a real directory: {root}")
    current = root
    for part in parts:
        if not safe_name(part):
            raise ConfigError(f"Unsafe directory name: {part}")
        current = current / part
        if current.is_symlink():
            raise ConfigError(f"Refusing symlinked backup directory: {current}")
        if current.exists():
            if not current.is_dir():
                raise ConfigError(f"Not a backup directory: {current}")
        else:
            current.mkdir(mode=0o700)
        if current.stat().st_mode & 0o077:
            current.chmod(0o700)
    return current


def baseline_directories(root, kind):
    """Separate content baselines and timelines for configuration and full /home."""
    baseline = ensure_private_directory(root, "baseline", kind)
    objects = ensure_private_directory(baseline, "objects")
    incoming = ensure_private_directory(objects, ".incoming")
    timelines = ensure_private_directory(root, "timeline", kind)
    return objects, incoming, timelines


def object_path(objects, sha256):
    if not OBJECT_RE.fullmatch(sha256):
        raise ConfigError("Invalid baseline object checksum.")
    return objects / sha256[:2] / sha256


def publish_object(objects, incoming, temporary, sha256, size, created, sync=True):
    """Atomically publish one immutable, content-addressed baseline file."""
    if temporary.stat().st_size != size:
        raise ConfigError("Baseline object size changed before publication.")
    destination = object_path(objects, sha256)
    ensure_private_directory(objects, sha256[:2])
    if destination.is_symlink():
        raise ConfigError("Refusing symlinked baseline object.")
    if destination.exists():
        if not destination.is_file() or destination.stat().st_size != size:
            raise ConfigError("Existing baseline object has the wrong size or type.")
        temporary.unlink(missing_ok=True)
        return destination, created
    os.chmod(temporary, 0o400)
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        return destination, created
    finally:
        temporary.unlink(missing_ok=True)
    if sync:
        fsync_directory(destination.parent)
    created.append(destination)
    return destination, created


def copy_to_baseline(objects, incoming, source, sha256, size, created):
    """Store one regular file once, keyed by its verified SHA-256."""
    destination = object_path(objects, sha256)
    if destination.exists() and not destination.is_symlink():
        if destination.is_file() and destination.stat().st_size == size and digest(destination) == sha256:
            return destination, False
        raise ConfigError("Existing baseline object is corrupt; refusing to reuse it.")
    with tempfile.NamedTemporaryFile(dir=incoming, prefix=".object-", delete=False) as output:
        temporary = Path(output.name)
        value = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                output.write(block)
                value.update(block)
        output.flush()
        os.fsync(output.fileno())
    if value.hexdigest() != sha256:
        temporary.unlink(missing_ok=True)
        raise ConfigError(f"Checksum changed while storing baseline object: {source}")
    before = len(created)
    try:
        publish_object(objects, incoming, temporary, sha256, size, created)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, len(created) > before


def symlink_relative(path, destination):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = os.path.relpath(destination, path.parent)
    if path.is_symlink() or path.exists():
        raise ConfigError(f"Timeline path already exists: {path}")
    os.symlink(target, path)


def linked_config_manifest(source_manifest):
    manifest = dict(source_manifest)
    manifest["storage"] = LINKED_STORAGE
    manifest["baseline"] = "baseline/config"
    return manifest


def verify_linked_config(timeline):
    """Verify a configuration timeline made only of manifest + object symlinks."""
    if timeline.is_symlink() or not timeline.is_dir():
        raise ConfigError("External configuration timeline must be a real directory.")
    if (timeline / "manifest.json").is_symlink() or not (timeline / "manifest.json").is_file():
        raise ConfigError("External configuration manifest must be a real file.")
    manifest = json.loads((timeline / "manifest.json").read_text())
    if manifest.get("storage") != LINKED_STORAGE:
        raise ConfigError("Not a linked configuration timeline.")
    records = validate_manifest(manifest, {record["path"] for record in manifest.get("files", [])} | {"manifest.json"})
    baseline = timeline.parent.parent.parent / "baseline/config/objects"
    seen = set()
    hashes = {}
    for record in records:
        name = record["path"]
        link = safe_join(timeline, name, allow_final_symlink=True)
        if not link.is_symlink():
            raise ConfigError(f"External timeline entry is not a symlink: {name}")
        target = (link.parent / os.readlink(link)).resolve()
        expected = object_path(baseline, record["sha256"])
        if target != expected.resolve() or not expected.is_file() or expected.is_symlink():
            raise ConfigError(f"External timeline link does not resolve to its baseline object: {name}")
        if expected.stat().st_size != record["size"]:
            raise ConfigError(f"External baseline size mismatch: {name}")
        sha = record["sha256"]
        if sha not in hashes:
            hashes[sha] = digest(expected)
        if hashes[sha] != sha:
            raise ConfigError(f"External baseline checksum mismatch: {name}")
        seen.add(name)
    for directory, dirs, files in os.walk(timeline, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(timeline).as_posix()
            if relative == "manifest.json":
                continue
            if path.is_dir() and not path.is_symlink():
                if not any(name.startswith(relative + "/") for name in seen):
                    raise ConfigError(f"Unexpected directory in external timeline: {relative}")
                continue
            if relative not in seen:
                raise ConfigError(f"Unexpected file in external timeline: {relative}")
            if not path.is_symlink():
                raise ConfigError(f"Unexpected non-link timeline entry: {relative}")
    return manifest


def create_linked_config_snapshot(snapshot, root, name=None):
    """Store each configuration file once and publish a dated symlink tree."""
    name = name or snapshot.name
    if not DATED_RE.fullmatch(name):
        raise ConfigError("External configuration snapshot needs a dated snapshot name.")
    manifest = verify_tree(snapshot)
    objects, incoming, timelines = baseline_directories(root, "config")
    temporary = Path(tempfile.mkdtemp(prefix=".config-timeline-", dir=timelines))
    created = []
    shared = 0
    try:
        for record in manifest["files"]:
            source = checked_path(snapshot, record["path"])
            destination, new = copy_to_baseline(objects, incoming, source, record["sha256"], record["size"], created)
            shared += not new
            symlink_relative(temporary / record["path"], destination)
        linked = linked_config_manifest(manifest)
        write_json(temporary / "manifest.json", linked)
        verify_linked_config(temporary)
        result = timelines / name
        if result.exists() or result.is_symlink():
            raise ConfigError("External configuration timeline already exists; refusing overwrite.")
        temporary.rename(result)
        fsync_directory(timelines)
        return result, len(created), shared
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def referenced_config_objects(timeline):
    manifest = json.loads((timeline / "manifest.json").read_text())
    if manifest.get("storage") != LINKED_STORAGE:
        raise ConfigError("Unrecognized external configuration timeline.")
    return {record["sha256"] for record in manifest["files"]}


def prune_linked_config(root, keep):
    """Prune only verified dated timelines; never delete objects still referenced."""
    objects, _, timelines = baseline_directories(root, "config")
    candidates = sorted(path for path in timelines.iterdir()
                        if DATED_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink())
    keep_set = set(candidates[-keep:])
    removed = 0
    for path in candidates:
        if path in keep_set:
            continue
        try:
            verify_linked_config(path)
        except (ConfigError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            print(f"Warning: preserving unverified external timeline {path}: {exc}", file=sys.stderr)
            continue
        shutil.rmtree(path)
        removed += 1
    fsync_directory(timelines)
    remaining = sorted(path for path in timelines.iterdir()
                       if DATED_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink())
    if not remaining:
        return removed
    referenced = set()
    try:
        for path in remaining:
            referenced.update(referenced_config_objects(path))
    except (ConfigError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Warning: preserving all baseline objects because a timeline is unreadable: {exc}", file=sys.stderr)
        return removed
    for prefix in objects.iterdir():
        if not prefix.is_dir() or prefix.is_symlink() or prefix.name == ".incoming":
            continue
        for path in prefix.iterdir():
            if path.is_file() and not path.is_symlink() and OBJECT_RE.fullmatch(path.name) and path.name not in referenced:
                path.unlink()
        try:
            prefix.rmdir()
        except OSError:
            pass
    return removed


def mount_info(path):
    result = subprocess.run(["findmnt", "--json", "--mountpoint", str(path),
                             "--output", "TARGET,UUID,FSTYPE"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ConfigError(f"Not a mounted filesystem: {path}")
    information = json.loads(result.stdout)["filesystems"][0]
    if Path(information["target"]).resolve() != path.resolve() or not information.get("uuid"):
        raise ConfigError("Backup drive must be an actual mount point with a filesystem UUID.")
    if path.stat().st_dev in (HOME.stat().st_dev, Path("/").stat().st_dev):
        raise ConfigError("Backup drive must be on a different filesystem from home and the OS.")
    return information


def attached_drives():
    """Discover attached block filesystems without mounting or modifying them."""
    result = subprocess.run(["lsblk", "--json", "--bytes", "--output",
                             "PATH,TYPE,TRAN,RM,SIZE,FSTYPE,UUID,LABEL,MODEL,MOUNTPOINTS"],
                            capture_output=True, text=True, timeout=15, check=True)
    devices = []
    def visit(nodes, transport=None, removable=False):
        for node in nodes:
            kind = node.get("type")
            if kind in ("loop", "rom"):
                continue
            node_transport = node.get("tran") or transport
            node_removable = bool(node.get("rm")) or removable
            if kind in ("disk", "part", "crypt", "lvm"):
                devices.append({"path": node.get("path"), "type": kind, "transport": node_transport,
                                "removable": node_removable, "size": node.get("size"),
                                "fstype": node.get("fstype"), "uuid": node.get("uuid"),
                                "label": node.get("label"), "model": node.get("model"),
                                "mountpoints": [p for p in (node.get("mountpoints") or []) if p and not p.startswith("[")]})
            visit(node.get("children") or [], node_transport, node_removable)
    # Preserve lsblk discovery order. It does not provide connection timestamps.
    for node in json.loads(result.stdout).get("blockdevices", []):
        visit([node])
    return devices


FSTAB_PATH = Path("/etc/fstab")
MOUNT_OPTIONS = "defaults,nofail,x-systemd.device-timeout=10"
POSIX_FSTYPES = ("ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs")
MOUNT_ROOTS = (Path("/media"), Path("/mnt"))

MOUNT_HELPER = '''#!/usr/bin/python3 -I
"""Root-only one-shot helper: add a boot mount entry, mount it, hand the tree to the user."""
import os
import subprocess
import sys
from pathlib import Path

COMMENT = "# Managed by ubuntu-backup v1"
FSTAB = Path("/etc/fstab")
uuid_value, fstype, target, options, ownership, recursive = sys.argv[1:7]
mount_point = Path(target)
owner_uid, owner_gid = (int(part) for part in ownership.split(":"))

if os.geteuid() != 0:
    raise SystemExit("This helper only runs as root.")
if not uuid_value.replace("-", "").isalnum() or not mount_point.is_absolute():
    raise SystemExit("Invalid mount request.")

original = FSTAB.read_bytes()
entry = f"UUID={uuid_value} {mount_point} {fstype} {options} 0 2\\n"
lines = original.decode().splitlines(keepends=True)
kept = []
pending_comment = None
current_seen = False
changed = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("#"):
        pending_comment = line
        kept.append(line)
        continue
    if not stripped:
        pending_comment = None
        kept.append(line)
        continue
    fields = stripped.split("#", 1)[0].split()
    managed = pending_comment is not None and COMMENT in pending_comment
    pending_comment = None
    if len(fields) >= 4 and fields[1] == str(mount_point) and fields[0].startswith("UUID="):
        if fields[0] == f"UUID={uuid_value}" and not current_seen:
            current_seen = True
            kept.append(line)
            continue
        if managed:
            if kept and kept[-1].strip().startswith("#") and COMMENT in kept[-1]:
                kept.pop()
            changed = True
            continue
    kept.append(line)
if not current_seen:
    if kept and not kept[-1].endswith("\\n"):
        kept[-1] += "\\n"
    kept += ["\\n", COMMENT + " (backup drive boot mount)\\n", entry]
    changed = True
if changed:
    temporary = FSTAB.with_name(".fstab.ubuntu-backup-" + str(os.getpid()))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w") as output:
            output.write("".join(kept))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, FSTAB)
    finally:
        temporary.unlink(missing_ok=True)

mount_point.mkdir(parents=True, exist_ok=True)
result = subprocess.run(["/usr/bin/mount", str(mount_point)], capture_output=True, text=True)
if result.returncode:
    FSTAB.write_bytes(original)
    raise SystemExit(result.stderr.strip() or "mount failed")
if recursive == "1":
    subprocess.run(["/usr/bin/chown", "-R", "--", f"{owner_uid}:{owner_gid}", str(mount_point)], check=False)
else:
    subprocess.run(["/usr/bin/chown", "--", f"{owner_uid}:{owner_gid}", str(mount_point)], check=False)
print("mounted " + str(mount_point))
'''


def fstab_entries():
    """All /etc/fstab entries with their UUID and whether we manage the line; never writes."""
    try:
        lines = FSTAB_PATH.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    entries, comment = [], ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            comment = stripped
            continue
        if not stripped:
            comment = ""
            continue
        fields = stripped.split("#", 1)[0].split()
        if len(fields) < 4:
            comment = ""
            continue
        source, target, fstype, options = fields[:4]
        if source.startswith("UUID="):
            filesystem_uuid = source[5:]
        elif source.startswith("/dev/disk/by-uuid/"):
            filesystem_uuid = source.rsplit("/", 1)[-1]
        else:
            filesystem_uuid = None
        entries.append({"source": source, "uuid": filesystem_uuid, "mount_point": target,
                        "fstype": fstype, "options": options,
                        "managed": "ubuntu-backup" in comment})
        comment = ""
    return entries


def fstab_entry(filesystem_uuid):
    return next((entry for entry in fstab_entries() if entry["uuid"] == filesystem_uuid), None)


def system_disk():
    """Device that holds the running system, including its partition table parent."""
    result = subprocess.run(["findmnt", "-no", "SOURCE", "/"], capture_output=True, text=True, check=False)
    source = result.stdout.strip().splitlines()
    if not source or not source[0].startswith("/dev/"):
        return None
    return parent_disk(source[0])


def parent_disk(path):
    result = subprocess.run(["lsblk", "-no", "PKNAME", str(path)], capture_output=True, text=True, check=False)
    parent = result.stdout.strip().splitlines()
    return "/dev/" + parent[0].strip() if parent and parent[0].strip() else str(path)


def default_mount_point(device):
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", str(device.get("label") or "")).strip(".-").lower()
    label = label.replace("_", "-")
    name = label or (device.get("uuid") or "")[:8].lower() or "drive"
    return Path("/media") / pwd.getpwuid(os.geteuid()).pw_name / name


def mount_plan(device, mount_point=None):
    """Validate a rootless boot mount for one unmounted filesystem; changes nothing."""
    filesystem_uuid, fstype = device.get("uuid"), device.get("fstype")
    if not filesystem_uuid or not fstype:
        raise ConfigError("This device has no filesystem; prepare one first. Nothing was formatted.")
    if fstype not in POSIX_FSTYPES:
        raise ConfigError(f"{fstype} cannot store the backup safely; only Linux filesystems are supported.")
    disk = system_disk()
    if not disk or parent_disk(device.get("path") or "") == disk:
        raise ConfigError("Refusing to mount the disk that holds the running system.")
    if mount_point:
        target = Path(mount_point).expanduser()
        if not target.is_absolute():
            raise ConfigError("Mount point must be an absolute path.")
        target = target.resolve()
    else:
        target = default_mount_point(device)
    if not any(target == base or base in target.parents for base in MOUNT_ROOTS):
        raise ConfigError("Mount point must be under /media or /mnt.")
    existing = fstab_entry(filesystem_uuid)
    if existing and Path(existing["mount_point"]) != target:
        raise ConfigError(f"This filesystem already has an fstab entry at {existing['mount_point']}; "
                          "choose that path or edit fstab first.")
    if target.exists():
        try:
            occupied = any(target.iterdir())
        except OSError:
            raise ConfigError(f"Cannot verify that {target} is empty; choose a new path.") from None
        if occupied:
            raise ConfigError(f"Mount point {target} is not empty; choose an empty directory.")
    return {"uuid": filesystem_uuid, "fstype": fstype, "mount_point": target,
            "options": MOUNT_OPTIONS, "existing": existing is not None}


def mount_drive(device, mount_point=None, recursive=False, assume_yes=False):
    """Mount a prepared Linux filesystem at boot and give it to the current user."""
    plan = mount_plan(device, mount_point)
    if not sys.stdin.isatty():
        raise ConfigError("Mounting needs one sudo prompt; run './ubuntu-backup.py choose-drive' in a terminal.")
    print(f"Mount point: {plan['mount_point']}")
    print(f"Filesystem:  {plan['fstype']} (UUID {plan['uuid']})")
    print("fstab:       " + ("already configured; mounting now" if plan["existing"]
                             else "add a nofail boot entry owned by you"))
    if not assume_yes and not confirm("Mount this filesystem and make it writable by this account?"):
        raise ConfigError("Mount cancelled; nothing was changed.")
    with tempfile.NamedTemporaryFile("w", dir=KIT, prefix=".mount-drive-", suffix=".py", delete=False) as helper:
        helper.write(MOUNT_HELPER)
        helper_path = Path(helper.name)
    helper_path.chmod(0o600)
    try:
        result = subprocess.run(["sudo", "/usr/bin/python3", "-I", str(helper_path),
                                 plan["uuid"], plan["fstype"], str(plan["mount_point"]),
                                 plan["options"], f"{os.geteuid()}:{os.getegid()}",
                                 "1" if recursive else "0"],
                                capture_output=True, text=True, timeout=900)
    finally:
        helper_path.unlink(missing_ok=True)
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            print(line)
    if result.returncode:
        raise ConfigError("Mount failed; /etc/fstab was left unchanged.")
    information = None
    for attempt in range(10):
        try:
            information = mount_info(plan["mount_point"])
            break
        except ConfigError:
            if attempt == 9:
                raise
            time.sleep(0.2)
    if information["uuid"] != plan["uuid"]:
        raise ConfigError("Mounted filesystem does not match the selected UUID.")
    if plan["mount_point"].stat().st_uid != os.geteuid():
        print("Warning: the drive is mounted but not owned by this account; writes may be refused.")
    return str(plan["mount_point"])


def _stop_mount_retry():
    """Stop polling once the drive is mounted; the timer re-arms at next login."""
    subprocess.run(["/usr/bin/systemctl", "--user", "stop", "ubuntu-backup-mount.timer"], check=False)


def mount_saved(check=False, quiet=False):
    """Mount what /etc/fstab declares, then verify the on-disk marker.

    Nothing about the drive is read from settings: the UUID lives in fstab and the
    identity lives in the marker file. Returns "mounted", "unset", "absent",
    "failed" or (check only) "present"."""
    if not settings().get("external_backup"):
        if not quiet:
            print("No backup drive configured; nothing to mount.")
        return "unset"
    mounted = mounted_backup_drives()
    if mounted:
        if not check:
            _stop_mount_retry()
        if not quiet:
            print("Mounted: " + str(mounted[0]["directory"]))
        return "mounted"
    def mount_by_label():
        devices = attached_drives()
        candidates = [
            device for device in devices
            if (device.get("label") or "").lower() == "ubuntu-backup"
            and device.get("fstype") == "ext4"
            and not device.get("mountpoints")
        ]
        if not candidates:
            return None
        if check:
            print("Backup-labelled filesystem is attached but not mounted: "
                  + ", ".join(device["path"] for device in candidates))
            return "present"
        if not shutil.which("udisksctl"):
            return "failed"
        for device in candidates:
            subprocess.run(["udisksctl", "mount", "-b", device["path"], "--no-user-interaction"],
                           capture_output=True, text=True, check=False)
            for _attempt in range(6):
                mounted = mounted_backup_drives()
                if mounted:
                    _stop_mount_retry()
                    if not quiet:
                        print("Mounted: " + str(mounted[0]["directory"]))
                    return "mounted"
                time.sleep(0.25)
        return "failed"

    entries = [entry for entry in fstab_entries() if entry["managed"]]
    if not entries:
        fallback = mount_by_label()
        if fallback is not None:
            return fallback
        if check or not quiet:
            print("No mounted backup marker and no managed boot entry; select the drive from menu 4 to add one.")
        return "absent"
    attached = {device.get("uuid") for device in attached_drives() if device.get("uuid")}
    present = [entry for entry in entries if entry["uuid"] in attached]
    if not present:
        fallback = mount_by_label()
        if fallback is not None:
            return fallback
        if check or not quiet:
            print("The filesystem declared in /etc/fstab is not attached. "
                  "If the drive was replaced or reformatted, select it again from menu 4; nothing was changed.")
        return "absent"
    if check:
        print("Boot entry present but not mounted: " + ", ".join(entry["mount_point"] for entry in present))
        print("Run ./drive.sh mount for the single sudo prompt.")
        return "present"
    subprocess.run(["/usr/bin/systemctl", "--user", "start", "ubuntu-backup-mount.timer"], check=False)
    for entry in present:
        if entry["uuid"] and subprocess.run(["udisksctl", "mount", "-b", f"/dev/disk/by-uuid/{entry['uuid']}",
                                             "--no-user-interaction"],
                                            capture_output=True, text=True, check=False).returncode == 0:
            continue
        result = subprocess.run(["sudo", "-n", "/usr/bin/mount", entry["mount_point"]],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            print(f"Could not mount {entry['mount_point']} without a password"
                  + (f": {detail}" if detail else "") + "; run ./drive.sh mount in a terminal.")
            return "failed"
    mounted = mounted_backup_drives()
    if not mounted:
        print("A mount command ran, but no mounted filesystem carries the backup marker.")
        return "failed"
    _stop_mount_retry()
    if not quiet:
        print("Mounted: " + str(mounted[0]["directory"]))
    return "mounted"


def unmount_saved():
    """Unmount the marked filesystem; the boot entry in /etc/fstab is left alone."""
    if not settings().get("external_backup"):
        print("No backup drive configured; nothing to unmount.")
        return "unset"
    drives = mounted_backup_drives()
    if not drives:
        print("No mounted filesystem carries the backup marker; nothing to unmount.")
        return "absent"
    if len(drives) > 1:
        raise ConfigError("More than one mounted filesystem carries a backup marker ("
                          + ", ".join(str(item["directory"]) for item in drives) + "); unmount them explicitly.")
    directory, filesystem_uuid = drives[0]["directory"], drives[0]["uuid"]
    subprocess.run(["/usr/bin/systemctl", "--user", "stop", "ubuntu-backup-mount.service"], check=False)
    subprocess.run(["/usr/bin/systemctl", "--user", "stop", "ubuntu-backup-mount.timer"], check=False)
    os.sync()
    result = None
    if filesystem_uuid:
        result = subprocess.run(["udisksctl", "unmount", "-b", f"/dev/disk/by-uuid/{filesystem_uuid}"],
                                capture_output=True, text=True, check=False)
    if result is None or result.returncode:
        result = subprocess.run(["sudo", "/usr/bin/umount", str(directory)],
                                capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ConfigError("Could not unmount " + str(directory) + (f": {detail}" if detail else ""))
    print("Unmounted: " + str(directory))
    return "unmounted"



def marker_record(directory):
    """On-disk backup identity. The filesystem owns it; the config never stores it."""
    marker = Path(directory) / ".ubuntu-configuration-target.json"
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        value = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    if value.get("format") != FORMAT or not isinstance(value.get("id"), str) or not value["id"]:
        return None
    return value


def on_source_filesystem(path):
    try:
        device = Path(path).stat().st_dev
    except OSError:
        return True
    return device in (HOME.stat().st_dev, Path("/").stat().st_dev)


def mounted_backup_drives():
    """Mounted filesystems carrying a valid marker; no stored UUID or path is consulted."""
    found, seen = [], set()
    for device in attached_drives():
        for name in device["mountpoints"]:
            directory = Path(name)
            if directory in seen:
                continue
            marker = marker_record(directory)
            if marker is None or on_source_filesystem(directory):
                continue
            seen.add(directory)
            found.append({"directory": directory, "uuid": device.get("uuid"), "marker": marker})
    return found


def external_directory(config):
    """Resolve the backup drive from the on-disk marker; mount paths are never persisted."""
    external = config.get("external_backup")
    if not external:
        raise ConfigError("No external backup drive is configured.")
    drives = mounted_backup_drives()
    if not drives:
        raise ConfigError("No mounted filesystem carries the backup marker; mount the drive first (menu 4).")
    if len(drives) > 1:
        raise ConfigError("More than one mounted filesystem carries a backup marker ("
                          + ", ".join(str(item["directory"]) for item in drives)
                          + "); unmount the extra drives.")
    directory = drives[0]["directory"]
    expected = external.get("marker_id")
    if expected and drives[0]["marker"].get("id") != expected:
        raise ConfigError("The mounted backup drive identity changed; reselect the drive.")
    subdirectory = external.get("subdirectory", "")
    return checked_path(directory, subdirectory) if subdirectory else directory


@contextmanager
def pinned_external(config):
    """Open and verify the selected drive; return its pinned root and display path."""
    directory = external_directory(config)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        pinned = Path("/proc/self/fd") / str(fd)
        if on_source_filesystem(pinned):
            raise ConfigError("Backup drive disappeared; refusing writes onto the source filesystem.")
        if marker_record(pinned) is None:
            raise ConfigError("Pinned backup drive marker is missing or invalid.")
        yield pinned, directory
    finally:
        os.close(fd)


MIN_EXTERNAL_FREE_BYTES = 64 * 1024 * 1024


def validate_external_directory(directory):
    """Reject source filesystems, full targets, and non-POSIX/writable backup directories."""
    directory = Path(directory)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root = Path("/proc/self/fd") / str(fd)
        if on_source_filesystem(root):
            raise ConfigError("Backup drive must be on a different filesystem from home and the OS.")
        if marker_record(root) is None:
            raise ConfigError("Backup marker is missing or invalid.")
        free = shutil.disk_usage(root).free
        if free < MIN_EXTERNAL_FREE_BYTES:
            raise ConfigError(
                f"Backup drive has only {free // (1024 * 1024)} MiB free; "
                f"at least {MIN_EXTERNAL_FREE_BYTES // (1024 * 1024)} MiB is required.")
        probe = Path(tempfile.mkdtemp(prefix=".backup-probe-", dir=root))
        try:
            source = probe / "file"
            source.write_text("probe", encoding="utf-8")
            link = probe / "link"
            link.symlink_to(source.name)
            if link.read_text(encoding="utf-8") != "probe":
                raise ConfigError("Backup drive cannot create valid symlinks.")
        finally:
            shutil.rmtree(probe, ignore_errors=True)
    finally:
        os.close(fd)
    return directory


def copy_external_snapshot(snapshot, config, run_id=None):
    """Publish a dated external configuration timeline with one shared baseline."""
    with pinned_external(config) as (root, directory):
        name = snapshot.name if DATED_RE.fullmatch(snapshot.name) else dated_config_name(run_id)
        result, created, shared = create_linked_config_snapshot(snapshot, root, name)
        prune_linked_config(root, config["keep_snapshots"])
        display = directory / result.relative_to(root)
    print("External backup verified.")
    return display


def copy_external(path, config, filename=None):
    directory = external_directory(config)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    # A pinned directory stays on the selected filesystem even if the drive is unmounted.
    pinned = Path("/proc/self/fd") / str(directory_fd)
    temporary = None
    try:
        if os.fstat(directory_fd).st_dev in (HOME.stat().st_dev, Path("/").stat().st_dev):
            raise ConfigError("Backup drive disappeared; refusing a copy onto the source filesystem.")
        if marker_record(pinned) is None:
            raise ConfigError("Pinned backup directory marker is missing or invalid.")
        with tempfile.NamedTemporaryFile(dir=pinned, prefix=".ubuntu-copy-", delete=False) as output:
            temporary = Path(output.name)
            with path.open("rb") as source:
                shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        verify_archive(temporary)
        output_name = filename or path.name
        if not ARCHIVE_RE.fullmatch(output_name):
            raise ConfigError("Invalid external archive name.")
        os.replace(temporary.name, output_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries if ARCHIVE_RE.fullmatch(entry.name)
                           and entry.is_file(follow_symlinks=False))
        for name in names[:-config["keep_snapshots"]]:
            os.unlink(name, dir_fd=directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(directory_fd)
    print(f"External copy verified: {directory / output_name}")


def _claim_directory(directory):
    """Offer one sudo ownership fix for a mounted but otherwise valid drive root."""
    if os.access(directory, os.W_OK):
        return
    command = f"sudo chown {os.geteuid()}:{os.getegid()} {directory}"
    if not sys.stdin.isatty():
        raise ConfigError("Backup drive is not writable by this account. " + command)
    if not confirm(f"Take ownership of {directory} with sudo?", True):
        raise ConfigError("Backup drive remains read-only; no backup was configured.")
    result = subprocess.run(["sudo", "chown", f"{os.geteuid()}:{os.getegid()}", str(directory)],
                            check=False)
    if result.returncode or not os.access(directory, os.W_OK):
        raise ConfigError("Could not take ownership of the backup drive; nothing was changed.")


def set_external(mount_point, disable=False):
    config = settings()
    if disable:
        config["external_backup"] = None
    else:
        if not mount_point:
            raise ConfigError("Supply the mount point of the drive AFTER you prepare and mount it.")
        mount = Path(mount_point).expanduser().resolve()
        information = mount_info(mount)
        if information["fstype"] in ("vfat", "ntfs", "exfat", "msdos"):
            raise ConfigError("Backup drive must use a POSIX filesystem with symlink support.")
        if on_source_filesystem(mount):
            raise ConfigError("Backup drive must be on a different filesystem from home and the OS.")
        directory = mount
        marker = directory / ".ubuntu-configuration-target.json"
        if directory.exists():
            try:
                entries = list(directory.iterdir())
            except OSError:
                _claim_directory(directory)
                try:
                    entries = list(directory.iterdir())
                except OSError as exc:
                    raise ConfigError(
                        "Cannot inspect the mounted backup directory. "
                        f"Take ownership first: sudo chown {os.geteuid()}:{os.getegid()} {directory} "
                        f"({exc})") from None
            # A freshly formatted ext4 root normally contains only lost+found.
            adoptable = all(entry.name == "lost+found" and entry.is_dir() and not entry.is_symlink()
                            for entry in entries)
            if entries and not marker.is_file() and not adoptable:
                raise ConfigError("Refusing to adopt a nonempty directory without a backup marker. "
                                  "Choose an empty drive or a dedicated empty subdirectory.")
            _claim_directory(directory)
        mkdir(directory)
        _claim_directory(directory)
        if marker.is_symlink():
            raise ConfigError("Drive marker must not be a symlink.")
        marker_existed = marker.exists()
        if marker_existed:
            existing = json.loads(marker.read_text())
            if existing.get("format") != FORMAT or not isinstance(existing.get("id"), str):
                raise ConfigError("Refusing an unrecognized backup drive marker.")
            marker_id = existing["id"]
        else:
            marker_id = str(uuid.uuid4())
            try:
                write_json(marker, {"format": FORMAT, "id": marker_id})
            except OSError as exc:
                raise ConfigError(
                    "Cannot write the backup marker on this drive; no backup was configured. "
                    f"Check ownership/permissions: {exc}") from None
        # Persist only the relative subdirectory and marker identity: the UUID lives
        # in the filesystem superblock, the mount path in /etc/fstab.
        external = {"subdirectory": "", "marker_id": marker_id}
        try:
            validate_external_directory(directory)
            directory.chmod(0o700)
            if directory.stat().st_mode & 0o077:
                raise ConfigError("Drive cannot enforce private Unix permissions. Use an encrypted/Linux filesystem.")
        except Exception:
            if not marker_existed:
                marker.unlink(missing_ok=True)
            raise
        config["external_backup"] = external
    write_json(KIT / "config/settings.json", config)
    print("External backup disabled; existing drive data was not removed." if disable else
          "Backup drive configured at its root. No disk was formatted; the next backup will also copy there.")
    if not disable:
        stale = [entry for entry in fstab_entries()
                 if entry["managed"] and entry["uuid"] and entry["uuid"] != information["uuid"]]
        for entry in stale:
            print(f"Note: a managed boot entry for UUID {entry['uuid']} at {entry['mount_point']} "
                  "remains in /etc/fstab; remove that line if the disk was replaced or reformatted.")


def backup(skip_external=False, package_scan=True):
    """Run one configuration backup, publishing status for timer/CLI runs."""
    global _STATUS_RUN, _EXIT_CODE
    manage_status = not _STATUS_RUN
    if manage_status:
        _STATUS_RUN = True
        _EXIT_CODE = 0
        _diagnose_previous_status()
        _status_history("started", "Configuration backup started")
        _write_status("running", "configuration", "Capturing configuration")
    try:
        result = _backup_impl(skip_external, package_scan=package_scan)
    except KeyboardInterrupt:
        if manage_status:
            message = "Configuration backup cancelled"
            _write_status("cancelled", "cancelled", message)
            _status_history("cancelled", message, at=_LAST_BACKUP_RUN_ID)
        raise
    except Exception as exc:
        if manage_status:
            message = f"Configuration backup failed: {type(exc).__name__}: {exc}"
            _write_status("failed", "failed", message)
            _status_history("failed", message, KIT / "logs/ubuntu-backup.log",
                            at=_LAST_BACKUP_RUN_ID)
        raise
    else:
        if manage_status:
            warnings = _LAST_BACKUP_WARNINGS
            message = "Configuration backup complete"
            if warnings:
                message += f" (warnings): {warnings} unreadable file(s)"
                _EXIT_CODE = 3
            _write_status("complete", "complete", message,
                          result=str(result) if result else None,
                          warnings=bool(warnings), omissions=warnings)
            _status_history("complete", message, result, at=_LAST_BACKUP_RUN_ID)
        return result
    finally:
        if manage_status:
            _STATUS_RUN = False


def _backup_impl(skip_external=False, package_scan=True):
    global _LAST_BACKUP_RUN_ID, _LAST_BACKUP_WARNINGS
    _LAST_BACKUP_RUN_ID = None
    _LAST_BACKUP_WARNINGS = 0
    config = settings()
    run_id = run_id_value()
    _LAST_BACKUP_RUN_ID = run_id
    mkdir(KIT / "backups")
    lock_fd = os.open(KIT / ".backup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if package_scan:
            capture_root_package_scan(non_interactive=True)
        previous = KIT / ".previous-snapshot"
        current = KIT / "snapshot"
        if previous.exists():
            verify_tree(previous)
            if not current.exists():
                previous.rename(current)
            else:
                verify_tree(current)
                shutil.rmtree(previous)
        if current.exists():
            verify_tree(current)  # Never replace an unfamiliar or modified managed tree.
        stage = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=KIT))
        local_path = None
        external_path = None
        try:
            report = build_snapshot(stage, config)
            _LAST_BACKUP_WARNINGS = len(report.get("unreadable_files", []))
            verify_tree(stage)
            keep_local = bool(config.get("keep_local_backups", False))
            external_enabled = not skip_external and config["external_backup"] is not None
            if not keep_local and not external_enabled:
                raise ConfigError("Local backups are disabled and no external drive is selected. "
                                  "Choose a drive or set keep_local_backups to true.")
            shared = None
            if keep_local:
                local_path, shared = create_dated_snapshot(stage, KIT / "backups")
            if current.exists():
                current.rename(previous)
            stage.rename(current)
            fsync_directory(KIT)
            if previous.exists():
                shutil.rmtree(previous)
            if local_path is not None:
                prune_dated(local_path.parent, config["keep_snapshots"])
            print("Configuration backup verified.")
            if report.get("developments"):
                development = report["developments"]
                changes = development.get("changes", {})
                summary = ", ".join(f"{name}={changes[name]}" for name in sorted(changes)) or "none"
                print(f"Developments: {development['files']} code files; changes since "
                      f"{development.get('previous_capture_at') or 'first capture'}: {summary}.")
            if report["warnings"]:
                print(f"Warnings: {len(report['warnings'])} (see capture-report.json)")
            if _LAST_BACKUP_WARNINGS:
                print(f"Unreadable paths: {_LAST_BACKUP_WARNINGS} "
                      "(see inventory/unreadable-files.json)")
            if skip_external:
                print("External copy skipped for this run; saved target was not changed.")
            elif config["external_backup"] is None:
                print("External backup drive: not configured.")
            else:
                try:
                    external_path = copy_external_snapshot(current, config, run_id)
                except (ConfigError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
                    if isinstance(exc, FileNotFoundError):
                        raise ConfigError("Workspace disappeared during backup; no partial backup was published.") from None
                    if local_path is None:
                        raise ConfigError(f"External linked copy failed; no local backup was kept: {exc}") from None
                    raise ConfigError(f"Local backup succeeded, but external linked copy failed: {exc}") from None
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return external_path if external_path is not None else local_path


def verify_path(path):
    """Verify a local snapshot, a portable archive, or a linked external timeline."""
    if path.is_dir() and not path.is_symlink():
        if (path / "manifest.json").is_symlink() or not (path / "manifest.json").is_file():
            raise ConfigError("Snapshot manifest must be a real file.")
        manifest = json.loads((path / "manifest.json").read_text())
        if manifest.get("kind") == "full-home-linked":
            return verify_home_timeline(path)
        if manifest.get("storage") == LINKED_STORAGE:
            return verify_linked_config(path)
        return verify_tree(path)
    return verify_archive(path)


def restore(path=None):
    found = sorted(archives(KIT / "backups") + dated_snapshots(KIT / "backups"))
    if path is None:
        if not found:
            raise ConfigError("No backup archives found.")
        path = found[-1]
    path = Path(path).expanduser()
    manifest = verify_path(path)
    if manifest.get("kind") == "full-home-linked":
        return restore_home(path)
    mkdir(KIT / "restored")
    destination = Path(tempfile.mkdtemp(prefix="ubuntu-config-", dir=KIT / "restored"))
    records = {record["path"]: record for record in manifest["files"]}
    try:
        if path.is_dir() and not path.is_symlink():
            if manifest.get("storage") == LINKED_STORAGE:
                sources = [safe_join(path, record["path"], allow_final_symlink=True)
                           for record in manifest["files"]]
            else:
                sources = regular_files(path)
            for source in sources:
                name = source.relative_to(path).as_posix()
                write_path(source, checked_path(destination, name),
                           executable=records.get(name, {}).get("executable", False))
            if manifest.get("storage") == LINKED_STORAGE:
                write_json(destination / "manifest.json", dict(manifest, storage="local-snapshot"))
        else:
            with tarfile.open(path, "r:gz") as archive:
                for member in archive.getmembers():
                    executable = records.get(member.name, {}).get("executable", False)
                    with archive.extractfile(member) as stream:
                        write_stream(stream, checked_path(destination, member.name),
                                     executable=executable)
        verify_tree(destination)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    print(f"Recovered safely into: {destination}")
    entrypoint = next((name for name in ("kit/ubuntu-backup.py",)
                       if (destination / name).exists()), "kit/deploy.sh")
    print(f"Review/redeploy with: {destination / entrypoint}")
    print("Live settings were not changed.")
    return destination


def default_snapshot():
    if (KIT / "snapshot/manifest.json").is_file():
        return KIT / "snapshot"
    if (KIT.parent / "manifest.json").is_file():
        return KIT.parent  # A toolkit recovered from inside one of our archives.
    raise ConfigError("No snapshot found. Run backup.sh or restore a backup first.")


def apt_status(names):
    """Resolve saved package names against the current system; versions are never pinned."""
    status = {"supported": [], "unavailable": [], "candidates": {}, "installed_now": {}}
    if not names:
        return status
    if not all(PACKAGE_RE.fullmatch(name) for name in names):
        raise ConfigError("Invalid package name in saved inventory.")
    native_arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True,
                                 text=True, check=True).stdout.strip()
    aliases = {name: name.removesuffix(":" + native_arch) for name in names}
    result = subprocess.run(["apt-cache", "policy", *sorted(set(aliases.values()))], capture_output=True,
                            text=True, check=True, env=dict(os.environ, LC_ALL="C.UTF-8"))
    available, candidates, installed_now, package = set(), {}, {}, None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            package = line[:-1]
        elif package and line.strip().startswith("Candidate:"):
            value = line.split(":", 1)[1].strip()
            if value != "(none)":
                available.add(package)
                candidates[package] = value
        elif package and line.strip().startswith("Installed:"):
            value = line.split(":", 1)[1].strip()
            if value != "(none)":
                installed_now[package] = value
    supported = [name for name in names if name in available or aliases[name] in available]
    status["supported"] = supported
    status["unavailable"] = sorted(set(names) - set(supported))
    status["candidates"] = {name: candidates.get(name) or candidates.get(aliases[name])
                            for name in supported}
    status["installed_now"] = {name: installed_now.get(name) or installed_now.get(aliases[name])
                               for name in supported}
    return status


def apt_plan(names):
    status = apt_status(names)
    return status["supported"], status["unavailable"]


def package_drift(snapshot, status):
    """Report saved versions that differ; drift is expected and never blocks reapply."""
    path = Path(snapshot) / "inventory/package-baseline.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    drift = []
    for item in data.get("packages", []):
        if not isinstance(item, dict):
            continue
        name, saved = item.get("name"), item.get("version")
        current = status.get("candidates", {}).get(name)
        if name and saved and current and current != saved:
            drift.append({"package": name, "saved": saved, "candidate": current})
    return drift


def deploy(args):
    source = Path(args.snapshot).expanduser().resolve() if args.snapshot else default_snapshot()
    manifest = verify_tree(source)
    records = [record for record in manifest["files"] if record["path"].startswith("home/")]
    package_names = saved_package_names(source)
    if args.packages:
        try:
            package_status = apt_status(package_names)
        except (ConfigError, OSError, subprocess.SubprocessError) as error:
            print(f"Warning: saved packages could not be resolved ({error}); "
                  "continuing without package reapply.")
            package_status = {"supported": [], "unavailable": package_names,
                              "candidates": {}, "installed_now": {}}
    else:
        package_status = None
    supported = package_status["supported"] if package_status else []
    unavailable = package_status["unavailable"] if package_status else []
    drift = package_drift(source, package_status) if package_status else []
    delta_counts = {}
    delta_path = source / "inventory/package-delta.json"
    if delta_path.is_file():
        try:
            delta_counts = json.loads(delta_path.read_text()).get("counts", {})
        except (OSError, ValueError, AttributeError):
            delta_counts = {}
    developments = load_developments(source) or {}
    changed, unchanged, conflicts = [], [], []
    for record in records:
        relative = record["path"][len("home/"):]
        try:
            target = checked_path(HOME, relative)
        except ConfigError:
            conflicts.append(relative)
            continue
        if target.exists() and (not target.is_file() or digest(target) != record["sha256"]):
            if not args.overwrite or not target.is_file():
                conflicts.append(relative)
                continue
        if target.is_file() and digest(target) == record["sha256"]:
            unchanged.append(relative)
        else:
            changed.append(relative)
    ordered_changed = deploy_order(source, changed)
    plan = {"source": str(source), "home_files_to_write": ordered_changed, "unchanged": unchanged,
            "conflicts_skipped": conflicts, "apt_available": supported, "apt_unavailable": unavailable,
            "package_versions_pinned": False, "package_drift": drift,
            "package_delta_counts": delta_counts,
            "development_cycles": developments.get("cycles", []),
            "development_changes": developments.get("changes", []),
            "development_references": developments.get("references", {}),
            "development_order": [name for name in ordered_changed
                                  if name in {item.get("path") for item in developments.get("files", [])}],
            "system_files_automatically_restored": [], "apply": args.apply,
            "snap_install_requested": args.snaps, "flatpak_install_requested": args.flatpaks,
            "desktop_settings_requested": args.desktop_settings}
    write_json(KIT / "plans/last-deploy.json", plan)
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(changed)} home files to write; "
          f"{len(unchanged)} unchanged; {len(conflicts)} conflicts skipped.")
    if developments:
        print(f"Developments: {len(developments.get('files', []))} code files reapply in dependency order.")
        changes = developments.get("changes", [])
        if changes:
            counts = {}
            for item in changes:
                counts[item.get("change", "unknown")] = counts.get(item.get("change", "unknown"), 0) + 1
            summary = ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
            captured = developments.get("previous_capture_at") or "first capture"
            print(f"Development changes since {captured}: {summary}.")
        references = developments.get("references", {})
        if references.get("packages") or references.get("libraries"):
            print(f"Development references: {len(references.get('packages', []))} packages, "
                  f"{len(references.get('libraries', []))} libraries.")
        if developments.get("cycles"):
            print(f"Warning: {len(developments['cycles'])} development cycle(s); those members apply "
                  "in lexical order after their shared dependencies.")
    if args.packages:
        print(f"APT: {len(supported)} available; {len(unavailable)} unavailable/review required. "
              "Package names are resolved again; versions are not pinned.")
        if drift:
            print(f"APT: {len(drift)} saved version(s) differ from current candidates; newer versions are accepted.")
    if delta_counts:
        print("Packages vs distro baseline: "
              f"{delta_counts.get('added_manual', 0)} added, "
              f"{delta_counts.get('default_manual', 0)} default/manual, "
              f"{delta_counts.get('removed', 0)} removed, "
              f"{delta_counts.get('held_installed', 0)} held, "
              f"{delta_counts.get('added_dependencies', 0)} added dependencies, "
              f"{delta_counts.get('modified_conffiles', 0)} modified conffiles, "
              f"{delta_counts.get('modified_package_files', 0)} modified package files.")
    if args.snaps or args.flatpaks or args.desktop_settings:
        print(f"Optional actions: snaps={args.snaps}, flatpaks={args.flatpaks}, desktop settings={args.desktop_settings}")
    print(f"Full plan: {KIT / 'plans/last-deploy.json'}")
    try:
        source_info = json.loads((source / "inventory/source-system.json").read_text())
        old_home = source_info.get("home") if isinstance(source_info, dict) else None
    except (OSError, ValueError, TypeError):
        old_home = None
    if old_home is None:
        print("Warning: source home is unavailable in this snapshot; review absolute paths in scripts and launchers.")
    elif old_home != str(HOME):
        print(f"Warning: old home was {old_home}; review absolute paths in scripts and launchers.")
    if not args.apply:
        print("Nothing installed or applied. Use --apply; add --overwrite only to replace conflicting files.")
        return
    if args.packages and supported:
        # Newer candidates and missing optional packages must not block code reapply.
        result = subprocess.run(["sudo", "apt-get", "install", "--no-remove", "--no-install-recommends",
                                 "--", *supported], check=False)
        if result.returncode:
            print(f"Warning: APT returned {result.returncode}; continuing with development reapply.")
    if args.snaps:
        snap_path = source / "inventory/snap-apps.json"
        try:
            snap_apps = json.loads(snap_path.read_text())
        except (OSError, ValueError, TypeError):
            print("Warning: snap inventory is unavailable; no snaps installed.")
            snap_apps = []
        if not isinstance(snap_apps, list):
            raise ConfigError("Invalid snap inventory.")
        for app in snap_apps:
            name = app.get("name") if isinstance(app, dict) else None
            channel = app.get("channel") if isinstance(app, dict) else None
            if (not isinstance(name, str) or not isinstance(channel, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
                    or not re.fullmatch(r"[A-Za-z0-9./_-]+", channel)):
                raise ConfigError("Invalid snap inventory.")
            command = ["sudo", "snap", "install", name, "--channel=" + channel]
            if app.get("classic"):
                command.append("--classic")
            subprocess.run(command, check=True)
    if args.flatpaks:
        flatpak_path = source / "inventory/flatpak-apps.tsv"
        try:
            flatpak_lines = flatpak_path.read_text().splitlines()
        except OSError:
            print("Warning: Flatpak inventory is unavailable; no Flatpaks installed.")
            flatpak_lines = []
        remote_cache = {}

        def remotes_for(scope):
            if scope not in remote_cache:
                try:
                    remote_cache[scope] = set(subprocess.run(
                        ["flatpak", "remotes", scope, "--columns=name"],
                        capture_output=True, text=True, check=True).stdout.splitlines())
                except (OSError, subprocess.SubprocessError):
                    remote_cache[scope] = set()
            return remote_cache[scope]

        for line in flatpak_lines:
            columns = line.split("\t")
            if len(columns) < 3:
                continue
            app, remote, branch = columns[:3]
            installation = columns[3] if len(columns) >= 4 and columns[3] in ("user", "system") else "user"
            scope = "--" + installation
            if remote not in remotes_for(scope):
                print(f"Skipping Flatpak app: configure its trusted {installation} remote first.")
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", app) or not re.fullmatch(r"[A-Za-z0-9_.-]+", branch):
                raise ConfigError("Invalid Flatpak inventory.")
            command = ["flatpak", "install", scope, "--noninteractive", "--", remote, app + "//" + branch]
            if installation == "system":
                command = ["sudo"] + command
            subprocess.run(command, check=True)
    rollback = KIT / "rollback" / timestamp()
    development_paths = {item.get("path") for item in developments.get("files", [])}
    for relative in ordered_changed:
        target = checked_path(HOME, relative)
        if target.exists():
            write_path(target, rollback / "home" / relative, bool(target.stat().st_mode & 0o111))
        record = next(item for item in records if item["path"] == "home/" + relative)
        write_path(source / record["path"], target, record["executable"])
        if relative in development_paths:
            print(f"Reapplied development: {relative}")
    if ordered_changed:
        write_json(rollback / "changes.json", {"files_written": ordered_changed, "previous_files": "home/"})
        print(f"Pre-deploy copies of replaced files: {rollback}")
    if args.build:
        reapply_builds(source, set(ordered_changed))
    if args.desktop_settings:
        dconf_path = source / "inventory/dconf.ini"
        if not dconf_path.is_file():
            print("Warning: dconf inventory is unavailable; desktop settings were not imported.")
        else:
            before = subprocess.run(["dconf", "dump", "/"], capture_output=True, check=True).stdout
            write(rollback / "dconf-before.ini", before)
            subprocess.run(["dconf", "load", "/"], input=dconf_path.read_bytes(), check=True)
    print("Deployment complete. System references, service enablement, drivers, repositories and disks were NOT replayed.")


def systemd_units():
    script = str(KIT / "scripts/ubuntu-config.py").replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    service = f"""{MARKER}
[Unit]
Description=Private Ubuntu configuration snapshot

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I "{script}" backup
SuccessExitStatus=3
UMask=0077
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
TimeoutStartSec=20min
"""
    timer = f"""{MARKER}
[Unit]
Description=Daily Ubuntu configuration backup

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
"""
    return service, timer


def install_timer():
    directory = HOME / ".config/systemd/user"
    service, timer = systemd_units()
    for suffix in ("service", "timer"):
        path = directory / (UNIT + "." + suffix)
        if path.is_symlink() or (path.exists() and MARKER not in path.read_text()):
            raise ConfigError(f"Refusing to replace an unmanaged unit: {path}")
    write(directory / (UNIT + ".service"), service)
    write(directory / (UNIT + ".timer"), timer)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", UNIT + ".timer"], check=True)
    print("Daily user backup timer enabled. Missed runs catch up when your user manager next starts.")


EMBEDDED_DEFAULT_SETTINGS = {'coverage': 'system-wide-configuration', 'keep_snapshots': 30, 'exclude_cpus': [2, 8], 'external_backup': None, 'keep_local_backups': False, 'development_include': [], 'home_include': ['.bashrc', '.bash_logout', '.bash_aliases', '.profile', '.inputrc', '.gitconfig', '.Xresources', '.xprofile', '.xsessionrc', '.config/autostart', '.config/systemd/user', '.config/mimeapps.list', '.config/user-dirs.dirs', '.config/user-dirs.locale', '.config/fontconfig', '.config/gtk-3.0', '.config/gtk-4.0', '.config/environment.d', '.config/gnome-shell', '.config/gnome-session', '.config/libadwaita-1', '.config/pipewire', '.config/xdg-desktop-portal', '.local/share/nautilus', '.local/share/gnome-shell', '.config/Code/User/settings.json', '.config/Code/User/keybindings.json', '.config/Code/User/snippets', '.config/sublime-text/Packages/User', '.config/libreoffice/4/user/registrymodifications.xcu', '.local/share/applications', '.local/share/fonts', '.local/share/gnome-shell/extensions', '.local/share/themes', '.local/share/icons', '.fonts', '.themes', '.icons', 'bin', '*.sh', '*.py'], 'home_reference_only': ['.local/bin', '.nvidia-settings-rc', '.config/monitors.xml'], 'system_reference_only': ['/etc/default/keyboard', '/etc/default/locale', '/etc/environment', '/etc/locale.gen', '/etc/timezone', '/etc/apt/sources.list', '/etc/apt/sources.list.d', '/etc/apt/preferences.d', '/etc/apt/keyrings', '/etc/apt/apt.conf.d', '/etc/dconf', '/etc/environment.d', '/etc/netplan', '/etc/ubuntu-advantage', '/etc/xdg', '/etc/sysctl.conf', '/etc/sysctl.d', '/etc/modprobe.d', '/etc/modules-load.d', '/etc/udev/rules.d', '/etc/security/limits.conf', '/etc/security/limits.d', '/etc/systemd/system', '/etc/systemd/journald.conf', '/etc/systemd/journald.conf.d', '/etc/systemd/logind.conf', '/etc/systemd/logind.conf.d', '/etc/docker/daemon.json', '/etc/containerd/config.toml', '/etc/ssh/sshd_config', '/etc/ssh/sshd_config.d', '/etc/cron.d'], 'exclude': ['**/__pycache__', '**/__pycache__/*', '**/*.pyc', '**/.git', '**/.git/*', '.ssh', '.ssh/*', '.gnupg', '.gnupg/*', '.kube', '.kube/*', '.git-credentials', '.config/gh/*', '.docker/config.json', '.config/systemd/user/ubuntu-configuration-backup.*', '.config/systemd/user/timers.target.wants/ubuntu-configuration-backup.timer', 'etc/systemd/system/snap*', 'etc/apt/auth.conf', 'etc/apt/auth.conf.d/*'], 'apt_review_only': ['linux-*', 'grub*', 'shim*', 'nvidia-*', 'libnvidia-*', 'cuda*', 'nsight*', 'firmware-*', 'oem-*', 'efibootmgr', 'os-prober', 'mokutil']}
EMBEDDED_README = 'Ubuntu Backup: self-contained interactive configuration backup and restore.\nNo personal configuration snapshot or backup data is bundled in this repository.\nGenerated snapshots and exports contain secrets and are NOT encrypted.\nOne backup action captures configuration, development code and full /home; identical files are stored once.\nEach development change records its time, content version and package/library references; reapply follows dependency order and never pins package versions.\nFull recovery stages configuration or full /home; live /home is never overwritten.\n'
EMBEDDED_GITIGNORE = '*\n!ubuntu-backup.py\n!README.md\n!.gitignore\n'
EMBEDDED_ROOT_EXPORTER = '#!/usr/bin/python3 -I\n"""System-wide CONFIGURATION capture; root reads, owner writes. Never a disk image."""\n# Managed by ubuntu-configuration system capture v1\nimport argparse\nimport datetime as dt\nimport hashlib\nimport io\nimport json\nimport os\nfrom pathlib import Path\nimport pwd\nimport stat\nimport subprocess\nimport sys\nimport tarfile\nimport tempfile\n\nMARKER = "# Managed by ubuntu-configuration system capture v1"\nUNIT = "ubuntu-configuration-system"\nHELPER = Path("/usr/local/libexec/ubuntu-configuration-system-capture.py")\nROOTS = ("/etc", "/usr/local/etc", "/usr/local/libexec/ubuntu-configuration-system-capture.py")\nMAX_FILE, MAX_TOTAL = 64 * 1024 * 1024, 1024 * 1024 * 1024\nDATA_DIRS = {"cache", "caches", "gpucache", "code cache", "cacheddata", "cachedextensions", "cachedextensionvsixs",\n             "crash reports", "crashpad", "logs", "history", "workspacestorage", "globalstorage",\n             "indexeddb", "local storage", "session storage", "service worker", "blob_storage", "__pycache__"}\nHOME_PATHS = (".config", ".local/share/applications", ".local/share/fonts", ".local/share/themes", ".local/share/icons",\n              ".local/share/gnome-shell/extensions", ".fonts", ".themes", ".icons", ".ssh/config", ".kube/config",\n              ".gnupg/gpg.conf", ".gnupg/gpg-agent.conf", ".docker/config.json", ".aws/config")\nHISTORIES = {".bash_history", ".python_history", ".scala_history", ".dotty_history", ".lesshst", ".wget-hsts",\n             ".sudo_as_admin_successful", ".Xauthority", ".ICEauthority", ".DS_Store"}\n\n\ndef collect(owner_uid, accounts=None, system_roots=ROOTS):\n    """Pinned descriptors, unlimited depth, no symlinks or nested mounts followed."""\n    files, metadata, links, omissions, excluded = {}, {}, [], [], []\n    total = 0\n\n    def scan(parent_fd, name, prefix, device=None):\n        nonlocal total\n        if name.lower() in DATA_DIRS:\n            excluded.append(prefix)\n            return\n        try:\n            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)\n            if stat.S_ISLNK(info.st_mode):\n                links.append({"path": prefix, "target": os.readlink(name, dir_fd=parent_fd)})\n                return\n            if stat.S_ISDIR(info.st_mode):\n                if device is not None and info.st_dev != device:\n                    excluded.append(prefix + " (nested mount)")\n                    return\n                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)\n                try:\n                    with os.scandir(fd) as entries:\n                        names = sorted(entry.name for entry in entries)\n                    for child in names:\n                        scan(fd, child, prefix + "/" + child, info.st_dev)\n                finally:\n                    os.close(fd)\n                return\n            if not stat.S_ISREG(info.st_mode):\n                excluded.append(prefix + " (runtime/special file)")\n                return\n            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)\n            with os.fdopen(fd, "rb") as source:\n                before = os.fstat(source.fileno())\n                if not stat.S_ISREG(before.st_mode):\n                    raise RuntimeError("Configuration changed type: " + prefix)\n                data = source.read(MAX_FILE + 1)\n                after = os.fstat(source.fileno())\n            if len(data) > MAX_FILE:\n                raise RuntimeError("Configuration exceeds 64 MiB; capture aborted: " + prefix)\n            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):\n                raise RuntimeError("Configuration changed during read; retry capture: " + prefix)\n            total += len(data)\n            if total > MAX_TOTAL:\n                raise RuntimeError("Configuration exceeds 1 GiB; review excluded application data before retrying.")\n            files[prefix] = data\n            metadata[prefix] = {"mode": stat.S_IMODE(before.st_mode), "uid": before.st_uid, "gid": before.st_gid}\n        except FileNotFoundError:\n            return\n        except PermissionError:\n            omissions.append(prefix + " (unreadable even with current privileges)")\n\n    for root in system_roots:\n        path = Path(root)\n        try:\n            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n        except FileNotFoundError:\n            continue\n        try:\n            scan(parent, path.name, "system/" + path.as_posix().lstrip("/"))\n        finally:\n            os.close(parent)\n    chosen = []\n    for account in accounts if accounts is not None else pwd.getpwall():\n        if account.pw_uid not in (0, owner_uid) and not (1000 <= account.pw_uid < 65534 and account.pw_shell not in ("/bin/false", "/usr/sbin/nologin", "/sbin/nologin")):\n            continue\n        home = Path(account.pw_dir)\n        if not home.is_dir() or home.is_symlink():\n            omissions.append(f"Account {account.pw_uid}: home missing or symlinked")\n            continue\n        chosen.append({"uid": account.pw_uid, "name": account.pw_name, "home": str(home)})\n        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n        try:\n            prefix = "home" if account.pw_uid == owner_uid else f"accounts/{account.pw_uid}/home"\n            paths = set(HOME_PATHS)\n            with os.scandir(fd) as entries:\n                paths.update(entry.name for entry in entries if entry.name.startswith(".") and entry.is_file(follow_symlinks=False)\n                             and entry.name not in HISTORIES and not entry.name.endswith((".swp", ".swo", "~"))\n                             and not entry.name.startswith(".xsession-errors"))\n            for relative in sorted(paths):\n                parent = os.dup(fd)\n                try:\n                    parts = Path(relative).parts\n                    for part in parts[:-1]:\n                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)\n                        os.close(parent)\n                        parent = child\n                    scan(parent, parts[-1], prefix + "/" + relative)\n                except FileNotFoundError:\n                    pass\n                except OSError as exc:\n                    omissions.append(prefix + "/" + relative + " (ancestor unavailable: " + exc.strerror + ")")\n                finally:\n                    os.close(parent)\n        finally:\n            os.close(fd)\n    files["inventory/privileged-links.json"] = (json.dumps(links, indent=2) + "\\n").encode()\n    files["inventory/privileged-source-metadata.json"] = (json.dumps(metadata, indent=2) + "\\n").encode()\n    files["inventory/privileged-exclusions.json"] = (json.dumps(excluded, indent=2) + "\\n").encode()\n    return files, {"accounts": chosen, "roots": list(system_roots), "omissions": omissions}\n\n\ndef units(owner_uid, kit):\n    def quote(value):\n        value = str(value)\n        if "\\n" in value or "\\r" in value:\n            raise RuntimeError("Newlines are not allowed in a scheduler path.")\n        return \'"\' + value.replace("\\\\", "\\\\\\\\").replace(\'"\', \'\\\\"\').replace("%", "%%") + \'"\'\n    service = f"""{MARKER}\n[Unit]\nDescription=System-wide configuration capture and private user backup\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -I {HELPER} --owner {owner_uid} --workspace {quote(kit)} --run-backup\nUMask=0077\nNice=10\nNoNewPrivileges=true\nProtectSystem=strict\nProtectHome=read-only\nReadWritePaths={quote(kit)}\nSuccessExitStatus=3\nTimeoutStartSec=20min\n"""\n    timer = f"""{MARKER}\n[Unit]\nDescription=Daily system-wide configuration backup\n[Timer]\nOnCalendar=daily\nPersistent=true\nRandomizedDelaySec=10m\n[Install]\nWantedBy=timers.target\n"""\n    return service, timer\n\n\ndef install_scheduler(owner_uid, kit):\n    root = Path("/etc/systemd/system")\n    service, timer = units(owner_uid, kit)\n    targets = [(HELPER, globals().get("SELF_SOURCE") or Path(__file__).read_text(), 0o700),\n               (root / (UNIT + ".service"), service, 0o644), (root / (UNIT + ".timer"), timer, 0o644)]\n    HELPER.parent.mkdir(mode=0o755, parents=True, exist_ok=True)\n    for directory in (HELPER.parent, root):\n        info = directory.lstat()\n        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:\n            raise RuntimeError("Scheduler directories must be protected and root-owned.")\n    for path, _, _ in targets:\n        if path.is_symlink() or (path.exists() and MARKER not in path.read_text()):\n            raise RuntimeError("Refusing unmanaged scheduler file: " + str(path))\n        if path.suffix == ".service" and path.exists() and f"--owner {owner_uid} " not in path.read_text():\n            raise RuntimeError("System-wide scheduler belongs to another account.")\n    for path, text, mode in targets:\n        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".system-config-", delete=False) as output:\n            temporary = Path(output.name)\n            try:\n                output.write(text.encode())\n                os.fchmod(output.fileno(), mode)\n                os.fchown(output.fileno(), 0, 0)\n                output.flush()\n                os.fsync(output.fileno())\n                os.replace(temporary, path)\n            finally:\n                temporary.unlink(missing_ok=True)\n    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)\n    subprocess.run(["/usr/bin/systemctl", "enable", "--now", UNIT + ".timer"], check=True)\n\n\ndef main(install=False, owner_uid=None, workspace=None, run_backup=False, replace_user_timer=False):\n    os.umask(0o077)\n    if os.geteuid() != 0:\n        raise RuntimeError("Run the confirmed sudo capture action, or sudo ./capture-root-configs.sh")\n    identity = owner_uid if owner_uid is not None else os.environ.get("SUDO_UID")\n    if identity is None:\n        raise RuntimeError("A non-root sudo caller or explicit scheduler owner is required.")\n    owner = pwd.getpwuid(int(identity))\n    if owner.pw_uid == 0:\n        raise RuntimeError("A non-root backup owner is required.")\n    kit = Path(workspace).expanduser().absolute() if workspace else Path(__file__).resolve().parents[1]\n    if Path(owner.pw_dir) not in kit.parents or ".." in kit.parts or kit.is_symlink():\n        raise RuntimeError("The private workspace must be inside the backup owner\'s real home directory.")\n    files, details = collect(owner.pw_uid)\n    captured = dt.datetime.now(dt.timezone.utc).isoformat()\n    manifest = {"format": "ubuntu-configuration-v1", "kind": "system-wide-configuration-cache", "created_at": captured,\n                "captured_at": captured, **details,\n                "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),\n                           "executable": False} for name, data in sorted(files.items())]}\n    if sum(len(data) for data in files.values()) > MAX_TOTAL or len(json.dumps(manifest).encode()) > MAX_FILE:\n        raise RuntimeError("Complete configuration/metadata exceeds safety limits; old cache preserved.")\n    if install:\n        install_scheduler(owner.pw_uid, kit)\n    os.setgroups([])\n    os.setgid(owner.pw_gid)\n    os.setuid(owner.pw_uid)\n    os.environ.update(HOME=owner.pw_dir, XDG_RUNTIME_DIR=f"/run/user/{owner.pw_uid}",\n                      DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{owner.pw_uid}/bus")\n    destination = kit / "privileged-system"\n    if destination.is_symlink():\n        raise RuntimeError("Refusing a symlinked capture directory.")\n    destination.mkdir(mode=0o700, exist_ok=True)\n    destination.chmod(0o700)\n    temporary = None\n    try:\n        with tempfile.NamedTemporaryFile(dir=destination, prefix=".system-wide-", delete=False) as output:\n            temporary = Path(output.name)\n            with tarfile.open(fileobj=output, mode="w:gz") as archive:\n                payload = dict(files)\n                payload["manifest.json"] = (json.dumps(manifest, indent=2) + "\\n").encode()\n                for name, data in sorted(payload.items()):\n                    info = tarfile.TarInfo(name)\n                    info.size, info.mode = len(data), 0o600\n                    archive.addfile(info, io.BytesIO(data))\n            output.flush()\n            os.fsync(output.fileno())\n        with tarfile.open(temporary, "r:gz") as archive:\n            for record in manifest["files"]:\n                if hashlib.sha256(archive.extractfile(record["path"]).read()).hexdigest() != record["sha256"]:\n                    raise RuntimeError("Privileged cache verification failed; old cache preserved.")\n        os.replace(temporary, destination / "system-wide.tar.gz")\n        temporary = None\n        fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n        try:\n            os.fsync(fd)\n        finally:\n            os.close(fd)\n    finally:\n        if temporary is not None:\n            temporary.unlink(missing_ok=True)\n    print(f"Privately captured {len(files)} configuration files across {len(details[\'accounts\'])} accounts (root plus local users); omissions: {len(details[\'omissions\'])}.")\n    if replace_user_timer:\n        old = Path(owner.pw_dir) / ".config/systemd/user/ubuntu-configuration-backup.timer"\n        if old.is_file() and not old.is_symlink() and "# Managed by ubuntu-configuration v1" in old.read_text():\n            subprocess.run(["systemctl", "--user", "disable", "--now", "ubuntu-configuration-backup.timer"], check=True)\n    if run_backup or install:\n        entrypoint = Path(owner.pw_dir) / "ubuntu-backup.py"\n        if not entrypoint.is_file():\n            entrypoint = kit / "ubuntu-backup.py"  # Compatibility with older recovered kits.\n        child = subprocess.run(["/usr/bin/python3", "-I", str(entrypoint), "--workspace", str(kit), "--non-interactive", "backup"], check=False)\n        if child.returncode not in (0, 3):\n            raise RuntimeError("Configuration backup exited with status " + str(child.returncode))\n        if child.returncode == 3:\n            raise SystemExit(3)\n    else:\n        print("Now run the normal backup command without sudo. Capture includes sensitive settings; it is NOT encrypted.")\n\n\nif __name__ == "__main__":\n    parser = argparse.ArgumentParser(description=__doc__)\n    parser.add_argument("--owner", type=int)\n    parser.add_argument("--workspace", type=Path)\n    parser.add_argument("--install-timer", action="store_true")\n    parser.add_argument("--replace-user-timer", action="store_true")\n    parser.add_argument("--run-backup", action="store_true")\n    options = parser.parse_args()\n    try:\n        main(options.install_timer, options.owner, options.workspace, options.run_backup, options.replace_user_timer)\n    except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:\n        print(f"Error: {exc}", file=sys.stderr)\n        sys.exit(1)\n'
# This interface is appended to the standalone backend by build-single-file.py.
import base64
import copy


SCRIPT_PATH = Path(__file__).resolve()
SINGLE_FILE_MARKER = "# Ubuntu configuration: self-contained interactive kit"
_original_build_snapshot = build_snapshot
_original_install_timer = install_timer
_original_default_snapshot = default_snapshot


class Cancelled(Exception):
    pass


_GUI_PROMPT_HANDLER = None
_GUI_PROCESS_ACTIVE = False


def ask(prompt, default=None):
    if _GUI_PROMPT_HANDLER is not None:
        return _GUI_PROMPT_HANDLER("ask", prompt, default)
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input(prompt + suffix + ": ").strip()
    except EOFError:
        raise Cancelled("Input ended; cancelled.") from None
    return value if value else (str(default) if default is not None else "")


def confirm(prompt, default=False):
    if _GUI_PROMPT_HANDLER is not None:
        return _GUI_PROMPT_HANDLER("confirm", prompt, default)
    while True:
        value = ask(prompt + (" [Y/n]" if default else " [y/N]")).lower()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("Please answer yes or no.")


def choose(prompt, choices, default=None):
    """Select one labelled answer; choices are (value, label[, detail]) tuples."""
    if _GUI_PROMPT_HANDLER is not None:
        return _GUI_PROMPT_HANDLER("choose", prompt, default, choices)
    for choice in choices:
        value, label = choice[0], choice[1]
        print(f"{value}. {label}")
        if len(choice) > 2 and choice[2]:
            print(f"   {choice[2]}")
    return ask(prompt, default)


def private_workspace(path):
    global KIT
    path = Path(path).expanduser().absolute()
    if path in (HOME, Path("/")):
        raise ConfigError("Choose a dedicated workspace, not your home directory or filesystem root.")
    checked_path(Path("/"), path.relative_to("/").as_posix())
    if path.exists():
        if not path.is_dir() or path.stat().st_uid != os.geteuid():
            raise ConfigError("Workspace must be an owned, real directory.")
        entries = {entry.name for entry in path.iterdir()}
        known = {"backups", "snapshot", ".previous-snapshot", "restored", "rollback",
                 "plans", "profiles", "privileged-system", "privileged-package-scan",
                 "system-integration-recovery", "staging", "config", "logs", "status.json", "history.txt", ".tray.lock",
                 ".maintenance.lock", ".home.lock", ".status.lock", ".home-snapshot"}
        if entries and not (path / "config/settings.json").is_file():
            # A recovered archive's kit/ is also a valid starting workspace.
            if not entries <= known and not (path.parent / "manifest.json").is_file():
                raise ConfigError("Refusing to adopt a nonempty, unrecognized workspace.")
    mkdir(path)
    path.chmod(0o700)
    KIT = path
    if not (KIT / "config/settings.json").exists():
        defaults = copy.deepcopy(EMBEDDED_DEFAULT_SETTINGS)
        defaults["external_backup"] = None  # A new machine never inherits a disk choice.
        defaults["home_snapshot"] = "off"
        write_json(KIT / "config/settings.json", defaults)
    if not (KIT / "README.md").exists():
        write(KIT / "README.md", WORKSPACE_README)
    if not (KIT / ".gitignore").exists():
        write(KIT / ".gitignore", EMBEDDED_GITIGNORE)
    settings()
    return KIT


def refresh_manifest(directory, created_at=None):
    records = []
    for path in regular_files(directory):
        relative = path.relative_to(directory).as_posix()
        if relative == "manifest.json":
            continue
        records.append({"path": relative, "size": path.stat().st_size,
                        "sha256": digest(path), "executable": bool(path.stat().st_mode & 0o111)})
    write_json(directory / "manifest.json", {"format": FORMAT, "created_at": created_at or timestamp(),
                                           "files": records})


def system_integration_inventory():
    """Metadata only: no secrets; records managed system timer/helper state."""
    paths = [
        ("service", Path("/etc/systemd/system/ubuntu-configuration-system.service")),
        ("timer", Path("/etc/systemd/system/ubuntu-configuration-system.timer")),
        ("helper", Path("/usr/local/libexec/ubuntu-configuration-system-capture.py")),
    ]
    units = []
    for role, path in paths:
        entry = {"role": role, "path": str(path), "present": path.is_file() and not path.is_symlink()}
        if entry["present"]:
            try:
                info = path.stat()
                entry.update({"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
                              "sha256": digest(path), "readable": True})
            except OSError as error:
                entry.update({"readable": False, "store_error": type(error).__name__})
            if role in ("service", "timer"):
                unit = path.name
                for key, command in (("enabled", ["systemctl", "is-enabled", unit]),
                                     ("active", ["systemctl", "is-active", unit])):
                    try:
                        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                        entry[key] = result.stdout.strip() or "unknown"
                    except (OSError, subprocess.SubprocessError):
                        entry[key] = "unknown"
        elif role in ("service", "timer"):
            entry.update({"enabled": "not-installed", "active": "not-installed"})
        units.append(entry)
    return {"format": "ubuntu-system-integration-v1",
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(), "items": units}


def capture_system_integration(destination):
    """Store managed timer/service/helper contents plus state for recovery."""
    data = system_integration_inventory()
    for item in data["items"]:
        if not item.get("present"):
            continue
        source = Path(item["path"])
        relative = "system-integration/" + item["role"]
        candidates = [source]
        cached = destination / "system" / source.relative_to("/")
        if cached.is_file():
            candidates.append(cached)
        for candidate in candidates:
            try:
                content = candidate.read_bytes()
            except OSError as error:
                item["store_error"] = type(error).__name__
                continue
            item["sha256"] = hashlib.sha256(content).hexdigest()
            write(destination / relative, content, executable=bool(item.get("mode", 0) & 0o111))
            item["stored"] = relative
            item.pop("store_error", None)
            break
    write_json(destination / "inventory/system-integration.json", data)


def restore_system_integration(snapshot):
    """Reinstall and synchronize captured system timer/helper; requires sudo."""
    snapshot = Path(snapshot)
    metadata = snapshot / "inventory/system-integration.json"
    if metadata.is_file():
        verify_tree(snapshot)
    else:
        fallback = KIT / "system-integration-recovery"
        metadata = fallback / "system-integration.json"
        if not metadata.is_file():
            print("No captured system-integration metadata.")
            return False
        snapshot = fallback
    data = json.loads(metadata.read_text())
    targets = {
        "service": Path("/etc/systemd/system/ubuntu-configuration-system.service"),
        "timer": Path("/etc/systemd/system/ubuntu-configuration-system.timer"),
        "helper": Path("/usr/local/libexec/ubuntu-configuration-system-capture.py"),
    }
    present = [item for item in data.get("items", []) if item.get("present")]
    recoverable = [item for item in present if item.get("stored")]
    if not recoverable:
        print("Backup contains no recoverable system timer/helper contents.")
        return False
    if len(recoverable) != len(present):
        print("Warning: some system-integration files were not captured and will be skipped.")
    marker = b"Managed by ubuntu-configuration"
    for item in recoverable:
        role = item.get("role")
        if role not in targets or not item.get("stored"):
            raise ConfigError("Incomplete captured system-integration entry: " + str(role))
        source = checked_path(snapshot, item["stored"])
        content = source.read_bytes()
        if digest(source) != item.get("sha256"):
            raise ConfigError("Captured system-integration checksum mismatch: " + str(role))
        if marker not in content:
            raise ConfigError("Refusing to restore an unmanaged system-integration file: " + str(role))
        target = targets[role]
        if target.is_symlink():
            raise ConfigError("Refusing symlinked system-integration target: " + str(target))
        if target.exists():
            current = target.read_bytes()
            if marker not in current:
                raise ConfigError("Refusing to overwrite unmanaged file: " + str(target))
        temporary = Path(tempfile.mkdtemp(prefix=".system-integration-", dir=snapshot))
        staged = temporary / role
        try:
            staged.write_bytes(content)
            staged.chmod(0o700 if role == "helper" else 0o644)
            subprocess.run(["sudo", "install", "-o", "root", "-g", "root",
                            "-m", "0700" if role == "helper" else "0644",
                            str(staged), str(target)], check=True)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    timer = next((item for item in recoverable if item.get("role") == "timer"), None)
    if timer is not None:
        if timer.get("enabled") == "enabled":
            subprocess.run(["sudo", "systemctl", "enable", "--now", "ubuntu-configuration-system.timer"], check=True)
        else:
            subprocess.run(["sudo", "systemctl", "disable", "--now", "ubuntu-configuration-system.timer"], check=False)
    print("System-wide timer/helper restored; enabled state synchronized.")
    return True


def build_snapshot(destination, config):
    selected = copy.deepcopy(config)
    try:
        selected["exclude"].append(SCRIPT_PATH.relative_to(HOME).as_posix())
    except ValueError:
        pass
    report = _original_build_snapshot(destination, selected)
    capture_system_integration(destination)
    write(destination / "kit" / SCRIPT_PATH.name, SCRIPT_PATH.read_bytes(), executable=True)
    refresh_manifest(destination)
    return report


def extract_verified(archive_path, destination):
    manifest = verify_archive(archive_path)
    if destination.exists():
        if verify_tree(destination) != manifest:
            raise ConfigError("An existing profile directory does not match the bundled snapshot.")
        return destination
    mkdir(destination)
    try:
        records = {record["path"]: record for record in manifest["files"]}
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                record = records.get(member.name, {})
                with archive.extractfile(member) as source:
                    write(checked_path(destination, member.name), source.read(), bool(record.get("executable")))
        verify_tree(destination)
    except BaseException:
        shutil.rmtree(destination)  # Only this invocation's newly created private directory.
        raise
    return destination


def bundled_profile():
    if not EMBEDDED_PROFILE:
        raise ConfigError("No profile is bundled. Create a backup or select a recovered snapshot first.")
    data = base64.b85decode(EMBEDDED_PROFILE)
    if len(data) > MAX_TOTAL or hashlib.sha256(data).hexdigest() != EMBEDDED_PROFILE_SHA256:
        raise ConfigError("Bundled profile checksum mismatch.")
    directory = KIT / "profiles" / EMBEDDED_PROFILE_SHA256[:20]
    with tempfile.NamedTemporaryFile(dir=KIT, prefix=".bundled-profile-", suffix=".tar.gz") as output:
        output.write(data)
        output.flush()
        return extract_verified(Path(output.name), directory)


def default_snapshot():
    try:
        return _original_default_snapshot()
    except ConfigError:
        return bundled_profile()


def capture_root(non_interactive=False, install=False, replace_user_timer=False,
                 hide_followup_hint=False, package_scan=True):
    print("System-wide configuration and local accounts' settings will be read, including sensitive configuration.")
    command = ["sudo"] + (["-n"] if non_interactive else [])
    command += ["/usr/bin/python3", "-I", str(SCRIPT_PATH), "--workspace", str(KIT), "_capture-root-worker"]
    if install:
        command.append("--install-system-timer")
    if replace_user_timer:
        command.append("--replace-user-timer")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in result.stdout.splitlines():
        if hide_followup_hint and line.startswith("Now run the normal backup command without sudo."):
            continue
        print(line)
    for line in result.stderr.splitlines():
        print(line, file=sys.stderr)
    if result.returncode in (0, 3) and package_scan:
        capture_root_package_scan(non_interactive)
    if result.returncode == 3:
        print("System-wide capture completed with warnings; inspect unreadable-files.json.",
              file=sys.stderr)
        return
    if result.returncode:
        detail = "System-wide capture failed"
        errors = [line for line in result.stderr.splitlines() if line.strip()]
        if errors:
            detail += ": " + errors[-1]
        raise ConfigError(detail)


def systemd_units():
    def quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    calendar = settings().get("backup_calendar", "daily")
    if calendar not in ("daily", "weekly"):
        raise ConfigError("Backup calendar must be daily or weekly.")
    service = f"""{MARKER}
[Unit]
Description=Private Ubuntu configuration snapshot (single-file kit)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I {quote(HOME / 'ubuntu-backup.py')} --workspace {quote(KIT)} --non-interactive backup
SuccessExitStatus=3
UMask=0077
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
TimeoutStartSec=20min
"""
    timer = f"""{MARKER}
[Unit]
Description=Scheduled Ubuntu configuration backup

[Timer]
OnCalendar={calendar}
Persistent=true
RandomizedDelaySec=10m

[Install]
WantedBy=timers.target
"""
    return service, timer


def install_timer():
    installed = HOME / "ubuntu-backup.py"
    if SCRIPT_PATH != installed:
        if installed.exists() and SINGLE_FILE_MARKER not in installed.read_text()[:200]:
            raise ConfigError("Refusing to replace an unfamiliar script for scheduling.")
        checked_path(HOME, installed.name)
        write(installed, SCRIPT_PATH.read_bytes(), executable=True)
    _original_install_timer()


def disable_timer():
    path = HOME / ".config/systemd/user" / (UNIT + ".timer")
    if not path.is_file() or path.is_symlink() or MARKER not in path.read_text():
        raise ConfigError("No recognized backup timer to disable.")
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT + ".timer"], check=True)
    print("Automatic backups disabled. Existing backups were not removed.")


def show_status():
    config = settings()
    support = release_support()
    release = " ".join(value for value in (support.get("version"), support.get("name")) if value)
    print(f"Workspace: {KIT}\nRetention: {config['keep_snapshots']} snapshots\n"
          f"Ubuntu: {release or 'unknown'} "
          f"({'supported' if support.get('supported') else 'outside tested range'})")
    external = config["external_backup"]
    directory = None
    if not external:
        print("External drive: not chosen; local backups only")
    else:
        try:
            directory = external_directory(config)
            print("External drive: " + str(directory))
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            print("External drive: configured but unavailable (" + str(exc) + ")")
    print(f"Local dated snapshots: {len(dated_snapshots(KIT / 'backups'))}; legacy archives: {len(archives(KIT / 'backups'))}")
    if directory is not None:
        configuration = directory / "timeline/config"
        homes = directory / "timeline/home"
        print(f"External configuration timelines: {len(list(configuration.iterdir())) if configuration.is_dir() else 0}; "
              f"full /home timelines: {len(list(homes.iterdir())) if homes.is_dir() else 0}")
    if shutil.which("systemctl"):
        result = subprocess.run(["systemctl", "--user", "is-enabled", UNIT + ".timer"],
                                capture_output=True, text=True, timeout=10)
        print("Backup timer: " + (result.stdout.strip() or "unavailable"))
    print("Config data and archives are private but NOT encrypted.")


def choose_snapshot(default=None):
    has_profile = bool(EMBEDDED_PROFILE)
    has_workspace = ((KIT / "snapshot/manifest.json").is_file()
                     or (KIT.parent / "manifest.json").is_file())
    if default is None:
        default = "1" if has_profile else ("2" if has_workspace else "0")
    print("1. Personal configuration bundled in this script"
          + ("" if has_profile else " (not available)"))
    print("2. Latest workspace snapshot (may describe this machine, not the old one)")
    print("3. A recovered snapshot directory\n0. Cancel")
    while True:
        choice = ask("Snapshot", default)
        if choice == "0":
            return None
        if choice == "1":
            try:
                return bundled_profile()
            except ConfigError as exc:
                print(exc)
                continue
        if choice == "2":
            try:
                return _original_default_snapshot()
            except ConfigError as exc:
                print(exc)
                continue
        if choice == "3":
            value = ask("Snapshot directory (blank cancels)")
            return Path(value).expanduser().resolve() if value else None
        print("Choose 0, 1, 2 or 3.")


def wizard_deploy(source=None):
    source = source or choose_snapshot()
    if source is None:
        return
    verify_tree(source)
    print("Home configuration and backed-up developments are reapplied in dependency order. "
          "System references/repos/drivers/disks are not replayed.")
    args = argparse.Namespace(
        snapshot=str(source), apply=False,
        overwrite=confirm("Replace conflicting home files, keeping rollback copies?"),
        packages=confirm("Install available APT packages from current trusted repositories?"),
        snaps=confirm("Install captured Snap apps/channels (some use classic confinement)?"),
        flatpaks=confirm("Install Flatpak apps from already-configured trusted USER remotes?"),
        desktop_settings=confirm("Import GNOME/dconf preferences?"),
        build=confirm("Rebuild captured C/C++ binaries from source where a compiler is available?"),
    )
    deploy(args)
    if ask("Type APPLY to apply this plan; anything else cancels") != "APPLY":
        print("Deployment cancelled. Nothing from the plan was applied.")
        return
    args.apply = True
    deploy(args)


def external_timelines():
    """Return configuration and full-home timelines from the chosen drive."""
    config = settings()
    if not config["external_backup"]:
        return [], []
    try:
        directory = external_directory(config)
    except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"External drive unavailable: {exc}")
        return [], []
    configuration = directory / "timeline/config"
    homes = directory / "timeline/home"
    configs = sorted(path for path in configuration.iterdir()
                     if DATED_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink()) \
        if configuration.is_dir() else []
    full = sorted(path for path in homes.iterdir()
                  if HOME_TIMELINE_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink()) \
        if homes.is_dir() else []
    return configs, full


def recovery_choices(kind):
    if kind == "config":
        choices = [(path, "local") for path in archives(KIT / "backups") + dated_snapshots(KIT / "backups")]
        configs, _ = external_timelines()
        choices += [(path, "external") for path in configs]
    elif kind == "home":
        _, homes = external_timelines()
        choices = [(path, "external") for path in homes]
    else:
        raise ConfigError("Unknown recovery type.")
    choices.sort(key=lambda item: item[0].name)
    return choices[-10:][::-1]


def choose_archive(kind=None):
    if kind is None:
        print("1. Configuration backup or timeline")
        print("2. Full /home timeline")
        print("0. Cancel")
        choice = ask("Recovery source", "1")
        if choice == "0":
            return None
        if choice == "1":
            kind = "config"
        elif choice == "2":
            kind = "home"
        else:
            raise ConfigError("Choose one of the listed recovery sources.")
    found = recovery_choices(kind)
    if found:
        for number, (path, origin) in enumerate(found, 1):
            print(f"{number}. {path.name} ({origin})")
    else:
        print("No matching backup was found in the workspace or on the selected drive.")
    print("p. Enter another path\n0. Cancel")
    while True:
        choice = ask("Archive", "1" if found else "p")
        if choice == "0":
            return None
        if choice == "p":
            value = ask("Archive path (blank cancels)")
            return Path(value).expanduser() if value else None
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            return found[int(choice) - 1][0]
        print("Choose a listed archive, p, or 0.")


def is_full_home(path):
    path = Path(path).expanduser()
    if not path.is_dir() or path.is_symlink():
        return False
    manifest = path / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        return False
    try:
        return json.loads(manifest.read_text()).get("kind") == "full-home-linked"
    except (OSError, ValueError, KeyError, TypeError):
        return False


def wizard_recover(source=None):
    choice = choose("Recovery", (
        ("1", "Recover configuration (stage a backup, then optionally deploy)"),
        ("2", "Recover full /home (stage only; live /home is never changed)"),
        ("3", "Deploy a saved snapshot", "May install packages."),
    ), "1")
    if choice == "3":
        wizard_deploy(source)
        return
    if choice == "1":
        path = source or choose_archive("config")
        if path is None:
            return
        if is_full_home(path):
            print("That is a full /home timeline; staging it without deployment.")
            restore_home(path)
            return
        destination = restore(path)
        if confirm("Restore the captured system-wide timer/helper? (sudo required)", False):
            restore_system_integration(destination)
        if confirm("Deploy this recovered configuration now? This may install packages."):
            wizard_deploy(destination)
        if confirm("Restore modified package files captured in this snapshot now?", False):
            wizard_restore_package_files(destination)
        return
    if choice == "2":
        path = source or choose_archive("home")
        if path is None:
            return
        restore_home(path)
        return
    raise ConfigError("Choose one of the listed recovery actions.")


def wizard_verify():
    print("1. Bundled or latest workspace configuration")
    print("2. Configuration backup or timeline")
    print("3. Full /home timeline")
    print("0. Cancel")
    choice = ask("Verify", "1")
    if choice == "0":
        return
    if choice == "1":
        path = choose_snapshot()
    elif choice == "2":
        path = choose_archive("config")
    elif choice == "3":
        path = choose_archive("home")
    else:
        raise ConfigError("Choose one of the listed verification sources.")
    if path is None:
        return
    result = verify_path(path)
    print(f"Verified {result.get('entry_count', len(result.get('files', [])))} entries.")


def _disk_mounts(device, devices):
    """Every mount on a whole disk or any of its partitions."""
    children = [item for item in devices
                if item is not device and item.get("path")
                and parent_disk(item["path"]) == device.get("path")]
    mounts = []
    for item in [device, *children]:
        mounts.extend(item.get("mountpoints") or [])
    return sorted(set(mounts))


def _format_candidates(include_internal=False):
    """Whole disks that are safe to offer for formatting; partitions are excluded."""
    system = system_disk()
    if not system:
        raise ConfigError("Cannot determine the system disk; refusing to offer formatting.")
    devices = attached_drives()
    candidates = []
    for device in devices:
        path = device.get("path")
        if not path or device.get("type") != "disk":
            continue
        if not include_internal and not (device.get("removable") or device.get("transport") == "usb"):
            continue
        if parent_disk(path) == system:
            continue
        size = device.get("size")
        if not isinstance(size, int) or size < 4 * 1024 ** 3:
            continue
        device["format_mounts"] = _disk_mounts(device, devices)
        candidates.append(device)
    candidates.sort(key=lambda item: (not item.get("removable"), str(item.get("path"))))
    return candidates


def format_drive(device_path, allow_internal=False, assume_yes=False):
    """Format one explicitly selected whole disk as ext4, never a partition."""
    if not sys.stdin.isatty():
        raise ConfigError("Formatting needs an interactive terminal.")
    path = Path(device_path)
    if not path.is_absolute() or path.is_symlink():
        raise ConfigError("Choose an absolute block-device path.")
    try:
        if not stat.S_ISBLK(path.stat().st_mode):
            raise ConfigError("Not a block device: " + str(path))
    except OSError as exc:
        raise ConfigError(f"Cannot inspect {path}: {exc}") from None
    devices = attached_drives()
    selected = next((item for item in devices if item.get("path") == str(path)), None)
    if selected is None:
        raise ConfigError("The selected device is no longer attached.")
    if selected.get("type") != "disk":
        raise ConfigError("Prepare new drive formats a whole disk, not a partition. "
                          "Select the disk itself (for example /dev/sda).")
    mounted = _disk_mounts(selected, devices)
    internal = not (selected.get("removable") or selected.get("transport") == "usb")
    if internal and not allow_internal:
        raise ConfigError("This drive is internal/non-removable. Rerun with --allow-internal "
                          "and an explicit INTERNAL confirmation if it is a dedicated backup disk.")
    system = system_disk()
    if not system or parent_disk(str(path)) == system:
        raise ConfigError("Refusing to format the disk that holds the running system.")
    size = selected.get("size")
    if not isinstance(size, int) or size < 4 * 1024 ** 3:
        raise ConfigError("Refusing to format a device smaller than 4 GiB.")
    if not shutil.which("mkfs.ext4") or not shutil.which("sudo"):
        raise ConfigError("mkfs.ext4 and sudo are required to format a drive.")
    print(f"Selected disk: {path} ({size / 2**30:.1f} GiB, "
          f"{selected.get('model') or 'unknown model'}"
          f"{', INTERNAL DISK' if internal else ''})")
    print("This permanently erases the partition table and every file on the whole disk.")
    if mounted:
        print("These mounts will be unmounted first: " + ", ".join(mounted))
    if not assume_yes:
        phrase = f"FORMAT {'INTERNAL ' if internal else ''}{path} {size}"
        if ask(f"Type {phrase} to erase it") != phrase:
            print("Format cancelled; nothing was changed.")
            return None
        if not confirm("Final confirmation: erase this device and create ext4?", False):
            print("Format cancelled; nothing was changed.")
            return None
    for mountpoint in mounted:
        result = subprocess.run(["sudo", "umount", mountpoint], check=False)
        if result.returncode:
            raise ConfigError(f"Could not unmount {mountpoint}; close apps using it and retry.")
    if mounted:
        devices = attached_drives()
        selected = next((item for item in devices if item.get("path") == str(path)), None)
        if selected is None:
            raise ConfigError("The selected disk disappeared after unmounting.")
        still_mounted = _disk_mounts(selected, devices)
        if still_mounted:
            raise ConfigError("The disk still has mounted partitions: " + ", ".join(still_mounted))
    if shutil.which("wipefs"):
        subprocess.run(["sudo", "wipefs", "-a", str(path)], check=False)
    result = subprocess.run(["sudo", "mkfs.ext4", "-F", "-L", "ubuntu-backup", str(path)],
                            check=False)
    if result.returncode:
        raise ConfigError(f"Formatting failed with status {result.returncode}.")
    refreshed = None
    for _attempt in range(20):
        devices = attached_drives()
        refreshed = next((item for item in devices if item.get("path") == str(path)), None)
        if refreshed and refreshed.get("fstype") == "ext4" and refreshed.get("uuid"):
            break
        time.sleep(0.5)
    else:
        raise ConfigError("The device was formatted but its filesystem was not detected yet; "
                          "rerun drive selection after a moment.")
    target = default_mount_point(refreshed)
    try:
        if target.exists() and any(target.iterdir()):
            target = target.with_name(target.name + "-" + refreshed["uuid"][:8])
    except OSError:
        target = target.with_name(target.name + "-" + refreshed["uuid"][:8])
    mountpoint = mount_drive(refreshed, str(target), assume_yes=assume_yes)
    set_external(mountpoint)
    print("Drive formatted, mounted, owned by this account, and selected for backups.")
    return mountpoint


def wizard_prepare_drive(allow_internal=False):
    candidates = _format_candidates(allow_internal)
    if not candidates and not allow_internal:
        if confirm("No removable/USB drive was found. Include internal disks? (dangerous)", False):
            return wizard_prepare_drive(allow_internal=True)
        print("Connect a removable drive, ensure it is unmounted, and rescan.")
        return None
    if not candidates:
        print("No unmounted device of at least 4 GiB was found.")
        return None
    print("FORMATTING ERASES ALL DATA. These are the eligible devices:")
    for number, device in enumerate(candidates, 1):
        size = device.get("size")
        size_text = f"{size / 2**30:.1f} GiB" if isinstance(size, int) else "unknown size"
        kind = "" if (device.get("removable") or device.get("transport") == "usb") else " [INTERNAL]"
        mounts = device.get("format_mounts") or []
        mounted_text = " [MOUNTED: " + ", ".join(mounts) + "]" if mounts else ""
        print(f"{number}. {device['path']} ({size_text}, {device.get('model') or 'unknown model'}, "
              f"{device.get('fstype') or 'no filesystem'}){kind}{mounted_text}")
    if len(candidates) == 1:
        selected = candidates[0]
        print("Only one eligible disk found; using " + selected["path"])
    else:
        choice = ask("Device number (blank cancels)")
        if not choice:
            return None
        if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
            raise ConfigError("Choose a listed device number.")
        selected = candidates[int(choice) - 1]
    return format_drive(selected["path"], allow_internal=allow_internal,
                        assume_yes=True)


def wizard_drive(mount_point=None, disable=False):
    current = settings()["external_backup"]
    if not current:
        print("Current drive: unset")
    else:
        try:
            print("Current drive: " + str(external_directory(settings())))
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            print("Current drive: configured but unavailable (" + str(exc) + ")")
    print("No disk is formatted or partitioned. A selected Linux filesystem can be mounted at boot, after one confirmation.")
    value = "disable" if disable else mount_point
    if value is None:
        while True:
            devices = attached_drives()
            # USB/removable devices are presented first; lsblk has no reliable connection-time history.
            devices.sort(key=lambda d: not (d["transport"] == "usb" or d["removable"]))
            if not devices:
                print("No attached block devices detected. Connect a drive and choose rescan.")
            for number, device in enumerate(devices, 1):
                mounts = ", ".join(device["mountpoints"])
                if not mounts:
                    entry = fstab_entry(device["uuid"]) if device.get("uuid") else None
                    mounts = f"boot entry {entry['mount_point']} (not mounted now)" if entry else "NOT MOUNTED"
                print(f"{number}. {device['path']} ({device['type']}, {device['size']} bytes, "
                      f"{device['fstype'] or 'no filesystem'}, label {device.get('label') or 'none'}, "
                      f"UUID {device['uuid'] or 'none'}) — {mounts}")
            print("s. Skip external backup for now; keep previous target")
            print("r. Rescan after connecting a drive   p. Enter an already-mounted path")
            print("f. Format a new removable/USB drive (ERASES ALL DATA)")
            print("d. Disable/forget saved target (separate confirmation)")
            choice = ask("Choice", "s").lower()
            if choice in ("", "s", "0"):
                print("External drive skipped; saved target unchanged.")
                return False
            if choice == "r":
                continue
            if choice == "f":
                if wizard_prepare_drive():
                    return True
                continue
            if choice == "d":
                value = "disable"
                break
            if choice == "p":
                value = ask("Already-mounted path (blank skips)")
                break
            if choice.isdigit() and 1 <= int(choice) <= len(devices):
                selected = devices[int(choice) - 1]
                if not selected["mountpoints"]:
                    preferred = default_mount_point(selected)
                    entry = fstab_entry(selected["uuid"]) if selected.get("uuid") else None
                    if entry:
                        preferred = Path(entry["mount_point"])
                    target = ask("Mount point", str(preferred))
                    try:
                        value = mount_drive(selected, target or None)
                    except (ConfigError, OSError, subprocess.SubprocessError) as exc:
                        print(str(exc))
                        continue
                elif len(selected["mountpoints"]) > 1:
                    print("Multiple mountpoints: " + ", ".join(selected["mountpoints"]))
                    value = ask("Enter the desired mountpoint (blank skips)")
                    if value and value not in selected["mountpoints"]:
                        print("Not a listed mountpoint; choice unchanged.")
                        continue
                else:
                    value = selected["mountpoints"][0]
                break
            print("Choose a listed device, s, r, p, f or d.")
    if not value:
        return False
    if value == "disable":
        if confirm("Disable external copies without deleting any backups?"):
            set_external(None, True)
        return False
    mount = Path(value).expanduser().resolve()
    info = mount_info(mount)
    print(f"Mounted target: {mount}\nFilesystem UUID: {info['uuid']}\nType: {info['fstype']}")
    set_external(str(mount))
    print("Backup drive selected.")
    return True


def interactive_backup(package_scan=True):
    """Ask only when a saved destination is absent/unavailable; skip never clears it."""
    external = settings()["external_backup"]
    if external:
        try:
            directory = external_directory(settings())
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            print(f"Backup cancelled: saved drive unavailable: {exc}")
            return None
        else:
            print("Using previously approved backup drive: " + str(directory))
            return backup(package_scan=package_scan)
    else:
        print("No backup drive is selected. Select one from the tray or menu first; backup cancelled.")
        return None


def backup_everything(native_capture=False, refresh_package_scan=True):
    """Single flow: privileged configuration, configuration backup, full /home."""
    global _STATUS_RUN, _LAST_HOME_WARNINGS
    try:
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        signal.signal(signal.SIGHUP, signal.default_int_handler)
    except ValueError:
        pass  # GUI worker threads cannot install signal handlers.
    _STATUS_RUN = True
    _LAST_HOME_WARNINGS = 0
    _diagnose_previous_status()
    _status_history("started", "Backup started")
    _write_status("running", "preparing", "Preparing backup")
    _ensure_tray()
    try:
        external = settings().get("external_backup")
        if not external:
            message = "Backup cancelled: no backup drive selected."
            _write_status("cancelled", "cancelled", message)
            _status_history("cancelled", message)
            print(message)
            return None
        try:
            validate_external_directory(external_directory(settings()))
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            message = f"Backup cancelled: selected drive unavailable: {exc}"
            _write_status("cancelled", "cancelled", message)
            _status_history("cancelled", message)
            print(message)
            return None
        print("Package scan before backup: " + package_scan_summary())
        capture_root_package_scan(
            non_interactive=False, force=bool(refresh_package_scan))
        _write_status("running", "privileged", "Capturing system-wide configuration")
        capture_root(hide_followup_hint=True, package_scan=False)
        _write_status("running", "configuration", "Capturing configuration")
        path = interactive_backup(package_scan=False)
        if path is None:
            message = "Backup cancelled: backup drive unavailable or not selected."
            _write_status("cancelled", "cancelled", message)
            _status_history("cancelled", message)
            print(message)
            return None
        _write_status("running", "full_home", "Capturing full /home")
        print("Starting full /home capture.")
        home_done = wizard_home_backup(_LAST_BACKUP_RUN_ID, native_capture)
        warnings = _LAST_BACKUP_WARNINGS
        home_warnings = _LAST_HOME_WARNINGS
        message = ("Backup operation complete: configuration and full /home."
                   if home_done else
                   "Backup operation complete: configuration only; full /home was skipped.")
        if warnings:
            message += f" (warnings): {warnings} unreadable configuration file(s)"
        if home_warnings:
            message += f"; {home_warnings} full /home omission(s)"
        _write_status("complete", "complete", message, result=str(path) if path else None,
                      warnings=bool(warnings or home_warnings),
                      omissions=warnings + home_warnings)
        _status_history("complete", message, path, at=_LAST_BACKUP_RUN_ID)
        print(message)
        return path
    except KeyboardInterrupt:
        message = "Backup cancelled; no partial backup was published."
        _write_status("cancelled", "cancelled", message)
        _status_history("cancelled", message, at=_LAST_BACKUP_RUN_ID)
        raise
    except Exception as exc:
        message = f"Backup failed: {type(exc).__name__}: {exc}"
        _write_status("failed", "failed", message)
        _status_history("failed", message, KIT / "logs/ubuntu-backup.log",
                        at=_LAST_BACKUP_RUN_ID)
        raise
    finally:
        _STATUS_RUN = False


def retention_value():
    while True:
        value = ask("Successful snapshots to retain", str(settings()["keep_snapshots"]))
        if value.isdigit() and 1 <= int(value) <= 10000:
            return int(value)
        print("Enter an integer from 1 to 10000.")


def wizard_schedule():
    print("1. Daily")
    print("2. Weekly")
    print("3. Disable automatic backups")
    print("4. Show status")
    print("0. Leave unchanged")
    choice = ask("Schedule", "0")
    if choice == "0":
        return
    if choice == "4":
        show_status()
        return
    if choice == "3":
        if confirm("Disable the existing backup timer (archives are kept)?"):
            disable_timer()
        return
    if choice not in ("1", "2"):
        raise ConfigError("Choose one of the listed schedules.")
    keep = retention_value()
    calendar = "daily" if choice == "1" else "weekly"
    if not confirm(f"Keep the stable script and enable {calendar} backups, keeping {keep}?"):
        return
    config = settings()
    config.update(keep_snapshots=keep, backup_calendar=calendar)
    write_json(KIT / "config/settings.json", config)
    install_timer()
    if confirm("Also install the protected daily privileged configuration capture? It replaces the user timer to avoid duplicate snapshots."):
        capture_root(install=True, replace_user_timer=True)
    print("Logged-out runs require separately enabling user linger; otherwise missed runs catch up at login.")


def menu():
    while True:
        print()
        print("Ubuntu backup — configuration and full /home")
        print("1. Back up this machine now (configuration and full /home)")
        print("2. Full recovery (configuration or full /home)")
        print("3. Verify a snapshot, backup or timeline")
        print("4. Choose backup drive (or leave unset)")
        print("5. Automatic backup and privileged-capture schedule")
        print("6. Format a new backup drive (ERASES ALL DATA)")
        print("u. Undo restore")
        print("0. Exit")
        choice = ask("Choice", "0").lower()
        try:
            if choice == "0":
                return
            if choice == "1":
                backup_everything()
            elif choice == "2":
                wizard_recover()
            elif choice == "3":
                wizard_verify()
            elif choice == "4":
                wizard_drive()
            elif choice == "5":
                wizard_schedule()
            elif choice == "6":
                wizard_prepare_drive()
            elif choice == "u":
                wizard_undo_restore()
            else:
                print("Choose a listed action.")
        except (ConfigError, OSError, ValueError, KeyError, TypeError, tarfile.TarError,
                EOFError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"Action failed: {exc}", file=sys.stderr)


def preferred_workspace():
    parent = SCRIPT_PATH.parent
    if (parent / "config/settings.json").is_file() or (parent.parent / "manifest.json").is_file():
        if parent != HOME:
            return parent
    return HOME / "ubuntu-backup" / "data"


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Self-contained interactive Ubuntu configuration and full-home backup tool. Private, not encrypted.",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="store_true", dest="show_help",
                        help="Show embedded documentation and usage")
    parser.add_argument("--workspace", type=Path, help="Private working folder (default: ~/ubuntu-backup-data)")
    parser.add_argument("--non-interactive", action="store_true", help="Never ask questions; use explicit command/options")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    descriptions = {
        "menu": "Open the interactive menu (default)", "backup": "Capture a verified backup",
        "status": "Show workspace, drive and scheduler status",
        "capture-root": "Capture system-wide configuration using sudo", "disable-timer": "Stop scheduling; keep archives",
        "tray": "Show the backup status icon",
        "backup-now": "Run the combined backup in this terminal",
        "recover": "Restore configuration or full /home (interactive)",
        "choose-drive": "Select a backup drive; can add a boot mount",
        "prepare-drive": "Format a drive as ext4 and select it; internal disks need --allow-internal",
        "mount-saved": "Mount the fstab-declared backup drive and verify its on-disk marker",
        "unmount-saved": "Unmount the marked backup drive; keeps the boot entry",
    }
    for command, description in descriptions.items():
        commands.add_parser(command, help=description)
    for command in ("backup", "backup-now"):
        commands.choices[command].add_argument(
            "--native-capture", action="store_true",
            help="Use the native capture engine for full /home (experimental)")
    commands.choices["prepare-drive"].add_argument(
        "--allow-internal", action="store_true",
        help="Also offer non-removable internal disks (requires an INTERNAL confirmation)")
    commands.choices["mount-saved"].add_argument("--check", action="store_true", help="Report state only; mount nothing")
    commands.choices["mount-saved"].add_argument("--quiet", action="store_true", help="Silence the routine no-op states")
    worker = commands.add_parser("_capture-root-worker")
    worker.add_argument("--install-system-timer", action="store_true")
    worker.add_argument("--replace-user-timer", action="store_true")
    worker = commands.add_parser("_backup-home-worker")
    worker.add_argument("--run-id")
    worker.add_argument("--native-capture", action="store_true")
    worker = commands.add_parser("_restore-package-files-worker")
    worker.add_argument("--plan", required=True, type=Path)
    worker = commands.add_parser("_capture-package-files-worker")
    sub = commands.add_parser("restore-package-files",
                              help="Review or restore captured modified non-conffile package files in place")
    sub.add_argument("snapshot", nargs="?", type=Path)
    sub.add_argument("--apply", action="store_true", help="Write in place using sudo; default is a dry run")
    sub.add_argument("--package", action="append", default=[], help="Restore only this package (repeatable)")
    sub.add_argument("--path", action="append", default=[], help="Restore only this destination path (repeatable)")
    sub.add_argument("--force", action="store_true",
                     help="Allow files that differ from the capture at review time; "
                          "changes after review are always refused")
    sub.add_argument("--wizard", action="store_true",
                     help="Review interactively in a terminal before applying")
    sub = commands.add_parser("capture-package-files",
                              help="Capture modified package files using sudo")
    sub.add_argument("--force", action="store_true",
                     help="Run a fresh scan even if a recent cache exists")
    sub = commands.add_parser("restore-home", help="Stage a full /home linked snapshot into a private folder; no live changes")
    sub.add_argument("timeline", nargs="?", type=Path)
    sub = commands.add_parser("install-timer", help="Enable daily/weekly unattended backups")
    sub.add_argument("--calendar", choices=("daily", "weekly"))
    for command in ("verify", "restore"):
        sub = commands.add_parser(command, help="Verify a snapshot/archive" if command == "verify" else "Recover into a new directory")
        sub.add_argument("archive", nargs="?", type=Path)
        if command == "restore":
            sub.add_argument("--selective", action="store_true", help="After staging a configuration backup, open selective per-file restore")
    sub = commands.add_parser("selective-restore", help="Selective per-file restore; review-only by default; no system commands")
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--snapshot", type=Path)
    group.add_argument("--archive", type=Path)
    sub = commands.add_parser("undo-restore", help="Review and undo unchanged steps from a selective restore")
    sub.add_argument("journal", nargs="?", type=Path)
    sub = commands.add_parser("set-external", help="Choose a backup drive; can add a boot mount, never formats disks")
    sub.add_argument("mount_point", nargs="?")
    sub.add_argument("--disable", action="store_true")
    sub = commands.add_parser("deploy", help="Preview/apply a saved configuration; dry-run by default")
    sub.add_argument("--snapshot")
    for option in ("apply", "overwrite", "packages", "snaps", "flatpaks", "desktop-settings", "build"):
        sub.add_argument("--" + option, action="store_true")
    args = parser.parse_args()
    if args.show_help:
        print(EMBEDDED_README)
        print()
        parser.print_help()
        return
    workspace = args.workspace or preferred_workspace()
    if args.command == "_capture-root-worker":
        namespace = {"__name__": "root_reference_worker", "__file__": str(workspace.absolute() / "scripts/capture-root-configs.py"), "SELF_SOURCE": EMBEDDED_ROOT_EXPORTER}
        exec(compile(EMBEDDED_ROOT_EXPORTER, "<embedded root-reference exporter>", "exec"), namespace)
        namespace["main"](install=args.install_system_timer, replace_user_timer=args.replace_user_timer)
        return
    if args.command == "_backup-home-worker":
        root_home_backup(workspace, args.run_id, args.native_capture)
        return
    if args.command == "_restore-package-files-worker":
        restore_package_files_worker(args.plan, workspace.absolute())
        return
    if args.command == "_capture-package-files-worker":
        capture_package_files_worker(workspace.absolute())
        return
    if os.geteuid() == 0:
        raise ConfigError("Run normally, not with sudo. The root-capture action invokes sudo separately.")
    interactive = not args.non_interactive and sys.stdin.isatty()
    command = args.command or "menu"
    if command == "menu":
        if not interactive:
            raise ConfigError("The menu needs an interactive terminal. Use --non-interactive backup, deploy, verify, or --help.")
        if EMBEDDED_PROFILE:
            print("Personal configuration data is bundled in this file; it is NOT encrypted.")
        else:
            print("No personal profile is embedded; this build uses workspace snapshots.")
        print("Snapshots may contain secrets; they are NOT encrypted.")
        workspace = Path(ask("Private workspace folder", str(workspace)))
    private_workspace(workspace)
    if command in ("mount-saved", "unmount-saved"):
        state = (mount_saved(check=args.check, quiet=args.quiet) if command == "mount-saved"
                 else unmount_saved())
        if state == "failed":
            raise ConfigError("The saved backup drive is attached but could not be mounted.")
        return
    allowed_cpus = restrict_backup_cpus()
    if command != "tray":
        _setup_logging()
        print("Backup processes pinned to CPUs: " + ",".join(map(str, sorted(allowed_cpus))), file=sys.stderr)
    if command == "menu":
        menu()
    elif command == "backup":
        backup_everything(native_capture=args.native_capture) if interactive else backup()
    elif command == "recover":
        if not interactive:
            raise ConfigError("Recovery needs an interactive terminal.")
        wizard_recover()
    elif command == "restore-home":
        if interactive and args.timeline is None:
            wizard_restore_home()
        else:
            restore_home(args.timeline)
    elif command == "status":
        show_status()
    elif command == "restore-package-files":
        if args.wizard:
            if not interactive:
                raise ConfigError("The package-file restore wizard needs an interactive terminal.")
            wizard_restore_package_files()
        else:
            restore_package_files(args.snapshot, args.apply, args.package, args.path, args.force)
    elif command == "capture-package-files":
        capture_root_package_scan(non_interactive=not interactive, force=args.force)
    elif command == "tray":
        return run_tray()
    elif command == "backup-now":
        if not interactive:
            raise ConfigError("The manual backup action needs an interactive terminal.")
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        signal.signal(signal.SIGHUP, signal.default_int_handler)
        backup_everything(native_capture=args.native_capture)
    elif command == "choose-drive":
        if not interactive:
            raise ConfigError("Drive selection needs an interactive terminal.")
        wizard_drive()
    elif command == "prepare-drive":
        if not interactive:
            raise ConfigError("Formatting a drive needs an interactive terminal.")
        wizard_prepare_drive(allow_internal=args.allow_internal)
    elif command == "deploy":
        if interactive:
            wizard_deploy(Path(args.snapshot).expanduser() if args.snapshot else None)
        else:
            deploy(args)  # --apply is an explicit noninteractive choice; default remains a dry run.
    elif command == "capture-root":
        if not interactive or confirm("Use sudo to read the two whitelisted system references?"):
            capture_root(not interactive)
    elif command == "disable-timer":
        if not interactive or confirm("Disable the backup timer, retaining all archives?"):
            disable_timer()
    elif command == "install-timer":
        if interactive:
            wizard_schedule()
        else:
            if args.calendar:
                config = settings()
                config["backup_calendar"] = args.calendar
                write_json(KIT / "config/settings.json", config)
            install_timer()
    elif command == "set-external":
        if interactive:
            wizard_drive(args.mount_point, args.disable)
        else:
            set_external(args.mount_point, args.disable)
    elif command == "restore":
        path = args.archive or (choose_archive() if interactive else None)
        if not interactive or path:
            if args.selective and path is not None and is_full_home(path):
                raise ConfigError("Selective restore applies only to configuration backups; full /home is staged only.")
            destination = restore(path)
            if args.selective:
                if interactive:
                    wizard_selective_restore(destination)
                else:
                    show_selective_plan(selective_plan(destination))
    elif command == "selective-restore":
        if args.archive and is_full_home(args.archive):
            raise ConfigError("Selective restore applies only to configuration backups; full /home is staged only.")
        source = restore(args.archive) if args.archive else args.snapshot
        if interactive:
            wizard_selective_restore(source)
        else:
            show_selective_plan(selective_plan(source or default_snapshot()))
            print("Preview only. Applying a selective restore requires interactive per-file choices and a typed confirmation.")
    elif command == "undo-restore":
        if interactive:
            wizard_undo_restore(args.journal)
        elif args.journal:
            selective_undo_plan(args.journal)
            print("Preview only. Applying undo requires an interactive typed confirmation.")
        else:
            raise ConfigError("Supply a journal path to preview undo, or use the interactive menu.")
    elif command == "verify":
        result = verify_path(args.archive) if args.archive else verify_tree(default_snapshot())
        print(f"Verified {result.get('entry_count', len(result.get('files', [])))} entries.")


# Embedded in the standalone script; never requires a separate runtime module.
import ctypes
import difflib
import errno


SELECTIVE_FORMAT = "ubuntu-selective-restore-v1"
SELECTIVE_CATEGORIES = (
    ("preferences", "Application preferences", "Apps may reread these immediately; old paths/settings may be incompatible."),
    ("appearance", "Fonts, themes and icons", "Changes your user's appearance; no system fonts or packages are installed."),
    ("shell", "Shell, login and Git configuration", "HIGH RISK: can change PATH, credentials helpers, or commands run by shells/Git."),
    ("startup", "Autostart and user-service files", "HIGH RISK: can change what runs at next login/service start, even without enabling a service now."),
    ("programs", "Scripts, commands and application launchers", "HIGH RISK: changes executable/launcher behavior when you or an app invoke these files."),
    ("hardware", "Hardware-bound display/audio settings", "Device/output identities may differ. Restore only after a verified hardware match; otherwise adapt manually."),
)


def selective_category(name):
    path = PurePosixPath(name)
    if name in (".config/monitors.xml", ".nvidia-settings-rc", ".config/kwinoutputconfig.json") or name.startswith((".config/pulse/", ".config/pipewire/", ".config/wireplumber/", ".config/autorandr/", ".screenlayout/")):
        return "hardware"
    if name.startswith((".config/autostart/", ".config/systemd/", ".config/environment.d/", ".local/share/dbus-1/services/")):
        return "startup"
    if path.name in (".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".bash_aliases", ".profile",
                     ".zshrc", ".zprofile", ".xprofile", ".xsessionrc", ".Xresources", ".gitconfig", ".pam_environment") or name.startswith(".config/git/"):
        return "shell"
    if name.startswith(("bin/", ".local/bin/", ".local/share/applications/")) or path.suffix in (".sh", ".py", ".desktop"):
        return "programs"
    if name.startswith((".fonts/", ".themes/", ".icons/", ".local/share/fonts/", ".local/share/themes/", ".local/share/icons/")):
        return "appearance"
    return "preferences"


def selective_policy(name):
    if not safe_name(name):
        raise ConfigError("Unsafe home-relative path.")
    if HOME.is_symlink() or not HOME.is_dir() or HOME.stat().st_uid != os.geteuid():
        raise ConfigError("Selective restore requires a real home directory owned by the current user.")
    target = HOME / name
    if target == SCRIPT_PATH or target == KIT or KIT in target.parents:
        raise ConfigError("The running script and backup workspace are protected.")
    first = PurePosixPath(name).parts[0]
    if first in (".ssh", ".gnupg", ".kube", ".aws", ".azure", ".pki") or name == ".git-credentials" or name.startswith((".local/share/keyrings/", ".config/gh/", ".docker/config.json")):
        raise ConfigError("Credentials and machine identity require separate manual recovery.")
    if name in (".nvidia-settings-rc", ".config/monitors.xml"):
        raise ConfigError("Hardware-specific settings remain review-only.")


@contextmanager
def selective_parent(name, create=False):
    selective_policy(name)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(HOME, flags)
    device = os.fstat(fd).st_dev
    try:
        for part in PurePosixPath(name).parts[:-1]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    fd = None
                    break
                os.mkdir(part, mode=0o700, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            info = os.fstat(child)
            if info.st_dev != device or info.st_uid != os.geteuid():
                os.close(child)
                raise ConfigError("Refusing a mounted filesystem or directory owned by another user.")
            os.close(fd)
            fd = child
        yield fd, PurePosixPath(name).name
    finally:
        if fd is not None:
            os.close(fd)


def selective_read_at(directory_fd, name):
    if directory_fd is None:
        return None, b""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return None, b""
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ConfigError("Refusing a special file, hard link, or file owned by another user.")
        if info.st_mode & 0o7000 or info.st_dev != os.fstat(directory_fd).st_dev:
            raise ConfigError("Refusing special permissions or a filesystem boundary.")
        if os.listxattr(source.fileno()):
            raise ConfigError("Extended attributes/ACLs are present; manual restore avoids silently losing them.")
        data = source.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ConfigError("Existing target exceeds the 64 MiB configuration safety limit.")
    state = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": stat.S_IMODE(info.st_mode),
             "dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid, "gid": info.st_gid,
             "mtime_ns": info.st_mtime_ns}
    return state, data


def selective_read(name):
    with selective_parent(name) as (fd, leaf):
        return selective_read_at(fd, leaf)


def selective_rename(fd, source, target, exchange=False):
    # Ubuntu/Linux renameat2 gives no-clobber creation/moves and atomic exchanges.
    library = ctypes.CDLL(None, use_errno=True)
    operation = getattr(library, "renameat2", None)
    if operation is None:
        raise ConfigError("Atomic safe rename is unavailable; use staged/manual recovery instead.")
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(fd, os.fsencode(source), fd, os.fsencode(target), 2 if exchange else 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def selective_install(name, data, expected, mode):
    """Never clobber a new file; preserve/recover displaced concurrent edits."""
    with selective_parent(name, create=True) as (fd, leaf):
        current, _ = selective_read_at(fd, leaf)
        if current != expected:
            raise ConfigError(f"Target changed since review: {name}")
        temporary = ".ubuntu-restore-" + uuid.uuid4().hex
        output_fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        preserve = False
        try:
            with os.fdopen(output_fd, "wb") as output:
                output.write(data)
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
            staged, _ = selective_read_at(fd, temporary)
            if expected is None:
                selective_rename(fd, temporary, leaf)  # RENAME_NOREPLACE, even in a race.
            else:
                selective_rename(fd, temporary, leaf, exchange=True)
                preserve = True  # temporary now holds the displaced user's file.
                displaced, _ = selective_read_at(fd, temporary)
                if displaced != expected:
                    installed, _ = selective_read_at(fd, leaf)
                    if installed == staged:
                        selective_rename(fd, temporary, leaf, exchange=True)
                        preserve = False
                        raise ConfigError(f"Concurrent edit detected and put back: {name}")
                    raise ConfigError(f"Concurrent edits preserved at {HOME / name} and {(HOME / name).parent / temporary}; review manually.")
                preserve = False
            os.fsync(fd)
            installed, _ = selective_read_at(fd, leaf)
            if installed != staged:
                raise ConfigError(f"Target changed while restoring; journal retained: {name}")
            return installed
        finally:
            if not preserve:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass


def selective_remove_created(name, expected):
    with selective_parent(name) as (fd, leaf):
        current, _ = selective_read_at(fd, leaf)
        if current != expected:
            raise ConfigError(f"New file was modified after restore; kept: {name}")
        moved = ".ubuntu-undo-" + uuid.uuid4().hex
        selective_rename(fd, leaf, moved)
        captured, _ = selective_read_at(fd, moved)
        if captured != expected:
            try:
                selective_rename(fd, moved, leaf)
            except OSError:
                raise ConfigError(f"Concurrent edits kept at {(HOME / name).parent / moved}; review manually.") from None
            raise ConfigError(f"Concurrent edit detected and put back: {name}")
        os.unlink(moved, dir_fd=fd)  # An unchanged file created by this transaction; its bytes remain in after/.
        os.fsync(fd)


def selective_plan(source):
    source = Path(source).expanduser().absolute()
    manifest = verify_tree(source)
    comparison = hardware_comparison(source)
    entries = []
    for record in manifest["files"]:
        if not record["path"].startswith("home/"):
            continue
        name = record["path"][5:]
        entry = {"path": name, "category": selective_category(name), "after_sha256": record["sha256"],
                 "after_mode": 0o700 if record["executable"] else 0o600, "before": None}
        try:
            if comparison["changes"] and entry["category"] in ("startup", "shell", "programs", "hardware"):
                raise ConfigError("Hardware/OS differs or is unknown; command/startup settings require adaptation and manual review, not blind replay.")
            state, _ = selective_read(name)
            entry["before"] = state
            entry["status"] = "missing" if state is None else "unchanged" if state["sha256"] == record["sha256"] else "different"
            if state is not None:
                entry["after_mode"] = state["mode"]  # Preserve existing permission bits.
        except (ConfigError, OSError) as exc:
            entry.update(status="blocked", reason=str(exc))
        entries.append(entry)
    plan = {"format": "ubuntu-selective-restore-plan-v1", "source": str(source), "home": str(HOME),
            "source_manifest_sha256": digest(source / "manifest.json"), "entries": entries,
            "system_files_review_only": sum(record["path"].startswith("system/") for record in manifest["files"]),
            "selected": []}
    plan["hardware_comparison"] = comparison
    write_json(KIT / "plans/last-selective-restore.json", plan)
    return plan


def show_selective_plan(plan):
    print("\nSELECTIVE RESTORE — no /etc writes, packages, dconf imports, sudo, or service activation commands.")
    print(f"Source: {plan['source']}\nHome target: {HOME}")
    print(f"System reference files kept review-only: {plan['system_files_review_only']}")
    print("Hardware/OS review: " + ", ".join(plan["hardware_comparison"]["changes"] or ["saved and current identities match"]))
    print("Restoring user settings can still affect running apps or commands at the next login/invocation.")
    for number, entry in enumerate(plan["entries"], 1):
        print(f"{number:3}. {entry['status']:10} {entry['path']}")
        if entry["status"] == "blocked":
            print("     Kept unchanged: " + entry["reason"])
    print(f"Full private plan: {KIT / 'plans/last-selective-restore.json'}")


def selective_diff(plan, entry):
    print("Diffs may reveal passwords/tokens. They are displayed only here, not saved in the plan.")
    if not confirm("Display this file's contents/differences in the terminal?"):
        return
    _, before = selective_read(entry["path"])
    path = checked_path(Path(plan["source"]), "home/" + entry["path"])
    after = path.read_bytes()
    if hashlib.sha256(after).hexdigest() != entry["after_sha256"]:
        raise ConfigError("Source changed after verification.")
    try:
        if b"\0" in before or b"\0" in after:
            raise UnicodeError
        lines = list(difflib.unified_diff(before.decode("utf-8").splitlines(), after.decode("utf-8").splitlines(),
                                         fromfile="current/" + entry["path"], tofile="saved/" + entry["path"], lineterm=""))
    except UnicodeError:
        print("Binary data: no text diff displayed. Use the recorded sizes/checksums for comparison.")
        return
    print("\n".join(lines[:120]) or "No content differences.")
    if len(lines) > 120:
        print("Diff truncated after 120 lines; review the private source file for the rest.")


def selective_save_journal(directory, journal):
    write_json(directory / "journal.json", journal)
    fsync_directory(directory)


def verify_selective_journal(directory, journal):
    if journal.get("format") != SELECTIVE_FORMAT or journal.get("home") != str(HOME):
        raise ConfigError("Journal format/home does not match this user. No automatic replay allowed.")
    names = []
    for entry in journal["entries"]:
        selective_policy(entry["path"])
        names.append(entry["path"])
        for folder, expected in (("after", entry["after_sha256"]), ("before", entry["before"]["sha256"] if entry["before"] else None)):
            if expected is not None and digest(checked_path(directory, folder + "/" + entry["path"])) != expected:
                raise ConfigError("Rollback payload checksum mismatch; no live changes allowed.")
    if len(names) != len(set(names)):
        raise ConfigError("Duplicate rollback targets.")


def apply_selective_restore(plan, selected):
    source = Path(plan["source"])
    if hardware_comparison(source) != plan["hardware_comparison"]:
        raise ConfigError("Destination hardware/OS changed after preview; review a new adaptive restore plan.")
    if plan["home"] != str(HOME) or digest(source / "manifest.json") != plan["source_manifest_sha256"]:
        raise ConfigError("Source/home changed since preview; start a new review.")
    verify_tree(source)
    if not selected or len(selected) != len(set(selected)):
        raise ConfigError("Select at least one unique file explicitly.")
    entries = []
    by_name = {entry["path"]: entry for entry in plan["entries"]}
    for name in selected:
        entry = copy.deepcopy(by_name[name])
        if entry["status"] not in ("missing", "different"):
            raise ConfigError("Only reviewed, writable missing/different home files can be selected.")
        if selective_read(name)[0] != entry["before"]:
            raise ConfigError(f"Target changed after preview; nothing restored: {name}")
        entries.append(entry)
    mkdir(KIT / "rollback")
    directory = Path(tempfile.mkdtemp(prefix="selective-", dir=KIT / "rollback"))
    journal = {"format": SELECTIVE_FORMAT, "home": str(HOME), "created_at": timestamp(), "state": "preparing", "entries": entries}
    try:
        total = 0
        for entry in entries:
            state, before = selective_read(entry["path"])
            if state != entry["before"]:
                raise ConfigError("Destination changed while preparing rollback copies.")
            after = checked_path(source, "home/" + entry["path"]).read_bytes()
            if hashlib.sha256(after).hexdigest() != entry["after_sha256"]:
                raise ConfigError("Source changed while preparing rollback copies.")
            total += len(before) + len(after)
            if total > 2 * MAX_TOTAL:
                raise ConfigError("Rollback payload exceeds safety limits.")
            if state is not None:
                write(directory / "before" / entry["path"], before)
            write(directory / "after" / entry["path"], after)
            entry["step"] = "pending"
        verify_selective_journal(directory, journal)
        # Persist all recovery copies and the complete journal BEFORE touching live files.
        for parent, _, _ in os.walk(directory, topdown=False):
            fsync_directory(Path(parent))
        journal["state"] = "prepared"
        selective_save_journal(directory, journal)
        fsync_directory(directory.parent)
        for entry in entries:
            if selective_read(entry["path"])[0] != entry["before"]:
                raise ConfigError("Target changed during preparation; no live writes performed.")
        for entry in entries:
            entry["step"] = "installing"
            selective_save_journal(directory, journal)
            data = (directory / "after" / entry["path"]).read_bytes()
            entry["installed_state"] = selective_install(entry["path"], data, entry["before"], entry["after_mode"])
            entry["step"] = "applied"
            selective_save_journal(directory, journal)
        journal["state"] = "complete"
        selective_save_journal(directory, journal)
    except BaseException:
        journal["state"] = "interrupted"
        selective_save_journal(directory, journal)
        print(f"Stopped safely; completed steps, if any, are recorded in: {directory}", file=sys.stderr)
        raise
    print(f"Restored {len(entries)} explicitly selected home files. Verified undo journal: {directory}")
    print("No system settings or services were replayed. Empty newly created parent folders are left in place on undo.")
    return directory


def selective_undo_plan(directory):
    directory = Path(directory).expanduser().absolute()
    try:
        relative = directory.relative_to(KIT / "rollback")
    except ValueError:
        raise ConfigError("Choose a selective-restore journal under this workspace's rollback/ directory.") from None
    checked_path(KIT / "rollback", relative.as_posix())
    journal = json.loads((directory / "journal.json").read_text())
    verify_selective_journal(directory, journal)
    eligible = []
    for entry in journal["entries"]:
        if entry.get("step") == "undone" or entry.get("step") == "pending":
            continue
        try:
            state, _ = selective_read(entry["path"])
            if entry.get("step") != "applied" or state != entry.get("installed_state"):
                print(f"KEEP {entry['path']}: modified after restore or interrupted/uncertain step; manual review required.")
                continue
            eligible.append(entry)
            print(("REVERT " if entry["before"] else "REMOVE unchanged newly-created file ") + entry["path"])
        except (OSError, ConfigError) as exc:
            print(f"KEEP {entry['path']}: {exc}")
    return journal, eligible


def undo_selective_restore(directory):
    directory = Path(directory)
    journal, eligible = selective_undo_plan(directory)
    for entry in eligible:
        if selective_read(entry["path"])[0] != entry["installed_state"]:
            raise ConfigError("File changed during undo review; stopped without overwriting that change.")
        if entry["before"] is None:
            selective_remove_created(entry["path"], entry["installed_state"])
        else:
            data = (directory / "before" / entry["path"]).read_bytes()
            selective_install(entry["path"], data, entry["installed_state"], entry["before"]["mode"])
        entry["step"] = "undone"
        selective_save_journal(directory, journal)
    journal["state"] = "undone" if all(entry.get("step") in ("undone", "pending") for entry in journal["entries"]) else "partially-undone"
    selective_save_journal(directory, journal)
    print(f"Undid {len(eligible)} unchanged restore steps; later edits and recovery copies were kept.")


def wizard_undo_restore(directory=None):
    if directory is None:
        journals = sorted(path for path in (KIT / "rollback").glob("selective-*") if path.is_dir() and not path.is_symlink())
        if not journals:
            print("No restore journals found.")
            return
        for number, path in enumerate(journals, 1):
            print(f"{number}. {path.name}")
        value = ask("Journal number (blank cancels)")
        if not value:
            return
        if not value.isdigit() or not 1 <= int(value) <= len(journals):
            raise ConfigError("Choose a listed journal number.")
        directory = journals[int(value) - 1]
    _, eligible = selective_undo_plan(directory)
    if not eligible:
        print("No safe, unchanged restore steps to undo.")
        return
    print("Undo restores originals or removes only unchanged files created by this restore. All recovery copies remain private.")
    phrase = f"UNDO {len(eligible)} FILES"
    if ask(f"Type {phrase} to proceed; anything else cancels") == phrase:
        undo_selective_restore(directory)


def wizard_selective_restore(source=None):
    source = source or choose_snapshot()
    if source is None:
        return
    plan = selective_plan(source)
    show_selective_plan(plan)
    print("\n1. REVIEW ONLY (default): inspect metadata/diffs; no live writes")
    print("2. MISSING FILES ONLY: opt in per file; never replace an existing file")
    print("3. SELECTIVE REPLACE: opt in per file; originals are verified in an undo journal first")
    print("0. Cancel")
    mode = ask("Restore mode", "1")
    if mode == "0":
        return
    if mode == "1":
        while True:
            value = ask("File number to inspect a diff (blank finishes without changes)")
            if not value:
                print("Review complete; no live changes.")
                return
            if value.isdigit() and 1 <= int(value) <= len(plan["entries"]):
                selective_diff(plan, plan["entries"][int(value) - 1])
            else:
                print("Choose a listed file number.")
    if mode not in ("2", "3"):
        raise ConfigError("Choose review-only, missing-only, selective replace, or cancel.")
    for number, (key, title, consequence) in enumerate(SELECTIVE_CATEGORIES, 1):
        count = sum(entry["category"] == key for entry in plan["entries"])
        print(f"{number}. {title} ({count} files): {consequence}")
    value = ask("Category numbers to review, comma-separated (blank selects NOTHING)")
    if not value:
        print("No files selected; no live changes.")
        return
    numbers = [part.strip() for part in value.split(",")]
    if not all(number.isdigit() and 1 <= int(number) <= len(SELECTIVE_CATEGORIES) for number in numbers):
        raise ConfigError("Use only the listed category numbers, separated by commas.")
    categories = {SELECTIVE_CATEGORIES[int(number) - 1][0] for number in numbers}
    selected = []
    for entry in plan["entries"]:
        if entry["category"] not in categories or entry["status"] not in ("missing", "different"):
            continue
        if mode == "2" and entry["status"] != "missing":
            print("KEEP existing file: " + entry["path"])
            continue
        print(f"\n{entry['path']} — {entry['status']}")
        print(next(description for key, _, description in SELECTIVE_CATEGORIES if key == entry["category"]))
        action = "c" if entry["status"] == "missing" else "r"
        label = "CREATE" if action == "c" else "REPLACE after saving the original"
        while True:
            choice = ask(f"s=SKIP (default), p=private diff, {action}={label}", "s").lower()
            if choice == "p":
                selective_diff(plan, entry)
            elif choice == "s":
                break
            elif choice == action:
                selected.append(entry["path"])
                break
            else:
                print("Choose the displayed action, p, or s.")
    if not selected:
        print("No files selected; no live changes.")
        return
    plan["selected"] = selected
    write_json(KIT / "plans/last-selective-restore.json", plan)
    for entry in plan["entries"]:
        if entry["path"] in selected:
            print(("CREATE " if entry["before"] is None else "REPLACE ") + entry["path"])
    print("Existing permissions are preserved; new files are private. Targets are rechecked for concurrent changes.")
    print("Nothing is installed, enabled or restarted. Apps may nevertheless observe changed user preferences.")
    phrase = f"RESTORE {len(selected)} HOME FILES"
    if ask(f"Type {phrase} to proceed; anything else cancels") != phrase:
        print("Restore cancelled; no live changes.")
        return
    apply_selective_restore(plan, selected)
import threading


def hardware_comparison(source):
    changes = []
    path = Path(source) / "inventory/hardware.json"
    saved = json.loads(path.read_text()) if path.is_file() else None
    current = hardware_inventory()
    if not saved or not saved.get("complete") or not current.get("complete"):
        changes.append("hardware inventory unavailable/incomplete")
    if saved:
        for key in ("architecture", "vendor", "model", "disks", "gpus", "network"):
            if saved.get(key) != current.get(key):
                changes.append(key + " changed")
    info = Path(source) / "inventory/source-system.json"
    old_os = json.loads(info.read_text()).get("os", {}) if info.is_file() else {}
    if not old_os or old_os.get("VERSION_ID") != read_os_release().get("VERSION_ID"):
        changes.append("Ubuntu version unknown/changed")
    return {"changes": changes, "saved": saved, "current": current,
            "policy": "Portable preferences may be selected; hardware bindings, startup/scripts and all system references require manual adaptation when identities differ."}


def home_identity():
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        point = fields[4].replace("\\040", " ").replace("\\134", "\\")
        if point == "/home" or point.startswith("/home/"):
            mounts.append({"mount": point, "device": fields[2], "root": fields[3]})
    hardware = hardware_inventory()
    source_disk = system_disk()
    # Approval is about the source of /home, not every USB device or NIC that
    # happens to be attached when the operator reviews the backup.
    hardware_identity = {
        "architecture": hardware.get("architecture"),
        "vendor": hardware.get("vendor"),
        "model": hardware.get("model"),
        "source_disk": source_disk,
        "complete": bool(hardware.get("complete") and source_disk),
    }
    return {"hardware": hardware_identity, "home_device": Path("/home").stat().st_dev,
            "mounts": sorted(mounts, key=lambda entry: entry["mount"])}


def tar_member_name(name):
    pure = PurePosixPath(name)
    if name in ("", "."):
        return None
    if pure.is_absolute() or ".." in pure.parts:
        raise ConfigError(f"Unsafe tar member path: {name}")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts:
        return None
    return "/".join(parts)


def tar_member_metadata(member):
    headers = member.pax_headers or {}
    return {key: value for key, value in headers.items()
            if key.startswith(("SCHILY.", "LIBARCHIVE."))}


def stream_hash(stream, count, hasher):
    remaining = count
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            raise ConfigError("Short read in full /home tar stream.")
        hasher.update(block)
        remaining -= len(block)


def stream_object(stage_objects, stage_incoming, external_objects, stream, size, sparse, created):
    """Hash one logical file, stage it locally, and return its external object path."""
    with tempfile.NamedTemporaryFile(dir=stage_incoming, prefix=".object-", delete=False) as output:
        temporary = Path(output.name)
        hasher = hashlib.sha256()
        try:
            if sparse:
                current = 0
                for offset, length in sparse:
                    offset, length = int(offset), int(length)
                    if length < 0:
                        raise ConfigError("Invalid sparse file layout in tar stream.")
                    if length == 0:
                        # GNU tar appends (0, 0) and other zero-length sentinels.
                        continue
                    if offset < current:
                        raise ConfigError("Invalid sparse file layout in tar stream.")
                    if offset > current:
                        stream_hash(stream, offset - current, hasher)
                    output.seek(offset)
                    remaining = length
                    while remaining:
                        block = stream.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ConfigError("Short sparse read in full /home tar stream.")
                        output.write(block)
                        hasher.update(block)
                        remaining -= len(block)
                    current = offset + length
                if current < size:
                    stream_hash(stream, size - current, hasher)
                output.truncate(size)
            else:
                remaining = size
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ConfigError("Short read in full /home tar stream.")
                    output.write(block)
                    hasher.update(block)
                    remaining -= len(block)
            output.flush()
            # The local staging copy is disposable; it is flushed once after the bulk copy.
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    sha256 = hasher.hexdigest()
    external = object_path(external_objects, sha256)
    try:
        if external.is_symlink():
            raise ConfigError("Refusing symlinked baseline object.")
        if external.exists():
            if not external.is_file() or external.stat().st_size != size:
                raise ConfigError("Existing baseline object has the wrong size or type.")
            return external, sha256
        staged = []
        publish_object(stage_objects, stage_incoming, temporary, sha256, size, staged, sync=False)
        if staged:
            created.append(external)
    finally:
        temporary.unlink(missing_ok=True)
    return external, sha256


def remove_staging():
    """Remove the disposable staging tree, including root-owned remnants."""
    stage = KIT / "staging"
    if stage.is_symlink():
        stage.unlink(missing_ok=True)
    elif stage.is_dir():
        shutil.rmtree(stage)
    elif stage.exists():
        stage.unlink()


def prepare_staging(external_root):
    """Create the disposable local staging area used before a bulk copy to the drive."""
    stage = KIT / "staging"
    if stage.is_symlink() or (stage.exists() and not stage.is_dir()):
        raise ConfigError("Backup staging path is not a real directory: " + str(stage))
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    try:
        if stage.stat().st_dev == os.stat(external_root).st_dev:
            raise ConfigError("Full /home staging must be on a different filesystem from the backup drive.")
        free = shutil.disk_usage(stage).free
        if free < STAGING_RESERVE_BYTES:
            raise ConfigError(f"Only {free / 2**30:.1f} GiB free for staging; "
                              f"at least {STAGING_RESERVE_BYTES / 2**30:.1f} GiB is required.")
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage


def copy_staged_objects(stage_objects, external_objects):
    """Copy only new content-addressed objects to the drive, then flush once."""
    shards = [entry for entry in stage_objects.iterdir() if entry.is_dir()]
    if not shards:
        return 0
    copied = 0
    total = 0
    for shard in shards:
        target_shard = ensure_private_directory(external_objects, shard.name)
        for source in shard.iterdir():
            if source.is_symlink() or not source.is_file():
                raise ConfigError("Unexpected entry in staging: " + str(source))
            target = target_shard / source.name
            if target.is_symlink():
                raise ConfigError("Refusing symlinked baseline object: " + str(target))
            size = source.stat().st_size
            if target.exists():
                if not target.is_file() or target.stat().st_size != size:
                    raise ConfigError("Existing baseline object has the wrong size or type.")
                continue
            temporary = target_shard / (".copy-" + uuid.uuid4().hex)
            try:
                copy_sparse_file(source, temporary, size)
                os.chmod(temporary, 0o400)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            copied += 1
            total += size
            if copied % 4096 == 0:
                print(f"Bulk copy: {copied} objects, {total / 2**30:.1f} GiB", file=sys.stderr, flush=True)
    if copied:
        os.sync()
        print(f"Bulk copy complete: {copied} objects, {total / 2**30:.1f} GiB", file=sys.stderr, flush=True)
    return copied


def make_timeline_parent(tree, name):
    parent = PurePosixPath(name).parent
    if str(parent) == ".":
        return tree
    return ensure_private_directory(tree, *parent.parts)


def clean_created_objects(created):
    for path in created:
        try:
            path.unlink()
        except OSError:
            pass


_STATUS_RUN = False
_EXIT_CODE = 0
_LAST_BACKUP_RUN_ID = None
_LAST_BACKUP_WARNINGS = 0
_LAST_HOME_WARNINGS = 0


def _status_path():
    return KIT / "status.json"


def _history_path():
    return KIT / "history.txt"


def _write_status(state, phase, message, **extra):
    payload = {"state": state, "phase": phase, "message": message, "pid": os.getpid(),
               "updated": dt.datetime.now(dt.timezone.utc).isoformat(), **extra}
    try:
        if not KIT.is_dir():
            return
        temporary = KIT / (".status-" + str(os.getpid()))
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, _status_path())
        _status_path().chmod(0o600)
    except OSError:
        temporary.unlink(missing_ok=True) if "temporary" in locals() else None


HISTORY_LIMIT_BYTES = 1024 * 1024
HISTORY_KEEP_LINES = 500


def history_timestamp(run_id=None):
    if run_id:
        try:
            return dt.datetime.strptime(run_id, "%Y%m%dT%H%M%S.%fZ").strftime("%Y-%m-%d %H:%M:%SZ")
        except (TypeError, ValueError):
            pass
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _status_history(state, message, result=None, at=None):
    lock = None
    try:
        fd = os.open(KIT / ".status.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock = os.fdopen(fd, "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
    except OSError:
        lock = None
    try:
        line = history_timestamp(at)
        line += f"  {state.upper():9}  {message}"
        if result:
            line += f"  ->  {result}"
        with _history_path().open("a") as stream:
            stream.write(line + "\n")
        path = _history_path()
        path.chmod(0o600)
        if path.stat().st_size > HISTORY_LIMIT_BYTES:
            lines = path.read_text(errors="replace").splitlines()[-HISTORY_KEEP_LINES:]
            temporary = path.with_name(".history-" + str(os.getpid()))
            temporary.write_text("\n".join(lines) + "\n")
            temporary.chmod(0o600)
            os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                lock.close()


LOG_SEGMENT_BYTES = 16 * 1024 * 1024
LOG_SEGMENTS = 4


class RotatingLog:
    def __init__(self, path, segment_bytes=LOG_SEGMENT_BYTES, segments=LOG_SEGMENTS):
        self.path = Path(path)
        self.segment_bytes = segment_bytes
        self.segments = segments
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)

    def _rotate(self):
        for number in range(self.segments - 1, 0, -1):
            source = self.path.with_name(self.path.name + "." + str(number))
            target = self.path.with_name(self.path.name + "." + str(number + 1))
            if source.exists():
                os.replace(source, target)
        oldest = self.path.with_name(self.path.name + "." + str(self.segments))
        oldest.unlink(missing_ok=True)
        if self.path.exists():
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))

    def write(self, text):
        data = text.encode("utf-8", "replace")
        lock = None
        try:
            fd = os.open(self.path.with_name("." + self.path.name + ".lock"),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            lock = os.fdopen(fd, "a")
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            lock = None
        try:
            if self.path.exists() and self.path.stat().st_size + len(data) > self.segment_bytes:
                self._rotate()
            with self.path.open("ab") as stream:
                stream.write(data)
            self.path.chmod(0o600)
        except OSError:
            pass
        finally:
            if lock is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                finally:
                    lock.close()

    def flush(self):
        pass


class Tee:
    def __init__(self, stream, log):
        self.stream = stream
        self.log = log

    def write(self, text):
        self.log.write(text)
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()

    def isatty(self):
        return getattr(self.stream, "isatty", lambda: False)()


def _setup_logging():
    try:
        log = RotatingLog(KIT / "logs" / "ubuntu-backup.log")
        sys.stdout = Tee(sys.stdout, log)
        sys.stderr = Tee(sys.stderr, log)
    except OSError:
        pass


VALID_STATUS_STATES = {"idle", "running", "complete", "failed", "cancelled"}


def _read_status():
    try:
        return json.loads(_status_path().read_text())
    except (OSError, ValueError):
        return {}


def _read_tray_status():
    """Return a valid idle record when the workspace has no status file yet."""
    data = _read_status()
    if data:
        return data
    return {"state": "idle", "phase": "idle", "message": "No backup running",
            "pid": None, "updated": dt.datetime.now(dt.timezone.utc).isoformat()}


def _live_backup_pid(pid):
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        return False
    return any(b"ubuntu-backup" in arg or b"ubuntu-configuration" in arg for arg in args) and b"tray" not in args


def _status_problem(data):
    if not isinstance(data, dict):
        return "status record is not an object"
    state = data.get("state")
    if state not in VALID_STATUS_STATES:
        return f"unknown state {state!r}"
    updated = data.get("updated")
    if not isinstance(updated, str) or not updated:
        return "missing updated timestamp"
    try:
        dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return "invalid updated timestamp"
    if state == "running":
        pid = data.get("pid")
        if not isinstance(pid, int):
            return "running status has no process id"
        if pid == os.getpid():
            if _GUI_PROCESS_ACTIVE:
                return None
            return "running status points to this process"
        if not _live_backup_pid(pid):
            return f"recorded process {pid} is not active"
    return None


def _diagnose_previous_status():
    data = _read_status()
    problem = _status_problem(data) if data else None
    if problem:
        _status_history("inconsistent", "state=" + str(data.get("state")) +
                        " pid=" + str(data.get("pid")) + " updated=" + str(data.get("updated")) +
                        " reason=" + problem)


EXTERNAL_PURGE_NAMES = ("baseline", "timeline", "ubuntu-configuration-backups")


def _purge_path(path):
    if path.is_symlink():
        raise ConfigError("Refusing symlinked purge target: " + str(path))
    if path.is_mount():
        raise ConfigError("Refusing mounted purge target: " + str(path))
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def purge_all_data():
    data = _read_tray_status()
    if data.get("state") == "running":
        raise ConfigError("Cancel the active backup before purging data.")
    config = settings()
    external = config.get("external_backup")
    if external and not isinstance(external, dict):
        raise ConfigError("Saved backup drive settings are incomplete; nothing was purged.")
    external_dir = None
    if external:
        try:
            external_dir = external_directory(config)
            if external_dir.is_symlink():
                raise ConfigError("Refusing symlinked external backup directory.")
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            raise ConfigError("External backup drive is unavailable; nothing was purged. "
                              "Forget the drive first to purge local data only. " + str(exc)) from None
        if marker_record(external_dir) is None:
            raise ConfigError("External backup drive marker is missing or invalid; nothing was purged.")
    lock_fd = None
    try:
        lock_fd = os.open(KIT / ".backup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ConfigError("A backup is active; cancel it before purging data.") from None
        if external_dir is not None:
            for name in EXTERNAL_PURGE_NAMES:
                _purge_path(external_dir / name)
            marker = external_dir / ".ubuntu-configuration-target.json"
            if marker.is_symlink():
                raise ConfigError("Refusing symlinked external marker: " + str(marker))
            marker.unlink(missing_ok=True)
        script = SCRIPT_PATH.resolve()
        for entry in list(KIT.iterdir()):
            if entry.name in (".tray.lock", ".backup.lock"):
                continue
            if entry.resolve() == script or script.parent == entry:
                continue
            _purge_path(entry)
        defaults = copy.deepcopy(EMBEDDED_DEFAULT_SETTINGS)
        defaults["external_backup"] = None
        defaults["home_snapshot"] = "off"
        write_json(KIT / "config/settings.json", defaults)
        _write_status("idle", "idle", "All backup data purged")
        _status_history("idle", "all backup data purged")
    except OSError as exc:
        raise ConfigError("Purge incomplete: " + str(exc) +
                          ". Run ./uninstall.sh --purge-backups with sudo to remove privileged data.") from None
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    return True


def _ensure_tray():
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        lock_fd = os.open(KIT / ".tray.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        return
    os.close(lock_fd)
    try:
        subprocess.Popen([sys.executable, "-I", str(SCRIPT_PATH), "--workspace", str(KIT), "tray"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True)
    except OSError:
        pass


class ProgressBar:
    def __init__(self, label, width=28):
        self.label = label
        self.width = width
        self.last = 0.0

    def update(self, count, size, force=False):
        import time
        now = time.monotonic()
        if not force and now - self.last < 0.2:
            return
        self.last = now
        phase = int(now * 4) % (self.width * 2)
        filled = phase if phase <= self.width else self.width * 2 - phase
        bar = '#' * filled + '-' * (self.width - filled)
        print(f"\r{self.label} [{bar}] {count} entries, {size / 1048576:.1f} MiB", end='', flush=True)

    def finish(self, count, size):
        print(f"\r{self.label} [{'#' * self.width}] {count} entries, {size / 1048576:.1f} MiB", flush=True)

    def abort(self):
        print(flush=True)


class TailReader:
    """Track tar's trailing zero padding; the root tar exit status is authoritative."""
    def __init__(self, stream):
        self.stream = stream
        self.tail = b""

    def read(self, size=-1):
        data = self.stream.read(size)
        self.tail = (self.tail + data)[-1024:]
        return data


def read_home_entries(timeline):
    with (timeline / "entries.ndjson").open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def verify_home_timeline(timeline, hash_objects=True):
    """Verify a full-/home link tree and every baseline object it references."""
    if timeline.is_symlink() or not timeline.is_dir():
        raise ConfigError("Full /home timeline must be a real directory.")
    if (timeline / "manifest.json").is_symlink() or not (timeline / "manifest.json").is_file():
        raise ConfigError("Full /home manifest must be a real file.")
    manifest = json.loads((timeline / "manifest.json").read_text())
    if manifest.get("storage") != LINKED_STORAGE or manifest.get("kind") != "full-home-linked":
        raise ConfigError("Not a linked full /home timeline.")
    entries = timeline / "entries.ndjson"
    if not entries.is_file() or entries.is_symlink() or digest(entries) != manifest.get("entries_sha256"):
        raise ConfigError("Full /home entries manifest checksum mismatch.")
    tree = timeline / "tree"
    if tree.is_symlink() or not tree.is_dir():
        raise ConfigError("Full /home timeline has no real tree directory.")
    baseline = timeline.parent.parent.parent / "baseline/home/objects"
    seen = {}
    hashes = {}
    count = 0
    logical = 0
    for entry in read_home_entries(timeline):
        count += 1
        name = entry.get("path")
        if not isinstance(name, str) or not safe_name(name):
            raise ConfigError("Unsafe path in full /home manifest.")
        if name in seen:
            raise ConfigError(f"Duplicate path in full /home timeline: {name}")
        kind = entry.get("type")
        path = safe_join(tree, name, allow_final_symlink=kind != "dir")
        if kind == "dir":
            if path.is_symlink() or not path.is_dir():
                raise ConfigError(f"Missing full /home directory: {name}")
        elif kind == "file":
            sha = entry.get("sha256")
            size = entry.get("size")
            if not isinstance(sha, str) or not OBJECT_RE.fullmatch(sha) or not isinstance(size, int) or size < 0:
                raise ConfigError(f"Invalid file metadata in full /home timeline: {name}")
            if not path.is_symlink():
                raise ConfigError(f"Full /home file is not a baseline link: {name}")
            expected = object_path(baseline, sha)
            if path.resolve() != expected.resolve() or not expected.is_file() or expected.is_symlink():
                raise ConfigError(f"Full /home link does not resolve to its baseline object: {name}")
            if expected.stat().st_size != size:
                raise ConfigError(f"Full /home baseline size mismatch: {name}")
            if sha not in hashes:
                hashes[sha] = digest(expected) if hash_objects else sha
            if hashes[sha] != sha:
                raise ConfigError(f"Full /home baseline checksum mismatch: {name}")
            logical += size
        elif kind == "symlink":
            if not path.is_symlink() or os.readlink(path) != entry.get("target"):
                raise ConfigError(f"Full /home symlink mismatch: {name}")
        elif kind == "hardlink":
            target = entry.get("hardlink_to")
            if not isinstance(target, str) or not safe_name(target) or target not in seen:
                raise ConfigError(f"Full /home hardlink target is missing: {name}")
            if seen[target] not in ("file", "hardlink"):
                raise ConfigError(f"Full /home hardlink target is not a file: {name}")
            if not path.is_symlink() or path.resolve() != safe_join(tree, target, allow_final_symlink=True).resolve():
                raise ConfigError(f"Full /home hardlink mismatch: {name}")
        elif kind == "special":
            # Device nodes, FIFOs and sockets are recorded but never recreated.
            pass
        else:
            raise ConfigError(f"Unknown full /home entry type: {kind}")
        seen[name] = kind
    if count != manifest.get("entry_count"):
        raise ConfigError("Full /home entry count mismatch.")
    if logical != manifest.get("logical_bytes"):
        raise ConfigError("Full /home logical byte count mismatch.")
    for directory, dirs, files in os.walk(tree, followlinks=False):
        for child in dirs + files:
            path = Path(directory) / child
            relative = path.relative_to(tree).as_posix()
            if path.is_dir() and not path.is_symlink():
                if seen.get(relative) != "dir":
                    raise ConfigError(f"Unexpected full /home directory: {relative}")
            elif path.is_symlink():
                if seen.get(relative) not in ("file", "symlink", "hardlink"):
                    raise ConfigError(f"Unexpected full /home link: {relative}")
            else:
                raise ConfigError(f"Unexpected full /home regular file: {relative}")
    return manifest


def capture_home_archive(stream, root, identity, exclusions, snapshot=None):
    """Consume a root tar stream as the normal user and publish a linked timeline."""
    objects, incoming, timelines = baseline_directories(root, "home")
    stage = prepare_staging(root)
    stage_objects = ensure_private_directory(stage, "objects")
    stage_incoming = ensure_private_directory(stage, "incoming")
    print("Staging full /home on " + str(stage), file=sys.stderr)
    temporary = Path(tempfile.mkdtemp(prefix=".home-timeline-", dir=timelines))
    tree = ensure_private_directory(temporary, "tree")
    entries_path = temporary / "entries.ndjson"
    created = []
    written = set()
    warnings = []
    count = 0
    logical = 0
    progress = ProgressBar("Full /home")
    tracker = TailReader(stream)
    try:
        with entries_path.open("w") as entries, tarfile.open(fileobj=tracker, mode="r|") as archive:
            for member in archive:
                if count % 128 == 0 and shutil.disk_usage(stage).free < STAGING_RESERVE_BYTES:
                    raise ConfigError("Local staging space is nearly exhausted; no timeline published.")
                name = tar_member_name(member.name)
                if name is None:
                    continue
                if name in written:
                    raise ConfigError(f"Duplicate full /home path: {name}")
                parent = make_timeline_parent(tree, name)
                path = parent / PurePosixPath(name).name
                if member.isdir():
                    if path.is_symlink() or (path.exists() and not path.is_dir()):
                        raise ConfigError(f"Full /home directory path is unsafe: {name}")
                    if not path.exists():
                        path.mkdir(mode=0o700)
                elif path.is_symlink() or path.exists():
                    raise ConfigError(f"Full /home path already exists in timeline: {name}")
                base = {"path": name, "mode": member.mode, "uid": member.uid, "gid": member.gid,
                        "mtime_ns": int(float(getattr(member, "mtime", 0)) * 1_000_000_000),
                        "pax": tar_member_metadata(member)}
                if member.isdir():
                    entry = dict(base, type="dir")
                elif member.issym():
                    os.symlink(member.linkname, path)
                    entry = dict(base, type="symlink", target=member.linkname)
                elif member.islnk():
                    target = tar_member_name(member.linkname)
                    if target is None or target not in written:
                        raise ConfigError(f"Full /home hardlink target is missing: {name}")
                    symlink_relative(path, safe_join(tree, target, allow_final_symlink=True))
                    entry = dict(base, type="hardlink", hardlink_to=target)
                elif member.isfile():
                    source = archive.extractfile(member)
                    if source is None:
                        raise ConfigError(f"Cannot read full /home file: {name}")
                    sparse = [list(segment) for segment in member.sparse] if getattr(member, "sparse", None) else None
                    destination, sha256 = stream_object(stage_objects, stage_incoming, objects,
                                                        source, member.size, sparse, created)
                    symlink_relative(path, destination)
                    logical += member.size
                    entry = dict(base, type="file", size=member.size, sha256=sha256, sparse=sparse)
                else:
                    entry = dict(base, type="special")
                    warnings.append({"path": name, "type": str(member.type)})
                entries.write(json.dumps(entry, separators=(",", ":")) + "\n")
                written.add(name)
                count += 1
                progress.update(count, logical)
            # Tar normally supplies every parent directory. Record any that it omitted.
            for directory, dirs, _ in os.walk(tree, topdown=True, followlinks=False):
                for child in sorted(dirs):
                    path = Path(directory) / child
                    relative = path.relative_to(tree).as_posix()
                    if path.is_symlink():
                        continue
                    if relative not in written:
                        entry = {"path": relative, "type": "dir", "mode": 0o700,
                                 "uid": os.getuid(), "gid": os.getgid(), "mtime_ns": 0,
                                 "pax": {}, "synthetic": True}
                        entries.write(json.dumps(entry, separators=(",", ":")) + "\n")
                        written.add(relative)
                        count += 1
        progress.finish(count, logical)
        # A cut inside tar's trailing zero padding loses no file data; the root
        # tar process exit status is the authoritative completeness check.
        if len(tracker.tail) < 512 or tracker.tail[-512:] != b"\0" * 512:
            raise ConfigError("Full /home tar stream ended before its end marker; no timeline published.")
        manifest = {"format": FORMAT, "kind": "full-home-linked", "storage": LINKED_STORAGE,
                    "created_at": timestamp(), "source": "/home", "identity": identity,
                    "exclusions": exclusions, "entry_count": count, "logical_bytes": logical,
                    "warnings": warnings[:200], "warning_count": len(warnings),
                    "entries_sha256": digest(entries_path),
                    "restore_policy": "Stage privately. Never overwrite live /home or replay UID/hardware bindings blindly."}
        if snapshot:
            manifest["snapshot"] = snapshot
        write_json(temporary / "manifest.json", manifest)
        if created:
            print(f"Copying {len(created)} new objects from local staging to the backup drive...",
                  file=sys.stderr)
        copy_staged_objects(stage_objects, objects)
        print("Verifying full /home baseline checksums...", file=sys.stderr)
        verify_home_timeline(temporary)
        return temporary, manifest, created, timelines
    except BaseException:
        progress.abort()
        if temporary.exists():
            shutil.rmtree(temporary)
        clean_created_objects(created)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def copy_sparse_file(source, destination, size):
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        offset = 0
        while offset < size:
            try:
                data = os.lseek(source_fd, offset, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    break
                raise
            hole = min(os.lseek(source_fd, data, os.SEEK_HOLE), size)
            while data < hole:
                block = os.pread(source_fd, min(1024 * 1024, hole - data), data)
                if not block:
                    raise ConfigError("Short read while restoring a sparse file.")
                written = 0
                while written < len(block):
                    progress = os.pwrite(output_fd, block[written:], data + written)
                    if not progress:
                        raise ConfigError("Short write while restoring a sparse file.")
                    written += progress
                data += len(block)
            offset = hole
        os.ftruncate(output_fd, size)
    finally:
        os.close(source_fd)
        os.close(output_fd)


def restore_home(path=None):
    """Stage a verified full-/home linked snapshot; never write live /home."""
    if path is None:
        config = settings()
        if not config["external_backup"]:
            raise ConfigError("No external full /home timeline is available.")
        directory = external_directory(config)
        timelines = sorted(path for path in (directory / "timeline/home").iterdir()
                           if HOME_TIMELINE_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink())
        if not timelines:
            raise ConfigError("No full /home timeline found on the selected drive.")
        path = timelines[-1]
        print("Latest full /home timeline: " + str(path))
    else:
        path = Path(path).expanduser()
    manifest = verify_home_timeline(path)
    mkdir(KIT / "restored")
    destination = Path(tempfile.mkdtemp(prefix="home-full-", dir=KIT / "restored"))
    objects = path.parent.parent.parent / "baseline/home/objects"
    dir_modes = {}
    hardlinks = []
    hardlink_targets = {}
    specials = []
    try:
        for entry in read_home_entries(path):
            name = entry["path"]
            kind = entry["type"]
            target = safe_join(destination, name, allow_final_symlink=kind != "dir")
            if kind == "dir":
                if target.exists() and not target.is_dir():
                    raise ConfigError(f"Restore path changed type: {name}")
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                dir_modes[name] = entry.get("mode") or 0o700
            elif kind == "symlink":
                make_timeline_parent(destination, name)
                if target.is_symlink() or target.exists():
                    raise ConfigError(f"Restore path already exists: {name}")
                os.symlink(entry["target"], target)
            elif kind == "file":
                make_timeline_parent(destination, name)
                if target.is_symlink() or target.exists():
                    raise ConfigError(f"Restore path already exists: {name}")
                link = safe_join(path / "tree", name, allow_final_symlink=True)
                object_file = (link.parent / os.readlink(link)).resolve()
                if object_file != object_path(objects, entry["sha256"]).resolve():
                    raise ConfigError(f"Restore link changed: {name}")
                temporary = target.parent / (".restore-" + uuid.uuid4().hex)
                try:
                    if entry.get("sparse"):
                        copy_sparse_file(object_file, temporary, entry["size"])
                    else:
                        shutil.copyfile(object_file, temporary)
                    if digest(temporary) != entry["sha256"]:
                        raise ConfigError(f"Restored file checksum mismatch: {name}")
                    os.chmod(temporary, (entry.get("mode") or 0o600) & 0o777)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                if entry.get("mtime_ns"):
                    os.utime(target, ns=(entry["mtime_ns"], entry["mtime_ns"]))
            elif kind == "hardlink":
                hardlinks.append((name, entry["hardlink_to"]))
                hardlink_targets[name] = entry["hardlink_to"]
            elif kind == "special":
                specials.append(name)
        for name, _ in hardlinks:
            target = safe_join(destination, name, allow_final_symlink=True)
            make_timeline_parent(destination, name)
            if target.is_symlink() or target.exists():
                raise ConfigError(f"Restore hardlink path already exists: {name}")
        for name, link in hardlinks:
            final = link
            seen = set()
            while final not in seen:
                seen.add(final)
                if final not in hardlink_targets:
                    break
                final = hardlink_targets[final]
            source = safe_join(destination, final, allow_final_symlink=True)
            if source.is_symlink() or not source.is_file():
                raise ConfigError(f"Restore hardlink target is missing: {link}")
            os.link(source, safe_join(destination, name, allow_final_symlink=True))
        for name in sorted(dir_modes, key=lambda item: item.count("/"), reverse=True):
            path_to_set = safe_join(destination, name)
            os.chmod(path_to_set, dir_modes[name] & 0o777)
        notes = destination.parent / (destination.name + "-restore-notes.txt")
        notes.write_text(
            "Full /home files were copied into this private staging folder.\n"
            "Ownership, ACLs, extended attributes and original UID/GID were NOT applied.\n"
            f"Source timeline: {path}\n"
            f"Metadata: {path / 'entries.ndjson'}\n"
            f"Special/runtime entries not recreated: {len(specials)}\n"
            "Review owners and hardware-bound settings before copying anything toward live /home.\n")
        print(f"Staged full /home snapshot into: {destination}")
        print(f"Restore notes: {notes}")
        print("Live /home was not changed; ownership/ACLs require manual review.")
        return destination
    except BaseException:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def wizard_restore_home():
    config = settings()
    if config["external_backup"]:
        try:
            directory = external_directory(config)
            timelines = sorted(path for path in (directory / "timeline/home").iterdir()
                               if HOME_TIMELINE_RE.fullmatch(path.name) and path.is_dir() and not path.is_symlink())
            if timelines:
                print("Latest full /home timeline: " + str(timelines[-1]))
                if confirm("Stage the latest full /home snapshot into a private folder?", True):
                    restore_home(timelines[-1])
                    return
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            print(f"External drive unavailable: {exc}")
    value = ask("Full /home timeline directory (blank cancels)")
    if value:
        restore_home(Path(value).expanduser())


def run_logged(command, log_path):
    """Run a child, streaming merged output to the terminal and a private log."""
    log_path = Path(log_path)
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log_path.open("w") as log:
        log_path.chmod(0o600)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        try:
            for line in process.stdout:
                sys.stderr.write(line)
                sys.stderr.flush()
                log.write(line)
                log.flush()
            status = process.wait()
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
    if status:
        detail = log_path.read_text(errors="replace")[-2000:].strip()
        message = f"Full /home worker exited with status {status}. Log: {log_path}"
        if detail:
            message += "\n" + detail
        raise ConfigError(message)


def run_home_worker_with_fallback(command, log_path, native_capture=False):
    """Run the tar worker; on volatile-file failure retry with native capture."""
    try:
        run_logged(command, log_path)
        return native_capture
    except ConfigError as first_error:
        if native_capture:
            raise
        try:
            native_core_path()
        except ConfigError:
            raise first_error
        print("Strict tar capture failed; retrying with the native capture engine. "
              "This tolerates volatile app locks and changing files.", file=sys.stderr)
        native_command = list(command) + ["--native-capture"]
        run_logged(native_command, Path(log_path).with_name(
            Path(log_path).stem + "-native.log"))
        return True


def native_core_path():
    candidates = []
    override = os.environ.get("UBUNTU_BACKUP_NATIVE")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend([
        SCRIPT_PATH.parent / "ubackup-core",
        SCRIPT_PATH.parent / "native" / "ubackup-core",
        KIT.parent / "native" / "ubackup-core",
    ])
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise ConfigError(
        "Native capture core not found. Build native/ubackup-core or set UBUNTU_BACKUP_NATIVE.")


def native_home_command(source_home, directory, run_id, owner_uid, owner_gid,
                        identity, excluded):
    command = [str(native_core_path()), "scan",
               "--source", str(source_home),
               "--stage", str(KIT / "staging"),
               "--objects", str(directory / "baseline/home/objects"),
               "--entries", str(KIT / "native-home-entries.ndjson"),
               "--report", str(KIT / "native-home-report.json"),
               "--timeline", str(directory / "timeline/home"),
               "--run-id", run_id_value(run_id),
               "--owner-uid", str(owner_uid),
               "--owner-gid", str(owner_gid),
               "--one-file-system",
               "--identity-json", json.dumps(identity, separators=(",", ":")),
               "--exclusions-json", json.dumps(excluded)]
    for prefix in excluded:
        command.extend(["--exclude-prefix", prefix])
    return command


def wizard_home_backup(run_id=None, native_capture=False):
    global _LAST_HOME_WARNINGS
    config = settings()
    if not config["external_backup"]:
        print("Choose and mount the backup drive first (menu 4). No /home data was read or copied.")
        return False
    directory = external_directory(config)
    identity = home_identity()
    nested = [entry["mount"] for entry in identity["mounts"] if entry["mount"] != "/home"]
    if nested:
        raise ConfigError("Strict /home backup refuses unapproved nested mounts; prepare separate filesystem backups: " + ", ".join(nested))
    print("Full /home includes secrets; not encrypted. Stop apps/VMs first.")
    mode = config.get("home_snapshot", "off")
    if mode == "off":
        print("Filesystem snapshot: disabled; the capture is not atomic.")
    else:
        kind = snapshot_kind()
        if kind:
            print(f"Filesystem snapshot: {kind}; the capture will read a point-in-time source.")
        elif mode == "required":
            raise ConfigError("home_snapshot=required but no supported LVM/btrfs/ZFS snapshot is available.")
        else:
            print("Warning: no supported filesystem snapshot is available; the capture is not atomic.")
    print("Destination: " + str(directory))
    baseline = KIT / "config/home-backup-approval.json"
    changed = not baseline.is_file() or json.loads(baseline.read_text()) != identity
    if changed:
        print("Hardware/mount layout recorded for this backup.")
        write_json(baseline, identity)
    print("Starting the sudo, read-only full /home capture.")
    run_id = run_id_value(run_id)
    command = ["sudo", "/usr/bin/python3", "-I", str(SCRIPT_PATH),
               "--workspace", str(KIT), "_backup-home-worker", "--run-id", run_id]
    if native_capture:
        command.append("--native-capture")
    native_capture = run_home_worker_with_fallback(
        command, KIT / "logs" / ("home-worker-" + run_id + ".log"), native_capture)
    if native_capture:
        timeline = directory / "timeline" / "home" / ("home-full-" + run_id)
        manifest = verify_path(timeline)
        _LAST_HOME_WARNINGS = int(manifest.get("warning_count") or 0)
        if _LAST_HOME_WARNINGS:
            print(f"Native full /home timeline has {_LAST_HOME_WARNINGS} omission(s); "
                  "review the capture report.", file=sys.stderr)
        print(f"Full /home timeline verified: {timeline}")
    return True


class SnapshotUnavailable(Exception):
    pass


class Snapshot:
    """A read-only source snapshot and the root-only cleanup it requires."""

    def __init__(self, kind, snapshot_id, source_home, release, metadata):
        self.kind = kind
        self.id = snapshot_id
        self.source_home = Path(source_home)
        self.release = release
        self.metadata = metadata
        self._pipe = None
        self._supervisor = None

    def start_supervisor(self):
        """Fork a root supervisor before the writer drops privileges."""
        if self.release is None:
            return
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(write_fd)
            status = 0
            try:
                while os.read(read_fd, 65536):
                    pass
                self.release()
            except BaseException as exc:  # noqa: BLE001 - report from the root helper
                status = 1
                print(f"Snapshot cleanup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            finally:
                os._exit(status)
        os.close(read_fd)
        self._pipe = write_fd
        self._supervisor = pid

    def close(self):
        if self._pipe is None:
            return
        os.close(self._pipe)
        self._pipe = None
        try:
            _pid, status = os.waitpid(self._supervisor, 0)
        except ChildProcessError:
            return
        if status:
            print(f"Snapshot cleanup supervisor exited with status {status}.", file=sys.stderr)

    def release_now(self):
        if self.release is not None:
            self.release()
            self.release = None


def _snapshot_run(args, timeout=120):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _snapshot_mount(path):
    result = _snapshot_run(["findmnt", "-J", "-T", str(path),
                            "--output", "TARGET,SOURCE,FSTYPE"])
    if not result or result.returncode:
        return {}
    try:
        return json.loads(result.stdout)["filesystems"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return {}


def _snapshot_lvm_info():
    if not all(shutil.which(name) for name in ("lvs", "vgs", "lvcreate", "mount", "umount")):
        return None
    try:
        if os.stat("/home").st_dev != os.stat("/").st_dev:
            return None
    except OSError:
        return None
    mount = _snapshot_mount("/")
    source = mount.get("source")
    if not isinstance(source, str) or not source.startswith("/dev/"):
        return None
    result = _snapshot_run([
        "lvs", "--noheadings", "--separator", "\t",
        "-o", "vg_name,lv_name,lv_size,pool_lv", "--units", "b", "--nosuffix",
        source,
    ])
    if not result or result.returncode:
        return None
    fields = result.stdout.strip().split("\t")
    if len(fields) < 3:
        return None
    try:
        vg, lv, size = fields[0].strip(), fields[1].strip(), int(fields[2].strip())
    except ValueError:
        return None
    free_result = _snapshot_run([
        "vgs", "--noheadings", "--separator", "\t", "-o", "vg_free",
        "--units", "b", "--nosuffix", vg,
    ])
    try:
        free = int(free_result.stdout.strip()) if free_result and free_result.returncode == 0 else 0
    except ValueError:
        free = 0
    return {"kind": "lvm", "source": source, "vg": vg, "lv": lv,
            "size": size, "free": free}


def _snapshot_btrfs_info():
    if not shutil.which("btrfs"):
        return None
    root = _snapshot_mount("/")
    if root.get("fstype") != "btrfs":
        return None
    try:
        if os.stat("/home").st_dev != os.stat("/").st_dev:
            return None
    except OSError:
        return None
    subvolume = _snapshot_run(["btrfs", "subvolume", "show", "/home"])
    source = "/home" if subvolume and subvolume.returncode == 0 else "/"
    return {"kind": "btrfs", "source": source}


def _snapshot_zfs_info():
    if not shutil.which("zfs"):
        return None
    root = _snapshot_mount("/")
    if root.get("fstype") != "zfs":
        return None
    try:
        if os.stat("/home").st_dev != os.stat("/").st_dev:
            return None
    except OSError:
        return None
    source = root.get("source")
    if not isinstance(source, str) or not source:
        return None
    result = _snapshot_run(["zfs", "list", "-H", "-o", "name", source])
    if not result or result.returncode:
        return None
    dataset = result.stdout.strip().splitlines()
    mountpoint = _snapshot_run(["zfs", "get", "-H", "-o", "value", "mountpoint", source])
    if not dataset or not mountpoint or mountpoint.returncode:
        return None
    point = mountpoint.stdout.strip()
    if not point.startswith("/"):
        return None
    return {"kind": "zfs", "source": source, "dataset": dataset[0],
            "mountpoint": point}


def snapshot_kind():
    for detector in (_snapshot_btrfs_info, _snapshot_zfs_info, _snapshot_lvm_info):
        try:
            info = detector()
        except (OSError, subprocess.SubprocessError):
            info = None
        if info:
            return info["kind"]
    return None


def _create_lvm_snapshot(info, kit):
    mib = 1024 * 1024
    if info["free"] < 2 * 1024 * mib:
        raise SnapshotUnavailable("LVM volume group has less than 2 GiB free for a snapshot.")
    size_mib = max(1024, min(8192, info["free"] // 2 // mib,
                             max(1024, info["size"] // 10 // mib)))
    snapshot_id = "ubuntu-backup-" + uuid.uuid4().hex[:8]
    mountpoint = kit / ".home-snapshot"
    if mountpoint.is_symlink() or mountpoint.exists():
        raise SnapshotUnavailable("Snapshot mount point already exists: " + str(mountpoint))
    mkdir(mountpoint)
    started = time.monotonic()
    snapdev = "/dev/" + info["vg"] + "/" + snapshot_id
    result = _snapshot_run(["lvcreate", "-s", "-L", f"{size_mib}M",
                            "-n", snapshot_id, info["vg"] + "/" + info["lv"]])
    if not result or result.returncode:
        mountpoint.rmdir()
        detail = (result.stderr or "").strip() if result else "lvcreate unavailable"
        raise SnapshotUnavailable("LVM snapshot creation failed: " + detail[-400:])
    path_result = _snapshot_run(["lvs", "--noheadings", "-o", "lv_path",
                                 info["vg"] + "/" + snapshot_id])
    if path_result and path_result.returncode == 0 and path_result.stdout.strip():
        snapdev = path_result.stdout.strip().splitlines()[0]
    try:
        result = _snapshot_run(["mount", "-o", "ro,nouuid", snapdev, str(mountpoint)])
        if not result or result.returncode:
            detail = (result.stderr or "").strip() if result else "mount unavailable"
            raise SnapshotUnavailable("LVM snapshot mount failed: " + detail[-400:])
    except BaseException:
        _snapshot_run(["lvremove", "-f", snapdev])
        try:
            mountpoint.rmdir()
        except OSError:
            pass
        raise

    def release():
        _snapshot_run(["umount", str(mountpoint)])
        _snapshot_run(["lvremove", "-f", snapdev])
        try:
            mountpoint.rmdir()
        except OSError:
            pass

    metadata = {"kind": "lvm", "id": snapshot_id, "created_at": timestamp(),
                "window_ms": round((time.monotonic() - started) * 1000, 1),
                "source": info["source"], "home": str(mountpoint / "home"),
                "atomic": True}
    return Snapshot("lvm", snapshot_id, mountpoint / "home", release, metadata)


def _create_btrfs_snapshot(info, kit):
    snapshot_id = "ubuntu-backup-" + uuid.uuid4().hex[:8]
    destination = Path("/.ubuntu-backup-snapshot-" + snapshot_id)
    if destination.exists() or destination.is_symlink():
        raise SnapshotUnavailable("Btrfs snapshot destination already exists.")
    started = time.monotonic()
    result = _snapshot_run(["btrfs", "subvolume", "snapshot", "-r",
                            info["source"], str(destination)])
    if not result or result.returncode:
        detail = (result.stderr or "").strip() if result else "btrfs unavailable"
        raise SnapshotUnavailable("Btrfs snapshot creation failed: " + detail[-400:])
    try:
        destination.chmod(0o700)
    except OSError:
        pass
    source_home = destination if info["source"] == "/home" else destination / "home"

    def release():
        _snapshot_run(["btrfs", "subvolume", "delete", str(destination)])

    metadata = {"kind": "btrfs", "id": snapshot_id, "created_at": timestamp(),
                "window_ms": round((time.monotonic() - started) * 1000, 1),
                "source": info["source"], "home": str(source_home), "atomic": True}
    return Snapshot("btrfs", snapshot_id, source_home, release, metadata)


def _create_zfs_snapshot(info, kit):
    snapshot_id = "ubuntu-backup-" + uuid.uuid4().hex[:8]
    name = info["dataset"] + "@" + snapshot_id
    started = time.monotonic()
    result = _snapshot_run(["zfs", "snapshot", name])
    if not result or result.returncode:
        detail = (result.stderr or "").strip() if result else "zfs unavailable"
        raise SnapshotUnavailable("ZFS snapshot creation failed: " + detail[-400:])
    source_home = Path(info["mountpoint"]) / ".zfs/snapshot" / snapshot_id
    if info["mountpoint"] == "/":
        source_home = source_home / "home"

    def release():
        _snapshot_run(["zfs", "destroy", name])

    metadata = {"kind": "zfs", "id": snapshot_id, "created_at": timestamp(),
                "window_ms": round((time.monotonic() - started) * 1000, 1),
                "source": info["dataset"], "home": str(source_home), "atomic": True}
    return Snapshot("zfs", snapshot_id, source_home, release, metadata)


def create_home_snapshot(config, kit):
    """Create a point-in-time source snapshot, or explain why it cannot."""
    mode = config.get("home_snapshot", "off")
    if mode == "off":
        return None
    detectors = (
        ("btrfs", _snapshot_btrfs_info, _create_btrfs_snapshot),
        ("zfs", _snapshot_zfs_info, _create_zfs_snapshot),
        ("lvm", _snapshot_lvm_info, _create_lvm_snapshot),
    )
    for kind, detector, creator in detectors:
        try:
            info = detector()
        except (OSError, subprocess.SubprocessError):
            info = None
        if not info:
            continue
        try:
            return creator(info, kit)
        except SnapshotUnavailable as exc:
            if mode == "required":
                raise ConfigError(str(exc)) from None
            print(f"Warning: {exc}; full /home capture is not atomic.", file=sys.stderr)
            return None
    message = "No supported filesystem snapshot is available (need LVM, btrfs, or ZFS)."
    if mode == "required":
        raise ConfigError(message)
    print(f"Warning: {message} Full /home capture is not atomic.", file=sys.stderr)
    return None


@contextmanager
def home_backup_lock():
    """One full-/home capture at a time; staging is not safe to share."""
    try:
        fd = os.open(KIT / ".home.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ConfigError(f"Cannot open the full /home backup lock: {exc}") from None
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ConfigError("A full /home backup is already running; wait for it to finish.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def root_home_backup(workspace, run_id=None, native_capture=False):
    global KIT, HOME
    if os.geteuid() != 0 or not os.environ.get("SUDO_UID"):
        raise ConfigError("Use the confirmed normal-user full /home backup action.")
    owner = pwd.getpwuid(int(os.environ["SUDO_UID"]))
    if owner.pw_uid == 0:
        raise ConfigError("A non-root backup owner is required.")
    HOME = Path(owner.pw_dir)
    KIT = Path(workspace).absolute()
    if HOME not in KIT.parents or ".." in KIT.parts:
        raise ConfigError("Workspace must be inside the sudo caller's home.")
    with home_backup_lock():
        return _root_home_backup_locked(workspace, run_id, native_capture)


def _root_home_backup_locked(workspace, run_id=None, native_capture=False):
    global KIT, HOME
    if os.geteuid() != 0 or not os.environ.get("SUDO_UID"):
        raise ConfigError("Use the confirmed normal-user full /home backup action.")
    owner = pwd.getpwuid(int(os.environ["SUDO_UID"]))
    if owner.pw_uid == 0:
        raise ConfigError("A non-root backup owner is required.")
    HOME = Path(owner.pw_dir)
    KIT = Path(workspace).absolute()
    if HOME not in KIT.parents or ".." in KIT.parts:
        raise ConfigError("Workspace must be inside the sudo caller's home.")
    try:
        remove_staging()
    except OSError as exc:
        raise ConfigError("Cannot clear stale full /home staging: " + str(exc)) from None
    config = settings()
    allowed_cpus = restrict_backup_cpus()
    print("Backup worker pinned to CPUs: " + ",".join(map(str, sorted(allowed_cpus))), file=sys.stderr)
    if not config["external_backup"]:
        raise ConfigError("External backup drive has not been selected.")
    expected = json.loads((KIT / "config/home-backup-approval.json").read_text())
    actual = home_identity()
    if expected != actual or not actual["hardware"].get("complete"):
        raise ConfigError("Source hardware/mount identities changed or are unknown; start the backup again.")
    if any(entry["mount"] != "/home" for entry in actual["mounts"]):
        raise ConfigError("Nested /home mounts require separate reviewed filesystem backups.")
    directory = external_directory(config)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(fd).st_dev in (Path("/home").stat().st_dev, HOME.stat().st_dev, Path("/").stat().st_dev):
        os.close(fd)
        raise ConfigError("Backup drive must differ from every source filesystem.")
    try:
        snapshot = create_home_snapshot(config, KIT)
        if snapshot is not None:
            try:
                snapshot.start_supervisor()
            except OSError as exc:
                snapshot.release_now()
                raise ConfigError(f"Cannot start snapshot cleanup supervisor: {exc}") from None
            source_home = snapshot.source_home
            print(f"Using {snapshot.kind} source snapshot {snapshot.id}.", file=sys.stderr)
        else:
            source_home = Path("/home")
    except BaseException:
        os.close(fd)
        raise
    relative = KIT.relative_to("/home").as_posix()
    excluded = [relative + "/" + name for name in (
        "backups", ".previous-snapshot", "privileged-system", "privileged-package-scan",
        "restored", "rollback", "profiles", "snapshot", "staging", ".home-snapshot")]
    if native_capture:
        try:
            if marker_record(Path(directory)) is None:
                raise ConfigError("Backup drive marker is missing or invalid.")
            native_excluded = list(excluded)
            for name in ("native-home-entries.ndjson", "native-home-report.json"):
                native_excluded.append((KIT / name).relative_to("/home").as_posix())
            command = native_home_command(
                source_home, Path(directory), run_id, owner.pw_uid, owner.pw_gid,
                actual, native_excluded)
            result = subprocess.run(command, capture_output=True, text=True)
            for line in result.stdout.splitlines():
                print(line)
            for line in result.stderr.splitlines():
                print(line, file=sys.stderr)
            if result.returncode not in (0, 3):
                raise ConfigError("Native full /home capture failed with status " + str(result.returncode))
            if result.returncode == 3:
                print("Native full /home capture completed with omissions.", file=sys.stderr)
            report = json.loads((KIT / "native-home-report.json").read_text())
            print("Native full /home timeline: " + str(report.get("timeline")))
            return
        finally:
            try:
                remove_staging()
            except OSError as exc:
                print("Warning: could not remove full /home staging: " + str(exc), file=sys.stderr)
            if snapshot is not None:
                snapshot.close()
            os.close(fd)
    command = ["/usr/bin/tar", "--create", "--file=-", "--acls", "--xattrs", "--numeric-owner",
               "--sparse", "--one-file-system", "--directory=" + str(source_home)]
    for name in excluded:
        command.append("--exclude=./" + name)
    command.append(".")
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except BaseException:
        if snapshot is not None:
            snapshot.close()
        os.close(fd)
        raise
    os.setgroups([])
    os.setgid(owner.pw_gid)
    os.setuid(owner.pw_uid)
    pinned = Path("/proc/self/fd") / str(fd)
    errors = bytearray()
    def drain():
        for block in iter(lambda: child.stderr.read(8192), b""):
            errors.extend(block)
            del errors[:-65536]
    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    temporary = None
    created = []
    try:
        if marker_record(pinned) is None:
            raise ConfigError("Pinned backup drive marker is missing or invalid.")
        temporary, manifest, created, timelines = capture_home_archive(
            child.stdout, pinned, actual, excluded,
            snapshot=snapshot.metadata if snapshot is not None else None)
        status = child.wait()
        thread.join()
        if status or errors:
            raise ConfigError("Strict /home capture failed; old snapshots kept. Stop changing apps and retry. " + errors.decode(errors="replace")[-2000:])
        result = timelines / ("home-full-" + run_id_value(run_id))
        if result.exists() or result.is_symlink():
            raise ConfigError("Full /home timeline already exists; refusing overwrite.")
        temporary.rename(result)
        temporary = None
        fsync_directory(timelines)
        print(f"Full /home verified (checksums): {manifest['entry_count']} entries, "
              f"{len(created)} new objects.")
    finally:
        if child.poll() is None:
            # The writer has dropped root and must not try to signal a root tar process.
            # Closing its output pipe makes tar stop on SIGPIPE, including on disk-full errors.
            child.stdout.close()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print(f"Root reader {child.pid} has not exited; inspect it with sudo. Incomplete snapshot was not published.", file=sys.stderr)
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
            clean_created_objects(created)
        try:
            remove_staging()
        except OSError as exc:
            print("Warning: could not remove full /home staging: " + str(exc), file=sys.stderr)
        if snapshot is not None:
            snapshot.close()
        os.close(fd)

def run_process_window(title, target):
    """Run a Python action in a GTK log window with GUI prompts and sudo askpass."""
    global _GUI_PROMPT_HANDLER, _GUI_PROCESS_ACTIVE
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk
    import queue
    if _GUI_PROCESS_ACTIVE:
        raise ConfigError("A backup or restore process window is already running.")
    _GUI_PROCESS_ACTIVE = True

    window = Gtk.Window(title="Ubuntu Backup — " + title)
    window.set_default_size(760, 460)
    window.set_icon_name("ubuntu-backup")
    window.set_border_width(12)
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    window.add(outer)
    progress = Gtk.ProgressBar()
    outer.pack_start(progress, False, False, 0)
    view = Gtk.TextView(editable=False, monospace=True)
    scroll = Gtk.ScrolledWindow()
    scroll.add(view)
    outer.pack_start(scroll, True, True, 0)
    controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    close_button = Gtk.Button(label="Running…")
    close_button.set_sensitive(False)
    close_button.connect("clicked", lambda _button: window.destroy())
    controls.pack_end(close_button, False, False, 0)
    outer.pack_start(controls, False, False, 0)
    output = queue.Queue()
    state = {"active": True, "ok": False}

    class Writer:
        def write(self, text):
            if text:
                output.put(text)
            return len(text)

        def flush(self):
            pass

    def append(text):
        buffer = view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        view.scroll_to_mark(buffer.get_insert(), 0.0, True, 0.0, 1.0)

    def drain():
        if not window.get_visible():
            return False
        while True:
            try:
                append(output.get_nowait())
            except queue.Empty:
                break
        return True

    def finish(ok):
        global _GUI_PROMPT_HANDLER, _GUI_PROCESS_ACTIVE
        state["active"] = False
        state["ok"] = ok
        _GUI_PROMPT_HANDLER = None
        _GUI_PROCESS_ACTIVE = False
        progress.set_fraction(1.0 if ok else 0.0)
        close_button.set_sensitive(True)
        close_button.set_label("Close")
        append("\nProcess finished.\n" if ok else "\nProcess failed.\n")
        return False

    def pulse():
        if not state["active"] or not window.get_visible():
            return False
        progress.pulse()
        return True

    def prompt(kind, message, default, choices=None):
        result = {"value": None}
        done = threading.Event()

        def show():
            if kind == "confirm":
                dialog = Gtk.MessageDialog(
                    parent=window, flags=Gtk.DialogFlags.MODAL,
                    type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                    text=message)
                dialog.set_default_response(
                    Gtk.ResponseType.YES if default else Gtk.ResponseType.NO)
                response = dialog.run()
                dialog.destroy()
                result["value"] = (response == Gtk.ResponseType.YES if response in
                                   (Gtk.ResponseType.YES, Gtk.ResponseType.NO) else bool(default))
            elif kind == "choose":
                dialog = Gtk.Dialog(title="Ubuntu Backup", parent=window,
                                    flags=Gtk.DialogFlags.MODAL)
                dialog.set_default_size(560, 170)
                dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
                dialog.add_button("Continue", Gtk.ResponseType.OK)
                dialog.set_default_response(Gtk.ResponseType.OK)
                box = dialog.get_content_area()
                box.set_border_width(12)
                box.set_spacing(8)
                label = Gtk.Label(label=message, xalign=0, wrap=True)
                label.set_max_width_chars(72)
                box.add(label)
                chooser = Gtk.ComboBoxText()
                items = list(choices or [])
                for item in items:
                    chooser.append(item[0], item[1])
                active = next((index for index, item in enumerate(items)
                               if item[0] == default), 0)
                if items:
                    chooser.set_active(active)
                detail_label = Gtk.Label(xalign=0, wrap=True)
                detail_label.set_max_width_chars(72)

                def update_detail(_chooser=None):
                    index = chooser.get_active()
                    detail = (items[index][2] if 0 <= index < len(items)
                              and len(items[index]) > 2 else "")
                    if detail:
                        detail_label.set_markup(
                            "<small>" + GLib.markup_escape_text(detail) + "</small>")
                        detail_label.show()
                    else:
                        detail_label.set_text("")
                        detail_label.hide()

                chooser.connect("changed", update_detail)
                box.add(chooser)
                box.add(detail_label)
                box.show_all()
                update_detail()
                response = dialog.run()
                index = chooser.get_active()
                dialog.destroy()
                if response == Gtk.ResponseType.OK and 0 <= index < len(items):
                    result["value"] = items[index][0]
            else:
                dialog = Gtk.Dialog(title="Ubuntu Backup", parent=window,
                                    flags=Gtk.DialogFlags.MODAL)
                dialog.set_default_size(460, 150)
                dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
                dialog.add_button("Continue", Gtk.ResponseType.OK)
                dialog.set_default_response(Gtk.ResponseType.OK)
                box = dialog.get_content_area()
                box.set_border_width(12)
                box.set_spacing(8)
                label = Gtk.Label(label=message, xalign=0, wrap=True)
                label.set_max_width_chars(72)
                entry = Gtk.Entry()
                if default is not None:
                    entry.set_text(str(default))
                entry.set_activates_default(True)
                box.pack_start(label, False, False, 0)
                box.pack_start(entry, False, False, 0)
                box.show_all()
                response = dialog.run()
                value = entry.get_text()
                dialog.destroy()
                if response == Gtk.ResponseType.OK:
                    result["value"] = value.strip() if value.strip() else (
                        str(default) if default is not None else "")
            done.set()
            return False

        GLib.idle_add(show)
        done.wait()
        if result["value"] is None:
            raise Cancelled("Input cancelled.")
        return result["value"]

    def worker():
        global _GUI_PROMPT_HANDLER
        _GUI_PROMPT_HANDLER = prompt
        saved_env = {key: os.environ.get(key)
                     for key in ("SUDO_ASKPASS", "SUDO_ASKPASS_REQUIRE")}
        helper = next((path for path in (
            SCRIPT_PATH.parent / "ubuntu-backup-askpass",
            SCRIPT_PATH.parent / "ubuntu-backup-askpass.py",
        ) if path.is_file() and os.access(path, os.X_OK)), None)
        if helper is not None:
            os.environ["SUDO_ASKPASS"] = str(helper)
            os.environ["SUDO_ASKPASS_REQUIRE"] = "force"
        writer = Writer()
        ok = False
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                target()
            ok = True
        except Cancelled as exc:
            output.put("\nCancelled: " + str(exc) + "\n")
        except BaseException as exc:
            output.put(f"\n{type(exc).__name__}: {exc}\n")
        finally:
            _GUI_PROMPT_HANDLER = None
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            GLib.idle_add(finish, ok)

    window.connect("delete-event", lambda _window, _event: state["active"])
    window.show_all()
    GLib.timeout_add(100, drain)
    GLib.timeout_add(250, pulse)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def run_tray():
    """Top-bar status icon: flickers while running; menu shows live status/history."""
    import ctypes
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gio, GLib, Gtk

    try:
        lock_fd = os.open(KIT / ".tray.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 1

    def read_status():
        return _read_tray_status()

    stale_marks = set()

    def current_status():
        data = read_status()
        problem = _status_problem(data) if data else None
        if problem:
            key = (data.get("state"), data.get("pid"), data.get("updated"), problem)
            if key not in stale_marks:
                stale_marks.add(key)
                if data.get("state") == "running":
                    _write_status("cancelled", "cancelled", "Stale running backup: " + problem,
                                  diagnostic=problem, previous_pid=data.get("pid"),
                                  previous_updated=data.get("updated"))
                    _status_history("stale", "state=running pid=" + str(data.get("pid")) +
                                    " updated=" + str(data.get("updated")) + " reason=" + problem)
                else:
                    _write_status("failed", "inconsistent", "Inconsistent backup state: " + problem,
                                  diagnostic=problem)
                    _status_history("inconsistent", "state=" + str(data.get("state")) +
                                    " pid=" + str(data.get("pid")) + " updated=" + str(data.get("updated")) +
                                    " reason=" + problem)
            return {"state": "cancelled" if data.get("state") == "running" else "failed",
                    "phase": "cancelled" if data.get("state") == "running" else "inconsistent",
                    "message": problem}
        return data

    status_item = Gtk.MenuItem(label="Idle")
    status_item.set_sensitive(False)
    backup_item = Gtk.MenuItem(label="Backup")
    history_item = Gtk.MenuItem(label="History")
    restore_option = Gtk.MenuItem(label="Restore")
    drive_option = Gtk.MenuItem(label="Choose backup drive")
    purge_item = Gtk.MenuItem(label="Purge all data")
    options_menu = Gtk.Menu()
    options_menu.append(drive_option)
    options_menu.append(purge_item)
    options_item = Gtk.MenuItem(label="Options")
    options_item.set_submenu(options_menu)
    menu = Gtk.Menu()
    for item in (status_item, backup_item, Gtk.SeparatorMenuItem(), history_item,
                 restore_option, options_item):
        menu.append(item)
    menu.show_all()

    status_window = None
    status_text = None
    progress = None
    history_window = None
    history_view = None
    last_history = None
    quit_window = None
    quit_button = None
    quit_confirm_box = None
    quit_confirm_button = None
    quit_cancel_button = None

    def update_status():
        if status_window is None or not status_window.get_visible():
            return True
        data = current_status()
        if data.get("state") == "running":
            progress.pulse()
        elif data.get("state") == "complete":
            progress.set_fraction(1.0)
        else:
            progress.set_fraction(0.0)
        status_text.set_text(f"State: {data.get('state', 'idle')}\n"
                             f"Phase: {data.get('phase', '')}\n"
                             f"Updated: {data.get('updated', '')}\n"
                             f"{package_scan_summary()}\n\n"
                             f"{data.get('message', '')}")
        return True

    def update_history():
        nonlocal last_history
        if history_window is None or not history_window.get_visible():
            return True
        try:
            body = _history_path().read_text()
        except OSError:
            body = "No history yet."
        if body != last_history:
            last_history = body
            buffer = history_view.get_buffer()
            buffer.set_text(body)
            buffer.place_cursor(buffer.get_end_iter())
            history_view.scroll_to_mark(buffer.get_insert(), 0.0, True, 0.0, 1.0)
        return True

    def show_status(_item=None):
        nonlocal status_window, status_text, progress
        if status_window is None:
            status_window = Gtk.Window(title="Ubuntu Backup — Live status")
            status_window.set_default_size(520, 240)
            status_window.connect("delete-event", lambda w, e: (w.hide(), True)[1])
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_border_width(12)
            progress = Gtk.ProgressBar()
            status_text = Gtk.Label(xalign=0, yalign=0)
            status_text.set_selectable(True)
            box.pack_start(progress, False, False, 0)
            box.pack_start(status_text, True, True, 0)
            status_window.add(box)
            GLib.timeout_add(500, update_status)
        status_window.show_all()
        status_window.present()

    def show_history(_item=None):
        nonlocal history_window, history_view, last_history
        if history_window is None:
            history_window = Gtk.Window(title="Ubuntu Backup — History")
            history_window.set_default_size(780, 420)
            history_window.connect("delete-event", lambda w, e: (w.hide(), True)[1])
            history_view = Gtk.TextView(editable=False, monospace=True)
            scroll = Gtk.ScrolledWindow()
            scroll.add(history_view)
            history_window.add(scroll)
            last_history = None
            GLib.timeout_add(1000, update_history)
        history_window.show_all()
        history_window.present()
        update_history()
        buffer = history_view.get_buffer()
        buffer.place_cursor(buffer.get_end_iter())
        history_view.scroll_to_mark(buffer.get_insert(), 0.0, True, 0.0, 1.0)

    drive_cache = {"at": 0.0, "ready": False, "error": None}

    def drive_ready(force=False):
        import time
        now = time.monotonic()
        if not force and now - drive_cache["at"] < 30:
            return drive_cache["ready"]
        try:
            if not settings().get("external_backup"):
                ready, error = False, None
            else:
                validate_external_directory(external_directory(settings()))
                ready, error = True, None
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            ready, error = False, str(exc)
        drive_cache.update(at=now, ready=ready, error=error)
        return ready

    def backup_action(_item=None):
        data = current_status()
        if data.get("state") == "running":
            pid = data.get("pid")
            if isinstance(pid, int):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            return
        if not drive_ready(force=True):
            if drive_cache["error"]:
                message = "Backup cancelled: " + drive_cache["error"]
                _write_status("cancelled", "cancelled", message)
                _status_history("cancelled", message)
            GLib.idle_add(lambda: (show_drive_chooser(then_backup=True), False)[1])
            return
        show_status()
        run_process_window("Backup", lambda: backup_everything())

    def restore_action(_item=None):
        show_status()

        def run_restore():
            _write_status("running", "recovery", "Recovery started")
            _status_history("started", "Recovery started")
            try:
                wizard_recover()
            except BaseException:
                _write_status("failed", "failed", "Recovery failed")
                _status_history("failed", "Recovery failed")
                raise
            _write_status("idle", "idle", "Recovery finished")
            _status_history("idle", "Recovery finished")

        run_process_window("Restore", run_restore)

    def show_purge_dialog(_item=None):
        try:
            config = settings()
        except (OSError, ValueError, KeyError) as exc:
            _write_status("failed", "purge", "Cannot read settings: " + str(exc))
            return
        external = config.get("external_backup")
        external_text = "No external backup drive is configured."
        if external:
            try:
                directory = external_directory(config)
            except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError):
                external_text = ("The configured external drive is unavailable. "
                                 "Purge would stop before deleting anything; forget the drive first "
                                 "to purge local data only.")
            else:
                external_text = f"External backup data on {directory} will also be deleted."
        dialog = Gtk.Dialog(title="Purge all backup data", flags=0)
        dialog.set_modal(True)
        dialog.set_keep_above(True)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        dialog.set_resizable(False)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        purge_button = dialog.add_button("Purge all data", 1)
        purge_button.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        box = dialog.get_content_area()
        box.set_border_width(12)
        box.set_spacing(8)
        for text in (
            "This permanently deletes local snapshots, backups, history, logs, and settings.",
            external_text,
            "The drive itself is not formatted. This cannot be undone.",
        ):
            label = Gtk.Label(label=text, xalign=0, wrap=True)
            label.set_max_width_chars(72)
            box.add(label)
        box.show_all()
        response = dialog.run()
        dialog.destroy()
        if response != 1:
            return
        try:
            purge_all_data()
        except Exception as exc:
            _write_status("failed", "purge", "Purge failed: " + str(exc))
            _status_history("failed", "purge failed: " + str(exc))

    def show_drive_chooser(_item=None, then_backup=False):
        options = []
        for device in attached_drives():
            for mountpoint in device.get("mountpoints", []):
                options.append((f"{mountpoint}  ({device.get('fstype') or 'unknown'}, {device.get('size', 0)} bytes)", mountpoint))
        dialog = Gtk.Dialog(title="Ubuntu Backup — choose backup drive", flags=Gtk.DialogFlags.MODAL)
        dialog.set_default_size(680, 180)
        dialog.set_keep_above(True)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Forget current drive", 2)
        dialog.add_button("Prepare new drive", 3)
        dialog.add_button("Use drive", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        current = settings().get("external_backup")
        try:
            current_text = str(external_directory(settings())) if current else "unset"
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            current_text = "configured but unavailable"
        box.add(Gtk.Label(label="Current: " + current_text, xalign=0))
        box.add(Gtk.Label(label="Select an already-mounted filesystem. No disk is formatted or mounted.", xalign=0))
        chooser = Gtk.ComboBoxText()
        for label, _mount in options:
            chooser.append_text(label)
        if options:
            chooser.set_active(0)
        else:
            chooser.append_text("No mounted drives found")
            chooser.set_sensitive(False)
        box.add(chooser)
        box.show_all()
        dialog.present()
        response = dialog.run()
        selected = options[chooser.get_active()][1] if options and chooser.get_active() >= 0 else None
        dialog.destroy()
        if response == 3:
            run_process_window("Prepare drive", lambda: wizard_prepare_drive())
            return
        try:
            if response == 2:
                set_external(None, True)
                drive_cache["at"] = 0
                if current_status().get("state") != "running":
                    _write_status("idle", "drive", "Backup drive forgotten")
            elif response == Gtk.ResponseType.OK and selected:
                set_external(selected)
                drive_cache["at"] = 0
                if current_status().get("state") != "running":
                    _write_status("idle", "drive", "Backup drive selected: " + selected)
                if then_backup:
                    GLib.idle_add(lambda: (backup_action(), False)[1])
            else:
                return
        except Exception as exc:
            _write_status("failed", "drive", "Drive selection failed: " + str(exc))
            _status_history("inconsistent", "drive selection failed: " + str(exc))

    quit_state = {"confirming": False, "timer": None}
    quit_focus = {"ready": False}

    def reset_quit(hide=False):
        if quit_state["timer"] is not None:
            GLib.source_remove(quit_state["timer"])
            quit_state["timer"] = None
        quit_state["confirming"] = False
        if quit_confirm_box is not None:
            quit_confirm_box.hide()
        if quit_button is not None:
            quit_button.show()
        if hide and quit_window is not None:
            quit_focus["ready"] = False
            quit_window.hide()
        return False

    def ask_confirm(_item=None):
        quit_state["confirming"] = True
        if quit_button is not None:
            quit_button.hide()
        if quit_confirm_box is not None:
            quit_confirm_box.show_all()
        if quit_state["timer"] is not None:
            GLib.source_remove(quit_state["timer"])
        quit_state["timer"] = GLib.timeout_add_seconds(5, lambda: reset_quit(hide=True))
        return False

    def quit_focus_ready():
        quit_focus["ready"] = True
        return False

    def quit_focus_lost(_window, _event):
        if quit_focus["ready"]:
            reset_quit(hide=True)
        return False

    def show_quit_menu():
        nonlocal quit_window, quit_button, quit_confirm_box, quit_confirm_button, quit_cancel_button
        if quit_window is not None and quit_window.get_visible():
            reset_quit(hide=True)
            return False
        if quit_window is None:
            quit_window = Gtk.Window(title="Ubuntu Backup", type=Gtk.WindowType.TOPLEVEL)
            quit_window.set_decorated(False)
            quit_window.set_resizable(False)
            quit_window.set_skip_taskbar_hint(True)
            quit_window.set_skip_pager_hint(True)
            quit_window.set_keep_above(True)
            quit_window.set_accept_focus(True)
            quit_window.set_position(Gtk.WindowPosition.MOUSE)
            quit_window.set_name("ubuntu-backup-quit")
            css = Gtk.CssProvider()
            css.load_from_data(b"""
                #ubuntu-backup-quit-box {
                    background-color: @theme_bg_color;
                    border: 1px solid alpha(@theme_fg_color, 0.18);
                    border-radius: 10px;
                    padding: 10px;
                }
            """)
            Gtk.StyleContext.add_provider_for_screen(quit_window.get_screen(), css,
                                                     Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_name("ubuntu-backup-quit-box")
            quit_button = Gtk.Button(label="Quit")
            quit_button.connect("clicked", ask_confirm)
            quit_confirm_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            quit_confirm_button = Gtk.Button(label="Confirm")
            quit_cancel_button = Gtk.Button(label="Cancel")
            quit_confirm_button.connect("clicked", lambda _button: Gtk.main_quit())
            quit_cancel_button.connect("clicked", lambda _button: reset_quit(hide=True))
            quit_confirm_box.pack_start(quit_confirm_button, True, True, 0)
            quit_confirm_box.pack_start(quit_cancel_button, True, True, 0)
            box.pack_start(quit_button, False, False, 0)
            box.pack_start(quit_confirm_box, False, False, 0)
            quit_window.add(box)
            quit_window.connect("focus-out-event", quit_focus_lost)
            quit_window.connect("delete-event", lambda window, _event: (reset_quit(hide=True), True)[1])
            quit_window.show_all()
            quit_window.hide()
        reset_quit()
        quit_focus["ready"] = False
        GLib.timeout_add(250, quit_focus_ready)
        quit_window.show()
        quit_window.present()
        return False

    backup_item.connect("activate", backup_action)
    restore_option.connect("activate", restore_action)
    drive_option.connect("activate", show_drive_chooser)
    purge_item.connect("activate", show_purge_dialog)
    history_item.connect("activate", show_history)

    # The patched AppIndicator host calls this service on right-click so the
    # full native menu can stay on left-click.
    tray_interface = """
<node>
  <interface name="org.ubuntu.Backup">
    <method name="ShowStatus"/>
    <method name="ShowQuitMenu"/>
  </interface>
</node>
"""
    tray_path = "/org/ubuntu/Backup"

    def tray_method(_connection, _sender, _path, _interface, method, _parameters, invocation):
        try:
            if method == "ShowStatus":
                show_status()
            elif method == "ShowQuitMenu":
                GLib.idle_add(show_quit_menu)
        except Exception as error:
            print(f"Tray service failed: {error}", file=sys.stderr)
        finally:
            invocation.return_value(None)

    def tray_bus_acquired(connection, _name):
        node = Gio.DBusNodeInfo.new_for_xml(tray_interface)
        connection.register_object(tray_path, node.interfaces[0], tray_method, None, None)

    def tray_name_acquired(_connection, _name):
        pass

    def tray_name_lost(_connection, _name):
        print("Tray status service name lost.", file=sys.stderr)

    Gio.bus_own_name(Gio.BusType.SESSION, "org.ubuntu.Backup", Gio.BusNameOwnerFlags.NONE,
                     tray_bus_acquired, tray_name_acquired, tray_name_lost)

    provider = None
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
        indicator = AppIndicator.Indicator.new(
            "ubuntu-backup", "media-record-symbolic",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        indicator.set_title("Ubuntu Backup")
        indicator.set_menu(menu)
        indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        provider = ("gi-indicator", indicator, AppIndicator)
    except Exception as error:  # Typelib missing, version mismatch, or binding failure.
        print(f"AppIndicator GI unavailable ({type(error).__name__}); using ctypes fallback.", file=sys.stderr)
        try:
            lib = None
            for library in ("libayatana-appindicator3.so.1", "libappindicator3.so.1"):
                try:
                    lib = ctypes.CDLL(library)
                    break
                except OSError:
                    continue
            if lib is None:
                raise OSError("AppIndicator library not found")

            class PyCapsule(ctypes.Structure):
                _fields_ = [("ob_refcnt", ctypes.c_ssize_t), ("ob_type", ctypes.c_void_p),
                            ("pointer", ctypes.c_void_p), ("name", ctypes.c_char_p),
                            ("context", ctypes.c_void_p)]

            def pointer(obj):
                return PyCapsule.from_address(id(obj.__gpointer__)).pointer

            lib.app_indicator_new.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            lib.app_indicator_new.restype = ctypes.c_void_p
            lib.app_indicator_set_status.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.app_indicator_set_menu.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            lib.app_indicator_set_icon_full.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
            lib.app_indicator_set_title.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            indicator = lib.app_indicator_new(b"ubuntu-backup", b"media-record-symbolic", 0)
            if not indicator:
                raise OSError("AppIndicator construction failed")
            menu_pointer = pointer(menu)
            if not menu_pointer:
                raise OSError("AppIndicator menu pointer unavailable")
            lib.app_indicator_set_title(indicator, b"Ubuntu Backup")
            lib.app_indicator_set_menu(indicator, menu_pointer)
            lib.app_indicator_set_status(indicator, 1)
            provider = ("indicator", indicator, lib)
        except (OSError, AttributeError, TypeError, ValueError) as error:
            print(f"AppIndicator ctypes unavailable ({type(error).__name__}); using legacy status icon.", file=sys.stderr)
            icon = Gtk.StatusIcon.new_from_icon_name("media-record-symbolic")
            icon.set_tooltip_text("Ubuntu Backup")
            icon.connect("popup-menu", lambda _i, button, activate_time: menu.popup(None, None, None, None, button, activate_time))
            icon.connect("activate", show_status)
            icon.set_visible(True)
            provider = ("statusicon", icon, None)

    last_icon = [None]

    def set_icon(name):
        if name == last_icon[0]:
            return
        last_icon[0] = name
        if provider[0] == "gi-indicator":
            provider[1].set_icon_full(name, "Ubuntu Backup")
        elif provider[0] == "indicator":
            provider[2].app_indicator_set_icon_full(provider[1], name.encode(), b"Ubuntu Backup")
        else:
            provider[1].set_from_icon_name(name)

    toggle = [False]

    def refresh():
        data = current_status()
        running = data.get("state") == "running"
        label = data.get("message") or data.get("state", "idle")
        label = label[:90]
        if status_item.get_label() != label:
            status_item.set_label(label)
        if running:
            toggle[0] = not toggle[0]
            set_icon("process-working-symbolic" if toggle[0] else "media-record-symbolic")
        else:
            state = data.get("state")
            set_icon("dialog-warning-symbolic" if data.get("warnings") and state == "complete" else
                     "dialog-information-symbolic" if state == "complete" else
                     "dialog-error-symbolic" if state == "failed" else
                     "dialog-warning-symbolic" if state == "cancelled" else "media-record-symbolic")
        return True

    GLib.timeout_add(750, refresh)
    refresh()
    Gtk.main()
    return 0


# BEGIN UBUNTU CONFIGURATION PROFILE
EMBEDDED_PROFILE = ''
EMBEDDED_PROFILE_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
# END UBUNTU CONFIGURATION PROFILE

if __name__ == "__main__":
    try:
        main()
    except Cancelled as exc:
        print(str(exc), file=sys.stderr)
    except KeyboardInterrupt:
        print("\nCancelled. Completed actions, if any, are retained.", file=sys.stderr)
        sys.exit(130)
    except (ConfigError, OSError, ValueError, KeyError, TypeError, RuntimeError,
            tarfile.TarError, EOFError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if _EXIT_CODE:
        sys.exit(_EXIT_CODE)
