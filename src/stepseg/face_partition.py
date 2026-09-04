"""Face partitioning based on the local seam evidence used by unionseam.

This module deliberately uses the project's OCP bindings.  It does not split a
solid: it only imprints analytic edges into faces, leaving the underlying shape
and its volume unchanged.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import OCP
from OCP.BRep import BRep_Tool
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
from OCP.BRep import BRep_Builder
from OCP.BRepFeat import BRepFeat_SplitShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GProp import GProp_GProps
from OCP.Geom import Geom_Circle, Geom_Curve, Geom_Ellipse, Geom_Line
from OCP.GeomLProp import GeomLProp_SLProps
from OCP.ShapeAnalysis import ShapeAnalysis_Surface
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Face, TopoDS_Shape
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from .models import FaceAnnotationDocument, FaceRecord
from .topology import EntityShape, _bbox, _subshapes, load_step


TWO_PI = 2.0 * math.pi
MIN_SPAN = 1e-3
PARTITION_CACHE_VERSION = "3"


def _name(value: object) -> str:
    try:
        return value.DynamicType().Name()
    except Exception:
        return ""


def _xyz(value: object) -> tuple[float, float, float]:
    return float(value.X()), float(value.Y()), float(value.Z())


def _norm(value: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(item * item for item in value))
    return tuple(item / length for item in value) if length else (0.0, 0.0, 0.0)


def _canon_dir(value: tuple[float, float, float]) -> tuple[float, float, float]:
    value = _norm(value)
    for item in value:
        if abs(item) > 1e-12:
            return value if item > 0 else tuple(-part for part in value)
    return value


def _quantized(value: float, quantum: float) -> int:
    return int(round(value / quantum))


@dataclass(frozen=True, slots=True)
class CurveId:
    kind: str
    key: tuple
    origin: tuple[float, float, float]
    axis: tuple[float, float, float]
    ref: tuple[float, float, float] = ()
    radius: float = 0.0
    radius2: float = 0.0

    @property
    def periodic(self) -> bool:
        return self.kind in {"circle", "ellipse"}


def _basis(curve: Geom_Curve) -> Geom_Curve:
    current = curve
    for _ in range(4):
        if _name(current) != "Geom_TrimmedCurve":
            break
        current = current.BasisCurve()
    return current


def curve_id(curve: Geom_Curve, diagonal: float) -> CurveId | None:
    if curve is None:
        return None
    curve = _basis(curve)
    quantum = max(diagonal * 1e-6, 1e-9)
    kind = _name(curve)
    if kind == "Geom_Line":
        axis = curve.Position()
        direction = _canon_dir(_xyz(axis.Direction()))
        point = _xyz(axis.Location())
        projection = sum(a * b for a, b in zip(point, direction))
        origin = tuple(point[index] - projection * direction[index] for index in range(3))
        key = ("line", *(_quantized(item, quantum) for item in origin),
               *(_quantized(item * diagonal, quantum) for item in direction))
        return CurveId("line", key, origin, direction)
    if kind == "Geom_Circle":
        circle = curve
        position = circle.Position()
        origin = _xyz(position.Location())
        axis = _canon_dir(_xyz(position.Direction()))
        ref = _canon_dir(_xyz(position.XDirection()))
        radius = float(circle.Radius())
        key = ("circle", *(_quantized(item, quantum) for item in origin),
               *(_quantized(item * diagonal, quantum) for item in axis),
               _quantized(radius, quantum))
        return CurveId("circle", key, origin, axis, ref, radius)
    if kind == "Geom_Ellipse":
        ellipse = curve
        position = ellipse.Position()
        origin = _xyz(position.Location())
        axis = _canon_dir(_xyz(position.Direction()))
        ref = _canon_dir(_xyz(position.XDirection()))
        major, minor = float(ellipse.MajorRadius()), float(ellipse.MinorRadius())
        key = ("ellipse", *(_quantized(item, quantum) for item in origin),
               *(_quantized(item * diagonal, quantum) for item in axis),
               *(_quantized(item * diagonal, quantum) for item in ref),
               _quantized(major, quantum), _quantized(minor, quantum))
        return CurveId("ellipse", key, origin, axis, ref, major, minor)
    return None


def _param(cid: CurveId, point: tuple[float, float, float]) -> float:
    delta = tuple(point[index] - cid.origin[index] for index in range(3))
    if cid.kind == "line":
        return sum(a * b for a, b in zip(delta, cid.axis))
    y = (cid.axis[1] * cid.ref[2] - cid.axis[2] * cid.ref[1],
         cid.axis[2] * cid.ref[0] - cid.axis[0] * cid.ref[2],
         cid.axis[0] * cid.ref[1] - cid.axis[1] * cid.ref[0])
    x_value = sum(a * b for a, b in zip(delta, cid.ref)) / max(cid.radius, 1e-300)
    denominator = cid.radius2 if cid.kind == "ellipse" else cid.radius
    y_value = sum(a * b for a, b in zip(delta, y)) / max(denominator, 1e-300)
    return math.atan2(y_value, x_value) % TWO_PI


def _point_at(cid: CurveId, parameter: float) -> tuple[float, float, float]:
    if cid.kind == "line":
        return tuple(cid.origin[index] + parameter * cid.axis[index] for index in range(3))
    y = (cid.axis[1] * cid.ref[2] - cid.axis[2] * cid.ref[1],
         cid.axis[2] * cid.ref[0] - cid.axis[0] * cid.ref[2],
         cid.axis[0] * cid.ref[1] - cid.axis[1] * cid.ref[2])
    first = cid.radius * math.cos(parameter)
    second = (cid.radius2 if cid.kind == "ellipse" else cid.radius) * math.sin(parameter)
    return tuple(cid.origin[index] + first * cid.ref[index] + second * y[index] for index in range(3))


def _to_geom(cid: CurveId):
    origin = gp_Pnt(*cid.origin)
    if cid.kind == "line":
        return Geom_Line(origin, gp_Dir(*cid.axis))
    axis = gp_Ax2(origin, gp_Dir(*cid.axis), gp_Dir(*cid.ref))
    if cid.kind == "circle":
        return Geom_Circle(axis, cid.radius)
    return Geom_Ellipse(axis, cid.radius, cid.radius2)


def _edge_interval(cid: CurveId, edge: TopoDS_Shape, first: float, last: float,
                   start: tuple[float, float, float], end: tuple[float, float, float]) -> tuple[float, float]:
    a, b = _param(cid, start), _param(cid, end)
    if cid.kind == "line":
        return min(a, b), max(a, b)
    curve = BRepAdaptor_Curve(edge).Curve()
    value = curve.Value((first + last) / 2.0)
    midpoint = _param(cid, _xyz(value))
    span = (b - a) % TWO_PI
    if span <= 1e-12:
        return a, a + TWO_PI
    return (a, a + span) if (midpoint - a) % TWO_PI <= span + 1e-9 else (b, b + TWO_PI - span)


def _merge(intervals: list[tuple[float, float]], periodic: bool) -> list[tuple[float, float]]:
    values = sorted(intervals)
    result: list[tuple[float, float]] = []
    for start, end in values:
        if result and start <= result[-1][1] + 1e-9:
            result[-1] = result[-1][0], max(result[-1][1], end)
        else:
            result.append((start, end))
    if periodic and len(result) > 1 and result[-1][1] >= result[0][0] + TWO_PI - 1e-9:
        return [(result[0][0], result[0][0] + TWO_PI)]
    return result


def _close(first: float, second: float, cid: CurveId) -> bool:
    if cid.kind == "line":
        return abs(first - second) <= max(1e-6, abs(second) * 1e-9)
    delta = (first - second) % TWO_PI
    return min(delta, TWO_PI - delta) <= 1e-7


def _inside(face: TopoDS_Face, surface, point: tuple[float, float, float], tolerance: float, classifier) -> bool:
    uv = ShapeAnalysis_Surface(surface).ValueOfUV(gp_Pnt(*point), tolerance)
    return classifier.Perform(uv) == TopAbs_IN


def _divides(face: TopoDS_Face, surface, cid: CurveId, parameter: float, tolerance: float, epsilon: float) -> bool:
    point = _point_at(cid, parameter)
    delta = 1e-4
    before = _point_at(cid, parameter - delta)
    after = _point_at(cid, parameter + delta)
    tangent = _norm(tuple(after[index] - before[index] for index in range(3)))
    adaptor = ShapeAnalysis_Surface(surface)
    uv = adaptor.ValueOfUV(gp_Pnt(*point), tolerance)
    properties = GeomLProp_SLProps(surface, uv.X(), uv.Y(), 1, tolerance)
    if not properties.IsNormalDefined():
        return False
    normal = _xyz(properties.Normal())
    side = (normal[1] * tangent[2] - normal[2] * tangent[1],
            normal[2] * tangent[0] - normal[0] * tangent[2],
            normal[0] * tangent[1] - normal[1] * tangent[0])
    side = _norm(side)
    classifier = BRepTopAdaptor_FClass2d(face, tolerance)
    plus = tuple(point[index] + epsilon * side[index] for index in range(3))
    minus = tuple(point[index] - epsilon * side[index] for index in range(3))
    return _inside(face, surface, plus, tolerance, classifier) and _inside(face, surface, minus, tolerance, classifier)


@dataclass(slots=True)
class Seam:
    face_id: str
    cid: CurveId
    t0: float
    t1: float
    length: float
    why: str = ""


@dataclass(slots=True)
class FacePartition:
    shape: TopoDS_Shape
    faces: dict[str, TopoDS_Face]
    records: list[FaceRecord]
    fusion_mode: str
    seams: list[Seam]
    notes: list[str]


def _face_area_centroid(face: TopoDS_Face) -> tuple[float, tuple[float, float, float]]:
    properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, properties)
    center = properties.CentreOfMass()
    return float(properties.Mass()), _xyz(center)


def _surface_kind(face: TopoDS_Face) -> str:
    surface = BRep_Tool.Surface_s(face)
    # STEP writers may wrap the same analytic surface in a rectangular or
    # trimmed surface. Use its basis surface so face ordering survives a
    # snapshot write/read round trip.
    for _ in range(4):
        if not hasattr(surface, "BasisSurface"):
            break
        basis = surface.BasisSurface()
        if basis is surface:
            break
        surface = basis
    name = _name(surface)
    return name.removeprefix("Geom_").lower() or "unknown"


def _face_key(face: TopoDS_Face) -> tuple:
    area, center = _face_area_centroid(face)
    return (_surface_kind(face), round(area, 9), *(round(item, 8) for item in center), _bbox(face))


def _make_records(shape: TopoDS_Shape, source_entities: list[EntityShape] | None = None) -> tuple[dict[str, TopoDS_Face], list[FaceRecord]]:
    raw_faces = [TopoDS.Face_s(item) for item in _subshapes(shape, TopAbs_FACE)]
    raw_faces.sort(key=_face_key)
    faces: dict[str, TopoDS_Face] = {}
    records: list[FaceRecord] = []
    for index, face in enumerate(raw_faces, start=1):
        face_id = f"face_{index:06d}"
        area, center = _face_area_centroid(face)
        sources: list[str] = []
        if source_entities:
            for entity in source_entities:
                if any(
                    face.IsSame(original)
                    or (
                        _surface_kind(original) == _surface_kind(face)
                        and _point_in_bbox(center, _bbox(original), 1e-6)
                    )
                    for original in entity.faces.values()
                ):
                    sources.append(entity.source_body_id)
        faces[face_id] = face
        records.append(FaceRecord(face_id, _surface_kind(face), area, center, _bbox(face), sources))
    return faces, records


def _point_in_bbox(
    point: tuple[float, float, float], bounds: tuple[float, float, float, float, float, float], tolerance: float
) -> bool:
    return all(bounds[index] - tolerance <= point[index] <= bounds[index + 3] + tolerance for index in range(3))


def _face_edges(face: TopoDS_Face, diagonal: float):
    result = []
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        if BRep_Tool.Degenerated_s(edge):
            continue
        adaptor = BRepAdaptor_Curve(edge)
        first, last = adaptor.FirstParameter(), adaptor.LastParameter()
        curve = BRep_Tool.Curve_s(edge, first, last)
        cid = curve_id(curve, diagonal)
        if cid is None:
            continue
        vertices = [TopoDS.Vertex_s(item) for item in _subshapes(edge, TopAbs_VERTEX)]
        if len(vertices) < 2:
            continue
        start = _xyz(BRep_Tool.Pnt_s(vertices[0]))
        end = _xyz(BRep_Tool.Pnt_s(vertices[-1]))
        result.append((cid, edge, first, last, start, end))
    return result


def detect_seams(partition: FacePartition) -> list[Seam]:
    bounds = _bbox(partition.shape)
    diagonal = max(math.sqrt(sum((bounds[index + 3] - bounds[index]) ** 2 for index in range(3))), 1.0)
    tolerance = max(diagonal * 1e-7, 1e-9)
    seams: list[Seam] = []
    for face_id, face in partition.faces.items():
        groups: dict[tuple, tuple[CurveId, list[tuple]]] = {}
        vertices: dict[tuple, list[float]] = {}
        for cid, edge, first, last, start, end in _face_edges(face, diagonal):
            interval = _edge_interval(cid, edge, first, last, start, end)
            groups.setdefault(cid.key, (cid, []))[1].append((interval, edge))
            vertices.setdefault(cid.key, []).extend([_param(cid, start), _param(cid, end)])
        for key, (cid, items) in groups.items():
            if len(items) < 2:
                continue
            cover = _merge([interval for interval, _edge in items], cid.periodic)
            if len(cover) < 2:
                continue
            gaps = [(cover[index][1], cover[index + 1][0]) for index in range(len(cover) - 1)]
            if cid.periodic:
                gaps.append((cover[-1][1], cover[0][0] + TWO_PI))
            for start, end in gaps:
                width = end - start if cid.kind == "line" else (end - start) * (cid.radius if cid.kind == "circle" else max(cid.radius, cid.radius2))
                seam = Seam(face_id, cid, start, end, float(width))
                if width < MIN_SPAN * diagonal:
                    seam.why = "too short"
                elif sum(_close(value, start, cid) or _close(value, end, cid) for value in vertices[key]) < 2:
                    seam.why = "gap endpoints are not face vertices"
                elif not any(_divides(face, BRep_Tool.Surface_s(face), cid, start + (end - start) * fraction, tolerance * 10, max(diagonal * 3e-4, 1e-7)) for fraction in (0.5, 0.25, 0.75)):
                    seam.why = "gap is not a chord inside the face"
                if not seam.why:
                    seams.append(seam)
    return seams


def _fuse_entities(entities: list[EntityShape]) -> tuple[TopoDS_Shape, str, list[str]]:
    notes: list[str] = []
    if not entities:
        raise ValueError("STEP file has no solids")
    result = entities[0].shape
    for entity in entities[1:]:
        try:
            fuse = BRepAlgoAPI_Fuse(result, entity.shape)
            fuse.Build()
            if not fuse.IsDone() or not BRepCheck_Analyzer(fuse.Shape()).IsValid():
                raise ValueError("invalid fuse result")
            result = fuse.Shape()
        except Exception as error:
            notes.append(f"fusion failed at {entity.source_body_id}: {type(error).__name__}")
            compound = TopoDS_Compound()
            builder = BRep_Builder()
            builder.MakeCompound(compound)
            for item in entities:
                builder.Add(compound, item.shape)
            return compound, "compound", notes
    return result, "fused", notes


def create_partition(entities: list[EntityShape]) -> FacePartition:
    shape, fusion_mode, notes = _fuse_entities(entities)
    faces, records = _make_records(shape, entities)
    partition = FacePartition(shape, faces, records, fusion_mode, [], notes)
    partition.seams = detect_seams(partition)
    return imprint_seams(partition)


def _find_face(
    shape: TopoDS_Shape,
    expected: TopoDS_Face,
    record: FaceRecord | None = None,
) -> TopoDS_Face | None:
    current_faces = [TopoDS.Face_s(raw) for raw in _subshapes(shape, TopAbs_FACE)]
    for face in current_faces:
        if face.IsSame(expected):
            return face
    if record is None:
        return None

    bounds = _bbox(shape)
    diagonal = max(
        math.sqrt(sum((bounds[index + 3] - bounds[index]) ** 2 for index in range(3))),
        1.0,
    )
    linear_tolerance = max(diagonal * 1e-8, 1e-9)
    area_tolerance = max(abs(record.area) * 1e-8, diagonal * diagonal * 1e-10, 1e-10)
    matches: list[TopoDS_Face] = []
    for face in current_faces:
        if _surface_kind(face) != record.surface_kind:
            continue
        area, centroid = _face_area_centroid(face)
        face_bounds = _bbox(face)
        if abs(area - record.area) > area_tolerance:
            continue
        if any(
            abs(actual - expected_value) > linear_tolerance
            for actual, expected_value in zip(centroid, record.centroid, strict=True)
        ):
            continue
        if any(
            abs(actual - expected_value) > linear_tolerance
            for actual, expected_value in zip(face_bounds, record.bbox, strict=True)
        ):
            continue
        matches.append(face)
    return matches[0] if len(matches) == 1 else None


def imprint_seams(partition: FacePartition) -> FacePartition:
    shape = partition.shape
    records_by_id = {record.id: record for record in partition.records}
    by_face: dict[str, list[Seam]] = {}
    for seam in partition.seams:
        by_face.setdefault(seam.face_id, []).append(seam)
    for face_id, seams in by_face.items():
        face = _find_face(shape, partition.faces[face_id], records_by_id.get(face_id))
        if face is None:
            partition.notes.append(f"face {face_id} could not be uniquely rematched before imprint")
            continue
        edges = []
        for seam in seams:
            try:
                maker = BRepBuilderAPI_MakeEdge(_to_geom(seam.cid), seam.t0, seam.t1)
                if maker.IsDone():
                    edges.append(maker.Edge())
            except Exception as error:
                partition.notes.append(f"face {face_id} seam skipped: {type(error).__name__}")
        if not edges:
            continue
        try:
            splitter = BRepFeat_SplitShape(shape)
            for edge in edges:
                splitter.Add(TopoDS.Edge_s(edge), face)
            splitter.Build()
            candidate = splitter.Shape()
            if not BRepCheck_Analyzer(candidate).IsValid():
                raise ValueError("invalid BRep")
            shape = candidate
        except Exception as error:
            partition.notes.append(f"face {face_id} imprint skipped: {type(error).__name__}")
    faces, records = _make_records(shape)
    for record in records:
        matching = [old for old in partition.records if old.surface_kind == record.surface_kind and _point_in_bbox(record.centroid, old.bbox, 1e-6)]
        record.source_body_ids = sorted({body_id for old in matching for body_id in old.source_body_ids})
    return FacePartition(shape, faces, records, partition.fusion_mode, partition.seams, partition.notes)


def snapshot_path(step_path: Path, source_sha256: str) -> Path:
    filename = (
        f"{source_sha256[:16]}-{OCP.__version__}-p{PARTITION_CACHE_VERSION}.step"
    )
    return step_path.parent / ".stepseg-cache" / filename


def write_partition_step(shape: TopoDS_Shape, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = STEPControl_Writer()
    if writer.Transfer(shape, STEPControl_AsIs) != IFSelect_RetDone or writer.Write(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot write partition STEP: {path}")


def load_partition_snapshot(path: Path) -> FacePartition:
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot read partition snapshot: {path}")
    for index in range(1, reader.NbRootsForTransfer() + 1):
        reader.TransferRoot(index)
    shape = reader.OneShape()
    faces, records = _make_records(shape)
    return FacePartition(shape, faces, records, "snapshot", [], [])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_or_create_partition(
    step_path: Path, snapshot: Path | None = None, force: bool = False
) -> tuple[FacePartition, Path, str]:
    entities = load_step(step_path)
    source_sha256 = file_sha256(step_path)
    target = snapshot or snapshot_path(step_path, source_sha256)
    if target.exists() and not force:
        partition = load_partition_snapshot(target)
        return partition, target, file_sha256(target)
    partition = create_partition(entities)
    write_partition_step(partition.shape, target)
    return partition, target, file_sha256(target)


def face_document_for(
    step_path: Path, partition: FacePartition, snapshot: Path, source_sha256: str | None = None
) -> FaceAnnotationDocument:
    source_sha256 = source_sha256 or file_sha256(step_path)
    return FaceAnnotationDocument(
        source_path=str(step_path.resolve()),
        source_sha256=source_sha256,
        ocp_version=OCP.__version__,
        fusion_mode=partition.fusion_mode,
        snapshot_path=str(snapshot.resolve().relative_to(step_path.resolve().parent)),
        snapshot_sha256=file_sha256(snapshot),
        faces=partition.records,
    )


def partition_matches_document(partition: FacePartition, document: FaceAnnotationDocument) -> bool:
    if len(partition.records) != len(document.faces):
        return False
    for actual, expected in zip(partition.records, document.faces, strict=True):
        if actual.id != expected.id or actual.surface_kind != expected.surface_kind:
            return False
        if abs(actual.area - expected.area) > max(abs(expected.area) * 1e-7, 1e-9):
            return False
        if any(abs(a - b) > 1e-7 for a, b in zip(actual.centroid, expected.centroid, strict=True)):
            return False
    return True


def resolve_snapshot_path(document: FaceAnnotationDocument) -> Path:
    value = Path(document.snapshot_path)
    stored = value if value.is_absolute() else Path(document.source_path).resolve().parent / value
    expected = snapshot_path(Path(document.source_path), document.source_sha256)
    return stored if stored.name == expected.name else expected
