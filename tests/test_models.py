from pathlib import Path

import pytest

from stepseg.export import export_aagnet
from stepseg.models import AnnotationDocument, BodyRecord, FeatureInstance
from stepseg.storage import load_document, save_document


def document() -> AnnotationDocument:
    return AnnotationDocument(
        source_path="sample.step",
        source_sha256="abc",
        ocp_version="test",
        bodies=[BodyRecord("solid_0001", "solid_0001", ["solid_0001/face_00001", "solid_0001/face_00002"])],
        instances=[
            FeatureInstance("background_solid_0001", 0, "solid_0001", ["solid_0001/face_00002"]),
            FeatureInstance(
                "feature_0001",
                5,
                "solid_0001",
                ["solid_0001/face_00001"],
                ["solid_0001/face_00001"],
            ),
        ],
    )


def test_complete_document_is_valid() -> None:
    assert document().validate(require_complete=True) == []


def test_duplicate_face_is_invalid() -> None:
    value = document()
    value.instances[0].face_ids.append("solid_0001/face_00001")
    assert any("assigned by both" in error for error in value.validate())


def test_bottom_face_must_be_in_instance() -> None:
    value = document()
    value.instances[1].bottom_face_ids = ["solid_0001/face_00002"]
    assert any("bottom faces" in error for error in value.validate())


def test_save_reload_and_export(tmp_path: Path) -> None:
    value = document()
    annotation = tmp_path / "sample.stepanno.json"
    save_document(annotation, value)
    restored = load_document(annotation)
    output = export_aagnet(restored, tmp_path / "export")
    assert len(output) == 1
    assert (tmp_path / "export" / "source_map.json").exists()


def test_export_requires_complete_document(tmp_path: Path) -> None:
    value = document()
    value.instances.pop()
    with pytest.raises(ValueError, match="unassigned"):
        export_aagnet(value, tmp_path)
