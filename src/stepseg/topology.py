"""STEP import, planar split candidates, closed-solid splitting, and replay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import sqrt
from pathlib import Path
from typing import Any

import OCP
from OCP.BRep import BRep_Tool
from OCP.Bnd import Bnd_Box
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_Plane
from OCP.IFSelect import IFSelect_RetDone
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Shape
from OCP.TopExp import TopExp_Explorer
from OCP.TopTools import TopTools_ListOfShape

from .models import (
    AnnotationDocument,
    EntityRecord,
    FaceSource,
    GeometrySignature,
    PlaneSpec,
    SplitOperation,
)
from .storage import sha256_file


VOLUME_TOLERANCE = 1e-7
ENTITY_COLORS = ["#3B638A", "#3F7D3A", "#579695", "#B86B4B", "#8A5E9E", "#D29F3F", "#4C8A72"]


@dataclass(slots=True)
class EntityShape:
    id: str
    source_body_id: str
    shape: TopoDS_Shape
    faces: dict[str, TopoDS_Face]
    face_sources: dict[str, FaceSource]

    def face_id_for(self, picked: TopoDS_Shape) -> str | None:
        return next((face_id for face_id, face in self.faces.items() if face.IsSame(picked)), None)


@dataclass(slots=True)
class SplitCandidate:
    plane: PlaneSpec
    result_shapes: list[TopoDS_Shape]
    result_signatures: list[GeometrySignature]
    score: float


def _subshapes(shape: TopoDS_Shape, shape_type: int) -> list[TopoDS_Shape]:
    explorer = TopExp_Explorer(shape, shape_type)
    values: list[TopoDS_Shape] = []
    while explorer.More():
        values.append(explorer.Current())
        explorer.Next()
    return values


def _bbox(shape: TopoDS_Shape) -> tuple[float, float, float, float, float, float]:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return tuple(float(value) for value in box.Get())


def geometry_signature(shape: TopoDS_Shape) -> GeometrySignature:
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    center = properties.CentreOfMass()
    return GeometrySignature(
        volume=float(properties.Mass()),
        centroid=(center.X(), center.Y(), center.Z()),
        bbox=_bbox(shape),
    )


def _signature_key(value: GeometrySignature) -> tuple[float, ...]:
    return (*[round(item, 8) for item in value.centroid], round(value.volume, 8), *value.bbox)


def _canonical_plane(normal: tuple[float, float, float], offset: float) -> PlaneSpec:
    length = sqrt(sum(value * value for value in normal))
    values = tuple(value / length for value in normal)
    offset /= length
    for value in values:
        if abs(value) > 1e-12:
            if value < 0:
                values = tuple(-item for item in values)
                offset = -offset
            break
    return PlaneSpec(values, offset)


def _plane_from_face(face: TopoDS_Face) -> PlaneSpec | None:
    surface = BRepAdaptor_Surface(face, True)
    if surface.GetType() != GeomAbs_Plane:
        return None
    plane = surface.Plane()
    direction = plane.Axis().Direction()
    location = plane.Location()
    normal = (direction.X(), direction.Y(), direction.Z())
    offset = sum(value * coord for value, coord in zip(normal, (location.X(), location.Y(), location.Z())))
    return _canonical_plane(normal, offset)


def _plane_face(spec: PlaneSpec, shape: TopoDS_Shape) -> TopoDS_Face:
    bounds = _bbox(shape)
    diagonal = sqrt(
        (bounds[3] - bounds[0]) ** 2
        + (bounds[4] - bounds[1]) ** 2
        + (bounds[5] - bounds[2]) ** 2
    )
    extent = max(diagonal * 2.0, 1.0)
    center = tuple((bounds[index] + bounds[index + 3]) / 2.0 for index in range(3))
    correction = spec.offset - sum(
        normal * coordinate for normal, coordinate in zip(spec.normal, center)
    )
    point = gp_Pnt(
        *(coordinate + normal * correction for coordinate, normal in zip(center, spec.normal))
    )
    plane = gp_Pln(point, gp_Dir(*spec.normal))
    return BRepBuilderAPI_MakeFace(plane, -extent, extent, -extent, extent).Face()


def split_shape(shape: TopoDS_Shape, plane: PlaneSpec) -> tuple[list[TopoDS_Shape], Any]:
    arguments = TopTools_ListOfShape()
    arguments.Append(shape)
    tools = TopTools_ListOfShape()
    tools.Append(_plane_face(plane, shape))
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone():
        raise ValueError("OpenCascade split failed")
    solids = _subshapes(splitter.Shape(), TopAbs_SOLID)
    ordered = sorted(
        ((geometry_signature(item), item) for item in solids), key=lambda item: _signature_key(item[0])
    )
    solids = [item[1] for item in ordered]
    if len(solids) < 2:
        raise ValueError("plane does not split the entity")
    if not all(
        BRepCheck_Analyzer(item).IsValid()
        and (shells := _subshapes(item, TopAbs_SHELL))
        and all(BRep_Tool.IsClosed_s(shell) for shell in shells)
        for item in solids
    ):
        raise ValueError("split produced an invalid solid")
    original_volume = geometry_signature(shape).volume
    result_volume = sum(geometry_signature(item).volume for item in solids)
    relative_error = abs(result_volume - original_volume) / max(abs(original_volume), 1.0)
    if relative_error > VOLUME_TOLERANCE:
        raise ValueError(f"split volume error {relative_error:.3g} exceeds tolerance")
    return solids, splitter


def _surface_center(face: TopoDS_Face) -> tuple[float, float, float]:
    properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, properties)
    center = properties.CentreOfMass()
    return center.X(), center.Y(), center.Z()


def planar_split_candidates(entity: EntityShape, seed_face_id: str) -> list[SplitCandidate]:
    if seed_face_id not in entity.faces:
        return []
    seed_face = entity.faces[seed_face_id]
    seed_center = _surface_center(seed_face)
    bounds = _bbox(entity.shape)
    length_scale = max(
        sqrt(
            (bounds[3] - bounds[0]) ** 2
            + (bounds[4] - bounds[1]) ** 2
            + (bounds[5] - bounds[2]) ** 2
        ),
        1.0,
    )
    total_volume = geometry_signature(entity.shape).volume
    unique: dict[tuple[float, ...], PlaneSpec] = {}
    for face in entity.faces.values():
        plane = _plane_from_face(face)
        if plane is None:
            continue
        key = (*[round(value, 8) for value in plane.normal], round(plane.offset, 7))
        unique.setdefault(key, plane)
    candidates: list[SplitCandidate] = []
    for plane in unique.values():
        try:
            parts, splitter = split_shape(entity.shape, plane)
        except ValueError:
            continue
        signatures = [geometry_signature(item) for item in parts]
        distance = abs(sum(a * b for a, b in zip(plane.normal, seed_center)) - plane.offset)
        seed_variants: list[TopoDS_Shape] = [seed_face]
        try:
            seed_variants.extend(splitter.Modified(seed_face))
        except Exception:
            pass
        seed_part_volume = total_volume
        for part, signature in zip(parts, signatures, strict=True):
            if any(
                variant.IsSame(face)
                for variant in seed_variants
                for face in _subshapes(part, TopAbs_FACE)
            ):
                seed_part_volume = signature.volume
                break
        score = distance / length_scale + seed_part_volume / max(total_volume, 1.0)
        candidates.append(SplitCandidate(plane, parts, signatures, score))
    return sorted(candidates, key=lambda item: (item.score, len(item.result_shapes)))


def _face_mapping(
    parent: EntityShape,
    child_id: str,
    child: TopoDS_Shape,
    splitter: Any,
    split_id: str,
) -> EntityShape:
    faces: dict[str, TopoDS_Face] = {}
    sources: dict[str, FaceSource] = {}
    inherited: list[tuple[TopoDS_Shape, FaceSource]] = []
    for parent_id, parent_face in parent.faces.items():
        inherited.append((parent_face, parent.face_sources[parent_id]))
        try:
            for modified in splitter.Modified(parent_face):
                inherited.append((modified, parent.face_sources[parent_id]))
        except Exception:
            pass
    for index, raw_face in enumerate(_subshapes(child, TopAbs_FACE), start=1):
        face = TopoDS.Face_s(raw_face)
        face_id = f"{child_id}/face_{index:05d}"
        faces[face_id] = face
        source = next((item for shape, item in inherited if shape.IsSame(face)), None)
        sources[face_id] = FaceSource(
            face_id,
            source.source_face_id if source else None,
            source.generated_by_split_id if source else split_id,
        )
    return EntityShape(child_id, parent.source_body_id, child, faces, sources)


def build_entity(shape: TopoDS_Shape, entity_id: str, source_body_id: str) -> EntityShape:
    faces: dict[str, TopoDS_Face] = {}
    sources: dict[str, FaceSource] = {}
    for index, raw_face in enumerate(_subshapes(shape, TopAbs_FACE), start=1):
        face = TopoDS.Face_s(raw_face)
        face_id = f"{entity_id}/face_{index:05d}"
        source_id = f"{source_body_id}/face_{index:05d}"
        faces[face_id] = face
        sources[face_id] = FaceSource(face_id, source_id, None)
    return EntityShape(entity_id, source_body_id, shape, faces, sources)


def split_entity(
    parent: EntityShape, plane: PlaneSpec, result_ids: list[str], split_id: str
) -> list[EntityShape]:
    parts, splitter = split_shape(parent.shape, plane)
    if len(parts) != len(result_ids):
        raise ValueError("replayed split produced a different entity count")
    return [
        _face_mapping(parent, entity_id, part, splitter, split_id)
        for entity_id, part in zip(result_ids, parts, strict=True)
    ]


def load_step(path: Path) -> list[EntityShape]:
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot read STEP file: {path}")
    for root_index in range(1, reader.NbRootsForTransfer() + 1):
        reader.TransferRoot(root_index)
    solids: list[TopoDS_Shape] = []
    for shape_index in range(1, reader.NbShapes() + 1):
        solids.extend(_subshapes(reader.Shape(shape_index), TopAbs_SOLID))
    if not solids:
        raise ValueError("STEP file has no solid bodies")
    return [
        build_entity(shape, f"entity_{index:04d}", f"solid_{index:04d}")
        for index, shape in enumerate(solids, start=1)
    ]


def record_for(entity: EntityShape, color: str, name: str = "") -> EntityRecord:
    return EntityRecord(
        id=entity.id,
        source_body_id=entity.source_body_id,
        signature=geometry_signature(entity.shape),
        name=name or entity.id,
        color=color,
        face_sources=list(entity.face_sources.values()),
    )


def new_document(step_path: Path, entities: list[EntityShape]) -> AnnotationDocument:
    records = [
        record_for(entity, ENTITY_COLORS[index % len(ENTITY_COLORS)])
        for index, entity in enumerate(entities)
    ]
    return AnnotationDocument(
        source_path=str(step_path.resolve()),
        source_sha256=sha256_file(step_path),
        ocp_version=OCP.__version__,
        initial_entities=[replace(record, face_sources=list(record.face_sources)) for record in records],
        entities=records,
    )


def replay_document(step_path: Path, document: AnnotationDocument) -> list[EntityShape]:
    errors = document.validate()
    if errors:
        raise ValueError("invalid annotation: " + "; ".join(errors))
    if document.ocp_version != OCP.__version__:
        raise ValueError(
            f"OpenCascade version changed: {document.ocp_version} -> {OCP.__version__}"
        )
    active = {entity.id: entity for entity in load_step(step_path)}
    if set(active) != {item.id for item in document.initial_entities}:
        raise ValueError("source STEP solid count does not match the annotation")
    for operation in document.split_operations:
        parent = active.pop(operation.parent_entity.id, None)
        if parent is None:
            raise ValueError(f"cannot replay {operation.id}: parent entity is missing")
        results = split_entity(parent, operation.plane, operation.result_entity_ids, operation.id)
        for result, expected in zip(results, operation.result_signatures, strict=True):
            actual = geometry_signature(result.shape)
            volume_error = abs(actual.volume - expected.volume) / max(abs(expected.volume), 1.0)
            length_scale = max(
                sqrt(
                    (expected.bbox[3] - expected.bbox[0]) ** 2
                    + (expected.bbox[4] - expected.bbox[1]) ** 2
                    + (expected.bbox[5] - expected.bbox[2]) ** 2
                ),
                1.0,
            )
            centroid_error = max(
                abs(value - reference)
                for value, reference in zip(actual.centroid, expected.centroid)
            ) / length_scale
            bbox_error = max(
                abs(value - reference) for value, reference in zip(actual.bbox, expected.bbox)
            ) / length_scale
            if max(volume_error, centroid_error, bbox_error) > VOLUME_TOLERANCE:
                raise ValueError(f"cannot replay {operation.id}: geometry signature changed")
            active[result.id] = result
    if set(active) != {item.id for item in document.entities}:
        raise ValueError("replayed entities do not match the annotation")
    return [active[item.id] for item in document.entities]


def apply_split(
    document: AnnotationDocument,
    entities: list[EntityShape],
    parent_id: str,
    plane: PlaneSpec,
) -> list[EntityShape]:
    parent_index = next(
        (index for index, entity in enumerate(entities) if entity.id == parent_id), None
    )
    if parent_index is None:
        raise ValueError(f"unknown entity: {parent_id}")
    parent = entities[parent_index]
    parent_record = document.entity_by_id(parent_id)
    parts, _splitter = split_shape(parent.shape, plane)
    first_number = document.next_entity_number()
    result_ids = [f"entity_{first_number + index:04d}" for index in range(len(parts))]
    split_id = document.next_split_id()
    results = split_entity(parent, plane, result_ids, split_id)
    records = [
        record_for(
            result,
            ENTITY_COLORS[(first_number + index - 1) % len(ENTITY_COLORS)],
        )
        for index, result in enumerate(results)
    ]
    operation = SplitOperation(
        id=split_id,
        parent_entity=replace(parent_record, face_sources=list(parent_record.face_sources)),
        plane=plane,
        result_entity_ids=result_ids,
        result_signatures=[record.signature for record in records],
    )
    document.split_operations.append(operation)
    document.entities[parent_index : parent_index + 1] = records
    return [*entities[:parent_index], *results, *entities[parent_index + 1 :]]


def undo_last_split(document: AnnotationDocument) -> bool:
    if not document.split_operations:
        return False
    operation = document.split_operations.pop()
    result_ids = set(operation.result_entity_ids)
    indexes = [index for index, entity in enumerate(document.entities) if entity.id in result_ids]
    if len(indexes) != len(result_ids):
        raise ValueError("cannot undo split: result entities changed")
    insert_at = min(indexes)
    document.entities = [entity for entity in document.entities if entity.id not in result_ids]
    document.entities.insert(insert_at, operation.parent_entity)
    return True
