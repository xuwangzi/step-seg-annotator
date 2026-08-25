"""Minimal native Qt/OpenCascade face-selection viewport."""

from __future__ import annotations

import sys
from ctypes import c_char_p, c_void_p, py_object, pythonapi
from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QWidget

from OCP.AIS import AIS_ColoredShape, AIS_InteractiveContext, AIS_Shape
from OCP.Aspect import Aspect_DisplayConnection
from OCP.OpenGl import OpenGl_GraphicDriver
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.TopAbs import TopAbs_FACE
from OCP.V3d import V3d_Viewer

from .topology import ImportedBody


class OccViewport(QWidget):
    face_picked = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_PaintOnScreen)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self._initialized = False
        self._press = QPoint()
        self._last = QPoint()
        self._rotating = False
        self._bodies: list[ImportedBody] = []
        self._ais_by_body: dict[str, AIS_ColoredShape] = {}
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
        """Convert Qt's native view handle to the PyCapsule requested by OCP.

        PySide6 exposes ``winId`` as an integer on macOS and Windows, whereas
        OCP's native-window constructors deliberately accept a Python capsule.
        """
        capsule_new = pythonapi.PyCapsule_New
        capsule_new.argtypes = [c_void_p, c_char_p, c_void_p]
        capsule_new.restype = py_object
        return capsule_new(c_void_p(int(self.winId())), None, None)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
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

    def clear(self) -> None:
        self._context.EraseAll(True)
        self._context.RemoveAll(True)
        self._ais_by_body.clear()
        self._bodies.clear()

    def display_bodies(self, bodies: list[ImportedBody]) -> None:
        self.clear()
        self._bodies = bodies
        selection_mode = AIS_Shape.SelectionMode_s(TopAbs_FACE)
        for body in bodies:
            ais = AIS_ColoredShape(body.shape)
            self._context.Display(ais, True)
            self._context.Activate(ais, selection_mode, True)
            self._ais_by_body[body.id] = ais
        self.fit_all()

    def set_face_colors(self, colors: dict[str, str]) -> None:
        for body in self._bodies:
            ais = self._ais_by_body[body.id]
            for face_id, face in body.faces.items():
                color = colors.get(face_id, "#8B8B8B")
                ais.SetCustomColor(face, self._occ_color(color))
            self._context.Redisplay(ais, False)
        if self._initialized:
            self._view.Redraw()

    @staticmethod
    def _occ_color(hex_color: str) -> Quantity_Color:
        normalized = hex_color.lstrip("#")
        rgb = tuple(int(normalized[index : index + 2], 16) / 255 for index in (0, 2, 4))
        return Quantity_Color(*rgb, Quantity_TOC_RGB)

    def fit_all(self) -> None:
        self._view.FitAll()
        self._view.ZFitAll()
        if self._initialized:
            self._view.Redraw()

    def set_view(self, direction: tuple[float, float, float]) -> None:
        self._view.SetProj(*direction)
        self._view.Redraw()

    def toggle_wireframe(self, enabled: bool) -> None:
        for ais in self._ais_by_body.values():
            if enabled:
                self._context.SetDisplayMode(ais, 0, False)
            else:
                self._context.SetDisplayMode(ais, 1, False)
        self._view.Redraw()

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        self._view.SetZoom(1.15 if event.angleDelta().y() > 0 else 0.87)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self._press = event.position().toPoint()
        self._last = self._press
        self._rotating = event.button() == Qt.LeftButton
        if self._rotating:
            self._view.StartRotation(self._press.x(), self._press.y())

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        position = event.position().toPoint()
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
        position = event.position().toPoint()
        distance = abs(position.x() - self._press.x()) + abs(position.y() - self._press.y())
        if event.button() == Qt.LeftButton and distance < 5:
            self._context.MoveTo(position.x(), position.y(), self._view, True)
            self._context.Select(True)
            self._context.InitSelected()
            if self._context.HasSelectedShape():
                picked = self._context.SelectedShape()
                for body in self._bodies:
                    face_id = body.face_id_for(picked)
                    if face_id:
                        self.face_picked.emit(body.id, face_id)
                        break
        self._rotating = False
