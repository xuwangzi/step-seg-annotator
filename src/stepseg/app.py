"""Desktop application for interactive closed-solid STEP segmentation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import OCP
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .export import export_solids
from .models import AnnotationDocument, TaxonomyClass, annotation_path_for
from .storage import load_document, save_document, source_matches
from .topology import (
    ENTITY_COLORS,
    EntityShape,
    SplitCandidate,
    apply_split,
    load_step,
    new_document,
    planar_split_candidates,
    replay_document,
    undo_last_split,
)
from .viewer import OccViewport


class TaxonomyDialog(QDialog):
    def __init__(self, taxonomy: list[TaxonomyClass], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("实体类别")
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
        next_id = max(ids, default=0) + 1
        self._add_row(TaxonomyClass(next_id, f"class_{next_id}", f"类别 {next_id}", "#71717A"))

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
            values.append(
                TaxonomyClass(
                    class_id,
                    key,
                    name,
                    color,
                    self.table.item(row, 4).checkState() == Qt.Checked,
                )
            )
        return values


class MainWindow(QMainWindow):
    def __init__(self, initial_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("STEP 实体分割标注工具")
        self.resize(1500, 900)
        self.step_path: Path | None = None
        self.document: AnnotationDocument | None = None
        self.entities: list[EntityShape] = []
        self.active_entity_id = ""
        self.candidates: list[SplitCandidate] = []
        self.preview_index = -1
        self.hidden_entity_ids: set[str] = set()
        self.viewport: OccViewport
        self._build_ui()
        if initial_path:
            self.open_step(initial_path)

    def _build_ui(self) -> None:
        toolbar = QToolBar("工具")
        self.addToolBar(toolbar)
        for label, callback in (
            ("打开 STEP", self._choose_step),
            ("保存", self.save),
            ("导出实体", self.export),
            ("撤销切分", self.undo),
            ("重置", self.reset),
            ("实体类别", self.edit_taxonomy),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        for label, direction in (("等轴", (1, -1, 1)), ("顶", (0, 0, 1)), ("前", (0, 1, 0))):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, item=direction: self.viewport.set_view(item))
            toolbar.addWidget(button)
        fit_button = QPushButton("适配")
        fit_button.clicked.connect(lambda: self.viewport.fit_all())
        toolbar.addWidget(fit_button)
        wire_button = QPushButton("线框")
        wire_button.setCheckable(True)
        wire_button.toggled.connect(lambda enabled: self.viewport.toggle_wireframe(enabled))
        toolbar.addWidget(wire_button)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._make_left_panel())
        self.viewport = OccViewport(splitter)
        self.viewport.face_picked.connect(self._face_picked)
        splitter.addWidget(self.viewport)
        splitter.addWidget(self._make_right_panel())
        splitter.setSizes([300, 850, 350])
        self.setCentralWidget(splitter)
        self.status_label = QLabel("打开 STEP 文件开始实体分割")
        self.statusBar().addWidget(self.status_label)

    def _make_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("闭合实体"))
        self.entity_tree = QTreeWidget()
        self.entity_tree.setHeaderLabels(["显示", "实体", "类别", "体积"])
        self.entity_tree.itemClicked.connect(self._entity_selected)
        self.entity_tree.itemChanged.connect(self._visibility_changed)
        layout.addWidget(self.entity_tree)
        return panel

    def _make_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.selection_label = QLabel("点击实体表面以搜索平面切分候选")
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)
        self.candidate_combo = QComboBox()
        self.candidate_combo.currentIndexChanged.connect(self._preview_candidate)
        layout.addWidget(self.candidate_combo)
        row = QHBoxLayout()
        confirm = QPushButton("确认切分")
        confirm.clicked.connect(self.confirm_split)
        row.addWidget(confirm)
        cancel = QPushButton("取消预览")
        cancel.clicked.connect(self.cancel_preview)
        row.addWidget(cancel)
        layout.addLayout(row)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.class_combo = QComboBox()
        self.note_edit = QLineEdit()
        form.addRow("实体名称", self.name_edit)
        form.addRow("可选类别", self.class_combo)
        form.addRow("备注", self.note_edit)
        layout.addLayout(form)
        apply_metadata = QPushButton("更新实体信息")
        apply_metadata.clicked.connect(self.update_entity_metadata)
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

    def _choose_step(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "选择 STEP 文件", "", "STEP (*.step *.stp)")
        if filename:
            self.open_step(Path(filename))

    def open_step(self, path: Path) -> None:
        try:
            initial_entities = load_step(path)
            annotation = annotation_path_for(path)
            if annotation.exists():
                document = load_document(annotation)
                if not source_matches(path, document):
                    raise ValueError("源 STEP 哈希与标注不一致")
                if document.ocp_version != OCP.__version__:
                    raise ValueError("OpenCascade 版本与标注创建时不一致，无法安全重放")
                entities = replay_document(path, document)
            else:
                entities = initial_entities
                document = new_document(path, entities)
        except Exception as error:
            QMessageBox.critical(self, "导入失败", str(error))
            return
        self.step_path = path
        self.document = document
        self.entities = entities
        self.active_entity_id = entities[0].id
        self.annotator_edit.setText(document.annotator)
        self.reviewer_edit.setText(document.reviewer)
        self.status_combo.blockSignals(True)
        self.status_combo.setCurrentText(document.status)
        self.status_combo.blockSignals(False)
        self._clear_candidates()
        self._refresh_ui(fit=True)
        self.status_label.setText(f"已载入 {path.name}：{len(entities)} 个闭合实体")

    def _entity_colors(self) -> dict[str, str]:
        if not self.document:
            return {}
        return {item.id: item.color for item in self.document.entities}

    def _refresh_ui(self, fit: bool = False) -> None:
        if not self.document:
            return
        self.entity_tree.blockSignals(True)
        self.entity_tree.clear()
        for record in self.document.entities:
            category = self.document.class_by_id(record.class_id)
            item = QTreeWidgetItem(
                ["", record.name or record.id, category.name_zh if category else "未分类", f"{record.signature.volume:.6g}"]
            )
            item.setData(0, Qt.UserRole, record.id)
            item.setCheckState(
                0, Qt.Unchecked if record.id in self.hidden_entity_ids else Qt.Checked
            )
            self.entity_tree.addTopLevelItem(item)
            if record.id == self.active_entity_id:
                self.entity_tree.setCurrentItem(item)
        self.entity_tree.blockSignals(False)
        self._refresh_class_combo()
        self._load_active_metadata()
        self.viewport.display_entities(
            self.entities, self._entity_colors(), self.active_entity_id or None, fit=fit
        )

    def _refresh_class_combo(self) -> None:
        if not self.document:
            return
        current = self.class_combo.currentData()
        self.class_combo.clear()
        self.class_combo.addItem("未分类", None)
        for item in self.document.taxonomy:
            if item.enabled:
                self.class_combo.addItem(item.name_zh, item.id)
        index = self.class_combo.findData(current)
        self.class_combo.setCurrentIndex(max(index, 0))

    def _active_record(self):
        if not self.document or not self.active_entity_id:
            return None
        return next((item for item in self.document.entities if item.id == self.active_entity_id), None)

    def _active_shape(self):
        return next((item for item in self.entities if item.id == self.active_entity_id), None)

    def _load_active_metadata(self) -> None:
        record = self._active_record()
        if not record:
            return
        self.name_edit.setText(record.name)
        self.note_edit.setText(record.note)
        index = self.class_combo.findData(record.class_id)
        self.class_combo.setCurrentIndex(max(index, 0))

    def _entity_selected(self, item: QTreeWidgetItem) -> None:
        self.cancel_preview()
        self.active_entity_id = item.data(0, Qt.UserRole)
        self._load_active_metadata()
        self.viewport.display_entities(self.entities, self._entity_colors(), self.active_entity_id)

    def _visibility_changed(self, item: QTreeWidgetItem) -> None:
        entity_id = item.data(0, Qt.UserRole)
        visible = item.checkState(0) == Qt.Checked
        if visible:
            self.hidden_entity_ids.discard(entity_id)
        else:
            self.hidden_entity_ids.add(entity_id)
        self.viewport.set_entity_visible(entity_id, visible)

    def _face_picked(self, entity_id: str, face_id: str) -> None:
        self.active_entity_id = entity_id
        entity = self._active_shape()
        if entity is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.candidates = planar_split_candidates(entity, face_id)
        finally:
            QApplication.restoreOverrideCursor()
        self.candidate_combo.blockSignals(True)
        self.candidate_combo.clear()
        for index, candidate in enumerate(self.candidates, start=1):
            volumes = ", ".join(f"{item.volume:.4g}" for item in candidate.result_signatures)
            self.candidate_combo.addItem(
                f"候选 {index}：{len(candidate.result_shapes)} 个实体；体积 {volumes}"
            )
        self.candidate_combo.blockSignals(False)
        if not self.candidates:
            self.preview_index = -1
            self.selection_label.setText("未发现能产生多个闭合实体的平面")
            self.viewport.display_entities(self.entities, self._entity_colors(), entity_id)
            return
        self.selection_label.setText(f"{entity_id}：发现 {len(self.candidates)} 个候选切平面")
        self.candidate_combo.setCurrentIndex(0)
        self._preview_candidate(0)

    def _preview_candidate(self, index: int) -> None:
        if index < 0 or index >= len(self.candidates) or not self.active_entity_id:
            return
        self.preview_index = index
        candidate = self.candidates[index]
        normal = ", ".join(f"{item:.4g}" for item in candidate.plane.normal)
        self.selection_label.setText(
            f"预览 {index + 1}：平面 n=({normal}), d={candidate.plane.offset:.6g}"
        )
        self.viewport.display_preview(
            self.entities, self.active_entity_id, candidate.result_shapes, self._entity_colors()
        )

    def _clear_candidates(self) -> None:
        self.candidates = []
        self.preview_index = -1
        self.candidate_combo.blockSignals(True)
        self.candidate_combo.clear()
        self.candidate_combo.blockSignals(False)

    def cancel_preview(self) -> None:
        self._clear_candidates()
        if self.document:
            self.selection_label.setText("点击实体表面以搜索平面切分候选")
            self.viewport.display_entities(
                self.entities, self._entity_colors(), self.active_entity_id or None
            )

    def confirm_split(self) -> None:
        if not self.document or self.preview_index < 0:
            return
        candidate = self.candidates[self.preview_index]
        try:
            self.entities = apply_split(
                self.document, self.entities, self.active_entity_id, candidate.plane
            )
        except Exception as error:
            QMessageBox.warning(self, "切分失败", str(error))
            return
        self.active_entity_id = self.document.split_operations[-1].result_entity_ids[0]
        self.hidden_entity_ids.intersection_update(item.id for item in self.entities)
        self._clear_candidates()
        self._refresh_ui()
        self.save(silent=True)
        self.status_label.setText(f"切分完成：当前 {len(self.entities)} 个闭合实体")

    def undo(self) -> None:
        if not self.document or not self.step_path or not self.document.split_operations:
            return
        try:
            undo_last_split(self.document)
            self.entities = replay_document(self.step_path, self.document)
        except Exception as error:
            QMessageBox.warning(self, "撤销失败", str(error))
            return
        self.active_entity_id = self.entities[0].id
        self.hidden_entity_ids.intersection_update(item.id for item in self.entities)
        self._clear_candidates()
        self._refresh_ui()
        self.save(silent=True)

    def reset(self) -> None:
        if not self.step_path or not self.document:
            return
        if QMessageBox.question(self, "重置", "清除所有切分和实体信息？") != QMessageBox.Yes:
            return
        self.entities = load_step(self.step_path)
        self.document = new_document(self.step_path, self.entities)
        self.active_entity_id = self.entities[0].id
        self.hidden_entity_ids.clear()
        self._clear_candidates()
        self._refresh_ui()
        self.save(silent=True)

    def update_entity_metadata(self) -> None:
        record = self._active_record()
        if not record or not self.document:
            return
        record.name = self.name_edit.text().strip() or record.id
        record.note = self.note_edit.text().strip()
        record.class_id = self.class_combo.currentData()
        category = self.document.class_by_id(record.class_id)
        if category:
            record.color = category.color
        else:
            number = int(record.id.removeprefix("entity_"))
            record.color = ENTITY_COLORS[(number - 1) % len(ENTITY_COLORS)]
        self._refresh_ui()
        self.save(silent=True)

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

    def _status_changed(self, status: str) -> None:
        if self.document:
            self.document.status = status
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
        save_document(annotation_path_for(self.step_path), self.document)
        self.status_label.setText("已自动保存" if silent else "已保存")

    def export(self) -> None:
        if not self.document:
            return
        directory = QFileDialog.getExistingDirectory(self, "选择实体导出目录")
        if not directory:
            return
        try:
            paths = export_solids(self.document, self.entities, Path(directory))
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
