"""STEP import, deterministic B-Rep face indexing, and rule candidates."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import OCP
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Plane
from OCP.IFSelect import IFSelect_RetDone
from OCP.gp import gp_Vec
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Face, TopoDS_Shape
from OCP.TopExp import TopExp_Explorer

from .models import AnnotationDocument, BodyRecord
from .storage import sha256_file


@dataclass(slots=True)
class ImportedBody:
    id: str
    name: str
    shape: TopoDS_Shape
    faces: dict[str, TopoDS_Face]
    surface_types: dict[str, int]
    adjacency: dict[str, set[str]]
    shared_edges: dict[frozenset[str], list[TopoDS_Edge]]

    @property
    def face_ids(self) -> list[str]:
        return list(self.faces)

    def face_id_for(self, picked: TopoDS_Shape) -> str | None:
        for face_id, face in self.faces.items():
            if face.IsSame(picked):
                return face_id
        return None

    def candidate(self, kind: str, seed_id: str) -> set[str]:
        if seed_id not in self.faces:
            return set()
        if kind == "seed":
            return {seed_id}
        if kind == "connected":
            def allowed(_current: str, _neighbor: str) -> bool:
                return True
        elif kind == "same_surface":
            def allowed(current: str, neighbor: str) -> bool:
                return self._is_same_support(current, neighbor)
        elif kind == "tangent":
            allowed = self._is_tangent_pair
        else:
            raise ValueError(f"unknown candidate type: {kind}")
        selected = {seed_id}
        queue = deque([seed_id])
        while queue:
            current = queue.popleft()
            for neighbor in self.adjacency[current]:
                if neighbor not in selected and allowed(current, neighbor):
                    selected.add(neighbor)
                    queue.append(neighbor)
        return selected

    def _is_tangent_pair(self, first_id: str, second_id: str) -> bool:
        for edge in self.shared_edges.get(frozenset((first_id, second_id)), []):
            try:
                continuity = BRep_Tool.Continuity_s(edge, self.faces[first_id], self.faces[second_id])
                return int(continuity) >= 1  # GeomAbs_G1
            except Exception:
                return False
        return False

    def _is_same_support(self, first_id: str, second_id: str) -> bool:
        """Safely join directly adjacent faces on the same plane.

        STEP exporters often split a planar face. Other analytic surface comparisons
        need more kernel-specific tolerances, so v1 intentionally leaves them to the
        tangent candidate instead of merging unrelated cylinders or cones.
        """
        if self.surface_types[first_id] != self.surface_types[second_id]:
            return False
        first = BRepAdaptor_Surface(self.faces[first_id], True)
        second = BRepAdaptor_Surface(self.faces[second_id], True)
        if first.GetType() != GeomAbs_Plane:
            return False
        first_plane = first.Plane()
        second_plane = second.Plane()
        first_normal = first_plane.Axis().Direction()
        second_normal = second_plane.Axis().Direction()
        parallel = abs(abs(first_normal.Dot(second_normal)) - 1.0) < 1e-8
        offset_vector = gp_Vec(first_plane.Location(), second_plane.Location())
        offset = abs(
            offset_vector.X() * first_normal.X()
            + offset_vector.Y() * first_normal.Y()
            + offset_vector.Z() * first_normal.Z()
        )
        return parallel and offset < 1e-6


def _subshapes(shape: TopoDS_Shape, shape_type: int) -> list[TopoDS_Shape]:
    explorer = TopExp_Explorer(shape, shape_type)
    values: list[TopoDS_Shape] = []
    while explorer.More():
        values.append(explorer.Current())
        explorer.Next()
    return values


def _add_edge(edge_groups: list[tuple[TopoDS_Edge, list[str]]], edge: TopoDS_Edge, face_id: str) -> None:
    for previous, face_ids in edge_groups:
        if previous.IsSame(edge):
            face_ids.append(face_id)
            return
    edge_groups.append((edge, [face_id]))


def _build_body(shape: TopoDS_Shape, index: int) -> ImportedBody:
    body_id = f"solid_{index:04d}"
    faces: dict[str, TopoDS_Face] = {}
    surface_types: dict[str, int] = {}
    edge_groups: list[tuple[TopoDS_Edge, list[str]]] = []
    for face_index, raw_face in enumerate(_subshapes(shape, TopAbs_FACE), start=1):
        face = TopoDS.Face_s(raw_face)
        face_id = f"{body_id}/face_{face_index:05d}"
        faces[face_id] = face
        surface_types[face_id] = int(BRepAdaptor_Surface(face, True).GetType())
        for raw_edge in _subshapes(face, TopAbs_EDGE):
            _add_edge(edge_groups, TopoDS.Edge_s(raw_edge), face_id)
    adjacency = {face_id: set() for face_id in faces}
    shared_edges: dict[frozenset[str], list[TopoDS_Edge]] = {}
    for edge, face_ids in edge_groups:
        unique_ids = list(dict.fromkeys(face_ids))
        for first_index, first_id in enumerate(unique_ids):
            for second_id in unique_ids[first_index + 1 :]:
                adjacency[first_id].add(second_id)
                adjacency[second_id].add(first_id)
                shared_edges.setdefault(frozenset((first_id, second_id)), []).append(edge)
    return ImportedBody(body_id, body_id, shape, faces, surface_types, adjacency, shared_edges)


def load_step(path: Path) -> list[ImportedBody]:
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
    return [_build_body(shape, index) for index, shape in enumerate(solids, start=1)]


def new_document(step_path: Path, bodies: list[ImportedBody]) -> AnnotationDocument:
    return AnnotationDocument(
        source_path=str(step_path.resolve()),
        source_sha256=sha256_file(step_path),
        ocp_version=OCP.__version__,
        bodies=[BodyRecord(body.id, body.name, body.face_ids) for body in bodies],
    )
