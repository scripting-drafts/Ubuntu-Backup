"""Regression tests for the single-prompt privileged broker."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
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


class BrokerLoopTests(unittest.TestCase):
    def test_accepts_fixed_action_and_rejects_bad_package_list(self):
        with tempfile.TemporaryDirectory(dir=backup.HOME) as directory:
            workspace = Path(directory)
            requests = "\n".join(json.dumps(request) for request in (
                {"id": 1, "action": "package_scan"},
                {"id": 2, "action": "apt_install", "args": {"packages": ["-bad"]}},
                {"id": 3, "action": "exit"},
            )) + "\n"
            calls = []

            def fake_run(command, request_id, cancel_path, env=None):
                calls.append((command, request_id, env))
                return 0

            uid = os.geteuid()
            output = io.StringIO()
            with mock.patch.object(backup.os, "geteuid", return_value=0), \
                    mock.patch.dict(backup.os.environ,
                                    {"SUDO_UID": str(uid)}), \
                    mock.patch.object(backup, "_broker_run", side_effect=fake_run), \
                    mock.patch.object(sys, "stdin", io.StringIO(requests)), \
                    redirect_stdout(output):
                backup._privilege_broker_loop(workspace)
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(events[0], {"ready": True})
            self.assertEqual(len(calls), 1)
            self.assertIn("_capture-package-files-worker", calls[0][0])
            self.assertTrue(any(event.get("event") == "error" for event in events))
            self.assertTrue(any(event.get("event") == "done" and event.get("id") == 1
                                for event in events))


class BrokerClientTests(unittest.TestCase):
    def test_streams_output_until_done(self):
        script = (
            "import json,sys\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    if request.get('action') == 'exit': break\n"
            "    print(json.dumps({'id': request['id'], 'event': 'output', 'data': 'hello\\n'}), flush=True)\n"
            "    print(json.dumps({'id': request['id'], 'event': 'done', 'returncode': 0}), flush=True)\n"
        )
        broker = backup.PrivilegeBroker()
        broker.process = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, bufsize=1)
        seen = []
        try:
            result = broker.run("package_scan", on_output=seen.append)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "hello\n")
            self.assertEqual(seen, ["hello\n"])
        finally:
            broker.stop()

    def test_rejects_out_of_sequence_response(self):
        script = (
            "import json,sys\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    if request.get('action') == 'exit': break\n"
            "    print(json.dumps({'id': 999, 'event': 'done', 'returncode': 0}), flush=True)\n"
        )
        broker = backup.PrivilegeBroker()
        broker.process = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, bufsize=1)
        try:
            with self.assertRaises(backup.ConfigError):
                broker.run("package_scan")
        finally:
            broker.stop()

    def test_cancellation_file_stops_running_action(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            cancel_path = Path(directory) / ".cancel"
            seen = []
            saved_send = backup._broker_send
            backup._broker_send = seen.append
            try:
                timer = threading.Timer(0.3, lambda: cancel_path.write_text("cancel\n"))
                timer.start()
                started = time.monotonic()
                returncode = backup._broker_run(
                    ["/bin/sh", "-c", "echo started; sleep 30"], 7, cancel_path)
                elapsed = time.monotonic() - started
                timer.cancel()
            finally:
                backup._broker_send = saved_send
            self.assertLess(elapsed, 5)
            self.assertNotEqual(returncode, 0)
            self.assertTrue(any(event.get("data") == "started\n" for event in seen))
