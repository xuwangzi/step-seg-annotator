"""Annotation persistence and source integrity checks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .models import AnnotationDocument, now_iso


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_document(path: Path, document: AnnotationDocument) -> None:
    document.updated_at = now_iso()
    target = path.resolve()
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json.dumps(document.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)


def load_document(path: Path) -> AnnotationDocument:
    return AnnotationDocument.from_dict(json.loads(path.read_text(encoding="utf-8")))


def source_matches(step_path: Path, document: AnnotationDocument) -> bool:
    return sha256_file(step_path) == document.source_sha256
