"""Regression tests for the safety-critical helpers in ubuntu-backup.py."""

import argparse
import base64
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
import gzip
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ubuntu_backup", ROOT / "ubuntu-backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BoundedTraceTests(unittest.TestCase):
    def test_history_is_bounded(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.KIT, backup.HISTORY_LIMIT_BYTES, backup.HISTORY_KEEP_LINES
            backup.KIT = Path(directory)
            backup.HISTORY_LIMIT_BYTES = 200
            backup.HISTORY_KEEP_LINES = 3
            try:
                for index in range(20):
                    backup._status_history("test", f"entry {index}")
                lines = (Path(directory) / "history.txt").read_text().splitlines()
                self.assertLessEqual(len(lines), backup.HISTORY_KEEP_LINES + 1)
                self.assertIn("entry 19", lines[-1])
            finally:
                backup.KIT, backup.HISTORY_LIMIT_BYTES, backup.HISTORY_KEEP_LINES = saved

    def test_log_is_bounded(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            log = backup.RotatingLog(Path(directory) / "test.log", segment_bytes=100, segments=3)
            for index in range(40):
                log.write(f"line {index:03d}\n")
            segments = list(Path(directory).glob("test.log*"))
            self.assertEqual(len(segments), 3)
            self.assertLessEqual(sum(path.stat().st_size for path in segments), 300)


class WorkspaceDefaultTests(unittest.TestCase):
    def test_preferred_workspace_prefers_canonical_layout(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            home = Path(directory)
            saved = backup.HOME, backup.SCRIPT_PATH
            backup.HOME = home
            backup.SCRIPT_PATH = home / ".local/bin/ubuntu-backup.py"
            try:
                self.assertEqual(backup.preferred_workspace(), home / "Ubuntu-Backup/data")
                legacy = home / "ubuntu-backup/data"
                legacy.mkdir(parents=True)
                self.assertEqual(backup.preferred_workspace(), legacy)
                canonical = home / "Ubuntu-Backup/data"
                canonical.mkdir(parents=True)
                self.assertEqual(backup.preferred_workspace(), canonical)
            finally:
                backup.HOME, backup.SCRIPT_PATH = saved


class TraySingletonTests(unittest.TestCase):
    KEYS = ("XDG_RUNTIME_DIR", "XDG_STATE_HOME")

    def _save(self):
        return {key: os.environ.get(key) for key in self.KEYS}

    def _restore(self, saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_tray_lock_is_session_scoped_and_exclusive(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = self._save()
            os.environ["XDG_RUNTIME_DIR"] = str(Path(directory) / "runtime")
            os.environ.pop("XDG_STATE_HOME", None)
            first = second = None
            try:
                first = backup._acquire_tray_lock()
                self.assertIsNotNone(first)
                self.assertIsNone(backup._acquire_tray_lock())
                os.close(first)
                first = None
                second = backup._acquire_tray_lock()
                self.assertIsNotNone(second)
            finally:
                for fd in (first, second):
                    if fd is not None:
                        os.close(fd)
                self._restore(saved)

    def test_relative_runtime_dir_falls_back_to_state(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = self._save()
            state = Path(directory) / "state"
            os.environ["XDG_RUNTIME_DIR"] = "relative/runtime"
            os.environ["XDG_STATE_HOME"] = str(state)
            fd = None
            try:
                fd = backup._acquire_tray_lock()
                self.assertIsNotNone(fd)
                self.assertTrue((state / "ubuntu-backup/tray.lock").is_file())
            finally:
                if fd is not None:
                    os.close(fd)
                self._restore(saved)

    def test_symlinked_lock_directory_is_refused(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = self._save()
            runtime = Path(directory) / "runtime"
            state = Path(directory) / "state"
            target = Path(directory) / "target"
            (runtime / "ubuntu-backup").mkdir(parents=True)
            target.mkdir()
            (runtime / "ubuntu-backup").rmdir()
            (runtime / "ubuntu-backup").symlink_to(target)
            os.environ["XDG_RUNTIME_DIR"] = str(runtime)
            os.environ["XDG_STATE_HOME"] = str(state)
            fd = None
            try:
                fd = backup._acquire_tray_lock()
                self.assertIsNotNone(fd)
                self.assertTrue((state / "ubuntu-backup/tray.lock").is_file())
                self.assertFalse((target / "tray.lock").exists())
            finally:
                if fd is not None:
                    os.close(fd)
                self._restore(saved)

    def test_invalid_lock_file_fails_closed(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = self._save()
            runtime = Path(directory) / "runtime"
            state = Path(directory) / "state"
            (runtime / "ubuntu-backup/tray.lock").mkdir(parents=True)
            os.environ["XDG_RUNTIME_DIR"] = str(runtime)
            os.environ["XDG_STATE_HOME"] = str(state)
            try:
                self.assertIsNone(backup._acquire_tray_lock())
                self.assertFalse((state / "ubuntu-backup/tray.lock").exists())
            finally:
                self._restore(saved)


class DriveGuardTests(unittest.TestCase):
    def test_low_space_is_rejected_before_probe(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            (root / ".ubuntu-configuration-target.json").write_text(
                json.dumps({"format": backup.FORMAT, "id": "test"}))
            usage = namedtuple("usage", "total used free")(1024, 1023, 1)
            saved = (backup.on_source_filesystem, backup.shutil.disk_usage, backup.MIN_EXTERNAL_FREE_BYTES)
            backup.on_source_filesystem = lambda _path: False
            backup.shutil.disk_usage = lambda _path: usage
            backup.MIN_EXTERNAL_FREE_BYTES = 64
            try:
                with self.assertRaises(backup.ConfigError):
                    backup.validate_external_directory(root)
                self.assertEqual(list(root.glob(".backup-probe-*")), [])
            finally:
                backup.on_source_filesystem, backup.shutil.disk_usage, backup.MIN_EXTERNAL_FREE_BYTES = saved


class CaptureFallbackTests(unittest.TestCase):
    def test_unreadable_helper_uses_cached_copy(self):
        content = b"#!/usr/bin/python3\n# Managed by ubuntu-configuration test\n"
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            live = base / "live-helper"
            live.write_bytes(content)
            live.chmod(0)
            snapshot = base / "snapshot"
            cached = snapshot / "system" / live.relative_to("/")
            cached.parent.mkdir(parents=True)
            cached.write_bytes(content)
            saved = backup.system_integration_inventory
            backup.system_integration_inventory = lambda: {
                "format": "test",
                "items": [{"role": "helper", "path": str(live), "present": True}],
            }
            try:
                backup.capture_system_integration(snapshot)
                item = json.loads((snapshot / "inventory/system-integration.json").read_text())["items"][0]
                self.assertEqual(item["stored"], "system-integration/helper")
                self.assertEqual((snapshot / item["stored"]).read_bytes(), content)
            finally:
                backup.system_integration_inventory = saved
                live.chmod(0o600)


class EmbeddedCollectorTests(unittest.TestCase):
    def test_missing_optional_root_is_skipped(self):
        namespace = {
            "__name__": "root_reference_worker",
            "__file__": "/tmp/capture-root-configs.py",
            "SELF_SOURCE": backup.EMBEDDED_ROOT_EXPORTER,
        }
        exec(compile(backup.EMBEDDED_ROOT_EXPORTER, "<embedded>", "exec"), namespace)
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            missing = str(Path(directory) / "missing" / "helper.py")
            files, details = namespace["collect"](os.getuid(), accounts=[], system_roots=[missing])
            self.assertEqual(details["omissions"], [])
            self.assertIn("inventory/privileged-links.json", files)
            service, _timer = namespace["units"](1000, Path("/tmp/kit"))
            self.assertIn("SuccessExitStatus=3", service)


class CaptureRootTests(unittest.TestCase):
    def test_worker_stderr_is_surfaced(self):
        calls = []

        def fake_run(command):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "Error: missing parent\n")

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.run_cancellable, backup.KIT
            backup.run_cancellable = fake_run
            backup.KIT = Path(directory)
            stderr = io.StringIO()
            stdout = io.StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(backup.ConfigError) as raised:
                        backup.capture_root()
            finally:
                backup.run_cancellable, backup.KIT = saved
            self.assertIn("missing parent", str(raised.exception))
            self.assertIn("missing parent", stderr.getvalue())
            self.assertIn("_capture-root-worker", calls[0])


class CpuGuardTests(unittest.TestCase):
    def test_excluded_smt_siblings_are_removed(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            (kit / "config").mkdir()
            (kit / "config/settings.json").write_text(
                json.dumps({"keep_snapshots": 30, "exclude_cpus": [2]}))
            saved = backup.KIT, backup.cpu_siblings, backup.os.sched_getaffinity
            backup.KIT = kit
            backup.cpu_siblings = lambda cpu: {2, 8}
            backup.os.sched_getaffinity = lambda _pid: {0, 1, 2, 3, 8}
            try:
                self.assertEqual(backup.allowed_backup_cpus(), {0, 1, 3})
            finally:
                backup.KIT, backup.cpu_siblings, backup.os.sched_getaffinity = saved

    def test_rejects_a_set_that_excludes_everything(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            (kit / "config").mkdir()
            (kit / "config/settings.json").write_text(
                json.dumps({"keep_snapshots": 30, "exclude_cpus": [2]}))
            saved = backup.KIT, backup.cpu_siblings, backup.os.sched_getaffinity
            backup.KIT = kit
            backup.cpu_siblings = lambda cpu: {2, 8}
            backup.os.sched_getaffinity = lambda _pid: {2, 8}
            try:
                with self.assertRaises(backup.ConfigError):
                    backup.allowed_backup_cpus()
            finally:
                backup.KIT, backup.cpu_siblings, backup.os.sched_getaffinity = saved

    def test_no_exclusions_uses_every_available_cpu_without_pinning(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            (kit / "config").mkdir()
            (kit / "config/settings.json").write_text(
                json.dumps({"keep_snapshots": 30, "exclude_cpus": []}))
            available = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}
            calls = []
            saved = backup.KIT, backup.os.sched_getaffinity, backup.os.sched_setaffinity
            backup.KIT = kit
            backup.os.sched_getaffinity = lambda _pid: set(available)
            backup.os.sched_setaffinity = lambda pid, cpus: calls.append((pid, set(cpus)))
            try:
                self.assertEqual(backup.allowed_backup_cpus(), available)
                self.assertEqual(backup.restrict_backup_cpus(), available)
                self.assertEqual(calls, [])
            finally:
                backup.KIT, backup.os.sched_getaffinity, backup.os.sched_setaffinity = saved


class StagingCleanupTests(unittest.TestCase):
    def test_remove_staging_removes_tree_and_symlink_without_following(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            saved = backup.KIT
            backup.KIT = base
            try:
                stage = base / "staging"
                (stage / "objects").mkdir(parents=True)
                (stage / "objects" / "object").write_text("data")
                backup.remove_staging()
                self.assertFalse(stage.exists())

                target = base / "target"
                target.mkdir()
                (target / "keep").write_text("keep")
                stage.symlink_to(target)
                backup.remove_staging()
                self.assertFalse(stage.exists())
                self.assertTrue((target / "keep").is_file())
            finally:
                backup.KIT = saved


class DevelopmentOrderTests(unittest.TestCase):
    def _capture(self, directory):
        snapshot = Path(directory) / "snapshot"
        files = {
            "bin/b": "#!/usr/bin/env bash\necho b\n",
            "bin/a": "#!/usr/bin/env bash\n$HOME/bin/b\n",
            "bin/c.c": "#include <X11/Xlib.h>\n#include <Xrandr.h>\nint main(void){return 0;}\n",
            "bin/c": b"\x7fELF" + b"\x00" * 16,
            "bin/x": "#!/usr/bin/env bash\ny\n",
            "bin/y": "#!/usr/bin/env bash\nx\n",
        }
        for name, value in files.items():
            path = snapshot / "home" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, bytes):
                path.write_bytes(value)
            else:
                path.write_text(value)
            path.chmod(0o700)
        backup.capture_developments(snapshot, source_home=snapshot / "home")
        return snapshot, json.loads((snapshot / "inventory/developments.json").read_text())

    def test_dependencies_precede_dependents_and_cycles_are_kept(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            snapshot, data = self._capture(directory)
            order = data["order"]
            self.assertLess(order.index("bin/b"), order.index("bin/a"))
            self.assertLess(order.index("bin/c.c"), order.index("bin/c"))
            self.assertTrue(data["cycles"])
            self.assertIn("bin/x", order)
            self.assertIn("bin/y", order)
            record = next(item for item in data["files"] if item["path"] == "bin/c.c")
            self.assertEqual(record["build"]["output"], "bin/c")
            self.assertIn("-lXrandr", record["build"]["libraries"])

    def test_deploy_order_is_configuration_first_then_dependencies(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            snapshot, _ = self._capture(directory)
            ordered = backup.deploy_order(snapshot, ["bin/a", "bin/b", ".bashrc"])
            self.assertEqual(ordered[0], ".bashrc")
            self.assertLess(ordered.index("bin/b"), ordered.index("bin/a"))


class DevelopmentDetectionTests(unittest.TestCase):
    def test_markers_detect_checkouts_and_configured_roots(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            home = Path(directory) / "home"
            (home / "repo/.git").mkdir(parents=True)
            (home / "plain").mkdir()
            (home / "workspace").mkdir()
            config = {"coverage": "system-wide-configuration",
                      "development_include": ["extra"], "exclude": []}
            saved = backup.KIT
            backup.KIT = home / "workspace"
            try:
                roots = backup.detect_development_roots(home, config)
            finally:
                backup.KIT = saved
            self.assertIn("repo", roots)
            self.assertIn("extra", roots)
            self.assertNotIn("plain", roots)


class OversizedConfigTests(unittest.TestCase):
    def test_oversized_file_is_skipped_with_a_warning(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "big.bin"
            source.write_bytes(b"x" * 40)
            destination = base / "snapshot" / "big.bin"
            report = {"warnings": [], "symlinks_not_followed": [], "bytes_captured": 0}
            saved = backup.MAX_FILE
            backup.MAX_FILE = 10
            try:
                backup.copy_selection(source, destination, "big.bin", {"exclude": []}, report)
            finally:
                backup.MAX_FILE = saved
            self.assertFalse(destination.exists())
            self.assertTrue(report["warnings"])
            self.assertTrue(any("Omitted from configuration backup" in warning
                                and "per-file limit" in warning
                                for warning in report["warnings"]))
            self.assertEqual(report["oversized_files"], [str(source)])

    def test_development_capture_excludes_toolchain_directories(self):
        self.assertTrue(any("toolchain" in pattern
                            for pattern in backup.development_exclusions()))


class UnreadableHomeTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "root can read mode-000 files")
    def test_unreadable_file_is_recorded_and_snapshot_is_published(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            home, kit, destination = base / "home", base / "kit", base / "snapshot"
            (home / "repo/.git").mkdir(parents=True)
            (home / "repo/main.py").write_text("print('x')\n")
            secret = home / "repo/INDEX.md"
            secret.write_text("private\n")
            secret.chmod(0)
            config = {"coverage": "system-wide-configuration", "home_include": [],
                      "home_reference_only": [], "system_reference_only": [],
                      "apt_review_only": [], "exclude": []}
            saved = backup.HOME, backup.KIT, backup.collect_inventory, backup.SYSTEM_CONFIG_ROOTS
            backup.HOME, backup.KIT = home, kit
            backup.collect_inventory = lambda *args, **kwargs: None
            backup.SYSTEM_CONFIG_ROOTS = ()
            try:
                report = backup._original_build_snapshot(destination, config)
            finally:
                secret.chmod(0o600)
                backup.HOME, backup.KIT, backup.collect_inventory, backup.SYSTEM_CONFIG_ROOTS = saved
            self.assertFalse((destination / "home/repo/INDEX.md").exists())
            unreadable = json.loads(
                (destination / "inventory/unreadable-files.json").read_text())
            self.assertEqual(unreadable["format"], "ubuntu-unreadable-files-v1")
            self.assertEqual([entry["path"] for entry in unreadable["files"]], [str(secret)])
            entry = unreadable["files"][0]
            self.assertEqual(entry["uid"], os.getuid())
            self.assertEqual(entry["gid"], os.getgid())
            self.assertEqual(entry["mode"], 0)
            self.assertEqual(entry["kind"], "file")
            self.assertEqual(entry["reason"], "permission denied")
            self.assertFalse(report["coverage_complete"])
            self.assertEqual(report["unreadable_count"], 1)
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertIn("inventory/unreadable-files.json",
                          {record["path"] for record in manifest["files"]})
            backup.verify_tree(destination)


class LargeConfigurationTests(unittest.TestCase):
    def test_large_file_uses_bounded_memory_copy(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "large.bin"
            source.write_bytes(b"large configuration\n" * 3)
            destination = base / "snapshot" / "large.bin"
            report = {"warnings": [], "symlinks_not_followed": [], "bytes_captured": 0}
            saved = backup.MAX_IN_MEMORY, backup.MAX_FILE, backup.MAX_TOTAL
            backup.MAX_IN_MEMORY, backup.MAX_FILE, backup.MAX_TOTAL = 10, 1024, 4096
            try:
                backup.copy_selection(source, destination, "large.bin",
                                      {"exclude": []}, report)
            finally:
                backup.MAX_IN_MEMORY, backup.MAX_FILE, backup.MAX_TOTAL = saved
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(report["bytes_captured"], source.stat().st_size)
            self.assertEqual(report["large_files"], [str(source)])


class DevelopmentCaptureTests(unittest.TestCase):
    def test_detected_checkout_is_captured_without_artifact_trees(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            home, kit, destination = base / "home", base / "kit", base / "snapshot"
            (home / "repo/.git").mkdir(parents=True)
            (home / "repo/main.py").write_text("print('x')\n")
            (home / "repo/node_modules").mkdir()
            (home / "repo/node_modules/dependency.js").write_text("ignored\n")
            config = {"coverage": "system-wide-configuration", "home_include": [],
                      "home_reference_only": [], "system_reference_only": [],
                      "apt_review_only": [], "exclude": []}
            saved = backup.HOME, backup.KIT, backup.collect_inventory, backup.SYSTEM_CONFIG_ROOTS
            backup.HOME, backup.KIT = home, kit
            backup.collect_inventory = lambda *args, **kwargs: None
            backup.SYSTEM_CONFIG_ROOTS = ()
            try:
                backup._original_build_snapshot(destination, config)
            finally:
                backup.HOME, backup.KIT, backup.collect_inventory, backup.SYSTEM_CONFIG_ROOTS = saved
            self.assertTrue((destination / "home/repo/main.py").is_file())
            self.assertFalse((destination / "home/repo/node_modules/dependency.js").exists())
            developments = json.loads((destination / "inventory/developments.json").read_text())
            self.assertIn("repo/main.py", developments["order"])

    def test_build_snapshot_compares_against_previous_snapshot(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            home, kit, destination = base / "home", base / "kit", base / "snapshot"
            (home / "repo/.git").mkdir(parents=True)
            (home / "repo/main.py").write_text("print('one')\n")
            previous = kit / "snapshot"
            (previous / "home/repo").mkdir(parents=True)
            (previous / "home/repo/main.py").write_text("print('one')\n")
            backup.capture_developments(previous, source_home=home)
            (home / "repo/main.py").write_text("print('two')\n")
            config = {"coverage": "system-wide-configuration", "home_include": [],
                      "home_reference_only": [], "system_reference_only": [],
                      "apt_review_only": [], "exclude": []}
            saved = backup.HOME, backup.KIT, backup.collect_inventory, backup.SYSTEM_CONFIG_ROOTS
            backup.HOME, backup.KIT = home, kit
            backup.collect_inventory = lambda *args, **kwargs: None
            backup.SYSTEM_CONFIG_ROOTS = ()
            try:
                backup._original_build_snapshot(destination, config)
            finally:
                backup.HOME, backup.KIT, backup.collect_inventory, backup.SYSTEM_CONFIG_ROOTS = saved
            developments = json.loads((destination / "inventory/developments.json").read_text())
            change = next(item for item in developments["changes"] if item["path"] == "repo/main.py")
            self.assertEqual(change["change"], "modified")
            self.assertNotEqual(change["version"], change["previous_version"])
            report = json.loads((destination / "inventory/capture-report.json").read_text())
            self.assertEqual(report["developments"]["changes"]["modified"], 1)


class DevelopmentHistoryTests(unittest.TestCase):
    def test_changes_carry_time_version_previous_version_and_references(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            first, second = base / "first", base / "second"
            for snapshot in (first, second):
                (snapshot / "home/bin").mkdir(parents=True)
            for name, value in (("a", "#!/usr/bin/env bash\necho one\n"),
                                ("stable", "#!/usr/bin/env bash\n"),
                                ("old", "#!/usr/bin/env bash\n")):
                path = first / "home/bin" / name
                path.write_text(value)
                path.chmod(0o700)
            backup.capture_developments(first, source_home=first / "home")
            for name, value in (("a", "#!/usr/bin/env bash\necho two\n"),
                                ("stable", "#!/usr/bin/env bash\n"),
                                ("b", "#!/usr/bin/env bash\n")):
                path = second / "home/bin" / name
                path.write_text(value)
                path.chmod(0o700)
            data = backup.capture_developments(second, previous=first, source_home=second / "home")
            changes = {item["path"]: item for item in data["changes"]}
            files = {item["path"]: item for item in data["files"]}
            self.assertEqual(changes["bin/a"]["change"], "modified")
            self.assertEqual(changes["bin/b"]["change"], "added")
            self.assertEqual(changes["bin/old"]["change"], "removed")
            self.assertNotIn("bin/stable", changes)
            self.assertEqual(files["bin/stable"]["change"], "unchanged")
            self.assertNotEqual(changes["bin/a"]["version"], changes["bin/a"]["previous_version"])
            self.assertEqual(changes["bin/a"]["revision"], 2)
            self.assertEqual(files["bin/a"]["revision"], 2)
            self.assertEqual(files["bin/stable"]["revision"], 1)
            self.assertTrue(changes["bin/a"]["changed_at"])
            self.assertIn("references", changes["bin/a"])
            self.assertIn("packages", changes["bin/a"]["references"])
            self.assertEqual(files["bin/a"]["version"], "sha256:" + files["bin/a"]["sha256"][:12])
            self.assertIn("bin/old", data["removed"])
            self.assertEqual(data["format"], "ubuntu-developments-v2")

    def test_usrmerge_paths_are_queried_by_dpkg_database_name(self):
        self.assertIn("/bin/bash", backup.dpkg_path_aliases("/usr/bin/bash"))
        self.assertIn("/usr/bin/bash", backup.dpkg_path_aliases("/bin/bash"))
        self.assertIn("/sbin/route", backup.dpkg_path_aliases("/usr/sbin/route"))
        self.assertIn("/usr/lib/x86_64-linux-gnu/libX11.so.6",
                      backup.dpkg_path_aliases("/lib/x86_64-linux-gnu/libX11.so.6"))


class DeployIntegrationTests(unittest.TestCase):
    def test_code_reapplies_after_dependencies_without_version_pins(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            home, kit = base / "home", base / "kit"
            home.mkdir()
            (kit / "config").mkdir(parents=True)
            snapshot = base / "snapshot"
            files = {
                "bin/b": "#!/usr/bin/env bash\necho b\n",
                "bin/a": "#!/usr/bin/env bash\n$HOME/bin/b\n",
            }
            for name, value in files.items():
                path = snapshot / "home" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
                path.chmod(0o700)
            (snapshot / "inventory").mkdir(parents=True)
            (snapshot / "inventory/apt-portable.txt").write_text("pkg\n")
            (snapshot / "inventory/source-system.json").write_text(json.dumps({"home": str(home)}))
            backup.capture_developments(snapshot, source_home=snapshot / "home")
            backup.refresh_manifest(snapshot)
            args = argparse.Namespace(snapshot=str(snapshot), apply=True, overwrite=True,
                                      packages=False, snaps=False, flatpaks=False,
                                      desktop_settings=False, build=False)
            saved = backup.HOME, backup.KIT
            backup.HOME, backup.KIT = home, kit
            output = io.StringIO()
            try:
                with redirect_stdout(output):
                    backup.deploy(args)
            finally:
                backup.HOME, backup.KIT = saved
            text = output.getvalue()
            self.assertTrue((home / "bin/a").is_file())
            self.assertIn("Reapplied development: bin/b", text)
            self.assertIn("Reapplied development: bin/a", text)
            self.assertLess(text.index("Reapplied development: bin/b"),
                            text.index("Reapplied development: bin/a"))
            plan = json.loads((kit / "plans/last-deploy.json").read_text())
            self.assertFalse(plan["package_versions_pinned"])


class PackageDriftTests(unittest.TestCase):
    def test_newer_candidate_is_supported_and_reported(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            snapshot = Path(directory) / "snapshot"
            (snapshot / "inventory").mkdir(parents=True)
            (snapshot / "inventory/apt-portable.txt").write_text("pkg\nmissing\n")
            (snapshot / "inventory/package-baseline.json").write_text(json.dumps({
                "format": "ubuntu-package-baseline-v1", "pin_versions": False,
                "packages": [{"name": "pkg", "version": "1.0"}],
            }))
            policy = ("pkg:\n  Installed: 2.0\n  Candidate: 2.5\n"
                      "missing:\n  Installed: (none)\n  Candidate: (none)\n")

            def fake_run(command, **kwargs):
                output = "amd64\n" if command[0] == "dpkg" else policy
                return subprocess.CompletedProcess(command, 0, output, "")

            saved = backup.subprocess.run
            backup.subprocess.run = fake_run
            try:
                supported, unavailable = backup.apt_plan(["pkg", "missing"])
                self.assertEqual(supported, ["pkg"])
                self.assertEqual(unavailable, ["missing"])
                status = backup.apt_status(["pkg"])
                drift = backup.package_drift(snapshot, status)
                self.assertEqual(drift, [{"package": "pkg", "saved": "1.0", "candidate": "2.5"}])
                self.assertEqual(backup.saved_package_names(snapshot), ["missing", "pkg"])
            finally:
                backup.subprocess.run = saved

    def test_names_come_from_baseline_when_legacy_lists_are_absent(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            snapshot = Path(directory) / "snapshot"
            (snapshot / "inventory").mkdir(parents=True)
            (snapshot / "inventory/package-baseline.json").write_text(json.dumps({
                "format": "ubuntu-package-baseline-v1", "pin_versions": False,
                "packages": [{"name": "pkg", "version": "1.0"}, {"name": "other", "version": "2.0"}],
            }))
            self.assertEqual(backup.saved_package_names(snapshot), ["other", "pkg"])


class PackageDeltaTests(unittest.TestCase):
    def test_delta_separates_additions_defaults_and_removals(self):
        installed = {"ubuntu-desktop", "gnome-shell", "docker-ce", "curl", "zlib1g"}
        manual = {"ubuntu-desktop", "docker-ce", "curl"}
        statuses = {
            "ubuntu-desktop": "ii", "gnome-shell": "ii", "docker-ce": "ii",
            "curl": "ii", "zlib1g": "ii", "old-default": "rc",
        }
        closure = {"ubuntu-desktop", "gnome-shell", "curl", "old-default"}
        held = {"docker-ce", "phantom-hold"}
        history = {
            "events": 2, "installed": ["docker-ce"],
            "upgraded": [{"package": "curl", "from": "1.0", "to": "2.0"}],
            "removed": ["old-default"], "purged": [], "removed_all": ["old-default"],
        }
        delta = backup.package_delta(installed, manual, statuses, closure,
                                     ["ubuntu-desktop"], held, history, {"old-default"})
        self.assertEqual(delta["added_manual"], ["docker-ce"])
        self.assertEqual(delta["default_manual"], ["curl", "ubuntu-desktop"])
        self.assertEqual(delta["added_dependencies"], ["zlib1g"])
        self.assertEqual(delta["held_installed"], ["docker-ce"])
        self.assertEqual(delta["counts"]["held_not_installed"], 1)
        self.assertEqual(delta["removed"], ["old-default"])
        self.assertEqual(delta["removed_from_default"], ["old-default"])
        self.assertEqual(delta["removed_traces"], ["old-default"])
        self.assertEqual(delta["apt_history"]["upgraded"][0]["to"], "2.0")

    def test_apt_history_parsing_reads_installs_upgrades_and_removals(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "history.log"
            path.write_text(
                "Start-Date: 2026-11-01  09:38:39\n"
                "Install: docker-ce:amd64 (5:27, automatic), curl (1.0)\n"
                "Upgrade: curl:amd64 (1.0, 1.1, automatic)\n"
                "Remove: old-default:amd64 (2.0)\n"
                "Purge: gone:amd64 (3.0)\n"
                "End-Date: 2026-11-01  09:39:00\n")
            history = backup.parse_apt_history([path])
        self.assertEqual(history["events"], 1)
        self.assertEqual(history["installed"], ["curl", "docker-ce"])
        self.assertEqual(history["removed"], ["old-default"])
        self.assertEqual(history["purged"], ["gone"])
        self.assertEqual(history["removed_all"], ["gone", "old-default"])
        self.assertEqual(history["upgraded"], [{"package": "curl", "from": "1.0", "to": "1.1"}])

    def test_apt_history_reads_rotated_gzip_logs(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "history.log.1.gz"
            with gzip.open(path, "wt") as stream:
                stream.write("Start-Date: 2026-10-01  09:00:00\n"
                             "Remove: old-default:amd64 (2.0)\n"
                             "End-Date: 2026-10-01  09:01:00\n")
            history = backup.parse_apt_history([path])
        self.assertEqual(history["removed_all"], ["old-default"])
        self.assertEqual(history["events"], 1)


class FrontendPackageTests(unittest.TestCase):
    def test_latest_config_snapshot_prefers_a_snapshot_with_package_scan(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            snapshot = kit / "snapshot"
            (snapshot / "inventory").mkdir(parents=True)
            (snapshot / "manifest.json").write_text("{}")
            (snapshot / "inventory/package-integrity.json").write_text("{}")
            saved = backup.KIT
            backup.KIT = kit
            try:
                with mock.patch.object(backup, "settings",
                                       return_value={"external_backup": None}):
                    self.assertEqual(backup.latest_config_snapshot(), snapshot)
            finally:
                backup.KIT = saved

    def test_package_scan_summary_reads_the_privileged_cache(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            cache = kit / "privileged-package-scan"
            cache.mkdir()
            (cache / "package-integrity.json").write_text(json.dumps({
                "captured_at": backup.dt.datetime.now(backup.dt.timezone.utc).isoformat(),
                "counts": {"captured": 2, "modified_files": 3},
            }))
            saved = backup.KIT
            backup.KIT = kit
            try:
                summary = backup.package_scan_summary()
            finally:
                backup.KIT = saved
        self.assertIn("2 captured", summary)
        self.assertIn("3 modified", summary)


class ReleaseCompatibilityTests(unittest.TestCase):
    def test_release_tuple_handles_point_releases(self):
        self.assertEqual(backup.release_tuple("26.10"), (26, 10))
        self.assertEqual(backup.release_tuple("26.04.1"), (26, 4))
        self.assertIsNone(backup.release_tuple(""))
        self.assertIsNone(backup.release_tuple("unknown"))

    def test_supported_range_includes_intermediate_and_2610(self):
        for version in ("22.04", "24.04", "24.10", "25.04", "25.10", "26.04", "26.10"):
            result = backup.release_support({"VERSION_ID": version,
                                             "UBUNTU_CODENAME": "test"})
            self.assertTrue(result["supported"], version)
        result = backup.release_support({"VERSION_ID": "26.10",
                                         "UBUNTU_CODENAME": "stonking"})
        self.assertEqual(result["name"], "Stonking Stingray")
        self.assertEqual(result["tested_through"], "26.10")

    def test_releases_outside_the_range_warn(self):
        self.assertFalse(backup.release_support({"VERSION_ID": "20.04"})["supported"])
        self.assertFalse(backup.release_support({"VERSION_ID": "27.04"})["supported"])
        self.assertFalse(backup.release_support({"VERSION_ID": ""})["supported"])


class PackageIntegrityTests(unittest.TestCase):
    def test_parse_dpkg_verify_reads_flags_notes_and_conffiles(self):
        records = backup.parse_dpkg_verify(
            "??5?????? /usr/bin/demo\n"
            "missing   c /etc/demo.conf\n"
            "missing   /var/lib/demo/state (Permission denied)\n"
            "????????? /boot/vmlinuz\n"
            "??5?????? c /etc/gdm3/custom.conf\n")
        self.assertEqual(records[0], {"path": "/usr/bin/demo", "flags": "??5??????",
                                      "conffile": False, "note": ""})
        self.assertTrue(records[1]["conffile"])
        self.assertEqual(records[2]["note"], "Permission denied")
        self.assertEqual(records[3]["flags"], "?????????")
        self.assertTrue(records[4]["conffile"])

    def test_conffile_drift_detects_modified_missing_and_obsolete(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            same, changed, missing, obsolete = (base / "same.conf", base / "changed.conf",
                                                base / "missing.conf", base / "old.conf")
            same.write_text("same")
            changed.write_text("changed")
            obsolete.write_text("old")
            status = base / "status"
            status.write_text(
                "Package: demo\nStatus: install ok installed\nConffiles:\n"
                f" {same} {hashlib.md5(same.read_bytes()).hexdigest()}\n"
                f" {changed} {'0' * 32}\n"
                f" {missing} {'0' * 32}\n"
                f" {obsolete} {'0' * 32} obsolete\n"
                "Description: demo\n\n"
                "Package: old\nStatus: deinstall ok config-files\nConffiles:\n"
                f" {changed} {'0' * 32}\nDescription: old\n")
            result = backup.conffile_drift(status)
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["modified"], [{"package": "demo", "path": str(changed)}])
        self.assertEqual(result["modified_packages"], ["demo"])
        self.assertEqual(result["missing"], [{"package": "demo", "path": str(missing)}])
        self.assertEqual(result["obsolete"], 1)

    def test_package_integrity_captures_modified_content(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            modified, conffile = base / "modified.bin", base / "demo.conf"
            modified.write_bytes(b"patched content")
            conffile.write_bytes(b"config")
            destination = base / "inventory"
            manifest = backup.package_integrity(
                destination, {}, verify_output=(
                    f"??5?????? {modified}\n"
                    f"??5?????? c {conffile}\n"),
                owners={str(modified): "demo", str(conffile): "demo"},
                conffiles={str(conffile)})
            self.assertEqual(manifest["counts"]["modified_files"], 1)
            self.assertEqual(manifest["counts"]["conffiles"], 1)
            self.assertEqual(manifest["counts"]["captured"], 1)
            record = manifest["records"][0]
            self.assertTrue(record["captured"])
            stored = destination / "package-files" / record["sha256"]
            self.assertEqual(stored.read_bytes(), b"patched content")
            self.assertEqual(manifest["records"][1]["captured"], False)

    def test_restore_worker_writes_with_rollback(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            workspace, snapshot = base / "workspace", base / "snapshot"
            (workspace / "plans").mkdir(parents=True)
            (snapshot / "inventory/package-files").mkdir(parents=True)
            content, current = b"restored content", b"current"
            sha256 = hashlib.sha256(content).hexdigest()
            (snapshot / "inventory/package-files" / sha256).write_bytes(content)
            destination = base / "target.bin"
            destination.write_bytes(current)
            plan = {
                "format": "ubuntu-package-restore-plan-v1", "snapshot": str(snapshot),
                "force": False, "records": [{
                    "package": "demo", "destination": str(destination),
                    "source": str(snapshot / "inventory/package-files" / sha256),
                    "sha256": sha256,
                    "current_sha": hashlib.sha256(current).hexdigest(),
                    "mode": 0o640, "uid": os.getuid(), "gid": os.getgid(),
                }],
            }
            plan_path = workspace / "plans/restore.json"
            plan_path.write_text(json.dumps(plan))
            with mock.patch.object(backup.os, "geteuid", return_value=0), \
                 mock.patch.object(backup.os, "chown"), \
                 mock.patch.object(backup.os, "fchown"), \
                 mock.patch.object(backup, "_dpkg_owner", return_value={"demo"}):
                backup.restore_package_files_worker(plan_path, workspace)
            self.assertEqual(destination.read_bytes(), content)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o640)
            result = json.loads((workspace / "plans/package-files-restore-result.json").read_text())
            self.assertEqual(result["restored"][0]["path"], str(destination))
            rollback = [path for path in (workspace / "rollback").rglob("*") if path.is_file()]
            self.assertEqual(len(rollback), 1)
            self.assertEqual(rollback[0].read_bytes(), current)

    def test_package_integrity_captures_xattrs(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            modified = base / "modified.bin"
            modified.write_bytes(b"patched")
            try:
                os.setxattr(modified, "user.demo", b"value")
            except OSError:
                self.skipTest("user xattrs are unavailable")
            manifest = backup.package_integrity(
                base / "inventory", {}, verify_output=f"??5?????? {modified}\n",
                owners={str(modified): "demo"}, conffiles=set())
            record = manifest["records"][0]
            self.assertEqual(record["xattrs"]["user.demo"],
                             base64.b64encode(b"value").decode("ascii"))

    def test_restore_worker_restores_xattrs(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            workspace, snapshot = base / "workspace", base / "snapshot"
            (workspace / "plans").mkdir(parents=True)
            (snapshot / "inventory/package-files").mkdir(parents=True)
            content = b"restored content"
            sha256 = hashlib.sha256(content).hexdigest()
            (snapshot / "inventory/package-files" / sha256).write_bytes(content)
            destination = base / "target.bin"
            destination.write_bytes(b"current")
            plan = {
                "format": "ubuntu-package-restore-plan-v1", "snapshot": str(snapshot),
                "force": False, "records": [{
                    "package": "demo", "destination": str(destination),
                    "source": str(snapshot / "inventory/package-files" / sha256),
                    "sha256": sha256,
                    "current_sha": hashlib.sha256(b"current").hexdigest(),
                    "mode": 0o640, "uid": os.getuid(), "gid": os.getgid(),
                    "xattrs": {"user.demo": base64.b64encode(b"cap").decode("ascii")},
                }],
            }
            plan_path = workspace / "plans/restore.json"
            plan_path.write_text(json.dumps(plan))
            with mock.patch.object(backup.os, "geteuid", return_value=0), \
                 mock.patch.object(backup.os, "chown"), \
                 mock.patch.object(backup.os, "fchown"), \
                 mock.patch.object(backup, "_dpkg_owner", return_value={"demo"}):
                backup.restore_package_files_worker(plan_path, workspace)
            try:
                self.assertEqual(os.getxattr(destination, "user.demo"), b"cap")
            except OSError:
                self.skipTest("user xattrs are unavailable")

    def test_restore_worker_refuses_a_changed_destination(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            workspace, snapshot = base / "workspace", base / "snapshot"
            (workspace / "plans").mkdir(parents=True)
            (snapshot / "inventory/package-files").mkdir(parents=True)
            content = b"restored content"
            sha256 = hashlib.sha256(content).hexdigest()
            (snapshot / "inventory/package-files" / sha256).write_bytes(content)
            destination = base / "target.bin"
            destination.write_bytes(b"current")
            plan = {
                "format": "ubuntu-package-restore-plan-v1", "snapshot": str(snapshot),
                "force": True, "records": [{
                    "package": "demo", "destination": str(destination),
                    "source": str(snapshot / "inventory/package-files" / sha256),
                    "sha256": sha256, "current_sha": "different",
                    "mode": 0o640, "uid": os.getuid(), "gid": os.getgid(),
                }],
            }
            plan_path = workspace / "plans/restore.json"
            plan_path.write_text(json.dumps(plan))
            with mock.patch.object(backup.os, "geteuid", return_value=0), \
                 mock.patch.object(backup, "_dpkg_owner", return_value={"demo"}), \
                 redirect_stderr(io.StringIO()):
                with self.assertRaises(backup.ConfigError):
                    backup.restore_package_files_worker(plan_path, workspace)
            self.assertEqual(destination.read_bytes(), b"current")

    def test_fresh_privileged_scan_is_reused(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            cache = kit / "privileged-package-scan"
            cache.mkdir()
            (cache / "package-integrity.json").write_text(json.dumps({
                "captured_at": backup.dt.datetime.now(backup.dt.timezone.utc).isoformat()}))
            saved = backup.KIT
            backup.KIT = kit
            try:
                with mock.patch.object(backup.subprocess, "run") as run:
                    self.assertTrue(backup.capture_root_package_scan())
                    run.assert_not_called()
            finally:
                backup.KIT = saved

    def test_forced_privileged_scan_runs_again(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            cache = kit / "privileged-package-scan"
            cache.mkdir()
            (cache / "package-integrity.json").write_text(json.dumps({
                "captured_at": backup.dt.datetime.now(backup.dt.timezone.utc).isoformat()}))
            saved = backup.KIT
            backup.KIT = kit
            try:
                with mock.patch.object(backup, "run_cancellable",
                                       return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                    self.assertTrue(backup.capture_root_package_scan(force=True))
                    run.assert_called_once()
            finally:
                backup.KIT = saved

    def test_capture_worker_publishes_privileged_cache(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()

            def fake_integrity(destination, report):
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "package-integrity.json").write_text("{}")
                return {"counts": {"total": 2, "captured": 1}, "captured_bytes": 10}

            with mock.patch.object(backup.os, "geteuid", return_value=0), \
                 mock.patch.object(backup.os, "chown"), \
                 mock.patch.object(backup, "package_integrity", side_effect=fake_integrity), \
                 mock.patch.object(backup, "conffile_drift", return_value={
                     "checked": 1, "modified": [], "unreadable": [],
                     "missing": [], "obsolete": 0}):
                backup.capture_package_files_worker(workspace)
            cache = workspace / "privileged-package-scan"
            self.assertTrue((cache / "package-integrity.json").is_file())
            self.assertTrue((cache / "package-conffiles.json").is_file())

    def test_merge_privileged_scan_adds_root_only_and_replaces_unreadable(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            workspace, cache = base / "workspace", base / "workspace/privileged-package-scan"
            (cache / "package-files").mkdir(parents=True)
            root_content, user_content = b"root only", b"user file"
            root_sha = hashlib.sha256(root_content).hexdigest()
            user_sha = hashlib.sha256(user_content).hexdigest()
            (cache / "package-files" / root_sha).write_bytes(root_content)
            (cache / "package-files" / user_sha).write_bytes(user_content)
            now = backup.dt.datetime.now(backup.dt.timezone.utc).isoformat()
            (cache / "package-integrity.json").write_text(json.dumps({
                "format": "ubuntu-package-integrity-v1", "captured_at": now,
                "records": [
                    {"path": "/usr/bin/rootonly", "package": "demo", "captured": True,
                     "sha256": root_sha, "size": len(root_content), "mode": 0o755,
                     "uid": 0, "gid": 0, "flags": "??5??????", "conffile": False},
                    {"path": "/usr/bin/userfile", "package": "demo", "captured": True,
                     "sha256": user_sha, "size": len(user_content), "mode": 0o755,
                     "uid": 0, "gid": 0, "flags": "??5??????", "conffile": False},
                ], "counts": {}}))
            (cache / "package-conffiles.json").write_text(json.dumps({
                "checked": 2, "modified": [{"package": "gdm3", "path": "/etc/gdm3/custom.conf"}],
                "modified_packages": ["gdm3"], "unreadable": [], "missing": [], "obsolete": 0}))
            inventory = base / "snapshot/inventory"
            inventory.mkdir(parents=True)
            unreadable = {"path": "/usr/bin/userfile", "package": "demo", "captured": False,
                          "flags": "??5??????", "conffile": False,
                          "note": "Permission denied", "reason": "unreadable without root"}
            stale = {"path": "/usr/bin/stale", "package": "demo", "captured": False,
                     "flags": "missing", "conffile": False,
                     "note": "Permission denied", "reason": "unreadable without root"}
            (inventory / "package-integrity.json").write_text(json.dumps({
                "format": "ubuntu-package-integrity-v1", "records": [unreadable, stale],
                "counts": backup.package_integrity_counts([unreadable, stale])}))
            (inventory / "package-conffiles.json").write_text(json.dumps({
                "checked": 1, "modified": [], "modified_packages": [],
                "unreadable": [{"package": "gdm3", "path": "/etc/gdm3/custom.conf"}],
                "missing": [], "obsolete": 0}))
            (inventory / "package-delta.json").write_text(json.dumps({
                "modified": {"conffiles": 0}, "counts": {"modified_conffiles": 0}}))
            saved = backup.KIT
            backup.KIT = workspace
            try:
                report = {"warnings": []}
                self.assertTrue(backup.merge_privileged_package_scan(base / "snapshot", report))
            finally:
                backup.KIT = saved
            merged = json.loads((inventory / "package-integrity.json").read_text())
            by_path = {record["path"]: record for record in merged["records"]}
            self.assertTrue(by_path["/usr/bin/rootonly"]["captured"])
            self.assertTrue(by_path["/usr/bin/userfile"]["captured"])
            self.assertEqual(by_path["/usr/bin/userfile"]["source"], "privileged-scan")
            self.assertNotIn("/usr/bin/stale", by_path)
            self.assertEqual((inventory / "package-files" / root_sha).read_bytes(), root_content)
            self.assertEqual(merged["counts"]["captured"], 2)
            conffiles = json.loads((inventory / "package-conffiles.json").read_text())
            self.assertEqual(conffiles["modified"],
                             [{"package": "gdm3", "path": "/etc/gdm3/custom.conf"}])
            self.assertEqual(conffiles["unreadable"], [])
            delta = json.loads((inventory / "package-delta.json").read_text())
            self.assertEqual(delta["counts"]["modified_package_files"], 2)
            self.assertEqual(delta["counts"]["modified_conffiles"], 1)

    def test_package_file_plan_detects_already_conflict_and_force(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            snapshot = base / "snapshot"
            records = []
            destinations = []
            for name, current in (("already", b"new"), ("conflict", b"old"), ("missing", None)):
                destination = base / name
                destinations.append(destination)
                if current is not None:
                    destination.write_bytes(current)
                content = b"new"
                digest = hashlib.sha256(content).hexdigest()
                stored = snapshot / "inventory/package-files" / digest
                stored.parent.mkdir(parents=True, exist_ok=True)
                stored.write_bytes(content)
                records.append({"path": str(destination), "package": "demo",
                                "captured": True, "conffile": False,
                                "sha256": digest, "size": len(content),
                                "mode": 0o644, "uid": 0, "gid": 0,
                                "xattrs": {"user.demo": "dmFsdWU="}})
            plan = backup.package_file_plan(snapshot, records)
            self.assertEqual(plan[0]["xattrs"], {"user.demo": "dmFsdWU="})
            self.assertEqual([item["action"] for item in plan],
                             ["already", "conflict", "restore"])
            forced = backup.package_file_plan(snapshot, records, force=True)
            self.assertEqual([item["action"] for item in forced],
                             ["already", "restore", "restore"])
            self.assertEqual(forced[1]["current_sha"],
                             hashlib.sha256(b"old").hexdigest())


class SavedDriveMountTests(unittest.TestCase):
    """Mounting follows /etc/fstab and the on-disk marker, never stored paths."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fstab = self.root / "fstab"
        self.fstab.write_text("")
        self.saved = (backup.KIT, backup.FSTAB_PATH, backup.attached_drives, backup.settings,
                      backup.subprocess.run, backup.system_disk, backup.parent_disk, backup.MOUNT_ROOTS)
        backup.KIT = self.root
        backup.FSTAB_PATH = self.fstab
        backup.MOUNT_ROOTS = (self.root,)
        backup.settings = lambda: {"external_backup": {"subdirectory": ""}}
        backup.system_disk = lambda: "/dev/nvme0n1"
        backup.parent_disk = lambda path: "/dev/sda" if str(path).startswith("/dev/sda") else "/dev/sdz"

    def tearDown(self):
        (backup.KIT, backup.FSTAB_PATH, backup.attached_drives, backup.settings,
         backup.subprocess.run, backup.system_disk, backup.parent_disk, backup.MOUNT_ROOTS) = self.saved

    @staticmethod
    def device(**overrides):
        value = {"path": "/dev/sda1", "uuid": "a8b1104a-7603-4c6e-bc9f-5d16aa7db9b2",
                 "fstype": "ext4", "label": "1", "mountpoints": []}
        value.update(overrides)
        return value

    def marked(self, name="drive", marker_id="id-1"):
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".ubuntu-configuration-target.json").write_text(
            json.dumps({"format": backup.FORMAT, "id": marker_id}))
        return directory

    def test_plan_uses_label_and_never_drops_nofail(self):
        self.assertEqual(backup.default_mount_point(self.device()),
                         Path("/media") / pwd.getpwuid(os.geteuid()).pw_name / "1")
        self.assertEqual(backup.default_mount_point(self.device(label="UBUNTU_BACKUP")),
                         Path("/media") / pwd.getpwuid(os.geteuid()).pw_name / "ubuntu-backup")
        plan = backup.mount_plan(self.device(), self.root / "1")
        self.assertEqual(plan["mount_point"], (self.root / "1").resolve())
        self.assertIn("nofail", plan["options"])
        self.assertFalse(plan["existing"])

    def test_plan_reuses_managed_fstab_entry(self):
        target = self.root / "drive"
        self.fstab.write_text("# Managed by ubuntu-backup v1 (backup drive boot mount)\n"
                              f"UUID=a8b1104a-7603-4c6e-bc9f-5d16aa7db9b2 {target} ext4 "
                              "defaults,nofail 0 2\n")
        plan = backup.mount_plan(self.device(), str(target))
        self.assertTrue(plan["existing"])
        self.assertTrue(backup.fstab_entry(self.device()["uuid"])["managed"])

    def test_plan_refuses_a_second_mount_point(self):
        self.fstab.write_text(f"UUID=a8b1104a-7603-4c6e-bc9f-5d16aa7db9b2 {self.root / 'old'} ext4 defaults 0 2\n")
        with self.assertRaises(backup.ConfigError):
            backup.mount_plan(self.device(), str(self.root / "1"))

    def test_plan_refuses_unformatted_and_non_posix(self):
        with self.assertRaises(backup.ConfigError):
            backup.mount_plan(self.device(fstype=None, uuid=None))
        with self.assertRaises(backup.ConfigError):
            backup.mount_plan(self.device(fstype="ntfs", uuid="54F0B99AF0B982B2"))

    def test_plan_refuses_the_running_system_disk(self):
        backup.parent_disk = lambda path: "/dev/nvme0n1"
        with self.assertRaises(backup.ConfigError):
            backup.mount_plan(self.device(path="/dev/nvme0n1p1"))

    def test_reformatted_drive_is_skipped_without_mounting(self):
        backup.attached_drives = lambda: [self.device(uuid="NEW-1111")]
        backup.mounted_backup_drives = lambda: []
        calls = []
        backup.subprocess.run = lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        output = io.StringIO()
        with redirect_stdout(output):
            state = backup.mount_saved()
        self.assertEqual(state, "absent")
        self.assertEqual(calls, [])
        self.assertIn("managed boot entry", output.getvalue())

    def test_fstab_entry_drives_the_mount(self):
        target = self.root / "drive"
        self.fstab.write_text("# Managed by ubuntu-backup v1 (backup drive boot mount)\n"
                              f"UUID=a8b1104a {target} ext4 defaults,nofail 0 2\n")
        backup.attached_drives = lambda: [self.device(uuid="a8b1104a", path="/dev/sdq1")]
        states = [[], [{"directory": target, "uuid": "a8b1104a", "marker": {"id": "x"}}]]
        backup.mounted_backup_drives = lambda: states.pop(0) if states else []
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1 if command[0] == "udisksctl" else 0, "", "")

        backup.subprocess.run = fake_run
        with redirect_stdout(io.StringIO()):
            self.assertEqual(backup.mount_saved(), "mounted")
        self.assertIn(["sudo", "-n", "/usr/bin/mount", str(target)], calls)

    def test_stale_fstab_falls_back_to_the_backup_label(self):
        from unittest import mock
        self.fstab.write_text("# Managed by ubuntu-backup v1 (backup drive boot mount)\n"
                              "UUID=old-unattached /media/old ext4 defaults,nofail 0 2\n")
        target = self.root / "drive"
        target.mkdir()
        backup.attached_drives = lambda: [self.device(
            label="ubuntu-backup", fstype="ext4", mountpoints=[], uuid="new-uuid")]
        states = [[], [{"directory": target, "uuid": "new-uuid", "marker": {"id": "x"}}]]
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        backup.subprocess.run = fake_run
        with mock.patch.object(backup, "mounted_backup_drives",
                               side_effect=lambda: states.pop(0) if states else []), \
             mock.patch.object(backup.shutil, "which", return_value="/usr/bin/udisksctl"), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(backup.mount_saved(), "mounted")
        self.assertIn(["udisksctl", "mount", "-b", "/dev/sda1", "--no-user-interaction"], calls)

    def test_quiet_mode_is_silent_for_routine_states(self):
        backup.attached_drives = lambda: []
        backup.mounted_backup_drives = lambda: []
        backup.subprocess.run = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", "")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(backup.mount_saved(quiet=True), "absent")
        self.assertEqual(output.getvalue(), "")

    def test_no_saved_drive_is_a_quiet_noop(self):
        backup.settings = lambda: {"external_backup": None}
        calls = []
        backup.subprocess.run = lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(backup.mount_saved(), "unset")
        self.assertEqual(calls, [])

    def test_external_directory_comes_from_the_disk_marker(self):
        directory = self.marked("drive")
        backup.on_source_filesystem = lambda path: False
        backup.attached_drives = lambda: [self.device(uuid="x", mountpoints=[str(directory)])]
        self.assertEqual(backup.external_directory({"external_backup": {"subdirectory": ""}}), directory)

    def test_changed_marker_identity_is_rejected(self):
        directory = self.marked("drive", "id-1")
        backup.on_source_filesystem = lambda path: False
        backup.attached_drives = lambda: [self.device(uuid="x", mountpoints=[str(directory)])]
        with self.assertRaises(backup.ConfigError):
            backup.external_directory({"external_backup": {
                "subdirectory": "", "marker_id": "id-2"}})
        self.assertEqual(backup.external_directory({"external_backup": {
            "subdirectory": "", "marker_id": "id-1"}}), directory)

    def test_unmarked_and_two_marked_mounts(self):
        plain = self.root / "plain"
        plain.mkdir()
        backup.on_source_filesystem = lambda path: False
        backup.attached_drives = lambda: [self.device(mountpoints=[str(plain)])]
        with self.assertRaises(backup.ConfigError):
            backup.external_directory({"external_backup": {"subdirectory": ""}})
        first, second = self.marked("a", "id-a"), self.marked("b", "id-b")
        backup.attached_drives = lambda: [self.device(mountpoints=[str(first)]),
                                          self.device(mountpoints=[str(second)])]
        with self.assertRaises(backup.ConfigError):
            backup.external_directory({"external_backup": {"subdirectory": ""}})

    def test_settings_store_no_drive_paths(self):
        mount = self.root / "mount"
        mount.mkdir()
        saved = (backup.mount_info, backup.validate_external_directory, backup.on_source_filesystem)
        backup.mount_info = lambda path: {"uuid": "a8b1104a", "fstype": "ext4"}
        backup.validate_external_directory = lambda path: path
        backup.on_source_filesystem = lambda path: False
        try:
            with redirect_stdout(io.StringIO()):
                backup.set_external(str(mount))
        finally:
            backup.mount_info, backup.validate_external_directory, backup.on_source_filesystem = saved
        marker = json.loads((mount / ".ubuntu-configuration-target.json").read_text())
        stored = json.loads((self.root / "config/settings.json").read_text())
        self.assertEqual(stored["external_backup"]["subdirectory"], "")
        self.assertEqual(stored["external_backup"]["marker_id"], marker["id"])

    def test_ext4_lost_and_found_does_not_block_adoption(self):
        mount = self.root / "mount"
        (mount / "lost+found").mkdir(parents=True)
        saved = (backup.mount_info, backup.validate_external_directory, backup.on_source_filesystem)
        backup.mount_info = lambda path: {"uuid": "a8b1104a", "fstype": "ext4"}
        backup.validate_external_directory = lambda path: path
        backup.on_source_filesystem = lambda path: False
        try:
            with redirect_stdout(io.StringIO()):
                backup.set_external(str(mount))
            marker = json.loads((mount / ".ubuntu-configuration-target.json").read_text())
            self.assertEqual(marker["format"], backup.FORMAT)
            stored = json.loads((self.root / "config/settings.json").read_text())
            self.assertEqual(stored["external_backup"]["subdirectory"], "")
            self.assertEqual(stored["external_backup"]["marker_id"], marker["id"])
        finally:
            backup.mount_info, backup.validate_external_directory, backup.on_source_filesystem = saved

    def test_unrelated_nonempty_mount_is_still_refused(self):
        mount = self.root / "mount"
        mount.mkdir()
        (mount / "not-a-backup").write_text("keep me\n")
        saved = (backup.mount_info, backup.on_source_filesystem)
        backup.mount_info = lambda path: {"uuid": "a8b1104a", "fstype": "ext4"}
        backup.on_source_filesystem = lambda path: False
        try:
            with self.assertRaises(backup.ConfigError):
                backup.set_external(str(mount))
        finally:
            backup.mount_info, backup.on_source_filesystem = saved
        self.assertTrue((mount / "not-a-backup").is_file())

    def test_mount_drive_needs_a_terminal_for_sudo(self):
        saved = backup.sys.stdin
        backup.sys.stdin = io.StringIO("")
        try:
            with redirect_stdout(io.StringIO()), self.assertRaises(backup.ConfigError):
                backup.mount_drive(self.device(), str(self.root / "1"))
        finally:
            backup.sys.stdin = saved


class HomeCaptureNativeTests(unittest.TestCase):
    def test_native_worker_is_the_only_worker(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            log = Path(directory) / "home-worker.log"
            calls = []

            def fake_run(command, path):
                calls.append((list(command), Path(path)))

            saved_run = backup.run_logged
            backup.run_logged = fake_run
            try:
                backup.run_home_worker(["sudo", "python"], log)
            finally:
                backup.run_logged = saved_run
        self.assertEqual(calls, [(["sudo", "python"], log)])

    def test_missing_native_core_is_fatal(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            saved = backup.KIT, backup.SCRIPT_PATH, os.environ.pop("UBUNTU_BACKUP_NATIVE", None)
            backup.KIT = base / "workspace" / "data"
            backup.SCRIPT_PATH = base / "bin" / "ubuntu-backup.py"
            try:
                with self.assertRaises(backup.ConfigError) as caught:
                    backup.native_core_path()
            finally:
                backup.KIT, backup.SCRIPT_PATH = saved[0], saved[1]
                if saved[2] is not None:
                    os.environ["UBUNTU_BACKUP_NATIVE"] = saved[2]
        self.assertIn("native backup core is required", str(caught.exception))


class NativeRestoreIntegrationTests(unittest.TestCase):
    def test_restore_home_uses_the_native_core(self):
        if shutil.which("cc") is None:
            self.skipTest("cc is unavailable")
        build = subprocess.run(["make", "-C", str(ROOT / "native")],
                               capture_output=True, text=True)
        if build.returncode:
            self.skipTest("native core build failed")
        core = ROOT / "native" / "ubackup-core"
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            (source / "dir").mkdir(parents=True)
            (source / "file.txt").write_text("hello\n")
            (source / "dir/nested.txt").write_text("nested\n")
            (source / "link").symlink_to("file.txt")
            root = base / "root"
            objects = root / "baseline/home/objects"
            timeline = root / "timeline/home"
            timeline.mkdir(parents=True)
            scan = subprocess.run([
                str(core), "scan", "--source", str(source),
                "--objects", str(objects),
                "--entries", str(base / "entries.ndjson"),
                "--report", str(base / "scan.json"),
                "--timeline", str(timeline),
                "--run-id", "20261112T120000.000000Z",
            ], capture_output=True, text=True)
            self.assertEqual(scan.returncode, 0, scan.stderr)
            saved = backup.KIT, os.environ.get("UBUNTU_BACKUP_NATIVE")
            backup.KIT = base / "kit"
            backup.KIT.mkdir()
            os.environ["UBUNTU_BACKUP_NATIVE"] = str(core)
            try:
                destination = backup.restore_home(
                    timeline / "home-full-20261112T120000.000000Z")
            finally:
                backup.KIT = saved[0]
                if saved[1] is None:
                    os.environ.pop("UBUNTU_BACKUP_NATIVE", None)
                else:
                    os.environ["UBUNTU_BACKUP_NATIVE"] = saved[1]
            self.assertEqual((destination / "file.txt").read_text(), "hello\n")
            self.assertEqual((destination / "dir/nested.txt").read_text(), "nested\n")
            self.assertEqual((destination / "link").readlink(), Path("file.txt"))
            self.assertTrue((destination.parent / (destination.name + "-restore.json")).is_file())

    def test_restore_home_cancel_removes_partial_destination(self):
        if shutil.which("cc") is None:
            self.skipTest("cc is unavailable")
        build = subprocess.run(["make", "-C", str(ROOT / "native")],
                               capture_output=True, text=True)
        if build.returncode:
            self.skipTest("native core build failed")
        core = ROOT / "native" / "ubackup-core"
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "file.txt").write_text("hello\n")
            root = base / "root"
            objects = root / "baseline/home/objects"
            timeline = root / "timeline/home"
            timeline.mkdir(parents=True)
            scan = subprocess.run([
                str(core), "scan", "--source", str(source),
                "--objects", str(objects),
                "--entries", str(base / "entries.ndjson"),
                "--report", str(base / "scan.json"),
                "--timeline", str(timeline),
                "--run-id", "20261112T120001.000000Z",
            ], capture_output=True, text=True)
            self.assertEqual(scan.returncode, 0, scan.stderr)
            slow = base / "slow-core"
            slow.write_text("#!/bin/sh\nsleep 30\n")
            slow.chmod(0o700)
            saved = backup.KIT, os.environ.get("UBUNTU_BACKUP_NATIVE")
            backup.KIT = base / "kit"
            backup.KIT.mkdir()
            os.environ["UBUNTU_BACKUP_NATIVE"] = str(slow)
            result = {}

            def run_restore():
                try:
                    backup.restore_home(timeline / "home-full-20261112T120001.000000Z")
                except BaseException as exc:
                    result["error"] = exc

            thread = threading.Thread(target=run_restore, daemon=True)
            try:
                thread.start()
                time.sleep(0.5)
                backup._request_cancel()
                thread.join(15)
            finally:
                backup._finish_operation_cancel()
                backup.KIT = saved[0]
                if saved[1] is None:
                    os.environ.pop("UBUNTU_BACKUP_NATIVE", None)
                else:
                    os.environ["UBUNTU_BACKUP_NATIVE"] = saved[1]
            self.assertIsInstance(result.get("error"), backup.OperationCancelled)
            self.assertEqual(list((backup.KIT / "restored").glob("home-full-*")), [])


class OperationCancelTests(unittest.TestCase):
    def test_cancel_file_requests_and_clears(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.KIT
            backup.KIT = Path(directory)
            try:
                backup._reset_operation_cancel()
                self.assertFalse(backup._cancel_requested())
                backup._request_cancel()
                self.assertTrue(backup._cancel_requested())
                with self.assertRaises(backup.OperationCancelled):
                    backup._check_cancel()
                backup._finish_operation_cancel()
                self.assertFalse(backup._cancel_requested())
            finally:
                backup.KIT = saved

    def test_run_cancellable_terminates_a_child(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.KIT
            backup.KIT = Path(directory)
            try:
                backup._reset_operation_cancel()
                threading.Thread(
                    target=lambda: (time.sleep(0.5), backup._request_cancel()),
                    daemon=True,
                ).start()
                started = time.monotonic()
                with self.assertRaises(backup.OperationCancelled):
                    backup.run_cancellable(["/bin/sh", "-c", "sleep 30"])
                self.assertLess(time.monotonic() - started, 10)
            finally:
                backup._finish_operation_cancel()
                backup.KIT = saved

    def test_run_logged_cancels_and_keeps_output(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.KIT
            backup.KIT = Path(directory)
            log = Path(directory) / "worker.log"
            try:
                backup._reset_operation_cancel()
                threading.Thread(
                    target=lambda: (time.sleep(0.5), backup._request_cancel()),
                    daemon=True,
                ).start()
                with self.assertRaises(backup.OperationCancelled):
                    backup.run_logged(
                        ["/bin/sh", "-c", "echo started; sleep 30"], log)
                self.assertIn("started", log.read_text())
            finally:
                backup._finish_operation_cancel()
                backup.KIT = saved


class FullHomeSafetyTests(unittest.TestCase):
    def test_home_backup_lock_rejects_a_second_capture(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.KIT
            backup.KIT = Path(directory)
            try:
                with backup.home_backup_lock():
                    with self.assertRaises(backup.ConfigError):
                        with backup.home_backup_lock():
                            pass
            finally:
                backup.KIT = saved

    def test_home_identity_ignores_unrelated_hardware(self):
        hardware = {
            "architecture": "x86_64", "vendor": "vendor", "model": "model",
            "complete": True,
            "disks": [{"uuid": "usb-drive"}],
            "network": [{"name": "eth0", "address": "00:11:22:33:44:55"}],
            "gpus": [{"vendor": "gpu"}],
        }
        from unittest import mock
        with mock.patch.object(backup, "hardware_inventory", return_value=hardware), \
             mock.patch.object(backup, "system_disk", return_value="/dev/sda"):
            identity = backup.home_identity()
        self.assertEqual(set(identity["hardware"]),
                         {"architecture", "vendor", "model", "source_disk", "complete"})
        self.assertEqual(identity["hardware"]["source_disk"], "/dev/sda")
        self.assertNotIn("disks", identity["hardware"])
        self.assertNotIn("network", identity["hardware"])


class FullHomeRunIdTests(unittest.TestCase):
    def test_home_worker_receives_the_configuration_run_id(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.KIT
            backup.KIT = Path(directory)
            calls = []
            config = {"external_backup": {"subdirectory": ""}, "home_snapshot": "off"}
            identity = {"hardware": {"complete": True}, "mounts": [{"mount": "/home"}]}

            def fake_run_logged(command, log_path):
                calls.append(command)
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                Path(log_path).write_text("")

            try:
                with (
                    mock.patch.object(backup, "settings", return_value=config),
                    mock.patch.object(backup, "external_directory", return_value=Path("/tmp/drive")),
                    mock.patch.object(backup, "home_identity", return_value=identity),
                    mock.patch.object(backup, "run_logged", side_effect=fake_run_logged),
                    mock.patch.object(backup, "verify_home_timeline", return_value={}),
                ):
                    self.assertTrue(backup.wizard_home_backup("20261106T113637.000000Z"))
            finally:
                backup.KIT = saved
            command = calls[-1]
            self.assertIn("--run-id", command)
            self.assertEqual(command[command.index("--run-id") + 1], "20261106T113637.000000Z")
            self.assertNotIn("--native-capture", command)


class LoggedWorkerTests(unittest.TestCase):
    def test_child_output_is_logged_and_failure_is_reported(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            log_path = Path(directory) / "worker.log"
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(backup.ConfigError) as caught:
                    backup.run_logged(
                        ["/bin/sh", "-c", "echo child-out; echo child-err >&2; exit 7"],
                        log_path)
            self.assertIn("status 7", str(caught.exception))
            text = log_path.read_text()
            self.assertIn("child-out", text)
            self.assertIn("child-err", text)
            self.assertEqual(oct(log_path.stat().st_mode & 0o777), "0o600")


class NativeCaptureFlagTests(unittest.TestCase):
    def test_native_core_path_uses_environment_override(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            binary = Path(directory) / "ubackup-core"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o700)
            saved = os.environ.get("UBUNTU_BACKUP_NATIVE")
            os.environ["UBUNTU_BACKUP_NATIVE"] = str(binary)
            try:
                self.assertEqual(backup.native_core_path(), binary)
            finally:
                if saved is None:
                    os.environ.pop("UBUNTU_BACKUP_NATIVE", None)
                else:
                    os.environ["UBUNTU_BACKUP_NATIVE"] = saved

    def test_native_home_command_includes_ownership_and_timeline(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            binary = Path(directory) / "ubackup-core"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o700)
            saved = os.environ.get("UBUNTU_BACKUP_NATIVE")
            os.environ["UBUNTU_BACKUP_NATIVE"] = str(binary)
            try:
                command = backup.native_home_command(
                    Path("/home"), Path("/media/backup"),
                    "20261106T160000.000000Z", 1000, 1000,
                    {"host": "test"}, ["ubuntu-backup/data/backups"])
            finally:
                if saved is None:
                    os.environ.pop("UBUNTU_BACKUP_NATIVE", None)
                else:
                    os.environ["UBUNTU_BACKUP_NATIVE"] = saved
            self.assertEqual(command[0], str(binary))
            self.assertIn("--timeline", command)
            self.assertEqual(command[command.index("--timeline") + 1],
                             "/media/backup/timeline/home")
            self.assertEqual(command[command.index("--stage") + 1],
                             str(backup.KIT / "staging"))
            self.assertEqual(command[command.index("--owner-uid") + 1], "1000")
            self.assertIn("--exclude-prefix", command)
            self.assertEqual(command[command.index("--identity-json") + 1],
                             '{"host":"test"}')

    def test_home_worker_has_no_native_capture_switch(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.KIT
            backup.KIT = Path(directory)
            calls = []
            config = {"external_backup": {"subdirectory": ""}, "home_snapshot": "off"}
            identity = {"hardware": {"complete": True}, "mounts": [{"mount": "/home"}]}

            def fake_run_logged(command, log_path):
                calls.append(command)
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                Path(log_path).write_text("")

            try:
                with (
                    mock.patch.object(backup, "settings", return_value=config),
                    mock.patch.object(backup, "external_directory", return_value=Path("/tmp/drive")),
                    mock.patch.object(backup, "home_identity", return_value=identity),
                    mock.patch.object(backup, "run_logged", side_effect=fake_run_logged),
                    mock.patch.object(backup, "verify_home_timeline", return_value={}),
                ):
                    self.assertTrue(backup.wizard_home_backup("20261106T113637.000000Z"))
            finally:
                backup.KIT = saved
            self.assertNotIn("--native-capture", calls[-1])
            self.assertTrue(calls[-1][-1] == "20261106T113637.000000Z")


class PrepareDriveIntegrationTests(unittest.TestCase):
    def test_prepare_new_drive_formats_mounts_and_selects(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            mount = root / "mnt"
            mount.mkdir()
            device_path = root / "fake-device"
            device_path.touch()
            part_path = "/dev/fake-part"
            size = 8 * 1024 ** 3
            disk = {
                "path": str(device_path), "type": "disk", "removable": True,
                "transport": "usb", "mountpoints": [], "size": size,
                "fstype": None, "uuid": None, "label": None, "model": "test drive",
            }
            child = {
                "path": part_path, "type": "part", "removable": True,
                "transport": "usb", "mountpoints": ["/media/test"], "size": size,
                "fstype": "ext4", "uuid": "old", "label": None, "model": "test drive",
            }
            initial = [disk, child]
            initial_unmounted = [disk, dict(child, mountpoints=[])]
            formatted = [dict(disk, fstype="ext4", uuid="a8b1104a")]
            phrase = f"FORMAT {device_path} {size}"
            config = {"keep_snapshots": 30, "keep_local_backups": False,
                      "external_backup": None, "home_snapshot": "off",
                      "coverage": "system-wide-configuration", "development_include": [],
                      "home_include": [], "home_reference_only": [],
                      "system_reference_only": [], "exclude": [], "apt_review_only": [],
                      "exclude_cpus": []}
            saved = backup.KIT
            backup.KIT = root
            calls = []
            mount_drive = mock.Mock(return_value=str(mount))

            def fake_run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            try:
                def parent(value):
                    return str(device_path) if value == part_path else "/dev/sda"

                with mock.patch.object(backup, "attached_drives",
                                       side_effect=[initial, initial, initial_unmounted, formatted]), \
                     mock.patch.object(backup, "system_disk", return_value="/dev/nvme0n1"), \
                     mock.patch.object(backup, "parent_disk", side_effect=parent), \
                     mock.patch.object(backup, "mount_drive", mount_drive), \
                     mock.patch.object(backup, "mount_info",
                                       return_value={"uuid": "a8b1104a", "fstype": "ext4"}), \
                     mock.patch.object(backup, "validate_external_directory",
                                       side_effect=lambda path: path), \
                     mock.patch.object(backup, "on_source_filesystem", return_value=False), \
                     mock.patch.object(backup, "settings", return_value=dict(config)), \
                     mock.patch.object(backup.shutil, "which",
                                       side_effect=lambda name: "/usr/bin/" + name), \
                     mock.patch.object(backup.stat, "S_ISBLK", return_value=True), \
                     mock.patch.object(backup.sys.stdin, "isatty", return_value=True), \
                     mock.patch.object(backup, "ask", side_effect=["1", phrase]), \
                     mock.patch.object(backup, "confirm", return_value=True), \
                     mock.patch.object(backup.subprocess, "run", side_effect=fake_run), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = backup.wizard_prepare_drive()
            finally:
                backup.KIT = saved

            self.assertEqual(result, str(mount))
            self.assertEqual(calls[0], ["sudo", "umount", "/media/test"])
            self.assertEqual(calls[1], ["sudo", "wipefs", "-a", str(device_path)])
            self.assertEqual(calls[2], ["sudo", "mkfs.ext4", "-F", "-L", "ubuntu-backup",
                                        str(device_path)])
            self.assertEqual(mount_drive.call_count, 1)
            self.assertIsInstance(mount_drive.call_args.args[1], str)
            marker = json.loads((mount / ".ubuntu-configuration-target.json").read_text())
            self.assertEqual(marker["format"], backup.FORMAT)
            stored = json.loads((root / "config/settings.json").read_text())
            self.assertEqual(stored["external_backup"]["subdirectory"], "")
            self.assertEqual(stored["external_backup"]["marker_id"], marker["id"])


class DriveFormatTests(unittest.TestCase):
    def test_format_refuses_unsafe_devices(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "fake-device"
            path.touch()
            base = {"path": str(path), "type": "disk", "removable": True,
                    "transport": "usb", "mountpoints": [], "size": 8 * 1024 ** 3,
                    "fstype": None, "uuid": None, "label": None}
            cases = [
                ("system", [dict(base)], "/dev/sda", "/dev/sda"),
                ("non-usb", [dict(base, removable=False, transport="sata")], "/dev/sdb", "/dev/nvme0n1"),
                ("unsupported-type", [dict(base, type="crypt")], "/dev/sdb", "/dev/nvme0n1"),
                ("partition", [dict(base, type="part")], "/dev/sdb", "/dev/nvme0n1"),
            ]
            for name, devices, parent, system in cases:
                with self.subTest(name=name), \
                     mock.patch.object(backup, "attached_drives", return_value=devices), \
                     mock.patch.object(backup, "system_disk", return_value=system), \
                     mock.patch.object(backup, "parent_disk",
                                       side_effect=lambda item: parent), \
                     mock.patch.object(backup.stat, "S_ISBLK", return_value=True), \
                     mock.patch.object(backup.sys.stdin, "isatty", return_value=True), \
                     mock.patch.object(backup.subprocess, "run") as run:
                    with self.assertRaises(backup.ConfigError):
                        backup.format_drive(str(path))
                    run.assert_not_called()

    def test_candidates_skip_system_partitioned_and_non_usb_disks(self):
        from unittest import mock
        gib = 1024 ** 3
        devices = [
            {"path": "/dev/sda", "type": "disk", "removable": True, "transport": "usb",
             "mountpoints": [], "size": 8 * gib},
            {"path": "/dev/sda1", "type": "part", "removable": True, "transport": "usb",
             "mountpoints": [], "size": 8 * gib},
            {"path": "/dev/sdb", "type": "disk", "removable": True, "transport": "usb",
             "mountpoints": [], "size": 8 * gib},
            {"path": "/dev/sdc", "type": "disk", "removable": False, "transport": "sata",
             "mountpoints": [], "size": 8 * gib},
            {"path": "/dev/sdd", "type": "disk", "removable": True, "transport": "usb",
             "mountpoints": ["/media/x"], "size": 8 * gib},
            {"path": "/dev/sde", "type": "disk", "removable": True, "transport": "usb",
             "mountpoints": [], "size": 1 * gib},
            {"path": "/dev/nvme0n1p1", "type": "part", "removable": False, "transport": None,
             "mountpoints": [], "size": 8 * gib},
        ]

        def parent(path):
            if path.startswith("/dev/sda"):
                return "/dev/sda"
            if path == "/dev/nvme0n1p1":
                return "/dev/nvme0n1"
            return path

        with mock.patch.object(backup, "attached_drives", return_value=devices), \
             mock.patch.object(backup, "system_disk", return_value="/dev/nvme0n1"), \
             mock.patch.object(backup, "parent_disk", side_effect=parent):
            candidates = backup._format_candidates()
        self.assertEqual([item["path"] for item in candidates],
                         ["/dev/sda", "/dev/sdb", "/dev/sdd"])


class SnapshotCapabilityTests(unittest.TestCase):
    def test_auto_falls_back_and_required_refuses(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            with mock.patch.object(backup, "_snapshot_btrfs_info", return_value=None), \
                 mock.patch.object(backup, "_snapshot_zfs_info", return_value=None), \
                 mock.patch.object(backup, "_snapshot_lvm_info", return_value=None):
                self.assertIsNone(backup.create_home_snapshot({"home_snapshot": "off"}, kit))
                with redirect_stderr(io.StringIO()):
                    self.assertIsNone(backup.create_home_snapshot({"home_snapshot": "auto"}, kit))
                with self.assertRaises(backup.ConfigError):
                    backup.create_home_snapshot({"home_snapshot": "required"}, kit)


class HomeTimelineVerificationTests(unittest.TestCase):
    def test_same_size_object_corruption_is_detected(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            data = b"hello"
            sha256 = hashlib.sha256(data).hexdigest()
            timeline = base / "timeline/home/home-full-20260101T000000.000000Z"
            tree = timeline / "tree"
            tree.mkdir(parents=True)
            objects = base / "baseline/home/objects"
            stored = backup.object_path(objects, sha256)
            stored.parent.mkdir(parents=True)
            stored.write_bytes(data)
            (tree / "file").symlink_to(os.path.relpath(stored, tree))
            entries = timeline / "entries.ndjson"
            entries.write_text(json.dumps({
                "path": "file", "type": "file", "size": len(data), "sha256": sha256,
            }) + "\n")
            (timeline / "manifest.json").write_text(json.dumps({
                "format": backup.FORMAT, "kind": "full-home-linked",
                "storage": backup.LINKED_STORAGE, "entry_count": 1,
                "logical_bytes": len(data), "entries_sha256": backup.digest(entries),
            }))
            backup.verify_home_timeline(timeline)
            stored.write_bytes(b"HELLO")  # same size, different bytes
            with self.assertRaises(backup.ConfigError):
                backup.verify_home_timeline(timeline)


class RunIdentityTests(unittest.TestCase):
    def test_timeline_and_history_share_the_same_run_id(self):
        run_id = "20261106T113637.000000Z"
        self.assertEqual(backup.dated_config_name(run_id), "ubuntu-config-" + run_id)
        self.assertEqual(backup.history_timestamp(run_id), "2026-11-06 11:36:37Z")
        with self.assertRaises(backup.ConfigError):
            backup.dated_config_name("../escape")


class BackupWarningTests(unittest.TestCase):
    def test_warning_run_reports_omissions_and_sets_exit_code(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = (backup.KIT, backup._STATUS_RUN, backup._EXIT_CODE,
                     backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS)
            backup.KIT, backup._STATUS_RUN, backup._EXIT_CODE = Path(directory), False, 0
            run_id = "20261106T113637.000000Z"
            calls = []

            def fake_impl(skip_external=False, **kwargs):
                backup._LAST_BACKUP_RUN_ID = run_id
                backup._LAST_BACKUP_WARNINGS = 2
                return "/tmp/backup"

            try:
                with (
                    mock.patch.object(backup, "_backup_impl", side_effect=fake_impl),
                    mock.patch.object(backup, "_diagnose_previous_status"),
                    mock.patch.object(backup, "_write_status",
                                      side_effect=lambda *args, **kwargs: calls.append(("status", args, kwargs))),
                    mock.patch.object(backup, "_status_history",
                                      side_effect=lambda *args, **kwargs: calls.append(("history", args, kwargs))),
                ):
                    self.assertEqual(backup.backup(), "/tmp/backup")
                self.assertEqual(backup._EXIT_CODE, 3)
            finally:
                (backup.KIT, backup._STATUS_RUN, backup._EXIT_CODE,
                 backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS) = saved
            status = next(item for item in calls
                          if item[0] == "status" and item[1][0] == "complete")
            self.assertEqual(status[1][:2], ("complete", "complete"))
            self.assertIn("(warnings): 2 unreadable file(s)", status[1][2])
            self.assertTrue(status[2]["warnings"])
            self.assertEqual(status[2]["omissions"], 2)
            history = next(item for item in calls
                           if item[0] == "history" and item[1][0] == "complete")
            self.assertEqual(history[2]["at"], run_id)


class AtomicFailureTests(unittest.TestCase):
    def test_failed_capture_publishes_no_snapshot_or_timeline(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory)
            (kit / "config").mkdir()
            (kit / "config/settings.json").write_text(json.dumps({
                "home_snapshot": "off", "keep_snapshots": 3, "keep_local_backups": True,
                "coverage": "system-wide-configuration", "backup_calendar": "daily",
                "development_include": [], "home_include": [], "home_reference_only": [],
                "system_reference_only": [], "exclude": [], "apt_review_only": [],
                "exclude_cpus": [], "external_backup": None,
            }))
            saved = backup.KIT, backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS
            backup.KIT = kit
            try:
                with mock.patch.object(backup, "capture_root_package_scan"), \
                     mock.patch.object(backup, "build_snapshot",
                                       side_effect=backup.ConfigError("capture failed")):
                    with self.assertRaises(backup.ConfigError):
                        backup._backup_impl()
            finally:
                backup.KIT, backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS = saved
            self.assertFalse((kit / "snapshot").exists())
            self.assertEqual(list((kit / "backups").iterdir()), [])
            self.assertEqual([path.name for path in kit.iterdir()
                              if path.name.startswith(".snapshot-")], [])


class SystemdWarningTests(unittest.TestCase):
    def test_user_service_accepts_warning_exit_code(self):
        from unittest import mock
        with mock.patch.object(backup, "settings", return_value={"backup_calendar": "daily"}):
            service, timer = backup.systemd_units()
        self.assertIn("SuccessExitStatus=3", service)
        self.assertNotIn("SuccessExitStatus", timer)


class BackupStatusTests(unittest.TestCase):
    def test_cli_backup_publishes_complete_status(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = (backup.KIT, backup._STATUS_RUN, backup._EXIT_CODE,
                     backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS)
            backup.KIT, backup._STATUS_RUN = Path(directory), False
            backup._EXIT_CODE, backup._LAST_BACKUP_WARNINGS = 0, 0
            calls = []
            try:
                with (
                    mock.patch.object(backup, "_backup_impl", return_value="/tmp/backup"),
                    mock.patch.object(backup, "_diagnose_previous_status"),
                    mock.patch.object(backup, "_write_status",
                                      side_effect=lambda *args, **kwargs: calls.append(("status", args))),
                    mock.patch.object(backup, "_status_history",
                                      side_effect=lambda *args, **kwargs: calls.append(("history", args))),
                ):
                    self.assertEqual(backup.backup(), "/tmp/backup")
            finally:
                (backup.KIT, backup._STATUS_RUN, backup._EXIT_CODE,
                 backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS) = saved
            self.assertIn(("status", ("complete", "complete", "Configuration backup complete")),
                          calls)
            self.assertTrue(any(call[0] == "history" for call in calls))

    def test_failed_backup_links_the_diagnostic_log(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = (backup.KIT, backup._STATUS_RUN, backup._EXIT_CODE,
                     backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS)
            backup.KIT, backup._STATUS_RUN = Path(directory), False
            backup._EXIT_CODE, backup._LAST_BACKUP_WARNINGS = 0, 0
            calls = []
            try:
                with (
                    mock.patch.object(backup, "_backup_impl",
                                      side_effect=backup.ConfigError("capture failed")),
                    mock.patch.object(backup, "_diagnose_previous_status"),
                    mock.patch.object(backup, "_write_status",
                                      side_effect=lambda *args, **kwargs: calls.append(("status", args))),
                    mock.patch.object(backup, "_status_history",
                                      side_effect=lambda *args, **kwargs: calls.append(("history", args))),
                ):
                    with self.assertRaises(backup.ConfigError):
                        backup.backup()
                self.assertEqual(backup._EXIT_CODE, 0)
            finally:
                (backup.KIT, backup._STATUS_RUN, backup._EXIT_CODE,
                 backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS) = saved
            history = next(item for item in calls
                           if item[0] == "history" and item[1][0] == "failed")
            self.assertTrue(str(history[1][2]).endswith("logs/ubuntu-backup.log"))


class BackupRestoreFlowTests(unittest.TestCase):
    def test_backup_flow_refreshes_the_package_scan(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            drive = Path(directory) / "drive"
            drive.mkdir()
            with (
                mock.patch.object(backup, "_diagnose_previous_status"),
                mock.patch.object(backup, "_ensure_tray"),
                mock.patch.object(backup, "_write_status"),
                mock.patch.object(backup, "_status_history"),
                mock.patch.object(backup, "package_scan_summary", return_value="Package scan: test"),
                mock.patch.object(backup, "capture_root_package_scan") as scan,
                mock.patch.object(backup, "capture_root") as root_capture,
                mock.patch.object(backup, "settings",
                                   return_value={"external_backup": {"subdirectory": ""}}),
                mock.patch.object(backup, "external_directory", return_value=drive),
                mock.patch.object(backup, "validate_external_directory"),
                mock.patch.object(backup, "interactive_backup") as interactive,
                mock.patch.object(backup, "wizard_home_backup", return_value=True),
                mock.patch.object(backup, "confirm", return_value=True),
                redirect_stdout(io.StringIO()),
            ):
                interactive.return_value = drive / "backup"
                backup.backup_everything()
        scan.assert_called_once_with(non_interactive=False, force=True)
        root_capture.assert_called_once_with(hide_followup_hint=True, package_scan=False)
        interactive.assert_called_once_with(package_scan=False)

    def test_backup_flow_omits_the_empty_package_cache_banner(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            drive = Path(directory) / "drive"
            drive.mkdir()
            output = io.StringIO()
            with (
                mock.patch.object(backup, "_diagnose_previous_status"),
                mock.patch.object(backup, "_ensure_tray"),
                mock.patch.object(backup, "_write_status"),
                mock.patch.object(backup, "_status_history"),
                mock.patch.object(backup, "package_scan_summary",
                                   return_value=backup.NO_PRIVILEGED_CACHE),
                mock.patch.object(backup, "capture_root_package_scan"),
                mock.patch.object(backup, "capture_root"),
                mock.patch.object(backup, "settings",
                                   return_value={"external_backup": {"subdirectory": ""}}),
                mock.patch.object(backup, "external_directory", return_value=drive),
                mock.patch.object(backup, "validate_external_directory"),
                mock.patch.object(backup, "interactive_backup",
                                   return_value=drive / "backup"),
                mock.patch.object(backup, "wizard_home_backup", return_value=True),
                redirect_stdout(output),
            ):
                backup.backup_everything()
        self.assertNotIn("Package scan before backup", output.getvalue())

    def test_backup_impl_skips_scan_when_flow_already_scanned(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = Path(directory) / "kit"
            (kit / "config").mkdir(parents=True)
            (kit / "config/settings.json").write_text(json.dumps({
                "home_snapshot": "off", "keep_snapshots": 3, "keep_local_backups": True,
                "coverage": "system-wide-configuration", "backup_calendar": "daily",
                "development_include": [], "home_include": [], "home_reference_only": [],
                "system_reference_only": [], "exclude": [], "apt_review_only": [],
                "exclude_cpus": [], "external_backup": None,
            }))
            saved = backup.KIT, backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS
            backup.KIT = kit
            try:
                with (
                    mock.patch.object(backup, "capture_root_package_scan") as scan,
                    mock.patch.object(backup, "build_snapshot",
                                      side_effect=backup.ConfigError("stop")),
                ):
                    with self.assertRaises(backup.ConfigError):
                        backup._backup_impl(package_scan=False)
            finally:
                backup.KIT, backup._LAST_BACKUP_RUN_ID, backup._LAST_BACKUP_WARNINGS = saved
        scan.assert_not_called()

    def test_backup_flow_uses_existing_scan_without_prompt(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            drive = Path(directory) / "drive"
            drive.mkdir()
            with (
                mock.patch.object(backup, "_diagnose_previous_status"),
                mock.patch.object(backup, "_ensure_tray"),
                mock.patch.object(backup, "_write_status"),
                mock.patch.object(backup, "_status_history"),
                mock.patch.object(backup, "package_scan_summary", return_value="Package scan: test"),
                mock.patch.object(backup, "capture_root_package_scan") as scan,
                mock.patch.object(backup, "capture_root"),
                mock.patch.object(backup, "settings",
                                   return_value={"external_backup": {"subdirectory": ""}}),
                mock.patch.object(backup, "external_directory", return_value=drive),
                mock.patch.object(backup, "validate_external_directory"),
                mock.patch.object(backup, "interactive_backup", return_value=drive / "backup"),
                mock.patch.object(backup, "wizard_home_backup", return_value=True),
                mock.patch.object(backup, "confirm") as confirm,
                redirect_stdout(io.StringIO()),
            ):
                backup.backup_everything(refresh_package_scan=False)
        scan.assert_called_once_with(non_interactive=False, force=False)
        confirm.assert_not_called()

    def test_recovery_menu_uses_a_dropdown_without_cancel_entry(self):
        from unittest import mock
        with (
            mock.patch.object(backup, "choose", return_value="0") as choose,
            redirect_stdout(io.StringIO()),
        ):
            backup.wizard_recover()
        choices = choose.call_args.args[1]
        self.assertEqual([value for value, _label, *_detail in choices], ["1", "2", "3"])
        self.assertEqual(choose.call_args.args[2], "1")

    def test_choose_archive_presents_a_dropdown_defaulting_to_latest(self):
        from unittest import mock
        found = [(Path("/tmp/config-a"), "external"), (Path("/tmp/config-b"), "local")]
        saved = backup._GUI_PROMPT_HANDLER
        backup._GUI_PROMPT_HANDLER = lambda *args: None
        try:
            with (
                mock.patch.object(backup, "recovery_choices", return_value=found),
                mock.patch.object(backup, "choose",
                                  return_value="/tmp/config-b") as choose,
            ):
                self.assertEqual(backup.choose_archive("config"), Path("/tmp/config-b"))
        finally:
            backup._GUI_PROMPT_HANDLER = saved
        values = [item[0] for item in choose.call_args.args[1]]
        self.assertEqual(values, ["/tmp/config-a", "/tmp/config-b", "__path__"])
        self.assertEqual(choose.call_args.args[2], "/tmp/config-a")

    def test_recovery_flow_uses_the_latest_configuration(self):
        from unittest import mock
        latest = Path("/tmp/latest-config")
        with (
            mock.patch.object(backup, "choose", return_value="1"),
            mock.patch.object(backup, "recovery_choices", return_value=[(latest, "external")]),
            mock.patch.object(backup, "is_full_home", return_value=False),
            mock.patch.object(backup, "restore", return_value=Path("/tmp/restored")) as restore,
            mock.patch.object(backup, "confirm", return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            backup.wizard_recover()
        restore.assert_called_once_with(latest)

    def test_recovery_flow_offers_package_restore(self):
        from unittest import mock
        source = Path("/tmp/snapshot")
        destination = Path("/tmp/restored")
        with (
            mock.patch.object(backup, "choose", return_value="1"),
            mock.patch.object(backup, "recovery_choices", return_value=[(source, "external")]),
            mock.patch.object(backup, "restore", return_value=destination),
            mock.patch.object(backup, "confirm", side_effect=[False, False, True]),
            mock.patch.object(backup, "wizard_restore_package_files") as package_restore,
            redirect_stdout(io.StringIO()),
        ):
            backup.wizard_recover()
        package_restore.assert_called_once_with(destination)

    def test_package_restore_wizard_accepts_an_explicit_source(self):
        from unittest import mock
        source = Path("/tmp/snapshot")
        plan = [{"action": "restore"}]
        with (
            mock.patch.object(backup, "ask", side_effect=["", ""]),
            mock.patch.object(backup, "confirm", return_value=True),
            mock.patch.object(backup, "restore_package_files", return_value=plan) as restore,
            redirect_stdout(io.StringIO()),
        ):
            backup.wizard_restore_package_files(source)
        self.assertEqual(restore.call_args_list[0].args[0], source)
        self.assertEqual(restore.call_args_list[1].args[0], source)


class MenuIntegrationTests(unittest.TestCase):
    def test_backup_action_starts_the_full_backup_flow(self):
        from unittest import mock
        with (
            mock.patch.object(backup, "ask", side_effect=["1", "0"]),
            mock.patch.object(backup, "backup_everything") as run,
            mock.patch.object(backup, "capture_root_package_scan") as scan,
            redirect_stdout(io.StringIO()),
        ):
            backup.menu()
        run.assert_called_once_with()
        scan.assert_not_called()

    def test_restore_action_starts_the_recovery_flow(self):
        from unittest import mock
        with (
            mock.patch.object(backup, "ask", side_effect=["2", "0"]),
            mock.patch.object(backup, "wizard_recover") as recover,
            redirect_stdout(io.StringIO()),
        ):
            backup.menu()
        recover.assert_called_once_with()


class GuiPromptTests(unittest.TestCase):
    def test_gui_handler_receives_ask_and_confirm(self):
        saved = backup._GUI_PROMPT_HANDLER
        calls = []

        def handler(kind, prompt, default, choices=None):
            calls.append((kind, prompt, default, choices))
            if kind == "ask":
                return "answer"
            if kind == "choose":
                return choices[1][0]
            return True

        backup._GUI_PROMPT_HANDLER = handler
        try:
            self.assertEqual(backup.ask("Question", "default"), "answer")
            self.assertTrue(backup.confirm("Continue?", False))
            self.assertEqual(
                backup.choose("Pick", [("1", "One"), ("2", "Two")], "1"), "2")
        finally:
            backup._GUI_PROMPT_HANDLER = saved
        self.assertEqual(calls, [
            ("ask", "Question", "default", None),
            ("confirm", "Continue?", False, None),
            ("choose", "Pick", "1", [("1", "One"), ("2", "Two")]),
        ])

    def test_cli_choice_prints_detail_line(self):
        from unittest import mock
        saved = backup._GUI_PROMPT_HANDLER
        backup._GUI_PROMPT_HANDLER = None
        output = io.StringIO()
        try:
            with mock.patch.object(backup, "ask", return_value="3"), redirect_stdout(output):
                self.assertEqual(backup.choose(
                    "Recovery", [("3", "Deploy a saved snapshot", "May install packages.")], "3"), "3")
        finally:
            backup._GUI_PROMPT_HANDLER = saved
        self.assertIn("3. Deploy a saved snapshot", output.getvalue())
        self.assertIn("May install packages.", output.getvalue())


class TrayStatusFallbackTests(unittest.TestCase):
    def test_gui_worker_pid_is_not_treated_as_stale(self):
        saved = backup._GUI_PROCESS_ACTIVE
        status = {"state": "running", "pid": os.getpid(),
                  "updated": "2026-01-01T00:00:00+00:00"}
        backup._GUI_PROCESS_ACTIVE = True
        try:
            self.assertIsNone(backup._status_problem(status))
        finally:
            backup._GUI_PROCESS_ACTIVE = saved
        self.assertEqual(backup._status_problem(status),
                         "running status points to this process")

    def test_missing_or_malformed_status_is_valid_idle(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.KIT
            backup.KIT = Path(directory)
            try:
                status = backup._read_tray_status()
                self.assertEqual(status["state"], "idle")
                self.assertIsNone(status["pid"])
                self.assertIsNone(backup._status_problem(status))
                (Path(directory) / "status.json").write_text("{not json")
                status = backup._read_tray_status()
                self.assertIsNone(backup._status_problem(status))
                (Path(directory) / "status.json").write_text('{"state": "idle"}')
                status = backup._read_tray_status()
                self.assertEqual(backup._status_problem(status), "missing updated timestamp")
            finally:
                backup.KIT = saved


class UninstallScriptTests(unittest.TestCase):
    def _prepare(self, base):
        home = base / "home"
        bin_dir = home / ".local/bin"
        data_dir = home / ".local/share"
        config_dir = home / ".config"
        workspace = home / "ubuntu-backup/data"
        for path in (bin_dir, data_dir / "applications", config_dir / "autostart",
                     workspace / "config", workspace / "logs", workspace / "snapshot",
                     workspace / "staging", workspace / "privileged-package-scan"):
            path.mkdir(parents=True, exist_ok=True)
        (bin_dir / "ubuntu-backup.py").write_text("#!/usr/bin/env python3\n")
        (bin_dir / "ubackup-core").write_text("core")
        (bin_dir / "ubuntu-backup").symlink_to("ubuntu-backup.py")
        (workspace / "config/settings.json").write_text(
            json.dumps({"external_backup": None}))
        for name in ("history.txt", "status.json", "native-home-entries.ndjson",
                     "logs/ubuntu-backup.log", "snapshot/manifest.json",
                     "staging/file", "privileged-package-scan/file"):
            (workspace / name).write_text("sentinel")
        script = base / "uninstall.sh"
        shutil.copy2(ROOT / "uninstall.sh", script)
        script.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "XDG_BIN_HOME": str(bin_dir),
            "XDG_DATA_HOME": str(data_dir),
            "XDG_CONFIG_HOME": str(config_dir),
            "UBUNTU_BACKUP_SKIP_SERVICE": "1",
        })
        return script, env, workspace

    def test_yes_purge_is_noninteractive_and_removes_all_local_data(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            script, env, workspace = self._prepare(Path(directory))
            result = subprocess.run(
                [str(script), "--yes", "--purge-backups"],
                input="", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Permanently delete all backup data?", result.stdout)
            self.assertFalse(workspace.exists())
            self.assertFalse((Path(env["XDG_BIN_HOME"]) / "ubuntu-backup.py").exists())
            self.assertFalse((Path(env["XDG_BIN_HOME"]) / "ubuntu-backup").exists())
            self.assertIn("Program and backup data removed", result.stdout)

    def test_without_purge_preserves_the_workspace(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            script, env, workspace = self._prepare(Path(directory))
            result = subprocess.run(
                [str(script), "--yes"],
                input="", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((workspace / "snapshot/manifest.json").is_file())
            self.assertFalse((Path(env["XDG_BIN_HOME"]) / "ubuntu-backup.py").exists())
            self.assertFalse((Path(env["XDG_BIN_HOME"]) / "ubuntu-backup").exists())
            self.assertIn("preserved", result.stdout)

    def test_purge_removes_the_configured_external_layout(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            script, env, workspace = self._prepare(base)
            external = base / "external"
            external.mkdir()
            (external / ".ubuntu-configuration-target.json").write_text(
                json.dumps({"format": "ubuntu-configuration-v1", "id": "test"}))
            for name in ("baseline", "timeline", "ubuntu-configuration-backups"):
                (external / name).mkdir()
                (external / name / "data").write_text("sentinel")
            settings_path = workspace / "config/settings.json"
            settings = json.loads(settings_path.read_text())
            settings["external_backup"] = {
                "mount_point": str(external), "subdirectory": "", "marker_id": "test"}
            settings_path.write_text(json.dumps(settings))
            result = subprocess.run(
                [str(script), "--yes", "--purge-backups"],
                input="", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(workspace.exists())
            self.assertFalse(external.exists())
            self.assertIn("Purged backup layout", result.stdout)

    def test_dry_run_prints_the_exact_external_root(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            script, env, workspace = self._prepare(base)
            external = base / "external"
            external.mkdir()
            (external / ".ubuntu-configuration-target.json").write_text(
                json.dumps({"format": "ubuntu-configuration-v1", "id": "test"}))
            (external / "baseline").mkdir()
            (external / "baseline/data").write_text("sentinel")
            settings_path = workspace / "config/settings.json"
            settings = json.loads(settings_path.read_text())
            settings["external_backup"] = {
                "mount_point": str(external), "subdirectory": "", "marker_id": "test"}
            settings_path.write_text(json.dumps(settings))
            result = subprocess.run(
                [str(script), "--dry-run", "--purge-backups"],
                input="", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("External backup root: " + str(external), result.stdout)
            self.assertTrue(workspace.exists())
            self.assertTrue((external / "baseline/data").is_file())

    def test_purge_refuses_a_changed_external_marker_identity(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            script, env, workspace = self._prepare(base)
            external = base / "external"
            external.mkdir()
            (external / ".ubuntu-configuration-target.json").write_text(
                json.dumps({"format": "ubuntu-configuration-v1", "id": "test"}))
            (external / "baseline").mkdir()
            (external / "baseline/data").write_text("sentinel")
            settings_path = workspace / "config/settings.json"
            settings = json.loads(settings_path.read_text())
            settings["external_backup"] = {
                "mount_point": str(external), "subdirectory": "", "marker_id": "other"}
            settings_path.write_text(json.dumps(settings))
            result = subprocess.run(
                [str(script), "--yes", "--purge-backups"],
                input="", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertIn("identity changed", result.stderr)
            self.assertFalse(workspace.exists())
            self.assertTrue((external / "baseline/data").is_file())
            self.assertTrue((external / ".ubuntu-configuration-target.json").is_file())

    def test_purge_refuses_a_symlinked_workspace(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            script, env, workspace = self._prepare(base)
            target = base / "workspace-target"
            target.mkdir()
            (target / "keep").write_text("keep")
            shutil.rmtree(workspace)
            workspace.symlink_to(target)
            result = subprocess.run(
                [str(script), "--yes", "--purge-backups"],
                input="", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertIn("symlinked workspace", result.stderr)
            self.assertTrue((target / "keep").is_file())

    def test_purge_refuses_a_mounted_workspace(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            script, env, workspace = self._prepare(base)
            fake = base / "fakebin"
            fake.mkdir()
            (fake / "mountpoint").write_text("#!/bin/sh\nexit 0\n")
            (fake / "mountpoint").chmod(0o755)
            env["PATH"] = str(fake) + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                [str(script), "--yes", "--purge-backups"],
                input="", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertIn("mounted workspace", result.stderr)
            self.assertTrue((workspace / "snapshot/manifest.json").is_file())

    def test_incomplete_external_purge_exits_nonzero(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            script, env, workspace = self._prepare(base)
            external = base / "external"
            external.mkdir()
            (external / ".ubuntu-configuration-target.json").write_text(
                json.dumps({"format": "ubuntu-configuration-v1", "id": "test"}))
            target = base / "unrelated"
            target.mkdir()
            (target / "keep").write_text("keep")
            (external / "baseline").symlink_to(target)
            settings_path = workspace / "config/settings.json"
            settings = json.loads(settings_path.read_text())
            settings["external_backup"] = {
                "mount_point": str(external), "subdirectory": "", "marker_id": "test"}
            settings_path.write_text(json.dumps(settings))
            result = subprocess.run(
                [str(script), "--yes", "--purge-backups"],
                input="", capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Could not purge", result.stderr)
            self.assertIn("purge was incomplete", result.stderr)
            self.assertTrue((target / "keep").is_file())
            self.assertFalse(workspace.exists())


class PurgeAllDataTests(unittest.TestCase):
    def _workspace(self, base, external=None):
        kit = base / "data"
        (kit / "config").mkdir(parents=True)
        settings_value = {"keep_snapshots": 30, "external_backup": external}
        (kit / "config/settings.json").write_text(json.dumps(settings_value))
        for name in ("snapshot", "logs", "privileged-package-scan"):
            (kit / name).mkdir()
            (kit / name / "sentinel").write_text("sentinel")
        (kit / "history.txt").write_text("old history\n")
        return kit

    def test_purge_removes_all_local_data_and_resets_defaults(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            kit = self._workspace(Path(directory))
            saved = backup.KIT
            backup.KIT = kit
            try:
                self.assertTrue(backup.purge_all_data())
            finally:
                backup.KIT = saved
            self.assertFalse((kit / "snapshot").exists())
            self.assertFalse((kit / "logs").exists())
            self.assertFalse((kit / "privileged-package-scan").exists())
            self.assertTrue((kit / "config/settings.json").is_file())
            self.assertTrue((kit / "status.json").is_file())
            self.assertIn("all backup data purged", (kit / "history.txt").read_text())
            status = json.loads((kit / "status.json").read_text())
            self.assertEqual(status["state"], "idle")
            self.assertIsNone(backup._status_problem(status))
            settings_value = json.loads((kit / "config/settings.json").read_text())
            self.assertIsNone(settings_value["external_backup"])
            self.assertEqual(settings_value["home_snapshot"], "off")

    def test_purge_removes_configured_external_layout(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            external = base / "external"
            external.mkdir()
            (external / ".ubuntu-configuration-target.json").write_text(
                json.dumps({"format": backup.FORMAT, "id": "test"}))
            for name in backup.EXTERNAL_PURGE_NAMES:
                (external / name).mkdir()
                (external / name / "sentinel").write_text("sentinel")
            kit = self._workspace(base, external={"subdirectory": ""})
            saved = (backup.KIT, backup.external_directory, backup.marker_record)
            backup.KIT = kit
            backup.external_directory = lambda config: external
            backup.marker_record = lambda path: {"format": backup.FORMAT, "id": "test"}
            try:
                self.assertTrue(backup.purge_all_data())
            finally:
                backup.KIT, backup.external_directory, backup.marker_record = saved
            for name in backup.EXTERNAL_PURGE_NAMES:
                self.assertFalse((external / name).exists())
            self.assertFalse((external / ".ubuntu-configuration-target.json").exists())


class InstallRoundTripTests(unittest.TestCase):
    def _prepare(self, base):
        repo = base / "repo"
        (repo / "native").mkdir(parents=True)
        for name in ("install.sh", "uninstall.sh", "ubuntu-backup.py",
                     "ubuntu-backup-askpass.py", "ubuntu-backup.svg"):
            shutil.copy2(ROOT / name, repo / name)
        shutil.copy2(ROOT / "native/ubackup-core", repo / "native/ubackup-core")
        (repo / "install.sh").chmod(0o755)
        (repo / "uninstall.sh").chmod(0o755)
        (repo / "native/ubackup-core").chmod(0o700)

        home = base / "home"
        bin_dir = home / ".local/bin"
        data_dir = home / ".local/share"
        config_dir = home / ".config"
        fake = base / "fakebin"
        fake.mkdir()
        for name in ("systemctl", "update-desktop-database", "gtk-update-icon-cache"):
            path = fake / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "XDG_BIN_HOME": str(bin_dir),
            "XDG_DATA_HOME": str(data_dir),
            "XDG_CONFIG_HOME": str(config_dir),
            "UBUNTU_BACKUP_SKIP_START": "1",
            "UBUNTU_BACKUP_SKIP_SERVICE": "1",
            "PATH": str(fake) + os.pathsep + env.get("PATH", ""),
        })
        return repo, env, bin_dir

    def test_install_uninstall_install_restores_the_command_symlink(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            repo, env, bin_dir = self._prepare(Path(directory))
            for _ in range(2):
                result = subprocess.run([str(repo / "install.sh")], capture_output=True,
                                        text=True, env=env, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((bin_dir / "ubuntu-backup.py").is_file())
                self.assertTrue((bin_dir / "ubuntu-backup-askpass").is_file())
                self.assertTrue((bin_dir / "ubuntu-backup").is_symlink())
                self.assertEqual((bin_dir / "ubuntu-backup").readlink(),
                                 Path("ubuntu-backup.py"))

            result = subprocess.run([str(repo / "uninstall.sh"), "--yes"],
                                    input="", capture_output=True, text=True, env=env,
                                    timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((bin_dir / "ubuntu-backup.py").exists())
            self.assertFalse((bin_dir / "ubuntu-backup-askpass").exists())
            self.assertFalse((bin_dir / "ubuntu-backup").exists())

            result = subprocess.run([str(repo / "install.sh")], capture_output=True,
                                    text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((bin_dir / "ubuntu-backup").is_symlink())
            self.assertEqual((bin_dir / "ubuntu-backup").readlink(),
                             Path("ubuntu-backup.py"))

    def test_launcher_and_autostart_share_the_canonical_workspace(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            repo, env, _bin_dir = self._prepare(Path(directory))
            result = subprocess.run([str(repo / "install.sh")], capture_output=True,
                                    text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            home = Path(env["HOME"])
            workspace = str(home / "Ubuntu-Backup/data")
            desktop = (home / ".local/share/applications/ubuntu-backup.desktop").read_text()
            autostart = (home / ".config/autostart/ubuntu-backup-tray.desktop").read_text()
            self.assertIn("--workspace", desktop)
            self.assertIn(workspace, desktop)
            self.assertIn(workspace, autostart)


class ExtensionInstallIntegrityTests(unittest.TestCase):
    UUID = "ubuntu-appindicators@ubuntu.com"

    def _prepare(self, base):
        repo = base / "repo"
        (repo / "extension").mkdir(parents=True)
        script = repo / "extension/install.sh"
        shutil.copy2(ROOT / "extension/install.sh", script)
        script.chmod(0o755)

        source = base / "source-extension"
        source.mkdir()
        (source / "metadata.json").write_text(json.dumps({
            "uuid": self.UUID, "name": "Test", "description": "Test", "version": 1}))
        (source / "appIndicator.js").write_text("// source\n")
        (source / "indicatorStatusIcon.js").write_text("// source\n")

        fake = base / "fakebin"
        fake.mkdir()
        (fake / "systemctl").write_text("#!/bin/sh\nexit 0\n")
        (fake / "gsettings").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'get org.gnome.shell enabled-extensions'*) echo \"['ding@rastersoft.com']\" ;;\n"
            "  *'get org.gnome.shell disabled-extensions'*) echo \"[]\" ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n")
        for name in ("systemctl", "gsettings"):
            (fake / name).chmod(0o755)

        env = os.environ.copy()
        env.update({
            "HOME": str(base / "home"),
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "UBUNTU_APPINDICATOR_SRC": str(source),
            "PATH": str(fake) + os.pathsep + env.get("PATH", ""),
        })
        dest = base / "data/ubuntu-backup-system/gnome-shell/extensions" / self.UUID
        return script, env, dest

    def test_install_is_atomic_and_repeatable(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            script, env, dest = self._prepare(Path(directory))
            for _ in range(2):
                result = subprocess.run([str(script)], capture_output=True,
                                        text=True, env=env, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(dest.is_dir())
                self.assertFalse(dest.is_symlink())
                metadata = json.loads((dest / "metadata.json").read_text())
                self.assertEqual(metadata["uuid"], self.UUID)

    def test_symlinked_destination_is_refused(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            script, env, dest = self._prepare(Path(directory))
            target = Path(directory) / "target"
            target.mkdir()
            (target / "metadata.json").write_text(json.dumps({"uuid": self.UUID}))
            (target / "sentinel").write_text("keep")
            dest.parent.mkdir(parents=True)
            dest.symlink_to(target, target_is_directory=True)
            result = subprocess.run([str(script)], capture_output=True,
                                    text=True, env=env, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlinked extension path", result.stderr)
            self.assertTrue(dest.is_symlink())
            self.assertTrue((target / "sentinel").is_file())


class DeployCompatibilityTests(unittest.TestCase):
    def test_dry_run_does_not_require_source_system_inventory(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            home, kit, snapshot = base / "home", base / "kit", base / "snapshot"
            home.mkdir()
            (snapshot / "home").mkdir(parents=True)
            content = b"# bashrc\n"
            (snapshot / "home/.bashrc").write_bytes(content)
            manifest = {
                "format": backup.FORMAT,
                "created_at": "2026-01-01T00:00:00Z",
                "files": [{
                    "path": "home/.bashrc", "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "executable": False,
                }],
            }
            (snapshot / "manifest.json").write_text(json.dumps(manifest))
            args = argparse.Namespace(
                snapshot=str(snapshot), apply=False, overwrite=False, packages=False,
                snaps=False, flatpaks=False, desktop_settings=False, build=False,
            )
            saved = backup.HOME, backup.KIT
            backup.HOME, backup.KIT = home, kit
            try:
                with redirect_stdout(io.StringIO()):
                    backup.deploy(args)
            finally:
                backup.HOME, backup.KIT = saved
            self.assertFalse((home / ".bashrc").exists())
            self.assertTrue((kit / "plans/last-deploy.json").is_file())


if __name__ == "__main__":
    unittest.main()
