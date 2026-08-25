"""Desktop annotation application."""
# ruff: noqa: E402

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _configure_qt_environment() -> None:
    """Set the PySide6 platform-plugin path before Qt loads its application layer."""
    spec = importlib.util.find_spec("PySide6")
    if spec and spec.submodule_search_locations:
        package_root = Path(next(iter(spec.submodule_search_locations)))
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(package_root / "Qt/plugins/platforms"))


_configure_qt_environment()

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .export import export_aagnet
from .models import AnnotationDocument, FeatureInstance, TaxonomyClass, annotation_path_for
from .storage import load_document, save_document, source_matches
from .topology import ImportedBody, load_step, new_document
from .viewer import OccViewport


CANDIDATE_LABELS = {
    "seed": "仅种子面",
    "same_surface": "同类曲面连通区",
    "tangent": "相切光滑区",
    "connected": "全部连通区",
}


def configure_qt_plugins() -> None:
    """Make PySide6 wheels find their bundled Cocoa/Windows/Linux platform plugin."""
    _configure_qt_environment()


class TaxonomyDialog(QDialog):
    """Small editor that keeps taxonomy IDs stable while permitting lab-specific labels."""

    def __init__(self, taxonomy: list[TaxonomyClass], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("标签体系")
        self.resize(700, 420)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["ID", "key", "中文名", "颜色", "AAGNet ID", "启用"])
        for item in taxonomy:
            self._add_row(item)
        layout.addWidget(self.table)
        add_button = QPushButton("新增类别")
        add_button.clicked.connect(self._add_new)
        layout.addWidget(add_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, item: TaxonomyClass) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [str(item.id), item.key, item.name_zh, item.color, "" if item.aagnet_class_id is None else str(item.aagnet_class_id)]
        for column, value in enumerate(values):
            cell = QTableWidgetItem(value)
            if column == 0:
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, column, cell)
        enabled = QTableWidgetItem()
        enabled.setCheckState(Qt.Checked if item.enabled else Qt.Unchecked)
        self.table.setItem(row, 5, enabled)

    def _add_new(self) -> None:
        ids = [int(self.table.item(row, 0).text()) for row in range(self.table.rowCount())]
        next_id = max(ids, default=0) + 1
        self._add_row(TaxonomyClass(next_id, f"class_{next_id}", f"类别 {next_id}", "#64748B"))

    def values(self) -> list[TaxonomyClass]:
        values: list[TaxonomyClass] = []
        keys: set[str] = set()
        for row in range(self.table.rowCount()):
            class_id = int(self.table.item(row, 0).text())
            key = self.table.item(row, 1).text().strip()
            name = self.table.item(row, 2).text().strip()
            color = self.table.item(row, 3).text().strip()
            raw_mapping = self.table.item(row, 4).text().strip()
            if not key or not name or not color.startswith("#") or len(color) != 7:
                raise ValueError("每个类别需要 key、中文名和 #RRGGBB 颜色")
            if key in keys:
                raise ValueError(f"重复 key：{key}")
            keys.add(key)
            values.append(
                TaxonomyClass(
                    class_id,
                    key,
                    name,
                    color,
                    int(raw_mapping) if raw_mapping else None,
                    self.table.item(row, 5).checkState() == Qt.Checked,
                )
            )
        if not any(item.id == 0 for item in values):
            raise ValueError("必须保留 background 类别（ID 0）")
        return values


