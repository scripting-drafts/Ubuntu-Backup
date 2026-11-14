"""Audit the strict-snap guards without touching the host."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ubuntu-backup.py"
URL = "https://github.com/scripting-drafts/Ubuntu-Backup#apt-repository"


class SnapLimitTests(unittest.TestCase):
    def run_module(self, body, snap):
        env = dict(os.environ)
        env.pop("SNAP", None)
        if snap:
            env["SNAP"] = "1"
        code = f"""
import importlib.util
spec = importlib.util.spec_from_file_location("ubuntu_backup", {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
{body}
"""
        return subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, env=env, timeout=30)

    def test_snap_mode_is_detected(self):
        result = self.run_module("print(module.SNAP_MODE); print(module.APT_ONLY_HINT)", True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("True", result.stdout)
        self.assertIn(URL, result.stdout)

    def test_apt_mode_is_not_snap(self):
        result = self.run_module("print(module.SNAP_MODE)", False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("False", result.stdout)

    def test_snap_guards_return_apt_guidance(self):
        body = """
import contextlib, io
for name, call in (
    ("restore_package_files", lambda: module.restore_package_files()),
    ("format_drive", lambda: module.format_drive("/dev/null")),
    ("mount_drive", lambda: module.mount_drive({})),
    ("restore_system_integration", lambda: module.restore_system_integration("/tmp/none")),
):
    try:
        call()
    except module.ConfigError as exc:
        text = str(exc)
    else:
        text = ""
    print(name + ": " + text)
print("package_scan: ", end="")
module.capture_root_package_scan()
print("home: ", end="")
print(module.wizard_home_backup())
print("capture: ", end="")
module.capture_root()
"""
        result = self.run_module(body, True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        for name in ("restore_package_files", "format_drive", "mount_drive",
                     "restore_system_integration"):
            self.assertIn(name + ": ", output)
        self.assertIn(URL, output)
        self.assertIn("ubuntu-backup-system", output)
        self.assertIn("False", output)  # wizard_home_backup returns False

    def test_help_tags_only_apply_to_snap(self):
        env = dict(os.environ)
        env.pop("SNAP", None)
        apt = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                             capture_output=True, text=True, env=env, timeout=30)
        env["SNAP"] = "1"
        snap = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(apt.returncode, 0, apt.stderr)
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertNotIn("APT-only", apt.stdout)
        self.assertNotIn("requires .deb helper", apt.stdout)
        self.assertIn("APT-only", snap.stdout)
        self.assertIn("requires .deb helper", snap.stdout)

    def test_staged_restore_path_is_reported(self):
        result = self.run_module("""
import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory(dir="/tmp") as directory:
    module.KIT = Path(directory)
    (module.KIT / "config").mkdir()
    (module.KIT / "config/settings.json").write_text('{"keep_snapshots": 1}')
    module.show_status()
""", False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Staged restores:", result.stdout)
        self.assertIn("restored", result.stdout)


if __name__ == "__main__":
    unittest.main()
