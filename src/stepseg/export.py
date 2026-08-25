"""AAGNet-compatible structural label export."""

from __future__ import annotations

import json
from pathlib import Path

from .models import AnnotationDocument


def export_aagnet(document: AnnotationDocument, output_dir: Path, strict_mf25: bool = False) -> list[Path]:
    errors = document.validate(require_complete=True)
    if errors:
        raise ValueError("cannot export invalid annotation: " + "; ".join(errors))
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    mapping = {item.id: item.aagnet_class_id for item in document.taxonomy}
    if strict_mf25:
        unmapped = [item.key for item in document.taxonomy if item.aagnet_class_id is None]
        if unmapped:
            raise ValueError("missing MFInstSeg mappings: " + ", ".join(unmapped))
    for body in document.bodies:
        face_index = {face_id: index for index, face_id in enumerate(body.face_ids)}
        count = len(body.face_ids)
        semantic = {str(index): 0 for index in range(count)}
        bottom = {str(index): 0 for index in range(count)}
        instance = [[int(row == column) for column in range(count)] for row in range(count)]
        for item in document.instances:
            if item.body_id != body.id:
                continue
            class_id = mapping[item.class_id] if strict_mf25 else item.class_id
            indexes = [face_index[face_id] for face_id in item.face_ids]
            for index in indexes:
                semantic[str(index)] = int(class_id or 0)
            for row in indexes:
                for column in indexes:
                    instance[row][column] = 1
            for face_id in item.bottom_face_ids:
                bottom[str(face_index[face_id])] = 1
        payload = [[body.id, {"seg": semantic, "inst": instance, "bottom": bottom}]]
        path = output_dir / f"{body.id}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        paths.append(path)
    (output_dir / "source_map.json").write_text(
        json.dumps(
            {
                "source_path": document.source_path,
                "source_sha256": document.source_sha256,
                "bodies": {body.id: body.face_ids for body in document.bodies},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return paths
