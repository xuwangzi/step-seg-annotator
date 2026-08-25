"""Dataset validation and export command line interface."""

from __future__ import annotations

import argparse
from pathlib import Path

from .export import export_aagnet
from .storage import load_document
from .topology import load_step


def main() -> None:
    parser = argparse.ArgumentParser(prog="stepseg")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate")
    validate.add_argument("annotation", type=Path)
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("step", type=Path)
    export = subcommands.add_parser("export-aagnet")
    export.add_argument("annotation", type=Path)
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--strict-mf25", action="store_true")
    args = parser.parse_args()
    if args.command == "inspect":
        bodies = load_step(args.step)
        for body in bodies:
            print(f"{body.id}: {len(body.face_ids)} faces")
        return
    document = load_document(args.annotation)
    if args.command == "validate":
        errors = document.validate(require_complete=True)
        if errors:
            raise SystemExit("\n".join(errors))
        print("valid")
        return
    paths = export_aagnet(document, args.output, args.strict_mf25)
    print("\n".join(str(path) for path in paths))
