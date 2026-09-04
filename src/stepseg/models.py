"""Persistent model for closed-solid segmentation annotations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "2.0"
FACE_SCHEMA_VERSION = "3.0"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class TaxonomyClass:
    id: int
    key: str
    name_zh: str
    color: str
    enabled: bool = True


def default_taxonomy() -> list[TaxonomyClass]:
    return [
        TaxonomyClass(1, "base", "基体/底座", "#3B638A"),
        TaxonomyClass(2, "boss", "凸台", "#3F7D3A"),
        TaxonomyClass(3, "column", "柱体", "#579695"),
        TaxonomyClass(4, "rib", "加强结构", "#D29F3F"),
        TaxonomyClass(5, "other", "其他", "#71717A"),
    ]


@dataclass(slots=True)
class PlaneSpec:
    normal: tuple[float, float, float]
    offset: float


@dataclass(slots=True)
class GeometrySignature:
    volume: float
    centroid: tuple[float, float, float]
    bbox: tuple[float, float, float, float, float, float]


@dataclass(slots=True)
class FaceSource:
    face_id: str
    source_face_id: str | None = None
    generated_by_split_id: str | None = None


@dataclass(slots=True)
class EntityRecord:
    id: str
    source_body_id: str
    signature: GeometrySignature
    class_id: int | None = None
    name: str = ""
    color: str = "#71717A"
    note: str = ""
    face_sources: list[FaceSource] = field(default_factory=list)


@dataclass(slots=True)
class SplitOperation:
    id: str
    parent_entity: EntityRecord
    plane: PlaneSpec
    result_entity_ids: list[str]
    result_signatures: list[GeometrySignature]


@dataclass(slots=True)
class FaceRecord:
    id: str
    surface_kind: str
    area: float
    centroid: tuple[float, float, float]
    bbox: tuple[float, float, float, float, float, float]
    source_body_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FaceGroupRecord:
    id: str
    name: str
    class_id: int | None = None
    color: str = "#71717A"
    note: str = ""
    face_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FaceAnnotationDocument:
    source_path: str
    source_sha256: str
    ocp_version: str
    fusion_mode: str
    snapshot_path: str
    snapshot_sha256: str
    faces: list[FaceRecord]
    groups: list[FaceGroupRecord] = field(default_factory=list)
    taxonomy: list[TaxonomyClass] = field(default_factory=default_taxonomy)
    status: str = "draft"
    annotator: str = ""
    reviewer: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    schema_version: str = FACE_SCHEMA_VERSION

    def class_by_id(self, class_id: int | None) -> TaxonomyClass | None:
        if class_id is None:
            return None
        return next((item for item in self.taxonomy if item.id == class_id), None)

    def group_by_id(self, group_id: str) -> FaceGroupRecord:
        for group in self.groups:
            if group.id == group_id:
                return group
        raise ValueError(f"unknown face group: {group_id}")

    def face_ids(self) -> set[str]:
        return {face.id for face in self.faces}

    def assignments(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for group in self.groups:
            for face_id in group.face_ids:
                result[face_id] = group.id
        return result

    def validate(self, require_complete: bool | None = None) -> list[str]:
        errors: list[str] = []
        face_ids = self.face_ids()
        class_ids = [item.id for item in self.taxonomy]
        if len(class_ids) != len(set(class_ids)):
            errors.append("taxonomy contains duplicate class ids")
        group_ids = [group.id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            errors.append("face groups contain duplicate ids")
        assignments: dict[str, str] = {}
        for group in self.groups:
            if not group.face_ids:
                errors.append(f"{group.id}: no faces")
            if group.class_id is not None and group.class_id not in class_ids:
                errors.append(f"{group.id}: unknown class {group.class_id}")
            for face_id in group.face_ids:
                if face_id not in face_ids:
                    errors.append(f"{group.id}: unknown face {face_id}")
                previous = assignments.setdefault(face_id, group.id)
                if previous != group.id:
                    errors.append(f"{face_id}: assigned by both {previous} and {group.id}")
        if require_complete is None:
            require_complete = self.status in {"completed", "reviewed"}
        if require_complete:
            missing = face_ids - set(assignments)
            if missing:
                errors.append(f"unassigned faces: {len(missing)}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FaceAnnotationDocument":
        if payload.get("schema_version") != FACE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported face annotation schema {payload.get('schema_version')!r}; "
                f"expected {FACE_SCHEMA_VERSION}"
            )

        def face(value: dict[str, Any]) -> FaceRecord:
            return FaceRecord(
                id=value["id"],
                surface_kind=value["surface_kind"],
                area=float(value["area"]),
                centroid=tuple(value["centroid"]),
                bbox=tuple(value["bbox"]),
                source_body_ids=list(value.get("source_body_ids", [])),
            )

        return cls(
            source_path=payload["source_path"],
            source_sha256=payload["source_sha256"],
            ocp_version=payload["ocp_version"],
            fusion_mode=payload.get("fusion_mode", "unknown"),
            snapshot_path=payload["snapshot_path"],
            snapshot_sha256=payload["snapshot_sha256"],
            faces=[face(item) for item in payload["faces"]],
            groups=[FaceGroupRecord(**item) for item in payload.get("groups", [])],
            taxonomy=[TaxonomyClass(**item) for item in payload.get("taxonomy", [])]
            or default_taxonomy(),
            status=payload.get("status", "draft"),
            annotator=payload.get("annotator", ""),
            reviewer=payload.get("reviewer", ""),
            created_at=payload.get("created_at", now_iso()),
            updated_at=payload.get("updated_at", now_iso()),
        )


@dataclass(slots=True)
class AnnotationDocument:
    source_path: str
    source_sha256: str
    ocp_version: str
    initial_entities: list[EntityRecord]
    entities: list[EntityRecord]
    split_operations: list[SplitOperation] = field(default_factory=list)
    taxonomy: list[TaxonomyClass] = field(default_factory=default_taxonomy)
    status: str = "draft"
    annotator: str = ""
    reviewer: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    schema_version: str = SCHEMA_VERSION

    def class_by_id(self, class_id: int | None) -> TaxonomyClass | None:
        if class_id is None:
            return None
        return next((item for item in self.taxonomy if item.id == class_id), None)

    def entity_by_id(self, entity_id: str) -> EntityRecord:
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        raise ValueError(f"unknown entity: {entity_id}")

    def next_entity_number(self) -> int:
        ids = [item.id for item in self.initial_entities]
        for operation in self.split_operations:
            ids.extend(operation.result_entity_ids)
        numbers = [
            int(value.removeprefix("entity_"))
            for value in ids
            if value.startswith("entity_") and value.removeprefix("entity_").isdigit()
        ]
        return max(numbers, default=0) + 1

    def next_split_id(self) -> str:
        return f"split_{len(self.split_operations) + 1:04d}"

    def validate(self) -> list[str]:
        errors: list[str] = []
        class_ids = [item.id for item in self.taxonomy]
        if len(class_ids) != len(set(class_ids)):
            errors.append("taxonomy contains duplicate class ids")
        entity_ids = [item.id for item in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            errors.append("entities contain duplicate ids")
        split_ids = [item.id for item in self.split_operations]
        if len(split_ids) != len(set(split_ids)):
            errors.append("split history contains duplicate ids")
        for entity in self.entities:
            if entity.class_id is not None and entity.class_id not in class_ids:
                errors.append(f"{entity.id}: unknown class {entity.class_id}")
            if entity.signature.volume <= 0:
                errors.append(f"{entity.id}: non-positive volume")
        active = {item.id for item in self.initial_entities}
        for operation in self.split_operations:
            parent_id = operation.parent_entity.id
            if parent_id not in active:
                errors.append(f"{operation.id}: inactive parent {parent_id}")
                continue
            if len(operation.result_entity_ids) < 2:
                errors.append(f"{operation.id}: split must create at least two entities")
            if len(operation.result_entity_ids) != len(operation.result_signatures):
                errors.append(f"{operation.id}: result/signature count mismatch")
            if len(operation.result_entity_ids) != len(set(operation.result_entity_ids)):
                errors.append(f"{operation.id}: duplicate result entity ids")
            if active.intersection(operation.result_entity_ids):
                errors.append(f"{operation.id}: result entity id is already active")
            active.remove(parent_id)
            active.update(operation.result_entity_ids)
        if active != set(entity_ids):
            errors.append("current entities do not match split history")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AnnotationDocument":
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported annotation schema {version!r}; expected {SCHEMA_VERSION}")

        def signature(value: dict[str, Any]) -> GeometrySignature:
            return GeometrySignature(
                float(value["volume"]), tuple(value["centroid"]), tuple(value["bbox"])
            )

        def entity(value: dict[str, Any]) -> EntityRecord:
            return EntityRecord(
                id=value["id"],
                source_body_id=value["source_body_id"],
                signature=signature(value["signature"]),
                class_id=value.get("class_id"),
                name=value.get("name", ""),
                color=value.get("color", "#71717A"),
                note=value.get("note", ""),
                face_sources=[FaceSource(**item) for item in value.get("face_sources", [])],
            )

        operations = [
            SplitOperation(
                id=item["id"],
                parent_entity=entity(item["parent_entity"]),
                plane=PlaneSpec(tuple(item["plane"]["normal"]), float(item["plane"]["offset"])),
                result_entity_ids=list(item["result_entity_ids"]),
                result_signatures=[signature(value) for value in item["result_signatures"]],
            )
            for item in payload.get("split_operations", [])
        ]
        return cls(
            source_path=payload["source_path"],
            source_sha256=payload["source_sha256"],
            ocp_version=payload["ocp_version"],
            initial_entities=[entity(item) for item in payload["initial_entities"]],
            entities=[entity(item) for item in payload["entities"]],
            split_operations=operations,
            taxonomy=[TaxonomyClass(**item) for item in payload.get("taxonomy", [])]
            or default_taxonomy(),
            status=payload.get("status", "draft"),
            annotator=payload.get("annotator", ""),
            reviewer=payload.get("reviewer", ""),
            created_at=payload.get("created_at", now_iso()),
            updated_at=payload.get("updated_at", now_iso()),
        )


def annotation_path_for(step_path: Path) -> Path:
    return step_path.with_suffix(".stepseg.json")
