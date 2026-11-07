"""Integration tests for the native capture core."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"
BINARY = Path(os.environ.get("UBACKUP_CORE", NATIVE / "ubackup-core"))


def stat_mode(path):
    return path.stat().st_mode & 0o7777


def build_core():
    if shutil.which("cc") is None:
        raise unittest.SkipTest("cc is unavailable")
    result = subprocess.run(["make", "-C", str(NATIVE)], capture_output=True, text=True)
    if result.returncode:
        raise unittest.SkipTest("native core build failed:\n" + result.stdout + result.stderr)


class NativeCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_core()
        spec = importlib.util.spec_from_file_location("ubuntu_backup_reference",
                                                      ROOT / "ubuntu-backup.py")
        cls.reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.reference)

    def scan(self, source, base, *arguments):
        entries = base / "entries.ndjson"
        report = base / "report.json"
        command = [str(BINARY), "scan", "--source", str(source),
                   "--objects", str(base / "objects"),
                   "--entries", str(entries), "--report", str(report)]
        command.extend(arguments)
        result = subprocess.run(command, capture_output=True, text=True)
        parsed = json.loads(report.read_text()) if report.is_file() else None
        lines = [json.loads(line) for line in entries.read_text().splitlines()] if entries.is_file() else []
        return result, parsed, lines
    def restore(self, base, destination=None):
        destination = destination or (base / "restored")
        report = base / "restore.json"
        command = [str(BINARY), "restore",
                   "--entries", str(base / "entries.ndjson"),
                   "--objects", str(base / "objects"),
                   "--destination", str(destination),
                   "--report", str(report)]
        result = subprocess.run(command, capture_output=True, text=True)
        parsed = json.loads(report.read_text()) if report.is_file() else None
        return result, parsed, destination

    def test_scan_stores_objects_and_describes_sparse_links(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            (source / "dir").mkdir(parents=True)
            (source / "file.txt").write_text("hello\n")
            (source / "dir/nested.txt").write_text("nested\n")
            (source / "link").symlink_to("file.txt")
            os.link(source / "file.txt", source / "hard")
            os.mkfifo(source / "pipe")
            with (source / "sparse.bin").open("wb") as stream:
                stream.write(b"A" * 4096)
                stream.seek(8 * 1024 * 1024)
                stream.write(b"B" * 4096)
                stream.truncate(16 * 1024 * 1024)

            result, report, entries = self.scan(source, base)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(report["complete"])
            self.assertEqual(report["dirs"], 1)
            self.assertEqual(report["symlinks"], 1)
            self.assertEqual(report["hardlinks"], 1)
            by_path = {entry["path"]: entry for entry in entries}
            self.assertEqual(by_path["link"]["target"], "file.txt")
            self.assertEqual(by_path["dir/nested.txt"]["type"], "file")
            linked = [entry for entry in (by_path["file.txt"], by_path["hard"])
                      if entry["type"] == "hardlink"]
            originals = [entry for entry in (by_path["file.txt"], by_path["hard"])
                         if entry["type"] == "file"]
            self.assertEqual(len(linked), 1)
            self.assertEqual(len(originals), 1)
            self.assertEqual(linked[0]["hardlink_to"], originals[0]["path"])
            sparse = by_path["sparse.bin"]
            self.assertEqual(sparse["sparse"], [[0, 4096], [8388608, 4096]])
            stored = base / "objects" / sparse["sha256"][:2] / sparse["sha256"]
            self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), sparse["sha256"])
            self.assertEqual(stored.read_bytes(), (source / "sparse.bin").read_bytes())

    def test_unreadable_file_is_recorded_as_an_omission(self):
        if os.geteuid() == 0:
            self.skipTest("root can read mode-000 files")
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "ok.txt").write_text("ok\n")
            secret = source / "secret.txt"
            secret.write_text("secret\n")
            secret.chmod(0)
            try:
                result, report, entries = self.scan(source, base)
            finally:
                secret.chmod(0o600)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse(report["complete"])
            self.assertEqual(report["omission_count"], 1)
            self.assertEqual(report["omissions"][0]["path"], "secret.txt")
            self.assertEqual([entry["path"] for entry in entries], ["ok.txt"])

    def test_second_scan_reuses_existing_objects(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "one.txt").write_text("one\n")
            (source / "two.txt").write_text("two\n")
            first_result, first, first_entries = self.scan(source, base)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            second_result, second, second_entries = self.scan(source, base)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertEqual(second["objects_created"], 0)
            self.assertEqual(second["objects_reused"], first["objects_created"])
            self.assertEqual(len(second_entries), len(first_entries))

    def test_restore_round_trip(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            (source / "dir").mkdir(parents=True)
            (source / "file.txt").write_text("hello\n")
            (source / "file.txt").chmod(0o640)
            (source / "dir/nested.txt").write_text("nested\n")
            (source / "link").symlink_to("file.txt")
            os.link(source / "file.txt", source / "hard")
            (source / "readonly").mkdir()
            (source / "readonly/inside.txt").write_text("inside\n")
            (source / "readonly").chmod(0o555)
            with (source / "sparse.bin").open("wb") as stream:
                stream.write(b"A" * 4096)
                stream.seek(8 * 1024 * 1024)
                stream.write(b"B" * 4096)
                stream.truncate(16 * 1024 * 1024)
            try:
                scan_result, scan_report, scan_entries = self.scan(source, base)
                self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
                restore_result, restore_report, destination = self.restore(base)
                self.assertEqual(restore_result.returncode, 0, restore_result.stderr)
                self.assertTrue(restore_report["complete"])
                self.assertEqual(restore_report["entries"], len(scan_entries))
                self.assertEqual(restore_report["files"], scan_report["files"])
                self.assertEqual(restore_report["dirs"], scan_report["dirs"])
                self.assertEqual(restore_report["symlinks"], scan_report["symlinks"])
                self.assertEqual(restore_report["hardlinks"], scan_report["hardlinks"])
                self.assertEqual((destination / "file.txt").read_text(), "hello\n")
                self.assertEqual((destination / "dir/nested.txt").read_text(), "nested\n")
                self.assertEqual((destination / "readonly/inside.txt").read_text(), "inside\n")
                self.assertEqual((destination / "link").readlink(), Path("file.txt"))
                self.assertEqual(os.stat(destination / "file.txt").st_ino,
                                 os.stat(destination / "hard").st_ino)
                self.assertEqual(stat_mode(destination / "file.txt"), 0o640)
                self.assertEqual(stat_mode(destination / "readonly"), 0o555)
                sparse = destination / "sparse.bin"
                self.assertEqual(sparse.read_bytes(), (source / "sparse.bin").read_bytes())
                self.assertLess(sparse.stat().st_blocks * 512, sparse.stat().st_size)
            finally:
                (source / "readonly").chmod(0o755)

    def test_restore_refuses_a_nonempty_destination(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "file.txt").write_text("x\n")
            scan_result, _report, _entries = self.scan(source, base)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            destination = base / "restored"
            destination.mkdir()
            (destination / "keep.txt").write_text("keep\n")
            result, _restore_report, _destination = self.restore(base, destination)
            self.assertEqual(result.returncode, 1)
            self.assertIn("not empty", result.stderr)
            self.assertTrue((destination / "keep.txt").is_file())

    def test_restore_detects_a_corrupt_object(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "file.txt").write_text("payload\n")
            scan_result, _report, entries = self.scan(source, base)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            entry = next(item for item in entries if item["path"] == "file.txt")
            stored = base / "objects" / entry["sha256"][:2] / entry["sha256"]
            stored.chmod(0o600)
            stored.write_bytes(b"X" * entry["size"])
            stored.chmod(0o400)
            result, _restore_report, _destination = self.restore(base)
            self.assertEqual(result.returncode, 1)
            self.assertIn("checksum mismatch", result.stderr)

    def test_restore_skips_special_files(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "file.txt").write_text("x\n")
            os.mkfifo(source / "pipe")
            scan_result, _report, _entries = self.scan(source, base)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            restore_result, restore_report, destination = self.restore(base)
            self.assertEqual(restore_result.returncode, 0, restore_result.stderr)
            self.assertEqual(restore_report["special_skipped"], 1)
            self.assertFalse((destination / "pipe").exists())

    def test_stage_refuses_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "file.txt").write_text("x\n")
            stage = base / "stage"
            stage.mkdir()
            important = stage / "important.txt"
            important.write_text("keep\n")
            result = subprocess.run([
                str(BINARY), "scan", "--source", str(source),
                "--stage", str(stage),
                "--objects", str(base / "objects"),
                "--entries", str(base / "entries.ndjson"),
                "--report", str(base / "report.json"),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("unrecognized stage", result.stderr)
            self.assertTrue(important.is_file())

    def test_stage_mode_removes_local_stage_and_publishes_timeline(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            for index in range(20):
                (source / f"file-{index}.txt").write_text(f"data {index}\n")
            root = base / "root"
            timeline_parent = root / "timeline/home"
            timeline_parent.mkdir(parents=True)
            stage = base / "stage"
            report = base / "report.json"
            result = subprocess.run([
                str(BINARY), "scan", "--source", str(source),
                "--stage", str(stage),
                "--objects", str(root / "baseline/home/objects"),
                "--entries", str(base / "entries.ndjson"),
                "--report", str(report),
                "--timeline", str(timeline_parent),
                "--run-id", "20261107T120000.000000Z",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(stage.exists())
            parsed = json.loads(report.read_text())
            self.assertEqual(parsed["objects_created"], 20)
            self.reference.verify_home_timeline(Path(parsed["timeline"]))

    def test_timeline_is_accepted_by_the_python_verifier(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            (source / "dir").mkdir(parents=True)
            (source / "file.txt").write_text("timeline\n")
            (source / "dir/nested.txt").write_text("nested\n")
            (source / "link").symlink_to("file.txt")
            os.link(source / "file.txt", source / "hard")
            with (source / "sparse.bin").open("wb") as stream:
                stream.write(b"A" * 4096)
                stream.seek(8 * 1024 * 1024)
                stream.write(b"B" * 4096)
                stream.truncate(16 * 1024 * 1024)
            os.setxattr(source / "file.txt", b"user.demo", b"timeline")
            root = base / "root"
            objects = root / "baseline/home/objects"
            timeline_parent = root / "timeline/home"
            timeline_parent.mkdir(parents=True)
            report = base / "report.json"
            entries = base / "entries.ndjson"
            result = subprocess.run([
                str(BINARY), "scan", "--source", str(source),
                "--objects", str(objects), "--entries", str(entries),
                "--report", str(report),
                "--timeline", str(timeline_parent),
                "--run-id", "20261106T160000.000000Z",
                "--identity-json", '{"host":"test"}',
                "--exclusions-json", '["backups"]',
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(report.read_text())
            timeline = Path(parsed["timeline"])
            self.assertTrue(timeline.is_dir())
            manifest = self.reference.verify_home_timeline(timeline)
            self.assertEqual(manifest["entry_count"], parsed["entries"])
            self.assertEqual(manifest["identity"], {"host": "test"})
            self.assertEqual(manifest["exclusions"], ["backups"])

    def test_timeline_with_omissions_is_still_verified(self):
        if os.geteuid() == 0:
            self.skipTest("root can read mode-000 files")
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "ok.txt").write_text("ok\n")
            secret = source / "secret.txt"
            secret.write_text("secret\n")
            secret.chmod(0)
            root = base / "root"
            timeline_parent = root / "timeline/home"
            timeline_parent.mkdir(parents=True)
            report = base / "report.json"
            try:
                result = subprocess.run([
                    str(BINARY), "scan", "--source", str(source),
                    "--objects", str(root / "baseline/home/objects"),
                    "--entries", str(base / "entries.ndjson"),
                    "--report", str(report),
                    "--timeline", str(timeline_parent),
                    "--run-id", "20261106T160002.000000Z",
                ], capture_output=True, text=True)
            finally:
                secret.chmod(0o600)
            self.assertEqual(result.returncode, 3, result.stderr)
            parsed = json.loads(report.read_text())
            timeline = Path(parsed["timeline"])
            manifest = self.reference.verify_home_timeline(timeline)
            self.assertEqual(manifest["warning_count"], 1)
            self.assertEqual(manifest["warnings"][0]["path"], "secret.txt")

    def test_differential_entries_match_python_capture(self):
        import tarfile
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            (source / "dir").mkdir(parents=True)
            (source / "file.txt").write_text("differential\n")
            (source / "dir/nested.txt").write_text("nested\n")
            (source / "link").symlink_to("file.txt")
            os.setxattr(source / "file.txt", b"user.demo", b"differential")
            tar_path = base / "home.tar"
            subprocess.run(["tar", "--create", "--file", str(tar_path),
                            "--sparse", "--acls", "--xattrs", "--numeric-owner",
                            "--directory", str(source), "."], check=True)

            root = base / "root"
            objects = root / "baseline/home/objects"
            timeline_parent = root / "timeline/home"
            timeline_parent.mkdir(parents=True)
            report = base / "report.json"
            entries = base / "entries.ndjson"
            result = subprocess.run([
                str(BINARY), "scan", "--source", str(source),
                "--objects", str(objects), "--entries", str(entries),
                "--report", str(report), "--timeline", str(timeline_parent),
                "--run-id", "20261106T160001.000000Z",
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            common = json.loads(report.read_text())

            external = base / "python-external"
            external.mkdir()
            stage = base / "python-stage"
            stage.mkdir()
            saved = (self.reference.KIT, self.reference.prepare_staging)
            self.reference.KIT = base / "python-kit"
            self.reference.KIT.mkdir()
            self.reference.prepare_staging = lambda _root: stage
            try:
                with tar_path.open("rb") as stream:
                    python_timeline, _manifest, _created, _timelines =                         self.reference.capture_home_archive(stream, external, {}, [])
            finally:
                self.reference.KIT, self.reference.prepare_staging = saved

            def core(path):
                result = {}
                for line in Path(path).read_text().splitlines():
                    entry = json.loads(line)
                    result[entry["path"]] = (
                        entry["type"], entry.get("size"), entry.get("sha256"),
                        entry.get("target"), entry.get("hardlink_to"),
                    )
                return result

            self.assertEqual(core(entries), core(python_timeline / "entries.ndjson"))
            self.reference.verify_home_timeline(python_timeline)

    def test_restore_preserves_xattrs_and_acls(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            target = source / "file.txt"
            target.write_text("attrs\n")
            os.setxattr(target, b"user.demo", b"value")
            acl = None
            if shutil.which("setfacl") and shutil.which("getfacl"):
                subprocess.run(["setfacl", "-m", f"u:{os.getuid()}:r", str(target)], check=True)
                acl = subprocess.run(["getfacl", "-cp", str(target)],
                                     capture_output=True, text=True, check=True).stdout
            scan_result, _report, _entries = self.scan(source, base)
            self.assertEqual(scan_result.returncode, 0, scan_result.stderr)
            restore_result, _restore_report, destination = self.restore(base)
            self.assertEqual(restore_result.returncode, 0, restore_result.stderr)
            self.assertEqual(os.getxattr(destination / "file.txt", b"user.demo"), b"value")
            if acl is not None:
                restored = subprocess.run(["getfacl", "-cp", str(destination / "file.txt")],
                                          capture_output=True, text=True, check=True).stdout
                self.assertEqual(restored, acl)

    def test_owner_options_require_root(self):
        if os.geteuid() == 0:
            self.skipTest("already root")
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "file.txt").write_text("x\n")
            result = subprocess.run([
                str(BINARY), "scan", "--source", str(source),
                "--objects", str(base / "objects"),
                "--entries", str(base / "entries.ndjson"),
                "--owner-uid", str(os.getuid()),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("require root", result.stderr)

    def test_exclude_prefix_and_name(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory)
            source = base / "source"
            (source / "skip").mkdir(parents=True)
            (source / "node_modules").mkdir()
            (source / "skip/a.txt").write_text("a\n")
            (source / "node_modules/b.txt").write_text("b\n")
            (source / "keep.txt").write_text("keep\n")
            result, report, entries = self.scan(
                source, base, "--exclude-prefix", "skip", "--exclude-name", "node_modules")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([entry["path"] for entry in entries], ["keep.txt"])
            self.assertTrue(report["complete"])


if __name__ == "__main__":
    unittest.main()
