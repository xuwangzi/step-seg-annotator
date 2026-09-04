"""Desktop application for face-level STEP annotation."""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import OCP
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QColorDialog, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget, QHeaderView,
)

from .export import export_faces, export_solids
from .face_partition import (
    FacePartition, face_document_for, file_sha256, load_or_create_partition,
    load_partition_snapshot, partition_matches_document, resolve_snapshot_path,
)
from .models import AnnotationDocument, FaceAnnotationDocument, FaceGroupRecord, TaxonomyClass, annotation_path_for
from .storage import load_document, save_document, source_matches
from .topology import EntityShape, apply_split, load_step, new_document, planar_split_candidates, replay_document, undo_last_split
from .viewer import OccViewport


GROUP_COLORS = ["#3B638A", "#3F7D3A", "#579695", "#B86B4B", "#8A5E9E", "#D29F3F", "#4C8A72"]


def update_face_selection(current: set[str], incoming: set[str], modifiers: int) -> set[str]:
    """Apply a unique add/remove selection operation."""
    selected = set(current)
    if modifiers & int(Qt.ControlModifier):
        selected.difference_update(incoming)
    else:
        selected.update(incoming)
    return selected


def color_for_group(document: FaceAnnotationDocument, group: FaceGroupRecord) -> str:
    category = document.class_by_id(group.class_id)
    used = {item.color.upper() for item in document.groups if item.id != group.id}
    stored = group.color.upper()
    if stored and stored != "#71717A" and stored not in used:
        return stored
    if category and category.color.upper() not in used:
        return category.color.upper()
    index = document.groups.index(group)
    for offset in range(len(GROUP_COLORS)):
        candidate = GROUP_COLORS[(index + offset) % len(GROUP_COLORS)]
        if candidate.upper() not in used:
            return candidate
    return category.color.upper() if category else "#71717A"


def available_faces_for_group(
    document: FaceAnnotationDocument, group: FaceGroupRecord, face_ids: set[str]
) -> set[str]:
    """Return faces that are unassigned or already owned by the active group."""
    assigned_elsewhere = {
        face_id
        for other in document.groups
        if other.id != group.id
        for face_id in other.face_ids
    }
    return set(face_ids) - assigned_elsewhere


def pending_face_ids(
    document: FaceAnnotationDocument, saved_assignments: dict[str, str], group_id: str
) -> set[str]:
    """Return faces newly assigned to a group since the last successful save."""
    assignments = document.assignments()
    return {
        face_id
        for face_id, assigned_group_id in assignments.items()
        if assigned_group_id == group_id and saved_assignments.get(face_id) != group_id
    }


