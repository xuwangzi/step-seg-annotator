"""Persistent annotation model and validation rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


BACKGROUND_CLASS_ID = 0


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class TaxonomyClass:
    id: int
    key: str
    name_zh: str
    color: str
    aagnet_class_id: int | None = None
    enabled: bool = True


def default_taxonomy() -> list[TaxonomyClass]:
    raw = [
        (0, "background", "背景/基体", "#8B8B8B"),
        (1, "hole", "孔", "#3B82F6"),
        (2, "slot", "槽", "#06B6D4"),
        (3, "pocket", "凹腔", "#8B5CF6"),
        (4, "step", "台阶", "#F97316"),
        (5, "boss", "凸台/螺钉柱", "#22C55E"),
        (6, "rib", "加强筋", "#EAB308"),
        (7, "fillet", "圆角", "#EC4899"),
        (8, "chamfer", "倒角", "#14B8A6"),
        (9, "buckle", "卡扣", "#EF4444"),
        (10, "positioning", "定位结构", "#A855F7"),
        (11, "hook", "挂钩", "#F59E0B"),
        (12, "other", "其他", "#64748B"),
    ]
    return [TaxonomyClass(*item) for item in raw]


@dataclass(slots=True)
class BodyRecord:
    id: str
    name: str
    face_ids: list[str]


@dataclass(slots=True)
class FeatureInstance:
    id: str
    class_id: int
    body_id: str
    face_ids: list[str]
    bottom_face_ids: list[str] = field(default_factory=list)
    note: str = ""


@dataclass(slots=True)
class AnnotationDocument:
    source_path: str
    source_sha256: str
    ocp_version: str
    bodies: list[BodyRecord]
    instances: list[FeatureInstance] = field(default_factory=list)
    taxonomy: list[TaxonomyClass] = field(default_factory=default_taxonomy)
    status: str = "draft"
    annotator: str = ""
    reviewer: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    schema_version: str = "1.0"

    def class_by_id(self, class_id: int) -> TaxonomyClass:
        for item in self.taxonomy:
            if item.id == class_id:
                return item
        raise ValueError(f"unknown class id: {class_id}")

    def all_face_ids(self) -> set[str]:
        return {face_id for body in self.bodies for face_id in body.face_ids}

    def assigned_face_ids(self) -> set[str]:
        return {face_id for instance in self.instances for face_id in instance.face_ids}

    def validate(self, require_complete: bool = False) -> list[str]:
        errors: list[str] = []
        face_ids = self.all_face_ids()
        occurrences: dict[str, str] = {}
        body_ids = {body.id for body in self.bodies}
        class_ids = {item.id for item in self.taxonomy}
        if BACKGROUND_CLASS_ID not in class_ids:
            errors.append("taxonomy must define class id 0 (background)")
        if len(class_ids) != len(self.taxonomy):
            errors.append("taxonomy contains duplicate class ids")
        for instance in self.instances:
            if instance.body_id not in body_ids:
                errors.append(f"{instance.id}: unknown body {instance.body_id}")
            if instance.class_id not in class_ids:
                errors.append(f"{instance.id}: unknown class {instance.class_id}")
            if not instance.face_ids:
                errors.append(f"{instance.id}: no faces")
            unknown = set(instance.face_ids) - face_ids
            if unknown:
                errors.append(f"{instance.id}: unknown faces {sorted(unknown)}")
            outside_bottom = set(instance.bottom_face_ids) - set(instance.face_ids)
            if outside_bottom:
                errors.append(f"{instance.id}: bottom faces are not in the instance")
            for face_id in instance.face_ids:
                previous = occurrences.setdefault(face_id, instance.id)
                if previous != instance.id:
                    errors.append(f"{face_id}: assigned by both {previous} and {instance.id}")
        if require_complete:
            missing = face_ids - set(occurrences)
            if missing:
                errors.append(f"unassigned faces: {len(missing)}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AnnotationDocument":
        return cls(
            source_path=payload["source_path"],
            source_sha256=payload["source_sha256"],
            ocp_version=payload["ocp_version"],
            bodies=[BodyRecord(**item) for item in payload["bodies"]],
            instances=[FeatureInstance(**item) for item in payload.get("instances", [])],
            taxonomy=[TaxonomyClass(**item) for item in payload.get("taxonomy", [])]
            or default_taxonomy(),
            status=payload.get("status", "draft"),
            annotator=payload.get("annotator", ""),
            reviewer=payload.get("reviewer", ""),
            created_at=payload.get("created_at", now_iso()),
            updated_at=payload.get("updated_at", now_iso()),
            schema_version=payload.get("schema_version", "1.0"),
        )


def annotation_path_for(step_path: Path) -> Path:
    return step_path.with_suffix(".stepanno.json")
