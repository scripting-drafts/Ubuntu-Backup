"""Regression tests for the single-prompt privileged broker."""

import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ubuntu_backup", ROOT / "ubuntu-backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BrokerWorkspaceTests(unittest.TestCase):
    def test_accepts_owned_directory_below_home(self):
        with tempfile.TemporaryDirectory(dir=backup.HOME) as directory:
            real = Path(directory).resolve()
            self.assertEqual(backup._broker_workspace(real, os.geteuid()), real)

    def test_rejects_path_outside_home(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            self.assertIsNone(backup._broker_workspace(Path(directory), os.geteuid()))

    def test_rejects_symlinked_workspace(self):
        with tempfile.TemporaryDirectory(dir=backup.HOME) as directory:
            real = Path(directory) / "real"
            real.mkdir()
            link = Path(directory) / "link"
            link.symlink_to(real)
            self.assertIsNone(backup._broker_workspace(link, os.geteuid()))


class BrokerRequestTests(unittest.TestCase):
    def test_package_scan_uses_the_fixed_worker(self):
        command, env = backup._broker_request("package_scan", {}, Path("/home/x/data"), 1000)
        self.assertIn("_capture-package-files-worker", command)
        self.assertIsNone(env)

    def test_rejects_bad_apt_package_list(self):
        with self.assertRaises(backup.ConfigError):
            backup._broker_request("apt_install", {"packages": ["-bad"]},
                                   Path("/home/x/data"), 1000)

    def test_rejects_unknown_action(self):
        with self.assertRaises(backup.ConfigError):
            backup._broker_request("shell", {"command": "id"},
                                   Path("/home/x/data"), 1000)


class FakeProcess:
    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class BrokerClientTests(unittest.TestCase):
    def make_broker(self, events):
        broker = backup.PrivilegeBroker()
        broker.process = FakeProcess()
        broker.reader = io.StringIO(
            "".join(json.dumps(event) + "\n" for event in events))
        broker.writer = io.StringIO()
        return broker

    def test_streams_output_until_done(self):
        broker = self.make_broker([
            {"id": 1, "event": "output", "data": "hello\n"},
            {"id": 1, "event": "done", "returncode": 0},
        ])
        seen = []
        result = broker.run("package_scan", on_output=seen.append)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "hello\n")
        self.assertEqual(seen, ["hello\n"])
        self.assertIn('"action": "package_scan"', broker.writer.getvalue())

    def test_rejects_out_of_sequence_response(self):
        broker = self.make_broker([{"id": 999, "event": "done", "returncode": 0}])
        with self.assertRaises(backup.ConfigError):
            broker.run("package_scan")

    def test_cancellation_file_stops_running_action(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            cancel_path = Path(directory) / ".cancel"
            seen = []
            timer = threading.Timer(0.3, lambda: cancel_path.write_text("cancel\n"))
            timer.start()
            try:
                started = time.monotonic()
                returncode = backup._broker_run(
                    ["/bin/sh", "-c", "echo started; sleep 30"], 7,
                    cancel_path, seen.append)
                elapsed = time.monotonic() - started
            finally:
                timer.cancel()
            self.assertLess(elapsed, 5)
            self.assertNotEqual(returncode, 0)
            self.assertTrue(any(event.get("data") == "started\n" for event in seen))


class BrokerSocketTests(unittest.TestCase):
    def test_socket_transport_runs_one_fixed_action(self):
        with tempfile.TemporaryDirectory(dir=backup.HOME) as directory:
            workspace = Path(directory)
            uid = os.geteuid()
            calls = []
            errors = []

            def fake_run(command, request_id, cancel_path, send, env=None, watch=None):
                calls.append(command)
                send({"id": request_id, "event": "output", "data": "ok\n"})
                return 0

            def serve():
                try:
                    with mock.patch.object(backup.os, "geteuid", return_value=0), \
                            mock.patch.dict(backup.os.environ,
                                            {"SUDO_UID": str(uid)}), \
                            mock.patch.object(backup, "_broker_run",
                                              side_effect=fake_run):
                        backup._privilege_broker_loop(workspace)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            thread = threading.Thread(target=serve)
            thread.start()
            path = workspace / ".privileged-broker.sock"
            deadline = time.monotonic() + 5
            client = None
            while time.monotonic() < deadline:
                try:
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.connect(str(path))
                    break
                except OSError:
                    if client is not None:
                        client.close()
                    client = None
                    time.sleep(0.02)
            self.assertIsNotNone(client)
            reader = client.makefile("r", encoding="utf-8")
            writer = client.makefile("w", encoding="utf-8")
            writer.write(json.dumps({"id": 1, "action": "package_scan"}) + "\n")
            writer.flush()
            events = []
            while True:
                event = json.loads(reader.readline())
                events.append(event)
                if event.get("event") == "done":
                    break
            writer.write(json.dumps({"id": 2, "action": "exit"}) + "\n")
            writer.flush()
            writer.close()
            reader.close()
            client.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(events, [
                {"id": 1, "event": "output", "data": "ok\n"},
                {"id": 1, "event": "done", "returncode": 0},
            ])
            self.assertIn("_capture-package-files-worker", calls[0])
