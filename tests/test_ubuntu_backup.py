"""Regression tests for the safety-critical helpers in ubuntu-backup.py."""

import argparse
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
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


class CaptureRootTests(unittest.TestCase):
    def test_worker_stderr_is_surfaced(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 1, "", "Error: missing parent\n")

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            saved = backup.subprocess.run, backup.KIT
            backup.subprocess.run = fake_run
            backup.KIT = Path(directory)
            stderr = io.StringIO()
            stdout = io.StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaises(backup.ConfigError) as raised:
                        backup.capture_root()
            finally:
                backup.subprocess.run, backup.KIT = saved
            self.assertIn("missing parent", str(raised.exception))
            self.assertIn("missing parent", stderr.getvalue())
            self.assertEqual(calls[0][1].get("stderr"), subprocess.PIPE)


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


class StagingTests(unittest.TestCase):
    def test_stream_object_stages_until_bulk_copy(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            stage_objects = base / "stage" / "objects"
            stage_incoming = base / "stage" / "incoming"
            external_objects = base / "external" / "objects"
            stage_objects.mkdir(parents=True)
            stage_incoming.mkdir(parents=True)
            external_objects.mkdir(parents=True)
            data = b"hello staged backup\n"
            created = []
            destination, sha256 = backup.stream_object(
                stage_objects, stage_incoming, external_objects,
                io.BytesIO(data), len(data), None, created)
            self.assertEqual(destination, backup.object_path(external_objects, sha256))
            self.assertFalse(destination.exists())
            self.assertEqual(created, [destination])
            self.assertEqual(backup.object_path(stage_objects, sha256).read_bytes(), data)
            self.assertEqual(backup.copy_staged_objects(stage_objects, external_objects), 1)
            self.assertEqual(destination.read_bytes(), data)

    def test_existing_external_object_is_reused(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            stage_objects = base / "stage" / "objects"
            stage_incoming = base / "stage" / "incoming"
            external_objects = base / "external" / "objects"
            stage_objects.mkdir(parents=True)
            stage_incoming.mkdir(parents=True)
            external_objects.mkdir(parents=True)
            data = b"already stored\n"
            sha256 = hashlib.sha256(data).hexdigest()
            destination = backup.object_path(external_objects, sha256)
            destination.parent.mkdir(parents=True)
            destination.write_bytes(data)
            created = []
            result, digest_value = backup.stream_object(
                stage_objects, stage_incoming, external_objects,
                io.BytesIO(data), len(data), None, created)
            self.assertEqual(result, destination)
            self.assertEqual(digest_value, sha256)
            self.assertEqual(created, [])
            self.assertFalse(backup.object_path(stage_objects, sha256).exists())
            self.assertEqual(list(stage_incoming.iterdir()), [])


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
        stored = json.loads((self.root / "config/settings.json").read_text())
        self.assertEqual(stored["external_backup"], {"subdirectory": ""})

    def test_mount_drive_needs_a_terminal_for_sudo(self):
        saved = backup.sys.stdin
        backup.sys.stdin = io.StringIO("")
        try:
            with redirect_stdout(io.StringIO()), self.assertRaises(backup.ConfigError):
                backup.mount_drive(self.device(), str(self.root / "1"))
        finally:
            backup.sys.stdin = saved


if __name__ == "__main__":
    unittest.main()
