from pathlib import Path

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

from stepseg.export import export_solids
from stepseg.models import PlaneSpec
from stepseg.storage import load_document, save_document
from stepseg.topology import (
    apply_split,
    build_entity,
    geometry_signature,
    load_step,
    new_document,
    planar_split_candidates,
    replay_document,
    split_shape,
)


def fuse_all(shapes):
    result = shapes[0]
    for shape in shapes[1:]:
        fuse = BRepAlgoAPI_Fuse(result, shape)
        fuse.Build()
        assert fuse.IsDone()
        result = fuse.Shape()
    return result


def two_stage_cylinder():
    base = BRepPrimAPI_MakeCylinder(10, 5).Shape()
    top_axis = gp_Ax2(gp_Pnt(0, 0, 5), gp_Dir(0, 0, 1))
    top = BRepPrimAPI_MakeCylinder(top_axis, 5, 8).Shape()
    return fuse_all([base, top])


def five_entity_model():
    shapes = [
        BRepPrimAPI_MakeBox(30, 20, 5).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(5, 4, 5), 20, 12, 10).Shape(),
    ]
    for x in (9, 15, 21):
        axis = gp_Ax2(gp_Pnt(x, 10, 15), gp_Dir(0, 0, 1))
        shapes.append(BRepPrimAPI_MakeCylinder(axis, 2, 8).Shape())
    return fuse_all(shapes)


def write_step(shape, path: Path) -> None:
    writer = STEPControl_Writer()
    assert writer.Transfer(shape, STEPControl_AsIs) == IFSelect_RetDone
    assert writer.Write(str(path)) == IFSelect_RetDone


def test_two_stage_cylinder_has_planar_split_candidate() -> None:
    entity = build_entity(two_stage_cylinder(), "entity_0001", "solid_0001")
    candidates = [
        candidate
        for face_id in entity.faces
        for candidate in planar_split_candidates(entity, face_id)
    ]
    matching = [candidate for candidate in candidates if candidate.plane.offset == pytest.approx(5.0)]
    assert matching
    assert len(matching[0].result_shapes) == 2
    assert sum(item.volume for item in matching[0].result_signatures) == pytest.approx(
        geometry_signature(entity.shape).volume, rel=1e-7
    )


def test_plain_box_has_no_internal_plane_candidate() -> None:
    entity = build_entity(BRepPrimAPI_MakeBox(10, 10, 10).Shape(), "entity_0001", "solid_0001")
    assert all(not planar_split_candidates(entity, face_id) for face_id in entity.faces)


def test_split_plane_is_positioned_near_far_from_origin_model() -> None:
    axis = gp_Ax2(gp_Pnt(1_000_000, 1_000_000, 0), gp_Dir(0, 0, 1))
    base = BRepPrimAPI_MakeCylinder(axis, 10, 5).Shape()
    upper_axis = gp_Ax2(gp_Pnt(1_000_000, 1_000_000, 5), gp_Dir(0, 0, 1))
    upper = BRepPrimAPI_MakeCylinder(upper_axis, 5, 8).Shape()
    parts, _ = split_shape(fuse_all([base, upper]), PlaneSpec((0.0, 0.0, 1.0), 5.0))
    assert len(parts) == 2


def test_geometry_signature_excludes_shape_tolerance() -> None:
    shape = BRepPrimAPI_MakeBox(10, 20, 30).Shape()
    assert geometry_signature(shape).bbox == pytest.approx(
        (0, 0, 0, 10, 20, 30), rel=0, abs=1e-12
    )


def test_two_splits_create_five_independent_entities(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.step"
    source.write_bytes(b"synthetic")
    initial = build_entity(five_entity_model(), "entity_0001", "solid_0001")
    document = new_document(source, [initial])
    entities = apply_split(document, [initial], initial.id, PlaneSpec((0.0, 0.0, 1.0), 15.0))
    assert len(entities) == 4
    lower = max(entities, key=lambda item: geometry_signature(item.shape).volume)
    entities = apply_split(document, entities, lower.id, PlaneSpec((0.0, 0.0, 1.0), 5.0))
    assert len(entities) == 5
    assert len({item.id for item in entities}) == 5
    assert document.validate() == []
    volumes = sorted(geometry_signature(item.shape).volume for item in entities)
    assert volumes[0] == pytest.approx(volumes[1], rel=1e-7)
    assert volumes[1] == pytest.approx(volumes[2], rel=1e-7)


def test_save_replay_and_export_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "two_stage.step"
    write_step(two_stage_cylinder(), source)
    entities = load_step(source)
    document = new_document(source, entities)
    entities = apply_split(
        document, entities, entities[0].id, PlaneSpec((0.0, 0.0, 1.0), 5.0)
    )
    assert all(any(item.source_face_id for item in entity.face_sources.values()) for entity in entities)
    assert all(
        any(item.generated_by_split_id == "split_0001" for item in entity.face_sources.values())
        for entity in entities
    )
    annotation = tmp_path / "two_stage.stepseg.json"
    save_document(annotation, document)
    restored = load_document(annotation)
    replayed = replay_document(source, restored)
    assert [item.id for item in replayed] == [item.id for item in entities]
    output = tmp_path / "export"
    paths = export_solids(restored, replayed, output)
    assert output / "combined.step" in paths
    assert (output / "manifest.json").exists()
    assert len(load_step(output / "combined.step")) == 2
    for record in restored.entities:
        assert len(load_step(output / "entities" / f"{record.id}.step")) == 1


def test_replay_rejects_changed_geometry_signature(tmp_path: Path) -> None:
    source = tmp_path / "two_stage.step"
    write_step(two_stage_cylinder(), source)
    entities = load_step(source)
    document = new_document(source, entities)
    apply_split(document, entities, entities[0].id, PlaneSpec((0.0, 0.0, 1.0), 5.0))
    signature = document.split_operations[0].result_signatures[0]
    signature.centroid = (signature.centroid[0] + 1.0, *signature.centroid[1:])
    with pytest.raises(ValueError, match="geometry signature changed"):
        replay_document(source, document)


def test_replay_accepts_legacy_bbox_tolerance_padding(tmp_path: Path) -> None:
    source = tmp_path / "two_stage.step"
    write_step(two_stage_cylinder(), source)
    entities = load_step(source)
    document = new_document(source, entities)
    apply_split(document, entities, entities[0].id, PlaneSpec((0.0, 0.0, 1.0), 5.0))
    signature = document.split_operations[0].result_signatures[0]
    signature.bbox = tuple(
        value + (-0.01 if index < 3 else 0.01)
        for index, value in enumerate(signature.bbox)
    )
    assert len(replay_document(source, document)) == 2


def test_replay_rejects_nonuniform_bbox_change(tmp_path: Path) -> None:
    source = tmp_path / "two_stage.step"
    write_step(two_stage_cylinder(), source)
    entities = load_step(source)
    document = new_document(source, entities)
    apply_split(document, entities, entities[0].id, PlaneSpec((0.0, 0.0, 1.0), 5.0))
    signature = document.split_operations[0].result_signatures[0]
    signature.bbox = (*signature.bbox[:3], signature.bbox[3] + 1.0, *signature.bbox[4:])
    with pytest.raises(ValueError, match="geometry signature changed"):
        replay_document(source, document)
