from pathlib import Path

import pytest

from stepseg.models import AnnotationDocument, EntityRecord, GeometrySignature
from stepseg.storage import load_document, save_document


def signature(volume: float = 1.0) -> GeometrySignature:
    return GeometrySignature(volume, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0, 1.0, 1.0))


def document() -> AnnotationDocument:
    entity = EntityRecord("entity_0001", "solid_0001", signature(), name="entity_0001")
    return AnnotationDocument("sample.step", "abc", "test", [entity], [entity])


def test_new_document_is_valid() -> None:
    assert document().validate() == []


def test_unknown_optional_class_is_invalid() -> None:
    value = document()
    value.entities[0].class_id = 999
    assert any("unknown class" in error for error in value.validate())


def test_save_and_reload_v2_document(tmp_path: Path) -> None:
    annotation = tmp_path / "sample.stepseg.json"
    save_document(annotation, document())
    restored = load_document(annotation)
    assert restored.schema_version == "2.0"
    assert restored.entities[0].id == "entity_0001"


def test_v1_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported annotation schema"):
        AnnotationDocument.from_dict({"schema_version": "1.0"})
