#!/usr/bin/env python3
"""Small-tree benchmark for direct versus staged native capture."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def make_tree(root, files, size):
    root.mkdir(parents=True)
    for index in range(files):
        prefix = f"{index:08d}".encode()
        payload = prefix + b"x" * max(0, size - len(prefix))
        (root / f"file-{index:05d}.dat").write_bytes(payload)
    (root / "sparse.dat").write_bytes(b"A" * 4096)
    with (root / "sparse.dat").open("r+b") as stream:
        stream.seek(size * 4)
        stream.write(b"B" * 4096)
        stream.truncate(size * 8)


def run(binary, source, objects, report, stage=None):
    command = [str(binary), "scan", "--source", str(source),
               "--objects", str(objects), "--entries", str(report.parent / "entries.ndjson"),
               "--report", str(report)]
    if stage is not None:
        command.extend(["--stage", str(stage)])
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True)
    elapsed = time.monotonic() - started
    if result.returncode not in (0, 3):
        raise SystemExit(result.stderr)
    data = json.loads(report.read_text())
    data["elapsed"] = elapsed
    data["objects_per_second"] = data["objects_created"] / elapsed if elapsed else 0
    data["input_megabytes_per_second"] = data["logical_bytes"] / elapsed / 1024 / 1024 if elapsed else 0
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, default=Path(__file__).with_name("ubackup-core"))
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--size", type=int, default=4096)
    parser.add_argument("--objects", type=Path, help="Destination for both runs; defaults to a temp directory")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory:
        base = Path(directory)
        source = base / "source"
        make_tree(source, args.files, args.size)
        target_root = args.objects or base
        stage_root = Path("/dev/shm") if Path("/dev/shm").is_dir() else base
        results = {}
        for mode in ("direct", "staged"):
            objects = target_root / (mode + "-objects")
            report = target_root / (mode + "-report.json")
            stage = stage_root / ("ubackup-benchmark-" + str(os.getpid())) if mode == "staged" else None
            results[mode] = run(args.binary, source, objects, report, stage)
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
        print("files:", args.files, "size:", args.size)
        for mode, data in results.items():
            print(f"{mode:6} objects/s={data['objects_per_second']:.1f} "
                  f"input MB/s={data['input_megabytes_per_second']:.1f} "
                  f"elapsed={data['elapsed']:.2f}s objects={data['objects_created']}")


if __name__ == "__main__":
    main()
