"""Image viewer: numpy -> QImage, fit / 1:1 / wheel zoom, pan, scale bar, crosshair, badge,
click-to-image-coordinates (for counting, ROIs and beacons) and simple overlay shapes."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView

from openevos.ui import theme


def to_qimage(arr: np.ndarray) -> QImage:
    a = np.ascontiguousarray(arr)
    if a.ndim == 2:
        if a.dtype != np.uint8:
            raise ValueError("gray QImage expects uint8; map display range first")
        img = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_Grayscale8)
    else:
        img = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_RGB888)
    return img.copy()  # detach from numpy memory


@dataclass
class Marker:
    x: float
    y: float
    color: str
    radius: float = 6.0
    text: str = ""


class ImageView(QGraphicsView):
    clicked = Signal(float, float, object)  # image x, y, Qt.MouseButton
    hovered = Signal(int, int, str)  # image x, y, value text ("" when outside)
    dragged = Signal(float, float, float, float)  # click mode: x0, y0, x1, y1 (image px)
    compare_changed = Signal(bool)  # hold-to-compare (B key) pressed/released

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self._item)
        self.setBackgroundBrush(QColor(theme.STAGE))
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pixel_um: float | None = None
        #: a scale bar is drawn only for a pixel size measured on this instrument; a nominal one is never shown
        #: (operator, 2026-11-18: "we will have to properly put the scale with strict precision")
        self.pixel_calibrated = False
        self.show_scale_bar = True
        self.show_crosshair = False
        self.badge = ""
        self.badge_color = theme.SIM
        #: one line centred on the image, on a dark pill (e.g. "Light off · click a light channel to light it")
        self.hint = ""
        self.watermark = ""
        self.markers: list[Marker] = []
        self.shapes: list[tuple[str, tuple, tuple, str, str]] = []  # kind, p0, p1, colour, label
        self.rects: list[tuple[QRectF, str]] = []
        self._fit = True
        self._size: tuple[int, int] | None = None
        self.placeholder = "No image"
        self.raw = None  # raw array for value readout and loupe
        self.loupe = False
        self._hover: tuple[int, int] | None = None
        self._qimage = None

    # ------------------------------------------------------------------ image
    def set_image(self, arr: np.ndarray | None, raw: np.ndarray | None = None) -> None:
        self.raw = raw if raw is not None else arr
        if arr is None:
            self._item.setPixmap(QPixmap())
            self._size = None
            self._qimage = None
            self.viewport().update()
            return
        new_size = (arr.shape[1], arr.shape[0])
        self._qimage = to_qimage(arr)
        self._item.setPixmap(QPixmap.fromImage(self._qimage))
        if new_size != self._size:
            self._size = new_size
            self.scene().setSceneRect(QRectF(0, 0, *new_size))
            if self._fit:
                self.fit()
        self.viewport().update()

    def has_image(self) -> bool:
        return self._size is not None

    def fit(self) -> None:
        self._fit = True
        if self._size:
            self.fitInView(self._item, Qt.KeepAspectRatio)

    def one_to_one(self) -> None:
        self._fit = False
        self.resetTransform()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._fit:
            self.fit()

    def wheelEvent(self, e):
        self._fit = False
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)

    def mousePressEvent(self, e):
        if self._size and (e.button() == Qt.RightButton or e.modifiers() & Qt.ShiftModifier
                           or self.dragMode() == QGraphicsView.NoDrag):
            p = self.mapToScene(e.position().toPoint())
            if 0 <= p.x() < self._size[0] and 0 <= p.y() < self._size[1]:
                self.clicked.emit(p.x(), p.y(), e.button())
                return
        super().mousePressEvent(e)

    def value_at(self, x: int, y: int) -> str:
        r = self.raw
        if r is None or not (0 <= y < r.shape[0] and 0 <= x < r.shape[1]):
            return ""
        v = r[y, x]
        return "RGB " + ", ".join(str(int(c)) for c in v) if np.ndim(v) else f"{int(v)}"

    def mouseMoveEvent(self, e):
        super().mouseMoveEvent(e)
        if self._size:
            p = self.mapToScene(e.position().toPoint())
            x, y = int(p.x()), int(p.y())
            inside = 0 <= x < self._size[0] and 0 <= y < self._size[1]
            self._hover = (x, y) if inside else None
            self.hovered.emit(x, y, self.value_at(x, y) if inside else "")
            if getattr(self, "_press", None) is not None:
                self._rubber = (self._press, (p.x(), p.y()))
                self.viewport().update()
            if self.loupe:
                self.viewport().update()

    def leaveEvent(self, e):
        self._hover = None
        self.hovered.emit(-1, -1, "")
        self.viewport().update()
        super().leaveEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_B and not e.isAutoRepeat():
            self.compare_changed.emit(True)
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_B and not e.isAutoRepeat():
            self.compare_changed.emit(False)
            return
        super().keyReleaseEvent(e)

    def set_click_mode(self, on: bool) -> None:
        """Click mode: left clicks report image coordinates instead of panning."""
        self.setDragMode(QGraphicsView.NoDrag if on else QGraphicsView.ScrollHandDrag)
        self.viewport().setCursor(Qt.CrossCursor if on else Qt.OpenHandCursor)

    def zoom_factor(self) -> float:
        return self.transform().m11()

    # ------------------------------------------------------------------ overlays
    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        painter.save()
        for r, color in self.rects:
            painter.setPen(QPen(QColor(color), 0))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(r)
        for m in self.markers:
            painter.setPen(QPen(QColor(m.color), 1.5 / max(0.05, self.zoom_factor())))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(m.x, m.y), m.radius, m.radius)
            if m.text:
                painter.setFont(QFont(painter.font().family(), max(6, int(9 / max(0.2, self.zoom_factor())))))
                painter.drawText(QPointF(m.x + m.radius + 2, m.y - m.radius), m.text)
        for kind, a, b, color, text in self.shapes:
            painter.setPen(QPen(QColor(color), 2 / max(0.05, self.zoom_factor())))
            painter.setBrush(Qt.NoBrush)
            r = QRectF(QPointF(*a), QPointF(*b)).normalized()
            if kind in ("rect", "histogram"):
                painter.drawRect(r)
            elif kind == "ellipse":
                painter.drawEllipse(r)
            elif kind in ("line", "arrow"):
                painter.drawLine(QPointF(*a), QPointF(*b))
                if kind == "arrow":
                    import math

                    ang = math.atan2(b[1] - a[1], b[0] - a[0])
                    k = 12 / max(0.2, self.zoom_factor())
                    for sgn in (0.45, -0.45):
                        painter.drawLine(QPointF(*b), QPointF(b[0] - k * math.cos(ang + sgn), b[1] - k * math.sin(ang + sgn)))  # noqa: E501
            if text:
                painter.setFont(QFont(painter.font().family(), max(6, int(9 / max(0.2, self.zoom_factor())))))
                painter.drawText(QPointF(r.left() + 3, r.top() - 4), text)
        rubber = getattr(self, "_rubber", None)
        if rubber:
            painter.setPen(QPen(QColor(theme.ACCENT), 1.5 / max(0.05, self.zoom_factor()), Qt.DashLine))
            painter.drawRect(QRectF(QPointF(*rubber[0]), QPointF(*rubber[1])).normalized())
        painter.restore()
        painter.save()
        painter.resetTransform()
        vw, vh = self.viewport().width(), self.viewport().height()
        if not self._size:
            painter.setPen(QColor(theme.MUTED))
            painter.setFont(QFont(painter.font().family(), 12))
            painter.drawText(QRectF(0, 0, vw, vh), Qt.AlignCenter, self.placeholder)
        if self.show_crosshair and self._size:
            painter.setPen(QPen(QColor(255, 255, 255, 110), 1, Qt.DashLine))
            painter.drawLine(vw // 2, 0, vw // 2, vh)
            painter.drawLine(0, vh // 2, vw, vh // 2)
        if self.badge:
            f = QFont(painter.font().family(), 9, QFont.Bold)
            painter.setFont(f)
            w = painter.fontMetrics().horizontalAdvance(self.badge) + 18
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(self.badge_color)))
            painter.drawRoundedRect(QRectF(12, 12, w, 22), 5, 5)
            painter.setPen(QColor(theme.on(self.badge_color)))
            painter.drawText(QRectF(12, 12, w, 22), Qt.AlignCenter, self.badge)
        if self.hint and self._size:
            f = QFont(painter.font().family())
            f.setPixelSize(14)
            painter.setFont(f)
            w = min(vw - 24, painter.fontMetrics().horizontalAdvance(self.hint) + 32)
            box = QRectF((vw - w) / 2, (vh - 36) / 2, w, 36)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 170))  # the image surround is black in both themes: a dark pill reads
            painter.drawRoundedRect(box, 18, 18)
            painter.setPen(QColor(255, 255, 255, 230))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self.hint)
        if self.watermark:
            painter.setPen(QColor(255, 255, 255, 150))
            painter.setFont(QFont(painter.font().family(), 9))
            painter.drawText(QRectF(12, vh - 26, vw - 24, 20), Qt.AlignLeft | Qt.AlignVCenter, self.watermark)
        if self.show_scale_bar and self.pixel_um and self.pixel_calibrated and self._size:
            um_per_view_px = self.pixel_um / max(1e-9, self.zoom_factor())
            target = um_per_view_px * vw * 0.18
            nice = _nice(target)
            length = nice / um_per_view_px
            x1, y = vw - 24, vh - 22
            painter.setPen(QPen(QColor("white"), 4))
            painter.drawLine(int(x1 - length), y, x1, y)
            painter.setFont(QFont(painter.font().family(), 10, QFont.Bold))
            label = f"{nice:g} µm" if nice < 1000 else f"{nice / 1000:g} mm"
            painter.drawText(QRectF(x1 - length - 40, y - 24, length + 40, 18), Qt.AlignRight | Qt.AlignVCenter, label)
        if self.loupe and self._hover and self._qimage is not None:
            hx, hy = self._hover
            n = 21  # source pixels shown (odd -> centred), 8 px each
            src = self._qimage.copy(hx - n // 2, hy - n // 2, n, n).scaled(168, 168, Qt.IgnoreAspectRatio,
                                                                         Qt.FastTransformation)
            box = QRectF(vw - 180, 44, 168, 168)
            painter.fillRect(box, QColor(theme.STAGE))
            painter.drawImage(box, src)
            painter.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(box)
            c = box.center()
            painter.setPen(QPen(QColor(255, 255, 255, 200), 1))
            painter.drawRect(QRectF(c.x() - 4, c.y() - 4, 8, 8))
            painter.fillRect(QRectF(box.left(), box.bottom(), 168, 20), QColor(theme.PANEL_2))
            painter.setPen(QColor(theme.TEXT))
            painter.setFont(QFont("JetBrains Mono", 9))
            painter.drawText(QRectF(box.left() + 6, box.bottom(), 162, 20), Qt.AlignLeft | Qt.AlignVCenter,
                             f"{hx},{hy}  {self.value_at(hx, hy)}")
        painter.restore()


def _nice(v: float) -> float:
    if v <= 0:
        return 1.0
    import math

    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if m * e >= v:
            return float(m * e)
    return float(10 * e)
