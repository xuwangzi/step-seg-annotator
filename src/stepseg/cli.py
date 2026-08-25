"""Validation, inspection, and solid export commands."""

from __future__ import annotations

import argparse
from pathlib import Path

from .export import export_solids
from .storage import load_document, source_matches
from .topology import geometry_signature, load_step, replay_document


def main() -> None:
    parser = argparse.ArgumentParser(prog="stepseg")
    subcommands = parser.add_subparsers(dest="command", required=True)
    validate = subcommands.add_parser("validate")
    validate.add_argument("annotation", type=Path)
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("step", type=Path)
    export = subcommands.add_parser("export-solids")
    export.add_argument("annotation", type=Path)
    export.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "inspect":
        for entity in load_step(args.step):
            signature = geometry_signature(entity.shape)
            print(f"{entity.id}: {len(entity.faces)} faces, volume={signature.volume:.8g}")
        return

    document = load_document(args.annotation)
    source_path = Path(document.source_path)
    errors = document.validate()
    if not source_path.exists():
        errors.append(f"source STEP does not exist: {source_path}")
    elif not source_matches(source_path, document):
        errors.append("source STEP hash does not match")
    if errors:
        raise SystemExit("\n".join(errors))
    entities = replay_document(source_path, document)
    if args.command == "validate":
        print(f"valid: {len(entities)} entities")
        return
    paths = export_solids(document, entities, args.output)
    print("\n".join(str(path) for path in paths))
