import json
from pathlib import Path

import pytest
from PyQt5.QtCore import Qt
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.GProp import GProp_GProps
from OCP.Geom import Geom_Circle, Geom_Ellipse, Geom_Line
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

import stepseg.face_partition as face_partition
from stepseg.export import export_faces
from stepseg.app import GROUP_COLORS, color_for_group, update_face_selection
from stepseg.models import FaceAnnotationDocument, FaceGroupRecord, FaceRecord
from stepseg.storage import load_document, save_document, sha256_file
from stepseg.topology import build_entity


def volume(shape) -> float:
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return float(properties.Mass())


def test_analytic_curve_identity_and_bspline_exclusion() -> None:
    line = Geom_Line(gp_Pnt(0, 2, 0), gp_Dir(1, 0, 0))
    circle = Geom_Circle(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 3)
    ellipse = Geom_Ellipse(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 4, 2)
    class BSplineStub:
        class Type:
            def Name(self):
                return "Geom_BSplineCurve"

        def DynamicType(self):
            return self.Type()

    spline = BSplineStub()

    assert face_partition.curve_id(line, 10).kind == "line"
    assert face_partition.curve_id(circle, 10).kind == "circle"
    assert face_partition.curve_id(ellipse, 10).kind == "ellipse"
    assert face_partition.curve_id(spline, 10) is None


def test_fuses_touching_solids_and_records_face_sources() -> None:
    first = build_entity(BRepPrimAPI_MakeBox(2, 2, 2).Shape(), "entity_0001", "solid_0001")
    second = build_entity(
        BRepPrimAPI_MakeBox(gp_Pnt(2, 0, 0), 2, 2, 2).Shape(),
        "entity_0002",
        "solid_0002",
    )
    partition = face_partition.create_partition([first, second])
    assert partition.fusion_mode == "fused"
    assert len(partition.records) == 10
    assert all(record.source_body_ids for record in partition.records)
    assert BRepCheck_Analyzer(partition.shape).IsValid()


def test_fusion_failure_falls_back_to_compound(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenFuse:
        def __init__(self, *_args):
            raise RuntimeError("forced failure")

    monkeypatch.setattr(face_partition, "BRepAlgoAPI_Fuse", BrokenFuse)
    entities = [
        build_entity(BRepPrimAPI_MakeBox(1, 1, 1).Shape(), "entity_0001", "solid_0001"),
        build_entity(
            BRepPrimAPI_MakeBox(gp_Pnt(3, 0, 0), 1, 1, 1).Shape(),
            "entity_0002",
            "solid_0002",
        ),
    ]
    shape, mode, notes = face_partition._fuse_entities(entities)
    assert mode == "compound"
    assert notes
    assert len(face_partition._subshapes(shape, face_partition.TopAbs_FACE)) == 12


def test_imprint_splits_a_face_without_changing_volume() -> None:
    shape = BRepPrimAPI_MakeBox(1, 1, 1).Shape()
    faces, records = face_partition._make_records(shape)
    partition = face_partition.FacePartition(shape, faces, records, "fused", [], [])
    top = next(record for record in records if record.centroid[2] == pytest.approx(1.0))
    curve = face_partition.CurveId("line", ("test",), (0, 0.5, 1), (1, 0, 0))
    partition.seams = [face_partition.Seam(top.id, curve, 0, 1, 1)]
    result = face_partition.imprint_seams(partition)

    assert len(result.records) == 7
    assert volume(result.shape) == pytest.approx(volume(shape), rel=1e-9)
    assert BRepCheck_Analyzer(result.shape).IsValid()


def test_snapshot_document_round_trip_and_export(tmp_path: Path) -> None:
    source = tmp_path / "part.step"
    shape = BRepPrimAPI_MakeBox(1, 2, 3).Shape()
    face_partition.write_partition_step(shape, source)
    partition, snapshot, _ = face_partition.load_or_create_partition(source)
    document = face_partition.face_document_for(source, partition, snapshot)
    document.groups = [
        FaceGroupRecord("group_0001", "all", face_ids=[record.id for record in document.faces])
    ]
    document.status = "completed"
    annotation = tmp_path / "part.stepseg.json"
    save_document(annotation, document)

    restored = load_document(annotation)
    assert restored.schema_version == "3.0"
    assert not Path(restored.snapshot_path).is_absolute()
    assert restored.validate() == []
    output = tmp_path / "export"
    paths = export_faces(restored, partition, output)
    assert paths == [output / "partition.step", output / "manifest.json"]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert {face["group_id"] for face in manifest["faces"]} == {"group_0001"}
    assert sha256_file(face_partition.resolve_snapshot_path(restored)) == restored.snapshot_sha256


def test_face_selection_is_unique_and_supports_add_remove() -> None:
    assert update_face_selection({"face_1"}, {"face_1"}, 0) == {"face_1"}
    assert update_face_selection({"face_1"}, {"face_2"}, 0) == {"face_1", "face_2"}
    assert update_face_selection({"face_1", "face_2"}, {"face_1"}, int(Qt.ControlModifier)) == {"face_2"}
    assert update_face_selection({"face_1", "face_2"}, set(), 0) == {"face_1", "face_2"}
    assert update_face_selection({"face_1", "face_2"}, {"face_2"}, int(Qt.ControlModifier)) == {"face_1"}


def test_unclassified_groups_get_distinct_stable_colors() -> None:
    faces = [FaceRecord("face_1", "plane", 1, (0, 0, 0), (0, 0, 0, 1, 1, 1))]
    document = FaceAnnotationDocument("x", "y", "z", "fused", "snapshot", "hash", faces)
    first = FaceGroupRecord("group_1", "first")
    second = FaceGroupRecord("group_2", "second")
    document.groups = [first, second]
    assert color_for_group(document, first) == GROUP_COLORS[0]
    assert color_for_group(document, second) == GROUP_COLORS[1]
