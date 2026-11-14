#!/usr/bin/env python3
"""Remove the embedded root-only exporter from the strict snap payload.

The deb ships the real helper; the snap only needs the user-space app. Keeping a
root worker as dead data invites review questions and bloats the snap.
"""
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1])
source = path.read_text()
tree = ast.parse(source)
target = None
for node in tree.body:
    if (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "EMBEDDED_ROOT_EXPORTER"):
        target = node
        break
if target is None:
    raise SystemExit("EMBEDDED_ROOT_EXPORTER assignment not found")

lines = source.splitlines(keepends=True)
lines[target.lineno - 1:target.end_lineno] = ["EMBEDDED_ROOT_EXPORTER = ''\n"]
path.write_text("".join(lines))
ast.parse(path.read_text())
print(f"Stripped embedded root exporter from {path}")
