"""Desktop application for face-level STEP annotation."""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import OCP
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .export import export_faces, export_solids
from .face_partition import (
    FacePartition, face_document_for, file_sha256, load_or_create_partition,
    load_partition_snapshot, partition_matches_document,
)
from .models import AnnotationDocument, FaceAnnotationDocument, FaceGroupRecord, TaxonomyClass, annotation_path_for
from .storage import load_document, save_document, source_matches
from .topology import EntityShape, apply_split, load_step, new_document, planar_split_candidates, replay_document, undo_last_split
from .viewer import OccViewport


class TaxonomyDialog(QDialog):
    def __init__(self, taxonomy: list[TaxonomyClass], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("面组类别")
        self.resize(620, 380)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["ID", "key", "中文名", "颜色", "启用"])
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
        for column, value in enumerate((str(item.id), item.key, item.name_zh, item.color)):
            cell = QTableWidgetItem(value)
            if column == 0:
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, column, cell)
        enabled = QTableWidgetItem()
        enabled.setCheckState(Qt.Checked if item.enabled else Qt.Unchecked)
        self.table.setItem(row, 4, enabled)

    def _add_new(self) -> None:
        ids = [int(self.table.item(row, 0).text()) for row in range(self.table.rowCount())]
        self._add_row(TaxonomyClass(max(ids, default=0) + 1, "class_new", "新类别", "#71717A"))

    def values(self) -> list[TaxonomyClass]:
        values: list[TaxonomyClass] = []
        keys: set[str] = set()
        for row in range(self.table.rowCount()):
            class_id = int(self.table.item(row, 0).text())
            key = self.table.item(row, 1).text().strip()
            name = self.table.item(row, 2).text().strip()
            color = self.table.item(row, 3).text().strip()
            if not key or not name or len(color) != 7 or not color.startswith("#"):
                raise ValueError("每个类别需要 key、中文名和 #RRGGBB 颜色")
            if key in keys:
                raise ValueError(f"重复 key：{key}")
            keys.add(key)
            values.append(TaxonomyClass(class_id, key, name, color, self.table.item(row, 4).checkState() == Qt.Checked))
        return values


