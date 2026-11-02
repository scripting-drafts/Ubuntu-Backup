"""Regression tests for the safety-critical helpers in ubuntu-backup.py."""

from collections import namedtuple
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
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
        @contextmanager
        def pinned(_config):
            yield Path(directory), Path(directory)

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            usage = namedtuple("usage", "total used free")(1024, 1023, 1)
            saved = backup.pinned_external, backup.shutil.disk_usage, backup.MIN_EXTERNAL_FREE_BYTES
            backup.pinned_external = pinned
            backup.shutil.disk_usage = lambda _path: usage
            backup.MIN_EXTERNAL_FREE_BYTES = 64
            try:
                with self.assertRaises(backup.ConfigError):
                    backup.validate_external_target({"mount_point": directory, "marker_id": "test"})
                self.assertEqual(list(Path(directory).glob(".backup-probe-*")), [])
            finally:
                backup.pinned_external, backup.shutil.disk_usage, backup.MIN_EXTERNAL_FREE_BYTES = saved


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


if __name__ == "__main__":
    unittest.main()
