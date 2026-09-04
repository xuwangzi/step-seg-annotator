"""Validation, inspection, and solid export commands."""

from __future__ import annotations

import argparse
from pathlib import Path

from .export import export_faces, export_solids
from .face_partition import file_sha256, load_partition_snapshot, partition_matches_document, resolve_snapshot_path
from .models import FaceAnnotationDocument
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
    export_faces_parser = subcommands.add_parser("export-faces")
    export_faces_parser.add_argument("annotation", type=Path)
    export_faces_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "inspect":
        for entity in load_step(args.step):
            signature = geometry_signature(entity.shape)
            print(f"{entity.id}: {len(entity.faces)} faces, volume={signature.volume:.8g}")
        return

    document = load_document(args.annotation)
    if isinstance(document, FaceAnnotationDocument):
        source_path = Path(document.source_path)
        errors = document.validate()
        if not source_path.exists():
            errors.append(f"source STEP does not exist: {source_path}")
        elif not source_matches(source_path, document):
            errors.append("source STEP hash does not match")
        snapshot = resolve_snapshot_path(document)
        if not snapshot.exists():
            errors.append(f"partition snapshot does not exist: {snapshot}")
        elif file_sha256(snapshot) != document.snapshot_sha256:
            errors.append("partition snapshot hash does not match")
        if not errors:
            partition = load_partition_snapshot(snapshot)
            if not partition_matches_document(partition, document):
                errors.append("partition snapshot faces do not match annotation")
        if errors:
            raise SystemExit("\n".join(errors))
        if args.command == "validate":
            print(f"valid: {len(document.faces)} faces, {len(document.groups)} groups")
            return
        if args.command == "export-faces":
            paths = export_faces(document, partition, args.output)
            print("\n".join(str(path) for path in paths))
            return
        raise SystemExit("face annotation requires the export-faces command")

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
    if args.command != "export-solids":
        raise SystemExit("solid annotation requires the export-solids command")
    paths = export_solids(document, entities, args.output)
    print("\n".join(str(path) for path in paths))
