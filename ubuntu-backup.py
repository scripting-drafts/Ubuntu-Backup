#!/usr/bin/python3 -I
# Ubuntu configuration: self-contained interactive kit
# Contains personal configuration data. PRIVATE; encoded, NOT encrypted.
"""Capture and redeploy a private, upgrade-conscious Ubuntu configuration kit."""

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid


KIT = Path(__file__).resolve().parents[1]
HOME = Path.home()
FORMAT = "ubuntu-configuration-v1"
UNIT = "ubuntu-configuration-backup"
MARKER = "# Managed by ubuntu-configuration v1"
WORKSPACE_README = '# Ubuntu configuration and backup\n\n- Live tool: `~/ubuntu-backup.py` (private, self-contained, not encrypted).\n- Workspace: keep its settings, snapshots/timelines and rollback journals.\n- Run `./ubuntu-backup.py` for the menu; `./ubuntu-backup.py --help` shows full docs.\n- One backup action captures configuration and full `/home`; identical files are stored once.\n- Full recovery stages configuration or full `/home`; live `/home` is never overwritten.\n- External copies use dated symlink timelines over shared baselines.\n- Full `/home` capture is manual, external-drive only, requires sudo approval, and is not auto-pruned.\n- Snapshots contain secrets and are not encrypted. Do not publish them.\n'
ARCHIVE_RE = re.compile(r"ubuntu-config-\d{8}T\d{6}\.\d{6}Z\.tar\.gz\Z")
PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?\Z")
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
SYSTEM_CONFIG_ROOTS = ("/etc", "/usr/local/etc")
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


def write_json(path, value):
    write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def settings():
    value = json.loads((KIT / "config/settings.json").read_text())
    if not isinstance(value["keep_snapshots"], int) or value["keep_snapshots"] < 1:
        raise ConfigError("keep_snapshots must be a positive integer.")
    return value


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


def collect_inventory(destination, config, report):
    mkdir(destination)
    write_json(destination / "hardware.json", hardware_inventory())
    native_arch = capture_command(["dpkg", "--print-architecture"], report).strip()
    commands = {
        "apt-manual.txt": ["apt-mark", "showmanual"],
        "apt-held.txt": ["apt-mark", "showhold"],
        "dpkg-packages.tsv": ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n"],
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
    for line in outputs["dpkg-packages.tsv"].splitlines():
        columns = line.split("\t")
        if len(columns) == 4 and columns[3][1:2] == "i":
            installed[columns[0]] = columns[1]
    manual = set(outputs["apt-manual.txt"].splitlines())
    portable, review = [], []
    for name in sorted(installed):
        if name not in manual and name.split(":")[0] not in manual:
            continue
        portable_name = name.removesuffix(":" + native_arch) if native_arch else name
        target = review if any(fnmatch.fnmatch(portable_name, pattern) for pattern in config["apt_review_only"]) else portable
        target.append(portable_name)
    write(destination / "apt-portable.txt", "\n".join(portable) + "\n")
    write(destination / "apt-review-only.txt", "\n".join(review) + "\n")
    snaps = []
    for line in outputs["snap-list.txt"].splitlines()[1:]:
        fields = line.split()
        if len(fields) < 6:
            continue
        name, _, _, channel, _, notes = fields[:6]
        if name in ("bare", "snapd", "gtk-common-themes") or re.match(r"^(core\d*|gnome-.*|mesa-.*)$", name):
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
    write_json(destination / "source-system.json", {
        "os": read_os_release(), "hostname": socket.gethostname(), "home": str(HOME),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
        "architecture": native_arch,
    })
    if not installed or not manual:
        raise ConfigError("Cannot capture installed/manual APT packages; snapshot aborted.")


def copy_selection(source, destination, relative, config, report, system=False, skip_kit=False, root_device=None):
    if skip_kit and (source == KIT or KIT in source.parents):
        return
    if any(fnmatch.fnmatch(relative, pattern) for pattern in config["exclude"]):
        return
    if config.get("coverage") == "system-wide-configuration" and any(part.lower() in CONFIG_DATA_DIRS for part in PurePosixPath(relative).parts):
        report.setdefault("excluded_cache_data", []).append(str(source))
        return
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
            raise ConfigError(f"Configuration exceeds the 64 MiB per-file safety limit: {source}; no partial snapshot accepted.")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ConfigError(f"Source changed type: {source}")
            data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ConfigError(f"Source grew beyond safety limit: {source}")
        report["bytes_captured"] += len(data)
        if report["bytes_captured"] > MAX_TOTAL:
            raise ConfigError("Selected configurations exceed the 1 GiB safety limit; review the cache/data exclusions.")
        write(destination, data, executable=bool(info.st_mode & 0o111))
    except FileNotFoundError:
        return
    except PermissionError:
        if not system:
            raise ConfigError(f"Cannot read selected home configuration: {source}") from None
        report["warnings"].append(f"Root-only system reference not captured: {source}")


def build_snapshot(destination, config):
    config = dict(config)
    if config.get("coverage") == "system-wide-configuration":
        config["home_include"] = sorted(set(config["home_include"] + configuration_home_paths(HOME)))
        # Children of a selected directory are already recursive. Avoid duplicate reads/copies.
        config["home_include"] = [name for name in config["home_include"] if not any(
            name.startswith(parent + "/") for parent in config["home_include"] if parent != name and "*" not in parent)]
        config["system_reference_only"] = list(SYSTEM_CONFIG_ROOTS)
    report = {"warnings": [], "unavailable_commands": [], "symlinks_not_followed": [], "bytes_captured": 0}
    collect_inventory(destination / "inventory", config, report)
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
                write(target, source.read_bytes(), bool(record['executable']))
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


def publish_object(objects, incoming, temporary, sha256, size, created):
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


def create_linked_config_snapshot(snapshot, root):
    """Store each configuration file once and publish a dated symlink tree."""
    if not DATED_RE.fullmatch(snapshot.name):
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
        result = timelines / snapshot.name
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
                             "PATH,TYPE,TRAN,RM,SIZE,FSTYPE,UUID,MOUNTPOINTS"],
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
                                "mountpoints": [p for p in (node.get("mountpoints") or []) if p and not p.startswith("[")]})
            visit(node.get("children") or [], node_transport, node_removable)
    # Preserve lsblk discovery order. It does not provide connection timestamps.
    for node in json.loads(result.stdout).get("blockdevices", []):
        visit([node])
    return devices


