"""Export final closed entities as STEP files plus a JSON manifest."""

from __future__ import annotations

import json
from pathlib import Path

from OCP.BRep import BRep_Builder
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopoDS import TopoDS_Compound

from .models import AnnotationDocument, FaceAnnotationDocument
from .topology import EntityShape


def _write_step(shape, path: Path) -> None:
    writer = STEPControl_Writer()
    if writer.Transfer(shape, STEPControl_AsIs) != IFSelect_RetDone:
        raise ValueError(f"cannot transfer STEP geometry for {path.name}")
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot write STEP file: {path}")


def export_solids(
    document: AnnotationDocument, entities: list[EntityShape], output_dir: Path
) -> list[Path]:
    errors = document.validate()
    if errors:
        raise ValueError("cannot export invalid annotation: " + "; ".join(errors))
    by_id = {entity.id: entity for entity in entities}
    if set(by_id) != {item.id for item in document.entities}:
        raise ValueError("runtime entities do not match the annotation")

    output_dir.mkdir(parents=True, exist_ok=True)
    entity_dir = output_dir / "entities"
    entity_dir.mkdir(exist_ok=True)
    paths: list[Path] = []

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    manifest_entities: list[dict[str, object]] = []
    for record in document.entities:
        entity = by_id[record.id]
        builder.Add(compound, entity.shape)
        path = entity_dir / f"{record.id}.step"
        _write_step(entity.shape, path)
        paths.append(path)
        category = document.class_by_id(record.class_id)
        manifest_entities.append(
            {
                "id": record.id,
                "name": record.name,
                "class_id": record.class_id,
                "class_key": category.key if category else None,
                "color": record.color,
                "note": record.note,
                "source_body_id": record.source_body_id,
                "step_file": str(path.relative_to(output_dir)),
                "signature": {
                    "volume": record.signature.volume,
                    "centroid": record.signature.centroid,
                    "bbox": record.signature.bbox,
                },
                "faces": [
                    {
                        "face_id": item.face_id,
                        "source_face_id": item.source_face_id,
                        "generated_by_split_id": item.generated_by_split_id,
                    }
                    for item in record.face_sources
                ],
            }
        )

    combined_path = output_dir / "combined.step"
    _write_step(compound, combined_path)
    paths.insert(0, combined_path)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": document.schema_version,
                "source_path": document.source_path,
                "source_sha256": document.source_sha256,
                "ocp_version": document.ocp_version,
                "entities": manifest_entities,
                "split_operations": [
                    {
                        "id": item.id,
                        "parent_entity_id": item.parent_entity.id,
                        "plane": {"normal": item.plane.normal, "offset": item.plane.offset},
                        "result_entity_ids": item.result_entity_ids,
                    }
                    for item in document.split_operations
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    paths.append(manifest_path)
    return paths


def export_faces(
    document: FaceAnnotationDocument, partition, output_dir: Path
) -> list[Path]:
    """Export the imprinted face partition and its face-group manifest."""
    errors = document.validate()
    if errors:
        raise ValueError("cannot export invalid annotation: " + "; ".join(errors))
    actual_ids = {record.id for record in partition.records}
    if actual_ids != {record.id for record in document.faces}:
        raise ValueError("runtime face partition does not match the annotation")

    output_dir.mkdir(parents=True, exist_ok=True)
    partition_path = output_dir / "partition.step"
    _write_step(partition.shape, partition_path)
    groups = {face_id: group.id for group in document.groups for face_id in group.face_ids}
    taxonomy = {item.id: item for item in document.taxonomy}
    payload = {
        "schema_version": document.schema_version,
        "source_path": document.source_path,
        "source_sha256": document.source_sha256,
        "ocp_version": document.ocp_version,
        "fusion_mode": document.fusion_mode,
        "snapshot_path": document.snapshot_path,
        "snapshot_sha256": document.snapshot_sha256,
        "status": document.status,
        "faces": [
            {
                "id": face.id,
                "surface_kind": face.surface_kind,
                "area": face.area,
                "centroid": face.centroid,
                "bbox": face.bbox,
                "source_body_ids": face.source_body_ids,
                "group_id": groups.get(face.id),
            }
            for face in document.faces
        ],
        "groups": [
            {
                "id": group.id,
                "name": group.name,
                "class_id": group.class_id,
                "class_key": taxonomy[group.class_id].key if group.class_id in taxonomy else None,
                "color": group.color,
                "note": group.note,
                "face_ids": group.face_ids,
            }
            for group in document.groups
        ],
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return [partition_path, manifest_path]