class MainWindow(QMainWindow):
    def __init__(self, initial_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("STEP 面分割标注工具")
        self.resize(1500, 900)
        self.step_path: Path | None = None
        self.document: AnnotationDocument | FaceAnnotationDocument | None = None
        self.entities: list[EntityShape] = []
        self.partition: FacePartition | None = None
        self.active_entity_id = ""
        self.active_group_id = ""
        self.selected_face_ids: set[str] = set()
        self.hidden_group_ids: set[str] = set()
        self.candidates = []
        self.preview_index = -1
        self._history: list[tuple[object, set[str], str]] = []
        self._build_ui()
        if initial_path:
            self.open_step(initial_path)

    def _build_ui(self) -> None:
        menu_bar = self.menuBar()
        menu_bar.setNativeMenuBar(False)
        file_menu = menu_bar.addMenu("文件")
        file_menu.addAction("打开 STEP", self._choose_step)
        file_menu.addSeparator()
        file_menu.addAction("保存", self.save)
        file_menu.addAction("导出面分割", self.export)
        edit_menu = menu_bar.addMenu("编辑")
        edit_menu.addAction("撤销", self.undo)
        edit_menu.addAction("重置", self.reset)
        annotation_menu = menu_bar.addMenu("标注")
        annotation_menu.addAction("面组类别", self.edit_taxonomy)
        view_menu = menu_bar.addMenu("视图")
        for label, direction in (("等轴", (1, -1, 1)), ("顶", (0, 0, 1)), ("前", (0, 1, 0))):
            view_menu.addAction(label, lambda _checked=False, item=direction: self.viewport.set_view(item))
        view_menu.addSeparator()
        view_menu.addAction("适配", lambda: self.viewport.fit_all())
        wire_action = view_menu.addAction("线框")
        wire_action.setCheckable(True)
        wire_action.toggled.connect(lambda enabled: self.viewport.toggle_wireframe(enabled))
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._make_left_panel())
        self.viewport = OccViewport(splitter)
        self.viewport.face_picked.connect(self._face_picked)
        self.viewport.faces_box_selected.connect(self._faces_box_selected)
        splitter.addWidget(self.viewport)
        splitter.addWidget(self._make_right_panel())
        splitter.setSizes([300, 850, 350])
        self.setCentralWidget(splitter)
        self.status_label = QLabel("打开 STEP 文件开始面分割标注")
        self.statusBar().addWidget(self.status_label)

    def _make_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("面组"))
        self.group_tree = QTreeWidget()
        self.group_tree.setHeaderLabels(["显示", "面组", "类别", "面数"])
        self.group_tree.itemClicked.connect(self._group_selected)
        self.group_tree.itemChanged.connect(self._visibility_changed)
        layout.addWidget(self.group_tree)
        self.entity_tree = self.group_tree  # compatibility with the previous smoke test
        return panel

    def _make_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.selection_label = QLabel("点击细面以选择，或启用面框选")
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)
        self.candidate_combo = QComboBox()  # legacy 2.0 compatibility
        self.candidate_combo.hide()
        self.box_button = QPushButton("面框选")
        self.box_button.setCheckable(True)
        self.box_button.toggled.connect(self.viewport_box_mode_changed)
        layout.addWidget(self.box_button)
        row = QHBoxLayout()
        for label, callback in (("新建面组", self.new_group), ("合并到当前组", self.merge_selected), ("清除选择", self.clear_selection)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        for label, callback in (("更新面组", self.update_group), ("删除面组", self.delete_group)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.class_combo = QComboBox()
        self.note_edit = QLineEdit()
        form.addRow("面组名称", self.name_edit)
        form.addRow("可选类别", self.class_combo)
        form.addRow("备注", self.note_edit)
        layout.addLayout(form)
        apply_metadata = QPushButton("更新面组信息")
        apply_metadata.clicked.connect(self.update_group)
        layout.addWidget(apply_metadata)
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

    def viewport_box_mode_changed(self, enabled: bool) -> None:
        self.viewport.set_box_mode(enabled)

    def _choose_step(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "选择 STEP 文件", "", "STEP (*.step *.stp)")
        if filename:
            self.open_step(Path(filename))

    def open_step(self, path: Path) -> None:
        try:
            annotation = annotation_path_for(path)
            if annotation.exists():
                document = load_document(annotation)
                if isinstance(document, FaceAnnotationDocument):
                    if not source_matches(path, document) or document.ocp_version != OCP.__version__:
                        raise ValueError("源 STEP 或 OpenCascade 版本与面标注不一致")
                    snapshot = Path(document.snapshot_path)
                    rebuild = not snapshot.exists() or file_sha256(snapshot) != document.snapshot_sha256
                    if rebuild:
                        partition, snapshot, _ = load_or_create_partition(path, snapshot, force=True)
                        document.snapshot_path = str(snapshot.resolve())
                        document.snapshot_sha256 = file_sha256(snapshot)
                    else:
                        partition = load_partition_snapshot(snapshot)
                    if not partition_matches_document(partition, document):
                        raise ValueError("面分割快照与面标注不一致")
                    self.partition, self.document = partition, document
                else:
                    self.entities = replay_document(path, document)
                    self.partition, self.document = None, document
            else:
                partition, snapshot, _ = load_or_create_partition(path)
                self.partition = partition
                self.document = face_document_for(path, partition, snapshot)
            self.step_path = path
            self._history.clear()
            self.selected_face_ids.clear()
            self.active_group_id = ""
            self.annotator_edit.setText(self.document.annotator)
            self.reviewer_edit.setText(self.document.reviewer)
            self.status_combo.blockSignals(True)
            self.status_combo.setCurrentText(self.document.status)
            self.status_combo.blockSignals(False)
            self._refresh_ui(fit=True)
            count = len(self.document.faces) if isinstance(self.document, FaceAnnotationDocument) else len(self.entities)
            self.status_label.setText(f"已载入 {path.name}：{count} 个" + ("细面" if isinstance(self.document, FaceAnnotationDocument) else "闭合实体"))
        except Exception as error:
            QMessageBox.critical(self, "导入失败", str(error))

    def _refresh_class_combo(self) -> None:
        if not self.document:
            return
        current = self.class_combo.currentData()
        self.class_combo.clear()
        self.class_combo.addItem("未分类", None)
        for item in self.document.taxonomy:
            if item.enabled:
                self.class_combo.addItem(item.name_zh, item.id)
        self.class_combo.setCurrentIndex(max(self.class_combo.findData(current), 0))

    def _refresh_ui(self, fit: bool = False) -> None:
        if not self.document:
            return
        self._refresh_class_combo()
        self.group_tree.blockSignals(True)
        self.group_tree.clear()
        if isinstance(self.document, FaceAnnotationDocument):
            for group in self.document.groups:
                category = self.document.class_by_id(group.class_id)
                item = QTreeWidgetItem(["", group.name or group.id, category.name_zh if category else "未分类", str(len(group.face_ids))])
                item.setData(0, Qt.UserRole, group.id)
                item.setCheckState(0, Qt.Unchecked if group.id in self.hidden_group_ids else Qt.Checked)
                self.group_tree.addTopLevelItem(item)
                if group.id == self.active_group_id:
                    self.group_tree.setCurrentItem(item)
            self._load_active_group_metadata()
            self._redraw_faces(fit)
        else:
            for record in self.document.entities:
                item = QTreeWidgetItem(["", record.name or record.id, "旧版实体", f"{record.signature.volume:.6g}"])
                item.setData(0, Qt.UserRole, record.id)
                self.group_tree.addTopLevelItem(item)
            self.viewport.display_entities(self.entities, {item.id: item.color for item in self.document.entities}, self.active_entity_id or None, fit=fit)
        self.group_tree.blockSignals(False)

    def _redraw_faces(self, fit: bool = False) -> None:
        if not self.partition or not isinstance(self.document, FaceAnnotationDocument):
            return
        colors = {face.id: "#8B8B8B" for face in self.document.faces}
        for group in self.document.groups:
            if group.id not in self.hidden_group_ids:
                for face_id in group.face_ids:
                    colors[face_id] = group.color
        for face_id in self.selected_face_ids:
            colors[face_id] = "#FACC15"
        self.viewport.display_partition(self.partition, colors, fit=fit)
        self.selection_label.setText(f"已选择 {len(self.selected_face_ids)} 个细面；面组 {self.active_group_id or '-'}")

    def _active_group(self) -> FaceGroupRecord | None:
        if not isinstance(self.document, FaceAnnotationDocument):
            return None
        return next((group for group in self.document.groups if group.id == self.active_group_id), None)

    def _load_active_group_metadata(self) -> None:
        group = self._active_group()
        if not group:
            self.name_edit.clear()
            self.note_edit.clear()
            return
        self.name_edit.setText(group.name)
        self.note_edit.setText(group.note)
        self.class_combo.setCurrentIndex(max(self.class_combo.findData(group.class_id), 0))

    def _group_selected(self, item: QTreeWidgetItem) -> None:
        value = item.data(0, Qt.UserRole)
        if isinstance(self.document, FaceAnnotationDocument):
            self.active_group_id = value
            group = self._active_group()
            self.selected_face_ids = set(group.face_ids if group else [])
            self._load_active_group_metadata()
            self._redraw_faces()
        else:
            self.active_entity_id = value

    def _visibility_changed(self, item: QTreeWidgetItem) -> None:
        if not isinstance(self.document, FaceAnnotationDocument):
            return
        group_id = item.data(0, Qt.UserRole)
        if item.checkState(0) == Qt.Checked:
            self.hidden_group_ids.discard(group_id)
        else:
            self.hidden_group_ids.add(group_id)
        self._redraw_faces()

    def _remember(self) -> None:
        if isinstance(self.document, FaceAnnotationDocument):
            self._history.append((copy.deepcopy(self.document), set(self.selected_face_ids), self.active_group_id))

    def _face_picked(self, entity_id: str, face_id: str) -> None:
        if not isinstance(self.document, FaceAnnotationDocument):
            self.active_entity_id = entity_id
            entity = next((item for item in self.entities if item.id == entity_id), None)
            if entity:
                self.candidates = planar_split_candidates(entity, face_id)
                self.candidate_combo.clear()
                for index, candidate in enumerate(self.candidates, 1):
                    self.candidate_combo.addItem(f"候选 {index}：{len(candidate.result_shapes)} 个实体")
                self.preview_index = 0 if self.candidates else -1
            return
        self._remember()
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            self.selected_face_ids.discard(face_id)
        elif modifiers & Qt.ShiftModifier:
            self.selected_face_ids.add(face_id)
        else:
            self.selected_face_ids = {face_id}
        self._redraw_faces()

    def _faces_box_selected(self, face_ids: list[str]) -> None:
        if not isinstance(self.document, FaceAnnotationDocument):
            return
        self._remember()
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            self.selected_face_ids.difference_update(face_ids)
        elif modifiers & Qt.ShiftModifier:
            self.selected_face_ids.update(face_ids)
        else:
            self.selected_face_ids = set(face_ids)
        self._redraw_faces()

    def confirm_split(self) -> None:
        """Compatibility entry point for 2.0 documents and older integrations."""
        if not isinstance(self.document, AnnotationDocument) or self.preview_index < 0:
            return
        candidate = self.candidates[self.preview_index]
        self.entities = apply_split(self.document, self.entities, self.active_entity_id, candidate.plane)
        self.active_entity_id = self.document.split_operations[-1].result_entity_ids[0]
        self._refresh_ui()
        self.save(silent=True)

    def clear_selection(self) -> None:
        if self.selected_face_ids:
            self._remember()
            self.selected_face_ids.clear()
            self._redraw_faces()

    def new_group(self) -> None:
        if not isinstance(self.document, FaceAnnotationDocument):
            return
        self._remember()
        numbers = [int(group.id.removeprefix("group_")) for group in self.document.groups if group.id.removeprefix("group_").isdigit()]
        number = max(numbers, default=0) + 1
        group = FaceGroupRecord(f"group_{number:04d}", f"面组_{number:04d}")
        self.document.groups.append(group)
        self.active_group_id = group.id
        self._refresh_ui()
        self.save(silent=True)

    def merge_selected(self) -> None:
        group = self._active_group()
        if not group or not self.selected_face_ids:
            return
        self._remember()
        selected = set(self.selected_face_ids)
        for other in self.document.groups:
            if other.id != group.id:
                other.face_ids = [face_id for face_id in other.face_ids if face_id not in selected]
        group.face_ids = sorted(set(group.face_ids) | selected)
        self._refresh_ui()
        self.save(silent=True)

    def update_group(self) -> None:
        group = self._active_group()
        if not group:
            return
        self._remember()
        group.name = self.name_edit.text().strip() or group.id
        group.note = self.note_edit.text().strip()
        group.class_id = self.class_combo.currentData()
        category = self.document.class_by_id(group.class_id)
        group.color = category.color if category else "#71717A"
        self._refresh_ui()
        self.save(silent=True)

    def delete_group(self) -> None:
        group = self._active_group()
        if not group:
            return
        self._remember()
        self.document.groups.remove(group)
        self.active_group_id = ""
        self.selected_face_ids.clear()
        self._refresh_ui()
        self.save(silent=True)

    def _status_changed(self, status: str) -> None:
        if not self.document:
            return
        if isinstance(self.document, FaceAnnotationDocument) and status in {"completed", "reviewed"}:
            errors = self.document.validate(require_complete=True)
            if errors:
                QMessageBox.warning(self, "无法完成", "\n".join(errors))
                self.status_combo.blockSignals(True)
                self.status_combo.setCurrentText("draft")
                self.status_combo.blockSignals(False)
                return
        self._remember()
        self.document.status = status
        self.save(silent=True)

    def undo(self) -> None:
        if isinstance(self.document, FaceAnnotationDocument) and self._history:
            document, selected, active = self._history.pop()
            self.document, self.selected_face_ids, self.active_group_id = document, selected, active
            self._refresh_ui()
            self.save(silent=True)
        elif isinstance(self.document, AnnotationDocument) and self.step_path and self.document.split_operations:
            undo_last_split(self.document)
            self.entities = replay_document(self.step_path, self.document)
            self._refresh_ui()
            self.save(silent=True)

    def reset(self) -> None:
        if not self.step_path or QMessageBox.question(self, "重置", "清除所有面组标注？") != QMessageBox.Yes:
            return
        try:
            if isinstance(self.document, AnnotationDocument):
                self.entities = load_step(self.step_path)
                self.document = new_document(self.step_path, self.entities)
                self._refresh_ui(fit=True)
                self.save(silent=True)
                return
            partition, snapshot, _ = load_or_create_partition(self.step_path)
            self.partition = partition
            self.document = face_document_for(self.step_path, partition, snapshot)
            self.selected_face_ids.clear()
            self.active_group_id = ""
            self._history.clear()
            self._refresh_ui(fit=True)
            self.save(silent=True)
        except Exception as error:
            QMessageBox.warning(self, "重置失败", str(error))

    def edit_taxonomy(self) -> None:
        if not self.document:
            return
        dialog = TaxonomyDialog(self.document.taxonomy, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        try:
            self.document.taxonomy = dialog.values()
        except ValueError as error:
            QMessageBox.warning(self, "类别无效", str(error))
            return
        self._refresh_ui()
        self.save(silent=True)

    def save(self, silent: bool = False) -> None:
        if not self.document or not self.step_path:
            return
        self.document.annotator = self.annotator_edit.text().strip()
        self.document.reviewer = self.reviewer_edit.text().strip()
        errors = self.document.validate()
        if errors:
            if not silent:
                QMessageBox.warning(self, "无法保存", "\n".join(errors))
            return
            return
        save_document(annotation_path_for(self.step_path), self.document)
        self.status_label.setText("已自动保存" if silent else "已保存")

    def export(self) -> None:
        if not self.document:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not directory:
            return
        try:
            paths = export_faces(self.document, self.partition, Path(directory)) if isinstance(self.document, FaceAnnotationDocument) else export_solids(self.document, self.entities, Path(directory))
        except Exception as error:
            QMessageBox.warning(self, "导出失败", str(error))
            return
        self.status_label.setText(f"已导出 {len(paths)} 个文件")


def main() -> None:
    app = QApplication(sys.argv)
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    window = MainWindow(initial)
    window.show()
    smoke_delay = os.environ.get("STEPSEG_GUI_SMOKE_MS")
    if smoke_delay:
        QTimer.singleShot(max(int(smoke_delay), 1), app.quit)
    sys.exit(app.exec_())
