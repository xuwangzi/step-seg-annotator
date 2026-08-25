"""Native Qt/OpenCascade viewport for closed-solid entity segmentation."""

from __future__ import annotations

import sys
from ctypes import c_char_p, c_void_p, py_object, pythonapi

from OCP.AIS import AIS_ColoredShape, AIS_InteractiveContext, AIS_Shape
from OCP.Aspect import Aspect_DisplayConnection
from OCP.OpenGl import OpenGl_GraphicDriver
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.TopAbs import TopAbs_FACE
from OCP.TopoDS import TopoDS_Shape
from OCP.V3d import V3d_Viewer
from PyQt5.QtCore import QPoint, Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget

from .topology import ENTITY_COLORS, EntityShape


class OccViewport(QWidget):
    face_picked = pyqtSignal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_PaintOnScreen)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self._initialized = False
        self._press = QPoint()
        self._last = QPoint()
        self._entities: list[EntityShape] = []
        self._ais_by_id: dict[str, AIS_ColoredShape] = {}
        self._hidden_ids: set[str] = set()
        self._wireframe = False
        self._connection = Aspect_DisplayConnection()
        self._driver = OpenGl_GraphicDriver(self._connection)
        self._viewer = V3d_Viewer(self._driver)
        self._view = self._viewer.CreateView()
        self._context = AIS_InteractiveContext(self._viewer)
        self._viewer.SetDefaultLights()
        self._viewer.SetLightOn()
        self._context.DefaultDrawer().SetFaceBoundaryDraw(True)

    def paintEngine(self):  # type: ignore[override]
        return None

    def _window(self):
        if sys.platform == "darwin":
            from OCP.Cocoa import Cocoa_Window

            return Cocoa_Window(self._native_handle_capsule())
        if sys.platform == "win32":
            from OCP.WNT import WNT_Window

            return WNT_Window(self._native_handle_capsule())
        from OCP.Xw import Xw_Window

        return Xw_Window(self._connection, int(self.winId()))

    def _native_handle_capsule(self):
        capsule_new = pythonapi.PyCapsule_New
        capsule_new.argtypes = [c_void_p, c_char_p, c_void_p]
        capsule_new.restype = py_object
        return capsule_new(c_void_p(int(self.winId())), None, None)

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self._view.SetWindow(self._window())
            self._initialized = True

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        self._ensure_initialized()
        self._view.MustBeResized()
        self._view.Redraw()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._initialized:
            self._view.MustBeResized()

    @staticmethod
    def _occ_color(hex_color: str) -> Quantity_Color:
        normalized = hex_color.lstrip("#")
        rgb = tuple(int(normalized[index : index + 2], 16) / 255 for index in (0, 2, 4))
        return Quantity_Color(*rgb, Quantity_TOC_RGB)

    def _display_shape(
        self, item_id: str, shape: TopoDS_Shape, color: str, selectable: bool
    ) -> None:
        ais = AIS_ColoredShape(shape)
        ais.SetColor(self._occ_color(color))
        self._context.Display(ais, False)
        self._context.SetDisplayMode(ais, 0 if self._wireframe else 1, False)
        if selectable:
            self._context.Activate(ais, AIS_Shape.SelectionMode_s(TopAbs_FACE), True)
        self._ais_by_id[item_id] = ais

    def clear(self) -> None:
        self._context.RemoveAll(False)
        self._ais_by_id.clear()
        self._entities.clear()

    def display_entities(
        self,
        entities: list[EntityShape],
        colors: dict[str, str],
        selected_id: str | None = None,
        fit: bool = False,
    ) -> None:
        self.clear()
        self._entities = entities
        for entity in entities:
            color = "#FACC15" if entity.id == selected_id else colors.get(entity.id, "#71717A")
            self._display_shape(entity.id, entity.shape, color, True)
            if entity.id in self._hidden_ids:
                self._context.Erase(self._ais_by_id[entity.id], False)
        self._context.UpdateCurrentViewer()
        if fit:
            self.fit_all()

    def display_preview(
        self,
        entities: list[EntityShape],
        parent_id: str,
        result_shapes: list[TopoDS_Shape],
        colors: dict[str, str],
    ) -> None:
        self.clear()
        self._entities = [entity for entity in entities if entity.id != parent_id]
        for entity in self._entities:
            self._display_shape(entity.id, entity.shape, colors.get(entity.id, "#71717A"), False)
            if entity.id in self._hidden_ids:
                self._context.Erase(self._ais_by_id[entity.id], False)
        for index, shape in enumerate(result_shapes):
            self._display_shape(
                f"preview_{index}", shape, ENTITY_COLORS[index % len(ENTITY_COLORS)], False
            )
        self._context.UpdateCurrentViewer()

    def set_entity_visible(self, entity_id: str, visible: bool) -> None:
        ais = self._ais_by_id.get(entity_id)
        if visible:
            self._hidden_ids.discard(entity_id)
            if ais:
                self._context.Display(ais, True)
        else:
            self._hidden_ids.add(entity_id)
            if ais:
                self._context.Erase(ais, True)

    def fit_all(self) -> None:
        self._view.FitAll()
        self._view.ZFitAll()
        if self._initialized:
            self._view.Redraw()

    def set_view(self, direction: tuple[float, float, float]) -> None:
        self._view.SetProj(*direction)
        self._view.Redraw()

    def toggle_wireframe(self, enabled: bool) -> None:
        self._wireframe = enabled
        for ais in self._ais_by_id.values():
            self._context.SetDisplayMode(ais, 0 if enabled else 1, False)
        self._view.Redraw()

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        self._view.SetZoom(1.15 if event.angleDelta().y() > 0 else 0.87)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self._press = event.pos()
        self._last = self._press
        if event.button() == Qt.LeftButton:
            self._view.StartRotation(self._press.x(), self._press.y())

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        position = event.pos()
        if event.buttons() & Qt.LeftButton and event.modifiers() == Qt.NoModifier:
            self._view.Rotation(position.x(), position.y())
        elif event.buttons() & Qt.MiddleButton:
            self._view.Pan(position.x() - self._last.x(), self._last.y() - position.y())
        elif event.buttons() & Qt.RightButton:
            self._view.ZoomAtPoint(self._last.x(), position.y(), position.x(), self._last.y())
        else:
            self._context.MoveTo(position.x(), position.y(), self._view, True)
        self._last = position

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        position = event.pos()
        distance = abs(position.x() - self._press.x()) + abs(position.y() - self._press.y())
        if event.button() == Qt.LeftButton and distance < 5:
            self._context.MoveTo(position.x(), position.y(), self._view, True)
            self._context.Select(True)
            self._context.InitSelected()
            if self._context.HasSelectedShape():
                picked = self._context.SelectedShape()
                for entity in self._entities:
                    face_id = entity.face_id_for(picked)
                    if face_id:
                        self.face_picked.emit(entity.id, face_id)
                        break
