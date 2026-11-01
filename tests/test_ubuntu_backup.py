"""Regression tests for the safety-critical helpers in ubuntu-backup.py."""

from collections import namedtuple
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