def saved_drive_location(external):
    """Relocate only the already-approved filesystem; never pick a different disk."""
    candidates = []
    original = external["mount_point"]
    for device in attached_drives():
        if device.get("uuid") == external["filesystem_uuid"]:
            candidates.extend(device["mountpoints"])
    # Try the previously recorded location too; verifies mount, UUID and marker below.
    candidates = list(dict.fromkeys([original, *candidates]))
    matches = []
    for name in candidates:
        try:
            mount = Path(name)
            if mount_info(mount)["uuid"] != external["filesystem_uuid"]:
                continue
            subdirectory = external.get("subdirectory", "")
            directory = checked_path(mount, subdirectory) if subdirectory else mount
            marker = directory / ".ubuntu-configuration-target.json"
            if directory.stat().st_mode & 0o077 or marker.is_symlink():
                continue
            value = json.loads(marker.read_text())
            if value.get("format") == FORMAT and value.get("id") == external["marker_id"]:
                matches.append(directory)
        except (ConfigError, OSError, ValueError, KeyError, TypeError):
            continue
    if len(matches) != 1:
        raise ConfigError("Saved backup drive is unavailable or ambiguous; matching UUID AND marker are required.")
    return matches[0]


def external_directory(config):
    external = config["external_backup"]
    if not external:
        raise ConfigError("No external backup drive is configured.")
    return saved_drive_location(external)


@contextmanager
def pinned_external(config):
    """Open and verify the selected drive; return its pinned root and display path."""
    directory = external_directory(config)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_dev in (HOME.stat().st_dev, Path("/").stat().st_dev):
            raise ConfigError("Backup drive disappeared; refusing writes onto the source filesystem.")
        pinned = Path("/proc/self/fd") / str(fd)
        marker = json.loads((pinned / ".ubuntu-configuration-target.json").read_text())
        if marker.get("format") != FORMAT or marker.get("id") != config["external_backup"]["marker_id"]:
            raise ConfigError("Pinned backup drive marker does not match.")
        yield pinned, directory
    finally:
        os.close(fd)


def copy_external_snapshot(snapshot, config):
    """Publish a dated external configuration timeline with one shared baseline."""
    with pinned_external(config) as (root, directory):
        result, created, shared = create_linked_config_snapshot(snapshot, root)
        prune_linked_config(root, config["keep_snapshots"])
        display = directory / result.relative_to(root)
    print(f"External linked snapshot verified: {display} ({created} new objects, {shared} files shared)")


def copy_external(path, config, filename=None):
    directory = external_directory(config)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    # A pinned directory stays on the selected filesystem even if the drive is unmounted.
    pinned = Path("/proc/self/fd") / str(directory_fd)
    temporary = None
    try:
        if os.fstat(directory_fd).st_dev in (HOME.stat().st_dev, Path("/").stat().st_dev):
            raise ConfigError("Backup drive disappeared; refusing a copy onto the source filesystem.")
        marker = json.loads((pinned / ".ubuntu-configuration-target.json").read_text())
        if marker["id"] != config["external_backup"]["marker_id"]:
            raise ConfigError("Pinned backup directory marker does not match.")
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


def set_external(mount_point, disable=False):
    config = settings()
    if disable:
        config["external_backup"] = None
    else:
        if not mount_point:
            raise ConfigError("Supply the mount point of the drive AFTER you prepare and mount it.")
        mount = Path(mount_point).expanduser().resolve()
        information = mount_info(mount)
        subdirectory = ""
        directory = mount
        marker = directory / ".ubuntu-configuration-target.json"
        if directory.exists() and any(directory.iterdir()) and not marker.is_file():
            raise ConfigError("Refusing to adopt a nonempty directory without a backup marker.")
        mkdir(directory)
        directory.chmod(0o700)
        if directory.stat().st_mode & 0o077:
            raise ConfigError("Drive cannot enforce private Unix permissions. Use an encrypted/Linux filesystem.")
        if marker.is_symlink():
            raise ConfigError("Drive marker must not be a symlink.")
        if marker.exists():
            existing = json.loads(marker.read_text())
            if existing.get("format") != FORMAT or not isinstance(existing.get("id"), str):
                raise ConfigError("Refusing an unrecognized backup drive marker.")
            marker_id = existing["id"]
        else:
            marker_id = str(uuid.uuid4())
        write_json(marker, {"format": FORMAT, "id": marker_id})
        config["external_backup"] = {"mount_point": str(mount), "filesystem_uuid": information["uuid"],
                                     "subdirectory": subdirectory, "marker_id": marker_id}
    write_json(KIT / "config/settings.json", config)
    print("External backup disabled; existing drive data was not removed." if disable else
          "Backup drive configured at its root. No disk was formatted; the next backup will also copy there.")


