"""Native GUI smoke runner executed in an isolated macOS subprocess."""

from __future__ import annotations

import tempfile
from pathlib import Path

from stepseg.launcher import _prepare_macos_qt_platform_plugin


def main() -> None:
    plugin_roots = _prepare_macos_qt_platform_plugin()

    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from PyQt5.QtCore import QCoreApplication, QTimer
    from PyQt5.QtWidgets import QApplication

    if plugin_roots is not None:
        QCoreApplication.setLibraryPaths([str(path) for path in plugin_roots])

    from stepseg.app import MainWindow
    from stepseg.topology import build_entity, new_document, planar_split_candidates

    base = BRepPrimAPI_MakeCylinder(10, 5).Shape()
    upper = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 5), gp_Dir(0, 0, 1)), 5, 8
    ).Shape()
    fuse = BRepAlgoAPI_Fuse(base, upper)
    fuse.Build()
    assert fuse.IsDone()
    entity = build_entity(fuse.Shape(), "entity_0001", "solid_0001")
    seed_face_id = next(
        face_id
        for face_id in entity.faces
        if planar_split_candidates(entity, face_id)
    )

    with tempfile.NamedTemporaryFile(suffix=".step") as source:
        source.write(b"gui smoke source")
        source.flush()
        source_path = Path(source.name)

        app = QApplication([])
        window = MainWindow()
        assert not window.menuBar().isNativeMenuBar()
        assert [action.text() for action in window.menuBar().actions()] == [
            "文件",
            "编辑",
            "标注",
            "视图",
        ]
        window.step_path = source_path
        window.entities = [entity]
        window.document = new_document(source_path, window.entities)
        window.active_entity_id = entity.id
        window.save = lambda *args, **kwargs: None
        window._refresh_ui()
        window._face_picked(entity.id, seed_face_id)

        assert [item.id for item in window.entities] == ["entity_0001"]
        window.confirm_split()
        assert len(window.entities) == 2
        assert [item.id for item in window.entities] == [
            item.id for item in window.document.entities
        ]

        window.show()
        QTimer.singleShot(1000, app.quit)
        assert app.exec_() == 0


if __name__ == "__main__":
    main()