class MainWindow(QMainWindow):
    def __init__(self, initial_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("STEP-Seg Annotator")
        self.resize(1500, 900)
        self.step_path: Path | None = None
        self.bodies: list[ImportedBody] = []
        self.document: AnnotationDocument | None = None
        self.active_body_id = ""
        self.working_faces: set[str] = set()
        self.viewport: OccViewport
        self._build_ui()
        if initial_path:
            self.open_step(initial_path)

    def _build_ui(self) -> None:
        toolbar = QToolBar("视图")
        self.addToolBar(toolbar)
        open_button = QPushButton("打开 STEP")
        open_button.clicked.connect(self._choose_step)
        toolbar.addWidget(open_button)
        for title, direction in [("等轴", (1, -1, 1)), ("顶", (0, 0, 1)), ("前", (0, 1, 0))]:
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, item=direction: self.viewport.set_view(item))
            toolbar.addWidget(button)
        fit_button = QPushButton("适配")
        fit_button.clicked.connect(lambda: self.viewport.fit_all())
        toolbar.addWidget(fit_button)
        wire_button = QPushButton("线框")
        wire_button.setCheckable(True)
        wire_button.toggled.connect(lambda enabled: self.viewport.toggle_wireframe(enabled))
        toolbar.addWidget(wire_button)
        save_button = QPushButton("保存")
        save_button.clicked.connect(self.save)
        toolbar.addWidget(save_button)
        export_button = QPushButton("导出 AAGNet")
        export_button.clicked.connect(self.export)
        toolbar.addWidget(export_button)
        taxonomy_button = QPushButton("标签体系")
        taxonomy_button.clicked.connect(self.edit_taxonomy)
        toolbar.addWidget(taxonomy_button)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._make_left_panel())
        self.viewport = OccViewport(splitter)
        self.viewport.face_picked.connect(self._face_picked)
        splitter.addWidget(self.viewport)
        splitter.addWidget(self._make_right_panel())
        splitter.setSizes([260, 850, 360])
        self.setCentralWidget(splitter)
        self.status_label = QLabel("打开 STEP 文件开始标注")
        self.statusBar().addWidget(self.status_label)

    def _make_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("模型 / Solid"))
        self.body_tree = QTreeWidget()
        self.body_tree.setHeaderLabels(["名称", "面数"])
        self.body_tree.itemClicked.connect(self._body_selected)
        layout.addWidget(self.body_tree)
        layout.addWidget(QLabel("标注实例"))
        self.instance_tree = QTreeWidget()
        self.instance_tree.setHeaderLabels(["实例", "类别", "面数"])
        self.instance_tree.itemClicked.connect(self._instance_selected)
        layout.addWidget(self.instance_tree)
        return panel

    def _make_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("当前选择"))
        self.selection_label = QLabel("未选择面")
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)
        self.candidate_combo = QComboBox()
        self.candidate_combo.addItems([CANDIDATE_LABELS[key] for key in CANDIDATE_LABELS])
        self.candidate_combo.currentIndexChanged.connect(self._refresh_candidate)
        form = QFormLayout()
        form.addRow("规则候选", self.candidate_combo)
        self.class_combo = QComboBox()
        form.addRow("类别", self.class_combo)
        layout.addLayout(form)
        row = QHBoxLayout()
        clear_button = QPushButton("清空选择")
        clear_button.clicked.connect(self._clear_selection)
        row.addWidget(clear_button)
        background_button = QPushButton("归为背景")
        background_button.clicked.connect(self._mark_background)
        row.addWidget(background_button)
        layout.addLayout(row)
        new_button = QPushButton("新建实例并赋值")
        new_button.clicked.connect(self._create_instance)
        layout.addWidget(new_button)
        update_button = QPushButton("更新已选实例")
        update_button.clicked.connect(self._update_instance)
        layout.addWidget(update_button)
        bottom_button = QPushButton("切换底面标记")
        bottom_button.clicked.connect(self._toggle_bottom)
        layout.addWidget(bottom_button)
        delete_button = QPushButton("删除已选实例")
        delete_button.clicked.connect(self._delete_instance)
        layout.addWidget(delete_button)
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        layout.addWidget(divider)
        self.annotator_edit = QLineEdit()
        self.reviewer_edit = QLineEdit()
        self.status_combo = QComboBox()
        self.status_combo.addItems(["draft", "completed", "reviewed"])
        self.status_combo.currentTextChanged.connect(self._status_changed)
        form = QFormLayout()
        form.addRow("标注者", self.annotator_edit)
        form.addRow("复核者", self.reviewer_edit)
        form.addRow("状态", self.status_combo)
        layout.addLayout(form)
        layout.addStretch()
        return panel

    def _choose_step(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "选择 STEP 文件", "", "STEP (*.step *.stp)")
        if filename:
            self.open_step(Path(filename))

    def open_step(self, path: Path) -> None:
        try:
            bodies = load_step(path)
        except Exception as error:
            QMessageBox.critical(self, "导入失败", str(error))
            return
        self.step_path = path
        self.bodies = bodies
        candidate_path = annotation_path_for(path)
        if candidate_path.exists():
            document = load_document(candidate_path)
            if not source_matches(path, document):
                QMessageBox.warning(self, "源文件已变化", "已有标注的源文件哈希不匹配，将创建新标注。")
                document = new_document(path, bodies)
        else:
            document = new_document(path, bodies)
            self._add_background_instances(document)
        self.document = document
        self.active_body_id = bodies[0].id
        self.annotator_edit.setText(document.annotator)
        self.reviewer_edit.setText(document.reviewer)
        self.status_combo.setCurrentText(document.status)
        self.viewport.display_bodies(bodies)
        self._refresh_panels()
        self._redraw_colors()
        self.status_label.setText(f"已载入 {path.name}：{len(bodies)} 个 solid")

    @staticmethod
    def _add_background_instances(document: AnnotationDocument) -> None:
        for body in document.bodies:
            document.instances.append(
                FeatureInstance(f"background_{body.id}", 0, body.id, list(body.face_ids))
            )

    def _refresh_panels(self) -> None:
        if not self.document:
            return
        self.body_tree.clear()
        for body in self.document.bodies:
            item = QTreeWidgetItem([body.name, str(len(body.face_ids))])
            item.setData(0, Qt.UserRole, body.id)
            self.body_tree.addTopLevelItem(item)
        self.instance_tree.clear()
        for instance in self.document.instances:
            category = self.document.class_by_id(instance.class_id)
            item = QTreeWidgetItem([instance.id, category.name_zh, str(len(instance.face_ids))])
            item.setData(0, Qt.UserRole, instance.id)
            self.instance_tree.addTopLevelItem(item)
        self.class_combo.blockSignals(True)
        current_id = self.class_combo.currentData()
        self.class_combo.clear()
        for item in self.document.taxonomy:
            if item.enabled:
                self.class_combo.addItem(item.name_zh, item.id)
        if current_id is not None:
            index = self.class_combo.findData(current_id)
            if index >= 0:
                self.class_combo.setCurrentIndex(index)
        self.class_combo.blockSignals(False)
        self._update_selection_label()

    def _body_selected(self, item: QTreeWidgetItem) -> None:
        self.active_body_id = item.data(0, Qt.UserRole)
        self._clear_selection()

    def _instance_selected(self, item: QTreeWidgetItem) -> None:
        instance = self._selected_instance()
        if instance:
            self.active_body_id = instance.body_id
            self.working_faces = set(instance.face_ids)
            self._redraw_colors()
            self._update_selection_label()

    def _face_picked(self, body_id: str, face_id: str) -> None:
        if body_id != self.active_body_id:
            self.active_body_id = body_id
            self.working_faces.clear()
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            self.working_faces.discard(face_id)
        elif modifiers & Qt.ShiftModifier:
            self.working_faces.add(face_id)
        else:
            self._set_candidate(face_id)
        self._redraw_colors()
        self._update_selection_label()

    def _candidate_key(self) -> str:
        return list(CANDIDATE_LABELS)[self.candidate_combo.currentIndex()]

    def _active_body(self) -> ImportedBody | None:
        return next((body for body in self.bodies if body.id == self.active_body_id), None)

    def _set_candidate(self, seed_face_id: str) -> None:
        body = self._active_body()
        if body:
            self.working_faces = body.candidate(self._candidate_key(), seed_face_id)

    def _refresh_candidate(self) -> None:
        if self.working_faces:
            self._set_candidate(next(iter(self.working_faces)))
            self._redraw_colors()
            self._update_selection_label()

    def _clear_selection(self) -> None:
        self.working_faces.clear()
        self._redraw_colors()
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        self.selection_label.setText(f"{len(self.working_faces)} 个面，solid：{self.active_body_id or '-'}")

    def _selected_instance(self) -> FeatureInstance | None:
        if not self.document:
            return None
        item = self.instance_tree.currentItem()
        if not item:
            return None
        instance_id = item.data(0, Qt.UserRole)
        return next((entry for entry in self.document.instances if entry.id == instance_id), None)

    def _remove_faces_from_other_instances(
        self, face_ids: set[str], body_id: str, preserve_id: str | None = None
    ) -> None:
        assert self.document
        survivors: list[FeatureInstance] = []
        for instance in self.document.instances:
            if instance.id == preserve_id:
                survivors.append(instance)
                continue
            if instance.body_id != body_id:
                survivors.append(instance)
                continue
            instance.face_ids = [face_id for face_id in instance.face_ids if face_id not in face_ids]
            instance.bottom_face_ids = [face_id for face_id in instance.bottom_face_ids if face_id in instance.face_ids]
            if instance.face_ids:
                survivors.append(instance)
        self.document.instances = survivors

    def _create_instance(self) -> None:
        if not self.document or not self.working_faces:
            return
        self._remove_faces_from_other_instances(self.working_faces, self.active_body_id)
        numbers = [
            int(item.id.removeprefix("feature_"))
            for item in self.document.instances
            if item.id.startswith("feature_") and item.id.removeprefix("feature_").isdigit()
        ]
        count = max(numbers, default=0) + 1
        instance = FeatureInstance(
            id=f"feature_{count:04d}",
            class_id=int(self.class_combo.currentData()),
            body_id=self.active_body_id,
            face_ids=sorted(self.working_faces),
        )
        self.document.instances.append(instance)
        self._changed()

    def _update_instance(self) -> None:
        instance = self._selected_instance()
        if not instance or not self.working_faces or instance.body_id != self.active_body_id:
            return
        self._remove_faces_from_other_instances(
            self.working_faces, self.active_body_id, preserve_id=instance.id
        )
        instance.face_ids = sorted(self.working_faces)
        instance.bottom_face_ids = [face_id for face_id in instance.bottom_face_ids if face_id in self.working_faces]
        self._changed()

    def _mark_background(self) -> None:
        if not self.document or not self.working_faces:
            return
        self._remove_faces_from_other_instances(self.working_faces, self.active_body_id)
        background = next(
            (item for item in self.document.instances if item.id == f"background_{self.active_body_id}"),
            None,
        )
        if background is None:
            background = FeatureInstance(f"background_{self.active_body_id}", 0, self.active_body_id, [])
            self.document.instances.append(background)
        background.face_ids = sorted(set(background.face_ids) | self.working_faces)
        self._changed()

    def _toggle_bottom(self) -> None:
        instance = self._selected_instance()
        if not instance or instance.body_id != self.active_body_id:
            return
        selected = set(instance.bottom_face_ids)
        for face_id in self.working_faces & set(instance.face_ids):
            if face_id in selected:
                selected.remove(face_id)
            else:
                selected.add(face_id)
        instance.bottom_face_ids = sorted(selected)
        self._changed()

    def _delete_instance(self) -> None:
        instance = self._selected_instance()
        if not self.document or not instance or instance.class_id == 0:
            return
        faces = set(instance.face_ids)
        self.document.instances.remove(instance)
        self.working_faces = faces
        self._mark_background()

    def _status_changed(self, status: str) -> None:
        if not self.document:
            return
        if status in {"completed", "reviewed"}:
            errors = self.document.validate(require_complete=True)
            if errors:
                QMessageBox.warning(self, "无法完成", "\n".join(errors))
                self.status_combo.blockSignals(True)
                self.status_combo.setCurrentText("draft")
                self.status_combo.blockSignals(False)
                return
        self.document.status = status
        self._changed()

    def _changed(self) -> None:
        self._refresh_panels()
        self._redraw_colors()
        self.save(silent=True)

    def _redraw_colors(self) -> None:
        if not self.document:
            return
        colors = {face_id: "#8B8B8B" for body in self.document.bodies for face_id in body.face_ids}
        for instance in self.document.instances:
            color = self.document.class_by_id(instance.class_id).color
            for face_id in instance.face_ids:
                colors[face_id] = color
        for face_id in self.working_faces:
            colors[face_id] = "#FACC15"
        self.viewport.set_face_colors(colors)

    def edit_taxonomy(self) -> None:
        if not self.document:
            return
        dialog = TaxonomyDialog(self.document.taxonomy, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.document.taxonomy = dialog.values()
        except ValueError as error:
            QMessageBox.warning(self, "标签体系无效", str(error))
            return
        self._changed()

    def save(self, silent: bool = False) -> None:
        if not self.document or not self.step_path:
            return
        self.document.annotator = self.annotator_edit.text().strip()
        self.document.reviewer = self.reviewer_edit.text().strip()
        errors = self.document.validate()
        if errors and not silent:
            QMessageBox.warning(self, "保存了草稿，但发现问题", "\n".join(errors))
        save_document(annotation_path_for(self.step_path), self.document)
        self.status_label.setText("已自动保存" if silent else "已保存标注")

    def export(self) -> None:
        if not self.document or not self.step_path:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not directory:
            return
        try:
            paths = export_aagnet(self.document, Path(directory))
        except Exception as error:
            QMessageBox.warning(self, "导出失败", str(error))
            return
        self.status_label.setText(f"已导出 {len(paths)} 个 body 标签")


def main() -> None:
    configure_qt_plugins()
    app = QApplication(sys.argv)
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    window = MainWindow(initial)
    window.show()
    sys.exit(app.exec())