def backup(skip_external=False):
    config = settings()
    mkdir(KIT / "backups")
    lock_fd = os.open(KIT / ".backup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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
        try:
            report = build_snapshot(stage, config)
            verify_tree(stage)
            path, shared = create_dated_snapshot(stage, KIT / "backups")
            if current.exists():
                current.rename(previous)
            stage.rename(current)
            fsync_directory(KIT)
            if previous.exists():
                shutil.rmtree(previous)
            prune_dated(path.parent, config["keep_snapshots"])
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        print(f"Local dated backup verified: {path} ({shared} files shared with previous snapshot)")
        print(f"Capture warnings: {len(report['warnings'])}; details: snapshot/inventory/capture-report.json")
        if skip_external:
            print("External copy skipped for this run; saved target was not changed.")
        elif config["external_backup"] is None:
            print("External backup drive: not configured (local backups only).")
        else:
            try:
                copy_external_snapshot(path, config)
            except (ConfigError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
                raise ConfigError(f"Local backup succeeded, but external linked copy failed: {exc}") from None
    return path


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
    if path.is_dir() and not path.is_symlink():
        if manifest.get("storage") == LINKED_STORAGE:
            sources = [safe_join(path, record["path"], allow_final_symlink=True) for record in manifest["files"]]
        else:
            sources = regular_files(path)
        for source in sources:
            name = source.relative_to(path).as_posix()
            write(checked_path(destination, name), source.read_bytes(),
                  executable=records.get(name, {}).get("executable", False))
        if manifest.get("storage") == LINKED_STORAGE:
            write_json(destination / "manifest.json", dict(manifest, storage="local-snapshot"))
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                executable = records.get(member.name, {}).get("executable", False)
                with archive.extractfile(member) as stream:
                    write(checked_path(destination, member.name), stream.read(), executable=executable)
    verify_tree(destination)
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


def apt_plan(names):
    if not names:
        return [], []
    if not all(PACKAGE_RE.fullmatch(name) for name in names):
        raise ConfigError("Invalid package name in saved inventory.")
    native_arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True,
                                 text=True, check=True).stdout.strip()
    aliases = {name: name.removesuffix(":" + native_arch) for name in names}
    result = subprocess.run(["apt-cache", "policy", *aliases.values()], capture_output=True, text=True, check=True,
                            env=dict(os.environ, LC_ALL="C.UTF-8"))
    available, package = set(), None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            package = line[:-1]
        elif line.strip().startswith("Candidate:") and line.split(":", 1)[1].strip() != "(none)":
            if package:
                available.add(package)
    supported = [name for name in names if name in available or aliases[name] in available]
    return supported, sorted(set(names) - set(supported))


def deploy(args):
    source = Path(args.snapshot).expanduser().resolve() if args.snapshot else default_snapshot()
    manifest = verify_tree(source)
    records = [record for record in manifest["files"] if record["path"].startswith("home/")]
    package_names = (source / "inventory/apt-portable.txt").read_text().split()
    supported, unavailable = apt_plan(package_names) if args.packages else ([], [])
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
    plan = {"source": str(source), "home_files_to_write": changed, "unchanged": unchanged,
            "conflicts_skipped": conflicts, "apt_available": supported, "apt_unavailable": unavailable,
            "system_files_automatically_restored": [], "apply": args.apply,
            "snap_install_requested": args.snaps, "flatpak_install_requested": args.flatpaks,
            "desktop_settings_requested": args.desktop_settings}
    write_json(KIT / "plans/last-deploy.json", plan)
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(changed)} home files to write; "
          f"{len(unchanged)} unchanged; {len(conflicts)} conflicts skipped.")
    if args.packages:
        print(f"APT: {len(supported)} available; {len(unavailable)} unavailable/review required. No versions pinned.")
    if args.snaps or args.flatpaks or args.desktop_settings:
        print(f"Optional actions: snaps={args.snaps}, flatpaks={args.flatpaks}, desktop settings={args.desktop_settings}")
    print(f"Full plan: {KIT / 'plans/last-deploy.json'}")
    old_home = json.loads((source / "inventory/source-system.json").read_text())["home"]
    if old_home != str(HOME):
        print(f"Warning: old home was {old_home}; review absolute paths in scripts and launchers.")
    if not args.apply:
        print("Nothing installed or applied. Use --apply; add --overwrite only to replace conflicting files.")
        return
    if args.packages and supported:
        subprocess.run(["sudo", "apt-get", "install", "--no-remove", "--no-install-recommends", "--", *supported], check=True)
    if args.snaps:
        for app in json.loads((source / "inventory/snap-apps.json").read_text()):
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", app["name"]) or not re.fullmatch(r"[A-Za-z0-9./_-]+", app["channel"]):
                raise ConfigError("Invalid snap inventory.")
            command = ["sudo", "snap", "install", app["name"], "--channel=" + app["channel"]]
            if app["classic"]:
                command.append("--classic")
            subprocess.run(command, check=True)
    if args.flatpaks:
        remotes = subprocess.run(["flatpak", "remotes", "--user", "--columns=name"], capture_output=True, text=True, check=True).stdout.splitlines()
        for line in (source / "inventory/flatpak-apps.tsv").read_text().splitlines():
            columns = line.split("\t")
            if len(columns) < 3 or columns[1] not in remotes:
                print("Skipping Flatpak app: configure its trusted user remote first.")
                continue
            app, remote, branch = columns[:3]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", app) or not re.fullmatch(r"[A-Za-z0-9_.-]+", branch):
                raise ConfigError("Invalid Flatpak inventory.")
            subprocess.run(["flatpak", "install", "--user", "--noninteractive", "--", remote, app + "//" + branch], check=True)
    rollback = KIT / "rollback" / timestamp()
    for relative in changed:
        target = checked_path(HOME, relative)
        if target.exists():
            write(rollback / "home" / relative, target.read_bytes(), bool(target.stat().st_mode & 0o111))
        record = next(item for item in records if item["path"] == "home/" + relative)
        write(target, (source / record["path"]).read_bytes(), record["executable"])
    if changed:
        write_json(rollback / "changes.json", {"files_written": changed, "previous_files": "home/"})
        print(f"Pre-deploy copies of replaced files: {rollback}")
    if args.desktop_settings:
        before = subprocess.run(["dconf", "dump", "/"], capture_output=True, check=True).stdout
        write(rollback / "dconf-before.ini", before)
        subprocess.run(["dconf", "load", "/"], input=(source / "inventory/dconf.ini").read_bytes(), check=True)
    print("Deployment complete. System references, service enablement, drivers, repositories and disks were NOT replayed.")


