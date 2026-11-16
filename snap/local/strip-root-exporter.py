#!/usr/bin/env python3
"""Remove APT-only privileged code from the strict snap payload.

The deb packages keep the sudo broker and the embedded root exporter. The
strict snap runs entirely in user space, so neither is installed or referenced
in its payload. Keeping dead privileged code in a strict snap invites review
problems even when SNAP_MODE would prevent execution.
"""
import ast
import sys
from pathlib import Path

REMOVE_NODES = {
    "PrivilegeBroker",
    "_privilege_socket_path",
    "_broker_workspace",
    "_broker_run",
    "_broker_request",
    "_privilege_broker_loop",
}

REMOVE_TEXT = (
    '    parser.add_argument("--privilege-broker", action="store_true", help=argparse.SUPPRESS)\n'
    '    parser.add_argument("--broker-client-pid", type=int, help=argparse.SUPPRESS)\n',
    "    if args.privilege_broker:\n"
    "        _privilege_broker_loop(workspace, args.broker_client_pid)\n"
    "        return\n",
    '            if title in ("Backup", "Restore") and not SNAP_MODE:\n'
    "                broker = PrivilegeBroker()\n"
    "                broker.start()\n"
    "                _PRIVILEGE_BROKER = broker\n"
    '                output.put("Privileged session started; one password prompt for this operation.\\n")\n',
    "            if broker is not None:\n"
    "                broker.stop()\n"
    "            _PRIVILEGE_BROKER = None\n",
)


def main(path):
    path = Path(path)
    source = path.read_text()
    for block in REMOVE_TEXT:
        if block not in source:
            raise SystemExit("Expected privileged block not found in " + str(path))
        source = source.replace(block, "", 1)

    tree = ast.parse(source)
    ranges = []
    for node in tree.body:
        name = getattr(node, "name", None)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and name in REMOVE_NODES:
            ranges.append((node.lineno, node.end_lineno))
    missing = REMOVE_NODES - {
        getattr(node, "name", None) for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    if missing:
        raise SystemExit("Privileged definitions not found: " + ", ".join(sorted(missing)))
    lines = source.splitlines(keepends=True)
    for start, end in sorted(ranges, reverse=True):
        del lines[start - 1:end]
    source = "".join(lines)

    tree = ast.parse(source)
    exporter = None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "EMBEDDED_ROOT_EXPORTER"):
            exporter = node
            break
    if exporter is None:
        raise SystemExit("EMBEDDED_ROOT_EXPORTER assignment not found")
    lines = source.splitlines(keepends=True)
    lines[exporter.lineno - 1:exporter.end_lineno] = ["EMBEDDED_ROOT_EXPORTER = ''\n"]
    source = "".join(lines)

    for forbidden in ("PrivilegeBroker", "--privilege-broker",
                      "--broker-client-pid", "_privilege_broker_loop",
                      "_privilege_socket_path", '"sudo", "-A"'):
        if forbidden in source:
            raise SystemExit("Stripped payload still contains: " + forbidden)
    ast.parse(source)
    path.write_text(source)
    print(f"Stripped privileged broker and root exporter from {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: strip-root-exporter.py PATH")
    main(sys.argv[1])