def step_files_in_folder(folder: Path) -> list[Path]:
    """Return direct STEP files in a folder in a stable display order."""
    return sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in {".step", ".stp"}
        ),
        key=lambda path: path.name.casefold(),
    )


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
        self.folder_path: Path | None = None
        self.step_paths: list[Path] = []
        self.document: AnnotationDocument | FaceAnnotationDocument | None = None
        self.entities: list[EntityShape] = []
        self.partition: FacePartition | None = None
        self.active_entity_id = ""
        self.active_group_id = ""
        self.selected_face_ids: set[str] = set()
        self._saved_assignments: dict[str, str] = {}
        self._face_changes_pending = False
        self.hidden_group_ids: set[str] = set()
        self.candidates = []
        self.preview_index = -1
        self._history: list[tuple[object, set[str], str, dict[str, str], bool]] = []
        self._build_ui()
        if initial_path:
            self.open_step(initial_path)

    def _build_ui(self) -> None:
        menu_bar = self.menuBar()
        menu_bar.setNativeMenuBar(False)
        file_menu = menu_bar.addMenu("文件")
        file_menu.addAction("打开 STEP", self._choose_step)
        file_menu.addAction("打开文件夹", self._choose_folder)
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
        self.viewport.faces_continuous_selected.connect(self._faces_continuous_selected)
        self.viewport.continuous_state_changed.connect(self._continuous_state_changed)
        splitter.addWidget(self.viewport)
        splitter.addWidget(self._make_right_panel())
        splitter.setSizes([300, 850, 350])
        self.setCentralWidget(splitter)
        self.status_label = QLabel("打开 STEP 文件开始面分割标注")
        self.statusBar().addWidget(self.status_label)

    def _make_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        file_panel = QWidget()
        file_layout = QVBoxLayout(file_panel)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.addWidget(QLabel("STEP 文件"))
        self.step_tree = QTreeWidget()
        self.step_tree.setHeaderLabels(["文件", "状态"])
        self.step_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.step_tree.header().setSectionResizeMode(1, QHeaderView.Fixed)
        self.step_tree.setColumnWidth(1, 64)
        self.step_tree.itemClicked.connect(self._step_selected)
        file_layout.addWidget(self.step_tree)

        group_panel = QWidget()
        group_layout = QVBoxLayout(group_panel)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.addWidget(QLabel("面组"))
        self.group_tree = QTreeWidget()
        self.group_tree.setHeaderLabels(["显示", "面组", "类别", "面数", "颜色"])
        header = self.group_tree.header()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        self.group_tree.setColumnWidth(0, 42)
        self.group_tree.setColumnWidth(2, 72)
        self.group_tree.setColumnWidth(3, 48)
        self.group_tree.setColumnWidth(4, 66)
        self.group_tree.itemClicked.connect(self._group_selected)
        self.group_tree.itemChanged.connect(self._visibility_changed)
        group_layout.addWidget(self.group_tree)
        button_row = QHBoxLayout()
        self.new_group_button = QPushButton("+ 新建面组")
        self.new_group_button.clicked.connect(self.new_group)
        self.delete_group_button = QPushButton("- 删除面组")
        self.delete_group_button.clicked.connect(self.delete_group)
        button_row.addWidget(self.new_group_button)
        button_row.addWidget(self.delete_group_button)
        group_layout.addLayout(button_row)

        split = QSplitter(Qt.Vertical)
        split.addWidget(file_panel)
        split.addWidget(group_panel)
        split.setSizes([260, 640])
        layout.addWidget(split)
        self.entity_tree = self.group_tree  # compatibility with the previous smoke test
        return panel

    def _make_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.candidate_combo = QComboBox()  # legacy 2.0 compatibility
        self.candidate_combo.hide()
        self.legacy_confirm_button = QPushButton("确认旧版实体切分")
        self.legacy_confirm_button.clicked.connect(self.confirm_split)
        self.legacy_confirm_button.hide()
        layout.addWidget(self.legacy_confirm_button)

        operation_box = QGroupBox("面组操作")
        operation_box.setObjectName("faceGroupOperations")
        self.face_group_operations = operation_box
        operation_layout = QVBoxLayout(operation_box)
        operation_layout.setContentsMargins(10, 12, 10, 10)
        operation_layout.setSpacing(8)
        self.selection_mode_card = QFrame()
        self.selection_mode_card.setObjectName("selectionModeCard")
        self.selection_mode_card.setMinimumHeight(82)
        card_layout = QVBoxLayout(self.selection_mode_card)
        card_layout.setContentsMargins(12, 9, 12, 9)
        card_layout.setSpacing(2)

        mode_header = QHBoxLayout()
        mode_header.setContentsMargins(0, 0, 0, 0)
        mode_title = QLabel("当前模式")
        mode_title.setObjectName("selectionModeTitle")
        self.selection_mode_button = QLabel("面点选")
        self.selection_mode_button.setObjectName("selectionModeName")
        self.selection_mode_button.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        mode_header.addWidget(mode_title)
        mode_header.addStretch()
        mode_header.addWidget(self.selection_mode_button)
        card_layout.addLayout(mode_header)

        self.selection_mode_description = QLabel("单击一个面进行选择")
        self.selection_mode_description.setObjectName("selectionModeDescription")
        self.selection_mode_description.setWordWrap(True)
        card_layout.addWidget(self.selection_mode_description)
        operation_layout.addWidget(self.selection_mode_card)

        self.selection_hint = QLabel("按住 Space 并移动鼠标，可连续选择经过的面")
        self.selection_hint.setWordWrap(True)
        self.selection_hint.setObjectName("selectionHint")
        self.selection_hint.setMinimumHeight(34)
        operation_layout.addWidget(self.selection_hint)
        self.box_button = self.selection_mode_button  # compatibility with the former mode control
        self.selection_label = QLabel("选择面后将直接添加到当前面组")
        self.selection_label.setWordWrap(True)
        self.selection_label.setObjectName("selectionStatus")
        self.selection_label.setMinimumHeight(28)
        operation_layout.addWidget(self.selection_label)
        self.clear_selection_button = QPushButton("清除选择")
        self.clear_selection_button.setObjectName("clearSelectionButton")
        self.clear_selection_button.setMinimumHeight(30)
        self.clear_selection_button.clicked.connect(self.clear_selection)
        operation_layout.addWidget(self.clear_selection_button)
        layout.addWidget(operation_box)

        info_box = QGroupBox("面组信息")
        info_layout = QVBoxLayout(info_box)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self.choose_group_color)
        self.class_combo = QComboBox()
        self.note_edit = QLineEdit()
        form.addRow("面组名称", self.name_edit)
        form.addRow("面组颜色", self.color_button)
        form.addRow("可选类别", self.class_combo)
        form.addRow("备注", self.note_edit)
        info_layout.addLayout(form)
        apply_metadata = QPushButton("更新面组信息")
        apply_metadata.clicked.connect(self.update_group)
        info_layout.addWidget(apply_metadata)
        layout.addWidget(info_box)

        status_box = QGroupBox("标注状态")
        status_layout = QVBoxLayout(status_box)
        self.annotator_edit = QLineEdit()
        self.status_combo = QComboBox()
        self.status_combo.addItems(["draft", "completed", "reviewed"])
        self.status_combo.currentTextChanged.connect(self._status_changed)
        form = QFormLayout()
        form.addRow("标注者", self.annotator_edit)
        form.addRow("状态", self.status_combo)
        status_layout.addLayout(form)
        layout.addWidget(status_box)
        layout.addStretch()
        self._group_color = "#71717A"
        self._set_selection_mode_card_style(False)
        return panel

    def viewport_box_mode_changed(self, enabled: bool) -> None:
        self.viewport_continuous_mode_changed(enabled)

    def viewport_continuous_mode_changed(self, enabled: bool) -> None:
        self.viewport.set_continuous_mode(enabled)

    def _continuous_state_changed(self, continuous: bool) -> None:
        self.selection_mode_button.setText("面连选" if continuous else "面点选")
        self.selection_mode_description.setText(
            "移动鼠标连续选择" if continuous else "单击一个面进行选择"
        )
        self._set_selection_mode_card_style(continuous)

    def _set_selection_mode_card_style(self, continuous: bool) -> None:
        """Apply the visual state for the temporary Spacebar selection mode."""
        self.selection_mode_card.setProperty("mode", "continuous" if continuous else "point")
        self.face_group_operations.setStyleSheet(
            """
            QFrame#selectionModeCard {
                border-radius: 6px;
                padding: 0px;
            }
            QFrame#selectionModeCard[mode="point"] {
                background-color: #EAF3FF;
                border: 1px solid #8DB8E8;
            }
            QFrame#selectionModeCard[mode="continuous"] {
                background-color: #FFF4D6;
                border: 2px solid #E59B22;
            }
            QLabel#selectionModeTitle {
                color: #5B6573;
                font-size: 11px;
            }
            QLabel#selectionModeName {
                font-size: 16px;
                font-weight: 600;
            }
            QFrame#selectionModeCard[mode="point"] QLabel#selectionModeName {
                color: #1D5F9F;
            }
            QFrame#selectionModeCard[mode="continuous"] QLabel#selectionModeName {
                color: #B86600;
            }
            QLabel#selectionModeDescription {
                color: #46515E;
            }
            QGroupBox#faceGroupOperations {
                margin-top: 8px;
            }
            QLabel#selectionHint {
                color: #536170;
                background-color: #F5F7FA;
                border: 1px solid #DCE2E8;
                border-radius: 4px;
                padding: 5px 8px;
            }
            QLabel#selectionStatus {
                color: #3F4B58;
                background-color: #F8FAFC;
                border: 1px solid #E3E8EE;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QPushButton#clearSelectionButton {
                color: #344454;
                background-color: #FFFFFF;
                border: 1px solid #B8C3CF;
                border-radius: 4px;
                padding: 4px 10px;
            }
            QPushButton#clearSelectionButton:hover {
                background-color: #EEF5FC;
                border-color: #6D9DCC;
            }
            QPushButton#clearSelectionButton:pressed {
                background-color: #DDEBF8;
            }
            """
        )
        self.face_group_operations.style().unpolish(self.face_group_operations)
        self.face_group_operations.style().polish(self.face_group_operations)
        self.selection_mode_card.style().unpolish(self.selection_mode_card)
        self.selection_mode_card.style().polish(self.selection_mode_card)
        self.selection_mode_card.update()

    def _choose_step(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "选择 STEP 文件", "", "STEP (*.step *.stp)")
        if filename:
            self.open_step(Path(filename))

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 STEP 文件夹", "")
        if folder:
            self.open_folder(Path(folder))

    def open_folder(self, folder: Path) -> None:
        paths = step_files_in_folder(folder)
        if not paths:
            QMessageBox.warning(self, "没有 STEP 文件", f"文件夹中没有 .step 或 .stp 文件：{folder}")
            return
        self.folder_path = folder.resolve()
        self.step_paths = paths
        self._refresh_step_tree()
        self.open_step(paths[0], update_file_list=False)

    def _refresh_step_tree(self) -> None:
        if not hasattr(self, "step_tree"):
            return
        current = self.step_path.resolve() if self.step_path else None
        self.step_tree.blockSignals(True)
        self.step_tree.clear()
        selected_item: QTreeWidgetItem | None = None
        for path in self.step_paths:
            item = QTreeWidgetItem([path.name, ""])
            item.setData(0, Qt.UserRole, str(path))
            if annotation_path_for(path).exists():
                try:
                    document = load_document(annotation_path_for(path))
                    item.setText(1, document.status)
                except Exception:
                    item.setText(1, "异常")
            if current and path.resolve() == current:
                selected_item = item
            self.step_tree.addTopLevelItem(item)
        if selected_item:
            self.step_tree.setCurrentItem(selected_item)
            selected_item.setSelected(True)
        self.step_tree.blockSignals(False)

    def _update_current_step_status(self) -> None:
        if not self.step_path or not self.document:
            return
        target = self.step_path.resolve()
        for index in range(self.step_tree.topLevelItemCount()):
            item = self.step_tree.topLevelItem(index)
            value = item.data(0, Qt.UserRole)
            if value and Path(value).resolve() == target:
                item.setText(1, self.document.status)
                break

    def _step_selected(self, item: QTreeWidgetItem, _column: int) -> None:
        value = item.data(0, Qt.UserRole)
        if value:
            self.open_step(Path(value), update_file_list=False)

    def open_step(self, path: Path, update_file_list: bool = True) -> None:
        if self.step_path and self.step_path.resolve() != path.resolve():
            self.save(silent=True)
        if update_file_list:
            self.folder_path = None
            self.step_paths = [path]
            self._refresh_step_tree()
        try:
            annotation = annotation_path_for(path)
            if annotation.exists():
                document = load_document(annotation)
                if isinstance(document, FaceAnnotationDocument):
                    if not source_matches(path, document) or document.ocp_version != OCP.__version__:
                        raise ValueError("源 STEP 或 OpenCascade 版本与面标注不一致")
                    snapshot = resolve_snapshot_path(document)
                    rebuild = not snapshot.exists() or file_sha256(snapshot) != document.snapshot_sha256
                    if rebuild:
                        # Rebuild into the current algorithm-versioned cache path.
                        partition, snapshot, _ = load_or_create_partition(path, force=True)
                        document.snapshot_path = str(snapshot.resolve().relative_to(path.resolve().parent))
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
            self._saved_assignments = self.document.assignments() if isinstance(self.document, FaceAnnotationDocument) else {}
            self._face_changes_pending = False
            self._refresh_step_tree()
            self._history.clear()
            self.selected_face_ids.clear()
            self.active_group_id = ""
            self.annotator_edit.setText(self.document.annotator)
            self.status_combo.blockSignals(True)
            self.status_combo.setCurrentText(self.document.status)
            self.status_combo.blockSignals(False)
            self._refresh_ui(fit=True)
            legacy = isinstance(self.document, AnnotationDocument)
            self.candidate_combo.setVisible(legacy)
            self.legacy_confirm_button.setVisible(legacy)
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
                group_color = color_for_group(self.document, group)
                group.color = group_color
                item = QTreeWidgetItem(
                    [
                        "",
                        group.name or group.id,
                        category.name_zh if category else "未分类",
                        str(len(group.face_ids)),
                        group_color,
                    ]
                )
                item.setData(0, Qt.UserRole, group.id)
                item.setCheckState(0, Qt.Unchecked if group.id in self.hidden_group_ids else Qt.Checked)
                item.setBackground(4, QColor(group_color))
                item.setForeground(4, QColor("#FFFFFF"))
                item.setToolTip(4, group_color)
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
                group_color = color_for_group(self.document, group)
                group.color = group_color
                for face_id in group.face_ids:
                    colors[face_id] = group_color
        self.selected_face_ids = pending_face_ids(
            self.document, self._saved_assignments, self.active_group_id
        )
        for face_id in self.selected_face_ids:
            colors[face_id] = "#FACC15"
        if not self.viewport.set_face_colors(self.partition, colors):
            self.viewport.display_partition(self.partition, colors, fit=fit)
        elif fit:
            self.viewport.fit_all()
        self.selection_label.setText(f"已选择 {len(self.selected_face_ids)} 个细面；面组 {self.active_group_id or '-'}")

    def _active_group(self) -> FaceGroupRecord | None:
        if not isinstance(self.document, FaceAnnotationDocument):
            return None
        return next((group for group in self.document.groups if group.id == self.active_group_id), None)

    def _load_active_group_metadata(self) -> None:
        group = self._active_group()
        if not group:
            self.name_edit.clear()
            self._set_group_color("#71717A")
            self.color_button.setEnabled(False)
            self.note_edit.clear()
            return
        self.name_edit.setText(group.name)
        self._set_group_color(color_for_group(self.document, group))
        self.color_button.setEnabled(True)
        self.note_edit.setText(group.note)
        self.class_combo.setCurrentIndex(max(self.class_combo.findData(group.class_id), 0))

    def _set_group_color(self, color: str) -> None:
        self._group_color = color
        self.color_button.setText(color)
        self.color_button.setStyleSheet(
            f"QPushButton {{ background-color: {color}; color: white; }}"
        )

    def choose_group_color(self) -> None:
        if not self._active_group():
            return
        color = QColorDialog.getColor(QColor(self._group_color), self, "选择面组颜色")
        if color.isValid():
            self._set_group_color(color.name().upper())

    def _group_selected(self, item: QTreeWidgetItem) -> None:
        value = item.data(0, Qt.UserRole)
        if isinstance(self.document, FaceAnnotationDocument):
            if value == self.active_group_id:
                return
            self.save(silent=True)
            self.active_group_id = value
            self.selected_face_ids.clear()
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
            self._history.append(
                (
                    copy.deepcopy(self.document),
                    set(self.selected_face_ids),
                    self.active_group_id,
                    dict(self._saved_assignments),
                    self._face_changes_pending,
                )
            )

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
        self._toggle_group_faces({face_id}, point_pick=True)

    def _faces_box_selected(self, face_ids: list[str]) -> None:
        if not isinstance(self.document, FaceAnnotationDocument):
            return
        self._toggle_group_faces(set(face_ids))

    def _faces_continuous_selected(self, face_ids: list[str]) -> None:
        if not isinstance(self.document, FaceAnnotationDocument):
            return
        self._toggle_group_faces(set(face_ids), force_add=True)

    def _toggle_group_faces(
        self, face_ids: set[str], point_pick: bool = False, force_add: bool = False
    ) -> None:
        group = self._active_group()
        if not group:
            if face_ids:
                self.status_label.setText("请先在左侧选择或新建一个面组")
            return
        face_ids &= {face.id for face in self.document.faces}
        if not face_ids:
            if not point_pick:
                self.clear_selection()
            return
        modifiers = QApplication.keyboardModifiers()
        current = set(group.face_ids)
        available = available_faces_for_group(self.document, group, face_ids)
        blocked = face_ids - available
        face_ids = available
        if blocked:
            self.status_label.setText(f"已有 {len(blocked)} 个面属于其他面组，无法重复选择")
        if not face_ids:
            return
        if modifiers & Qt.ControlModifier:
            to_remove = face_ids & current
            to_add = set()
        elif force_add or modifiers & Qt.ShiftModifier:
            to_remove = set()
            to_add = face_ids - current
        elif point_pick:
            to_remove = face_ids & current
            to_add = face_ids - current
        else:
            to_remove = face_ids & current
            to_add = face_ids - current
        if not to_add and not to_remove:
            return
        self._remember()
        for other in self.document.groups:
            if other.id != group.id and to_add:
                other.face_ids = [face_id for face_id in other.face_ids if face_id not in to_add]
        group.face_ids = sorted((current | to_add) - to_remove)
        self.selected_face_ids.difference_update(to_remove)
        self.selected_face_ids.update(to_add)
        self._face_changes_pending = (
            self.document.assignments() != self._saved_assignments
        )
        self._refresh_ui()

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
        if self._face_changes_pending:
            self.save(silent=True)
        elif self.selected_face_ids:
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
        group.color = color_for_group(self.document, group)
        self.active_group_id = group.id
        self._refresh_ui()
        self.save(silent=True)

    def merge_selected(self) -> None:
        """Compatibility helper for integrations using the previous workflow."""
        group = self._active_group()
        if not group or not self.selected_face_ids:
            return
        self._remember()
        selected = set(self.selected_face_ids)
        for other in self.document.groups:
            if other.id != group.id:
                other.face_ids = [face_id for face_id in other.face_ids if face_id not in selected]
        group.face_ids = sorted(set(group.face_ids) | selected)
        self.selected_face_ids.clear()
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
        group.color = self._group_color
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
            document, selected, active, saved_assignments, changes_pending = self._history.pop()
            self.document = document
            self.selected_face_ids = selected
            self.active_group_id = active
            self._saved_assignments = saved_assignments
            self._face_changes_pending = changes_pending
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
        errors = self.document.validate()
        if errors:
            if not silent:
                QMessageBox.warning(self, "无法保存", "\n".join(errors))
            return
            return
        save_document(annotation_path_for(self.step_path), self.document)
        if isinstance(self.document, FaceAnnotationDocument):
            self._saved_assignments = self.document.assignments()
            self._face_changes_pending = False
            self.selected_face_ids.clear()
            self._redraw_faces()
        self._update_current_step_status()
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