def systemd_units():
    script = str(KIT / "scripts/ubuntu-config.py").replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    service = f"""{MARKER}
[Unit]
Description=Private Ubuntu configuration snapshot

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I "{script}" backup
UMask=0077
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
TimeoutStartSec=10min
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


EMBEDDED_DEFAULT_SETTINGS = {'coverage': 'system-wide-configuration', 'keep_snapshots': 30, 'external_backup': None, 'home_include': ['.bashrc', '.bash_logout', '.bash_aliases', '.profile', '.inputrc', '.gitconfig', '.Xresources', '.xprofile', '.xsessionrc', '.config/autostart', '.config/systemd/user', '.config/mimeapps.list', '.config/user-dirs.dirs', '.config/user-dirs.locale', '.config/fontconfig', '.config/gtk-3.0/settings.ini', '.config/gtk-3.0/gtk.css', '.config/gtk-4.0/settings.ini', '.config/gtk-4.0/gtk.css', '.config/Code/User/settings.json', '.config/Code/User/keybindings.json', '.config/Code/User/snippets', '.config/sublime-text/Packages/User', '.config/libreoffice/4/user/registrymodifications.xcu', '.local/share/applications', '.local/share/fonts', '.local/share/gnome-shell/extensions', '.local/share/themes', '.local/share/icons', '.fonts', '.themes', '.icons', 'bin', '*.sh', '*.py'], 'home_reference_only': ['.local/bin', '.nvidia-settings-rc', '.config/monitors.xml'], 'system_reference_only': ['/etc/default/keyboard', '/etc/default/locale', '/etc/environment', '/etc/locale.gen', '/etc/timezone', '/etc/apt/sources.list', '/etc/apt/sources.list.d', '/etc/apt/preferences.d', '/etc/apt/keyrings', '/etc/sysctl.conf', '/etc/sysctl.d', '/etc/modprobe.d', '/etc/modules-load.d', '/etc/udev/rules.d', '/etc/security/limits.conf', '/etc/security/limits.d', '/etc/systemd/system', '/etc/systemd/journald.conf', '/etc/systemd/journald.conf.d', '/etc/systemd/logind.conf', '/etc/systemd/logind.conf.d', '/etc/docker/daemon.json', '/etc/containerd/config.toml', '/etc/ssh/sshd_config', '/etc/ssh/sshd_config.d', '/etc/cron.d'], 'exclude': ['**/__pycache__', '**/__pycache__/*', '**/*.pyc', '**/.git', '**/.git/*', '.ssh', '.ssh/*', '.gnupg', '.gnupg/*', '.kube', '.kube/*', '.git-credentials', '.config/gh/*', '.docker/config.json', '.config/systemd/user/ubuntu-configuration-backup.*', '.config/systemd/user/timers.target.wants/ubuntu-configuration-backup.timer', 'etc/systemd/system/snap*', 'etc/apt/auth.conf', 'etc/apt/auth.conf.d/*'], 'apt_review_only': ['linux-*', 'grub*', 'shim*', 'nvidia-*', 'libnvidia-*', 'cuda*', 'nsight*', 'firmware-*', 'oem-*', 'efibootmgr', 'os-prober', 'mokutil']}
EMBEDDED_README = 'Ubuntu Backup: self-contained interactive configuration backup and restore.\nNo personal configuration snapshot or backup data is bundled in this repository.\nGenerated snapshots and exports contain secrets and are NOT encrypted.\nOne backup action captures configuration and full /home; identical files are stored once.\nFull recovery stages configuration or full /home; live /home is never overwritten.\n'
EMBEDDED_GITIGNORE = '*\n!ubuntu-backup.py\n!README.md\n!.gitignore\n'
EMBEDDED_ROOT_EXPORTER = '#!/usr/bin/python3 -I\n"""System-wide CONFIGURATION capture; root reads, owner writes. Never a disk image."""\n# Managed by ubuntu-configuration system capture v1\nimport argparse\nimport datetime as dt\nimport hashlib\nimport io\nimport json\nimport os\nfrom pathlib import Path\nimport pwd\nimport stat\nimport subprocess\nimport sys\nimport tarfile\nimport tempfile\n\nMARKER = "# Managed by ubuntu-configuration system capture v1"\nUNIT = "ubuntu-configuration-system"\nHELPER = Path("/usr/local/libexec/ubuntu-configuration-system-capture.py")\nROOTS = ("/etc", "/usr/local/etc")\nMAX_FILE, MAX_TOTAL = 64 * 1024 * 1024, 1024 * 1024 * 1024\nDATA_DIRS = {"cache", "caches", "gpucache", "code cache", "cacheddata", "cachedextensions", "cachedextensionvsixs",\n             "crash reports", "crashpad", "logs", "history", "workspacestorage", "globalstorage",\n             "indexeddb", "local storage", "session storage", "service worker", "blob_storage", "__pycache__"}\nHOME_PATHS = (".config", ".local/share/applications", ".local/share/fonts", ".local/share/themes", ".local/share/icons",\n              ".local/share/gnome-shell/extensions", ".fonts", ".themes", ".icons", ".ssh/config", ".kube/config",\n              ".gnupg/gpg.conf", ".gnupg/gpg-agent.conf", ".docker/config.json", ".aws/config")\nHISTORIES = {".bash_history", ".python_history", ".scala_history", ".dotty_history", ".lesshst", ".wget-hsts",\n             ".sudo_as_admin_successful", ".Xauthority", ".ICEauthority", ".DS_Store"}\n\n\ndef collect(owner_uid, accounts=None, system_roots=ROOTS):\n    """Pinned descriptors, unlimited depth, no symlinks or nested mounts followed."""\n    files, metadata, links, omissions, excluded = {}, {}, [], [], []\n    total = 0\n\n    def scan(parent_fd, name, prefix, device=None):\n        nonlocal total\n        if name.lower() in DATA_DIRS:\n            excluded.append(prefix)\n            return\n        try:\n            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)\n            if stat.S_ISLNK(info.st_mode):\n                links.append({"path": prefix, "target": os.readlink(name, dir_fd=parent_fd)})\n                return\n            if stat.S_ISDIR(info.st_mode):\n                if device is not None and info.st_dev != device:\n                    excluded.append(prefix + " (nested mount)")\n                    return\n                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)\n                try:\n                    with os.scandir(fd) as entries:\n                        names = sorted(entry.name for entry in entries)\n                    for child in names:\n                        scan(fd, child, prefix + "/" + child, info.st_dev)\n                finally:\n                    os.close(fd)\n                return\n            if not stat.S_ISREG(info.st_mode):\n                excluded.append(prefix + " (runtime/special file)")\n                return\n            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)\n            with os.fdopen(fd, "rb") as source:\n                before = os.fstat(source.fileno())\n                if not stat.S_ISREG(before.st_mode):\n                    raise RuntimeError("Configuration changed type: " + prefix)\n                data = source.read(MAX_FILE + 1)\n                after = os.fstat(source.fileno())\n            if len(data) > MAX_FILE:\n                raise RuntimeError("Configuration exceeds 64 MiB; capture aborted: " + prefix)\n            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):\n                raise RuntimeError("Configuration changed during read; retry capture: " + prefix)\n            total += len(data)\n            if total > MAX_TOTAL:\n                raise RuntimeError("Configuration exceeds 1 GiB; review excluded application data before retrying.")\n            files[prefix] = data\n            metadata[prefix] = {"mode": stat.S_IMODE(before.st_mode), "uid": before.st_uid, "gid": before.st_gid}\n        except FileNotFoundError:\n            return\n        except PermissionError:\n            omissions.append(prefix + " (unreadable even with current privileges)")\n\n    for root in system_roots:\n        path = Path(root)\n        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n        try:\n            scan(parent, path.name, "system/" + path.as_posix().lstrip("/"))\n        finally:\n            os.close(parent)\n    chosen = []\n    for account in accounts if accounts is not None else pwd.getpwall():\n        if account.pw_uid not in (0, owner_uid) and not (1000 <= account.pw_uid < 65534 and account.pw_shell not in ("/bin/false", "/usr/sbin/nologin", "/sbin/nologin")):\n            continue\n        home = Path(account.pw_dir)\n        if not home.is_dir() or home.is_symlink():\n            omissions.append(f"Account {account.pw_uid}: home missing or symlinked")\n            continue\n        chosen.append({"uid": account.pw_uid, "name": account.pw_name, "home": str(home)})\n        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n        try:\n            prefix = "home" if account.pw_uid == owner_uid else f"accounts/{account.pw_uid}/home"\n            paths = set(HOME_PATHS)\n            with os.scandir(fd) as entries:\n                paths.update(entry.name for entry in entries if entry.name.startswith(".") and entry.is_file(follow_symlinks=False)\n                             and entry.name not in HISTORIES and not entry.name.endswith((".swp", ".swo", "~"))\n                             and not entry.name.startswith(".xsession-errors"))\n            for relative in sorted(paths):\n                parent = os.dup(fd)\n                try:\n                    parts = Path(relative).parts\n                    for part in parts[:-1]:\n                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)\n                        os.close(parent)\n                        parent = child\n                    scan(parent, parts[-1], prefix + "/" + relative)\n                except FileNotFoundError:\n                    pass\n                except OSError as exc:\n                    omissions.append(prefix + "/" + relative + " (ancestor unavailable: " + exc.strerror + ")")\n                finally:\n                    os.close(parent)\n        finally:\n            os.close(fd)\n    files["inventory/privileged-links.json"] = (json.dumps(links, indent=2) + "\\n").encode()\n    files["inventory/privileged-source-metadata.json"] = (json.dumps(metadata, indent=2) + "\\n").encode()\n    files["inventory/privileged-exclusions.json"] = (json.dumps(excluded, indent=2) + "\\n").encode()\n    return files, {"accounts": chosen, "roots": list(system_roots), "omissions": omissions}\n\n\ndef units(owner_uid, kit):\n    def quote(value):\n        value = str(value)\n        if "\\n" in value or "\\r" in value:\n            raise RuntimeError("Newlines are not allowed in a scheduler path.")\n        return \'"\' + value.replace("\\\\", "\\\\\\\\").replace(\'"\', \'\\\\"\').replace("%", "%%") + \'"\'\n    service = f"""{MARKER}\n[Unit]\nDescription=System-wide configuration capture and private user backup\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -I {HELPER} --owner {owner_uid} --workspace {quote(kit)} --run-backup\nUMask=0077\nNice=10\nNoNewPrivileges=true\nProtectSystem=strict\nProtectHome=read-only\nReadWritePaths={quote(kit)}\nTimeoutStartSec=20min\n"""\n    timer = f"""{MARKER}\n[Unit]\nDescription=Daily system-wide configuration backup\n[Timer]\nOnCalendar=daily\nPersistent=true\nRandomizedDelaySec=10m\n[Install]\nWantedBy=timers.target\n"""\n    return service, timer\n\n\ndef install_scheduler(owner_uid, kit):\n    root = Path("/etc/systemd/system")\n    service, timer = units(owner_uid, kit)\n    targets = [(HELPER, globals().get("SELF_SOURCE") or Path(__file__).read_text(), 0o700),\n               (root / (UNIT + ".service"), service, 0o644), (root / (UNIT + ".timer"), timer, 0o644)]\n    HELPER.parent.mkdir(mode=0o755, parents=True, exist_ok=True)\n    for directory in (HELPER.parent, root):\n        info = directory.lstat()\n        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:\n            raise RuntimeError("Scheduler directories must be protected and root-owned.")\n    for path, _, _ in targets:\n        if path.is_symlink() or (path.exists() and MARKER not in path.read_text()):\n            raise RuntimeError("Refusing unmanaged scheduler file: " + str(path))\n        if path.suffix == ".service" and path.exists() and f"--owner {owner_uid} " not in path.read_text():\n            raise RuntimeError("System-wide scheduler belongs to another account.")\n    for path, text, mode in targets:\n        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".system-config-", delete=False) as output:\n            temporary = Path(output.name)\n            try:\n                output.write(text.encode())\n                os.fchmod(output.fileno(), mode)\n                os.fchown(output.fileno(), 0, 0)\n                output.flush()\n                os.fsync(output.fileno())\n                os.replace(temporary, path)\n            finally:\n                temporary.unlink(missing_ok=True)\n    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)\n    subprocess.run(["/usr/bin/systemctl", "enable", "--now", UNIT + ".timer"], check=True)\n\n\ndef main(install=False, owner_uid=None, workspace=None, run_backup=False, replace_user_timer=False):\n    os.umask(0o077)\n    if os.geteuid() != 0:\n        raise RuntimeError("Run the confirmed sudo capture action, or sudo ./capture-root-configs.sh")\n    identity = owner_uid if owner_uid is not None else os.environ.get("SUDO_UID")\n    if identity is None:\n        raise RuntimeError("A non-root sudo caller or explicit scheduler owner is required.")\n    owner = pwd.getpwuid(int(identity))\n    if owner.pw_uid == 0:\n        raise RuntimeError("A non-root backup owner is required.")\n    kit = Path(workspace).expanduser().absolute() if workspace else Path(__file__).resolve().parents[1]\n    if Path(owner.pw_dir) not in kit.parents or ".." in kit.parts or kit.is_symlink():\n        raise RuntimeError("The private workspace must be inside the backup owner\'s real home directory.")\n    files, details = collect(owner.pw_uid)\n    captured = dt.datetime.now(dt.timezone.utc).isoformat()\n    manifest = {"format": "ubuntu-configuration-v1", "kind": "system-wide-configuration-cache", "created_at": captured,\n                "captured_at": captured, **details,\n                "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),\n                           "executable": False} for name, data in sorted(files.items())]}\n    if sum(len(data) for data in files.values()) > MAX_TOTAL or len(json.dumps(manifest).encode()) > MAX_FILE:\n        raise RuntimeError("Complete configuration/metadata exceeds safety limits; old cache preserved.")\n    if install:\n        install_scheduler(owner.pw_uid, kit)\n    os.setgroups([])\n    os.setgid(owner.pw_gid)\n    os.setuid(owner.pw_uid)\n    os.environ.update(HOME=owner.pw_dir, XDG_RUNTIME_DIR=f"/run/user/{owner.pw_uid}",\n                      DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{owner.pw_uid}/bus")\n    destination = kit / "privileged-system"\n    if destination.is_symlink():\n        raise RuntimeError("Refusing a symlinked capture directory.")\n    destination.mkdir(mode=0o700, exist_ok=True)\n    destination.chmod(0o700)\n    temporary = None\n    try:\n        with tempfile.NamedTemporaryFile(dir=destination, prefix=".system-wide-", delete=False) as output:\n            temporary = Path(output.name)\n            with tarfile.open(fileobj=output, mode="w:gz") as archive:\n                payload = dict(files)\n                payload["manifest.json"] = (json.dumps(manifest, indent=2) + "\\n").encode()\n                for name, data in sorted(payload.items()):\n                    info = tarfile.TarInfo(name)\n                    info.size, info.mode = len(data), 0o600\n                    archive.addfile(info, io.BytesIO(data))\n            output.flush()\n            os.fsync(output.fileno())\n        with tarfile.open(temporary, "r:gz") as archive:\n            for record in manifest["files"]:\n                if hashlib.sha256(archive.extractfile(record["path"]).read()).hexdigest() != record["sha256"]:\n                    raise RuntimeError("Privileged cache verification failed; old cache preserved.")\n        os.replace(temporary, destination / "system-wide.tar.gz")\n        temporary = None\n        fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n        try:\n            os.fsync(fd)\n        finally:\n            os.close(fd)\n    finally:\n        if temporary is not None:\n            temporary.unlink(missing_ok=True)\n    print(f"Privately captured {len(files)} configuration files across {len(details[\'accounts\'])} accounts; omissions: {len(details[\'omissions\'])}.")\n    if replace_user_timer:\n        old = Path(owner.pw_dir) / ".config/systemd/user/ubuntu-configuration-backup.timer"\n        if old.is_file() and not old.is_symlink() and "# Managed by ubuntu-configuration v1" in old.read_text():\n            subprocess.run(["systemctl", "--user", "disable", "--now", "ubuntu-configuration-backup.timer"], check=True)\n    if run_backup or install:\n        entrypoint = Path(owner.pw_dir) / "ubuntu-backup.py"\n        if not entrypoint.is_file():\n            entrypoint = kit / "ubuntu-backup.py"  # Compatibility with older recovered kits.\n        subprocess.run(["/usr/bin/python3", "-I", str(entrypoint), "--workspace", str(kit), "--non-interactive", "backup"], check=True)\n    else:\n        print("Now run the normal backup command without sudo. Capture includes sensitive settings; it is NOT encrypted.")\n\n\nif __name__ == "__main__":\n    parser = argparse.ArgumentParser(description=__doc__)\n    parser.add_argument("--owner", type=int)\n    parser.add_argument("--workspace", type=Path)\n    parser.add_argument("--install-timer", action="store_true")\n    parser.add_argument("--replace-user-timer", action="store_true")\n    parser.add_argument("--run-backup", action="store_true")\n    options = parser.parse_args()\n    try:\n        main(options.install_timer, options.owner, options.workspace, options.run_backup, options.replace_user_timer)\n    except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as exc:\n        print(f"Error: {exc}", file=sys.stderr)\n        sys.exit(1)\n'
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


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input(prompt + suffix + ": ").strip()
    except EOFError:
        raise Cancelled("Input ended; cancelled.") from None
    return value if value else (str(default) if default is not None else "")


def confirm(prompt, default=False):
    while True:
        value = ask(prompt + (" [Y/n]" if default else " [y/N]")).lower()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("Please answer yes or no.")


def private_workspace(path):
    global KIT
    path = Path(path).expanduser().absolute()
    if path in (HOME, Path("/")):
        raise ConfigError("Choose a dedicated workspace, not your home directory or filesystem root.")
    checked_path(Path("/"), path.relative_to("/").as_posix())
    if path.exists():
        if not path.is_dir() or path.stat().st_uid != os.geteuid():
            raise ConfigError("Workspace must be an owned, real directory.")
        if any(path.iterdir()) and not (path / "config/settings.json").is_file():
            # A recovered archive's kit/ is also a valid starting workspace.
            if not (path.parent / "manifest.json").is_file():
                raise ConfigError("Refusing to adopt a nonempty, unrecognized workspace.")
    mkdir(path)
    path.chmod(0o700)
    KIT = path
    if not (KIT / "config/settings.json").exists():
        defaults = copy.deepcopy(EMBEDDED_DEFAULT_SETTINGS)
        defaults["external_backup"] = None  # A new machine never inherits a disk choice.
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


def build_snapshot(destination, config):
    selected = copy.deepcopy(config)
    try:
        selected["exclude"].append(SCRIPT_PATH.relative_to(HOME).as_posix())
    except ValueError:
        pass
    report = _original_build_snapshot(destination, selected)
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


def capture_root(non_interactive=False, install=False, replace_user_timer=False, hide_followup_hint=False):
    print("System-wide configuration and local accounts' settings will be read, including sensitive configuration.")
    command = ["sudo"] + (["-n"] if non_interactive else [])
    command += ["/usr/bin/python3", "-I", str(SCRIPT_PATH), "--workspace", str(KIT), "_capture-root-worker"]
    if install:
        command.append("--install-system-timer")
    if replace_user_timer:
        command.append("--replace-user-timer")
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True)
    for line in result.stdout.splitlines():
        if hide_followup_hint and line.startswith("Now run the normal backup command without sudo."):
            continue
        print(line)


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
UMask=0077
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
TimeoutStartSec=10min
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
    print(f"Workspace: {KIT}\nRetention: {config['keep_snapshots']} snapshots")
    external = config["external_backup"]
    print("External drive: " + (external["mount_point"] if external else "not chosen; local backups only"))
    print(f"Local dated snapshots: {len(dated_snapshots(KIT / 'backups'))}; legacy archives: {len(archives(KIT / 'backups'))}")
    if external:
        try:
            directory = external_directory(config)
            configuration = directory / "timeline/config"
            homes = directory / "timeline/home"
            print(f"External configuration timelines: {len(list(configuration.iterdir())) if configuration.is_dir() else 0}; "
                  f"full /home timelines: {len(list(homes.iterdir())) if homes.is_dir() else 0}")
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            print(f"External drive unavailable: {exc}")
    if shutil.which("systemctl"):
        result = subprocess.run(["systemctl", "--user", "is-enabled", UNIT + ".timer"],
                                capture_output=True, text=True, timeout=10)
        print("Backup timer: " + (result.stdout.strip() or "unavailable"))
    print("Config data and archives are private but NOT encrypted.")


def choose_snapshot(default="1"):
    print("1. Personal configuration bundled in this script")
    print("2. Latest workspace snapshot (may describe this machine, not the old one)")
    print("3. A recovered snapshot directory\n0. Cancel")
    while True:
        choice = ask("Snapshot", default)
        if choice == "0":
            return None
        if choice == "1":
            return bundled_profile()
        if choice == "2":
            return _original_default_snapshot()
        if choice == "3":
            value = ask("Snapshot directory (blank cancels)")
            return Path(value).expanduser().resolve() if value else None
        print("Choose 0, 1, 2 or 3.")


def wizard_deploy(source=None):
    source = source or choose_snapshot()
    if source is None:
        return
    verify_tree(source)
    print("Only portable home settings are copied. System references/repos/drivers/disks are not replayed.")
    args = argparse.Namespace(
        snapshot=str(source), apply=False,
        overwrite=confirm("Replace conflicting home files, keeping rollback copies?"),
        packages=confirm("Install available APT packages from current trusted repositories?"),
        snaps=confirm("Install captured Snap apps/channels (some use classic confinement)?"),
        flatpaks=confirm("Install Flatpak apps from already-configured trusted USER remotes?"),
        desktop_settings=confirm("Import GNOME/dconf preferences?"),
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
    print("1. Recover configuration (stage a backup, then optionally deploy)")
    print("2. Recover full /home (stage only; live /home is never changed)")
    print("3. Deploy a saved snapshot directly (may install packages)")
    print("0. Cancel")
    choice = ask("Recovery", "0")
    if choice == "0":
        return
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
        if confirm("Deploy this recovered configuration now? This may install packages."):
            wizard_deploy(destination)
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


def wizard_drive(mount_point=None, disable=False):
    current = settings()["external_backup"]
    print("Current drive: " + (current["mount_point"] if current else "unset"))
    print("No disk will be formatted, partitioned, or mounted. Skip keeps the saved choice.")
    value = "disable" if disable else mount_point
    if value is None:
        while True:
            devices = attached_drives()
            # USB/removable devices are presented first; lsblk has no reliable connection-time history.
            devices.sort(key=lambda d: not (d["transport"] == "usb" or d["removable"]))
            if not devices:
                print("No attached block devices detected. Connect a drive and choose rescan.")
            for number, device in enumerate(devices, 1):
                mounts = ", ".join(device["mountpoints"]) or "NOT MOUNTED"
                print(f"{number}. {device['path']} ({device['type']}, {device['size']} bytes, "
                      f"{device['fstype'] or 'no filesystem'}, UUID {device['uuid'] or 'none'}) — {mounts}")
            print("s. Skip external backup for now; keep previous target")
            print("r. Rescan after connecting a drive   p. Enter an already-mounted path")
            print("d. Disable/forget saved target (separate confirmation)")
            choice = ask("Choice", "s").lower()
            if choice in ("", "s", "0"):
                print("External drive skipped; saved target unchanged.")
                return False
            if choice == "r":
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
                    print("This device is not mounted. Mount the correct prepared filesystem yourself, then rescan; nothing was changed.")
                    continue
                if len(selected["mountpoints"]) > 1:
                    print("Multiple mountpoints: " + ", ".join(selected["mountpoints"]))
                    value = ask("Enter the desired mountpoint (blank skips)")
                    if value and value not in selected["mountpoints"]:
                        print("Not a listed mountpoint; choice unchanged.")
                        continue
                else:
                    value = selected["mountpoints"][0]
                break
            print("Choose a listed device, s, r, p or d.")
    if not value:
        return False
    if value == "disable":
        if confirm("Disable external copies without deleting any backups?"):
            set_external(None, True)
        return False
    mount = Path(value).expanduser().resolve()
    info = mount_info(mount)
    print(f"Mounted target: {mount}\nFilesystem UUID: {info['uuid']}\nType: {info['fstype']}")
    if ask("Type USE DRIVE to create the private backup directory/marker there") == "USE DRIVE":
        set_external(str(mount))
        return True
    else:
        print("Drive choice unchanged.")
        return False


def interactive_backup():
    """Ask only when a saved destination is absent/unavailable; skip never clears it."""
    external = settings()["external_backup"]
    if external:
        try:
            print("Using previously approved backup drive: " + str(external_directory(settings())))
            return backup()
        except (ConfigError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            print(f"Saved backup drive is unavailable: {exc}")
    else:
        print("No external backup drive is selected.")
    selected = wizard_drive()
    return backup(skip_external=not selected)


def backup_everything():
    """Single flow: privileged configuration, configuration backup, full /home."""
    if confirm("Refresh system-wide configuration with sudo before backing up?"):
        capture_root(hide_followup_hint=True)
    path = interactive_backup()
    print("Configuration backup complete; continuing with full /home.")
    home_done = wizard_home_backup()
    if home_done:
        print("Backup operation complete: configuration and full /home.")
    else:
        print("Backup operation complete: configuration only; full /home was skipped.")
    return path


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
        print("u. Undo restore")
        print("s. Status")
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
            elif choice == "u":
                wizard_undo_restore()
            elif choice == "s":
                show_status()
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
    return HOME / "ubuntu-backup-data"


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
    }
    for command, description in descriptions.items():
        commands.add_parser(command, help=description)
    worker = commands.add_parser("_capture-root-worker")
    worker.add_argument("--install-system-timer", action="store_true")
    worker.add_argument("--replace-user-timer", action="store_true")
    worker = commands.add_parser("_backup-home-worker")
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
    sub = commands.add_parser("set-external", help="Choose an already-mounted drive; never formats disks")
    sub.add_argument("mount_point", nargs="?")
    sub.add_argument("--disable", action="store_true")
    sub = commands.add_parser("deploy", help="Preview/apply a saved configuration; dry-run by default")
    sub.add_argument("--snapshot")
    for option in ("apply", "overwrite", "packages", "snaps", "flatpaks", "desktop-settings"):
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
        root_home_backup(workspace)
        return
    if os.geteuid() == 0:
        raise ConfigError("Run normally, not with sudo. The root-capture action invokes sudo separately.")
    interactive = not args.non_interactive and sys.stdin.isatty()
    command = args.command or "menu"
    if command == "menu":
        if not interactive:
            raise ConfigError("The menu needs an interactive terminal. Use --non-interactive backup, deploy, verify, or --help.")
        print("Personal configuration data is bundled in this file; it is NOT encrypted.")
        workspace = Path(ask("Private workspace folder", str(workspace)))
    private_workspace(workspace)
    if command == "menu":
        menu()
    elif command == "backup":
        backup_everything() if interactive else backup()
    elif command == "restore-home":
        if interactive and args.timeline is None:
            wizard_restore_home()
        else:
            restore_home(args.timeline)
    elif command == "status":
        show_status()
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
    return {"hardware": hardware_inventory(), "home_device": Path("/home").stat().st_dev,
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


def stream_object(objects, incoming, stream, size, sparse, created):
    """Store one logical file as a sparse-friendly baseline object."""
    with tempfile.NamedTemporaryFile(dir=incoming, prefix=".object-", delete=False) as output:
        temporary = Path(output.name)
        hasher = hashlib.sha256()
        try:
            if sparse:
                current = 0
                for offset, length in sparse:
                    offset, length = int(offset), int(length)
                    if length < 0 or offset < current:
                        raise ConfigError("Invalid sparse file layout in tar stream.")
                    if length == 0:
                        continue
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
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    sha256 = hasher.hexdigest()
    try:
        destination, _ = publish_object(objects, incoming, temporary, sha256, size, created)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, sha256


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


def capture_home_archive(stream, root, identity, exclusions):
    """Consume a root tar stream as the normal user and publish a linked timeline."""
    objects, incoming, timelines = baseline_directories(root, "home")
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
                    destination, sha256 = stream_object(objects, incoming, source, member.size, sparse, created)
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
        write_json(temporary / "manifest.json", manifest)
        verify_home_timeline(temporary, hash_objects=False)
        return temporary, manifest, created, timelines
    except BaseException:
        progress.abort()
        if temporary.exists():
            shutil.rmtree(temporary)
        clean_created_objects(created)
        raise


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


def wizard_home_backup():
    config = settings()
    if not config["external_backup"]:
        print("Choose and mount the backup drive first (menu 4). No /home data was read or copied.")
        return False
    directory = external_directory(config)
    identity = home_identity()
    nested = [entry["mount"] for entry in identity["mounts"] if entry["mount"] != "/home"]
    if nested:
        raise ConfigError("Strict /home backup refuses unapproved nested mounts; prepare separate filesystem backups: " + ", ".join(nested))
    print("FULL /home: all users' files, including secrets, browser data and projects. Snapshot is NOT encrypted.")
    print("Each new file is stored once in a shared baseline; dated timelines contain relative symlinks to it.")
    print("The source is never rewritten. ACLs, extended attributes, ownership and sparse files are recorded.")
    print("Virtual filesystems, nested mounts and this kit's generated backup/cache directories are not traversed.")
    print("Stop applications/VMs/databases first: a live tar stream is NOT a filesystem-consistent snapshot.")
    print("Any reported read/change error fails the backup; no old full-home timeline is pruned.")
    print("Keep the baseline with every dated timeline; one date folder is not portable on its own.")
    print("Destination: " + str(directory))
    baseline = KIT / "config/home-backup-approval.json"
    changed = not baseline.is_file() or json.loads(baseline.read_text()) != identity
    if changed:
        print("Hardware/mount layout is new or changed. Explicit review is required; nothing is adapted on backup.")
        if ask("Type APPROVE SOURCE LAYOUT to approve this exact layout") != "APPROVE SOURCE LAYOUT":
            return False
        write_json(baseline, identity)
    if ask("Type BACK UP ALL HOME to start the sudo, read-only capture") != "BACK UP ALL HOME":
        return False
    subprocess.run(["sudo", "/usr/bin/python3", "-I", str(SCRIPT_PATH), "--workspace", str(KIT), "_backup-home-worker"], check=True)
    return True


def root_home_backup(workspace):
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
    config = settings()
    if not config["external_backup"]:
        raise ConfigError("External backup drive has not been selected.")
    expected = json.loads((KIT / "config/home-backup-approval.json").read_text())
    actual = home_identity()
    if expected != actual or not actual["hardware"].get("complete"):
        raise ConfigError("Source hardware/mount identities changed or are unknown; reapprove before backing up.")
    if any(entry["mount"] != "/home" for entry in actual["mounts"]):
        raise ConfigError("Nested /home mounts require separate reviewed filesystem backups.")
    directory = external_directory(config)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(fd).st_dev in (Path("/home").stat().st_dev, HOME.stat().st_dev, Path("/").stat().st_dev):
        os.close(fd)
        raise ConfigError("Backup drive must differ from every source filesystem.")
    command = ["/usr/bin/tar", "--create", "--file=-", "--acls", "--xattrs", "--numeric-owner",
               "--sparse", "--one-file-system", "--directory=/home"]
    relative = KIT.relative_to("/home").as_posix()
    excluded = [relative + "/" + name for name in ("backups", "privileged-system", "restored", "rollback", "profiles", "snapshot")]
    for name in excluded:
        command.append("--exclude=./" + name)
    command.append(".")
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
        if json.loads((pinned / ".ubuntu-configuration-target.json").read_text())["id"] != config["external_backup"]["marker_id"]:
            raise ConfigError("Pinned backup drive marker changed.")
        temporary, manifest, created, timelines = capture_home_archive(child.stdout, pinned, actual, excluded)
        status = child.wait()
        thread.join()
        if status or errors:
            raise ConfigError("Strict /home capture failed; old snapshots kept. Stop changing apps and retry. " + errors.decode(errors="replace")[-2000:])
        result = timelines / ("home-full-" + timestamp())
        if result.exists() or result.is_symlink():
            raise ConfigError("Full /home timeline already exists; refusing overwrite.")
        temporary.rename(result)
        temporary = None
        fsync_directory(timelines)
        print("Verified full /home linked snapshot: " + str(directory / result.relative_to(pinned)))
        print(f"Stored {manifest['entry_count']} entries, {len(created)} new baseline objects; {manifest['warning_count']} runtime/special entries were recorded but not recreated.")
        print("Full-home timelines are NOT automatically pruned. Keep the baseline; restore into an isolated directory and adapt owners/hardware manually.")
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
        os.close(fd)

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
