"""截图翻译的区域截取：按下热键时先截取每块屏幕，再在每块屏幕上显示压暗遮罩供拖框；截取结果只在内存中。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

_DIM = QColor(0, 0, 0, 120)
# 框选宽或高小于该像素数视为误触（单击），取消
_MIN_DRAG_PX = 4


def physical_rect(selection: QRect, screen: QRect, dpr: float) -> QRect:
    """把虚拟桌面上的逻辑矩形换算成该屏幕截图里的物理像素矩形（超出屏幕的部分裁掉）。"""
    local = QRect(selection.x() - screen.x(), selection.y() - screen.y(), selection.width(), selection.height())
    local = local.intersected(QRect(0, 0, screen.width(), screen.height()))
    return QRect(
        round(local.x() * dpr),
        round(local.y() * dpr),
        round(local.width() * dpr),
        round(local.height() * dpr),
    )


@dataclass(frozen=True)
class ScreenPiece:
    screen: int  # 屏幕在输入列表中的下标
    source: QRect  # 该屏截图中的物理像素区域
    target: QRect  # 拼合图中的像素区域


@dataclass(frozen=True)
class Stitch:
    size: tuple[int, int]  # 拼合图 (宽, 高)，物理像素
    pieces: tuple[ScreenPiece, ...]


def plan_stitch(selection: QRect, screens: Sequence[tuple[QRect, float]]) -> Stitch:
    """逻辑选框 → 各屏物理像素区域与拼合图布局。screens 为 (屏幕逻辑几何, 缩放比例) 列表，坐标可为负。

    拼合图按所涉屏幕中最大的缩放比例取像素密度，其它屏幕的区域在拼合图里按比例放大/缩小。
    与选框不相交的屏幕不参与；选框完全不在任何屏幕上时没有 pieces。
    """
    hits = [(i, geom, dpr) for i, (geom, dpr) in enumerate(screens) if not selection.intersected(geom).isEmpty()]
    if not hits:
        return Stitch((0, 0), ())
    scale = max(dpr for _, _, dpr in hits)
    pieces = []
    for i, geom, dpr in hits:
        inter = selection.intersected(geom)
        target = QRect(
            round((inter.x() - selection.x()) * scale),
            round((inter.y() - selection.y()) * scale),
            round(inter.width() * scale),
            round(inter.height() * scale),
        )
        pieces.append(ScreenPiece(i, physical_rect(selection, geom, dpr), target))
    size = (round(selection.width() * scale), round(selection.height() * scale))
    return Stitch(size, tuple(pieces))


def stitch_images(stitch: Stitch, images: Sequence[QImage]) -> QImage:
    """把各屏截图（与 plan_stitch 的屏幕列表同序）按 Stitch 布局拼成一张 RGB888 图，未覆盖处为黑色。"""
    canvas = QImage(stitch.size[0], stitch.size[1], QImage.Format.Format_RGB888)
    canvas.fill(QColor(0, 0, 0))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    for piece in stitch.pieces:
        painter.drawImage(piece.target, images[piece.screen], piece.source)
    painter.end()
    return canvas


def is_drag_selection(rect: QRect) -> bool:
    """宽和高都不小于 _MIN_DRAG_PX 才算有效框选；单击或极小框视为取消。"""
    return rect.width() >= _MIN_DRAG_PX and rect.height() >= _MIN_DRAG_PX


def image_to_rgb_array(image: QImage) -> np.ndarray:
    """QImage → RGB uint8 数组（H, W, 3），复制一份，不依赖 QImage 生命周期。"""
    img = image.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    ptr = img.constBits()
    rows = np.frombuffer(ptr, dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return rows[:, : w * 3].reshape(h, w, 3).copy()


def _grab_screen(screen) -> QPixmap:
    return screen.grabWindow(0)


def _all_screens() -> list:
    return list(QApplication.screens())


class RegionOverlay(QWidget):
    """覆盖一块屏幕的压暗遮罩：显示该屏快照，拖出的矩形（可能跨屏，由控制器同步给所有遮罩）保持原亮度。

    遮罩只转发鼠标/键盘事件（全局逻辑坐标），不自行决定框选结束。
    """

    pressed = Signal(QPoint)  # 全局逻辑坐标
    moved = Signal(QPoint)
    released = Signal(QPoint)
    cancelled = Signal()

    def __init__(self, snapshot: QPixmap, geometry: QRect) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool,
        )
        self._snapshot = snapshot
        self._selection: QRect | None = None  # 全局逻辑坐标
        self.setGeometry(geometry)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_selection(self, rect: QRect | None) -> None:
        self._selection = rect
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self._snapshot)
        painter.fillRect(self.rect(), _DIM)
        if self._selection is not None:
            sel = self._selection.translated(-self.geometry().topLeft()).intersected(self.rect())
            if not sel.isEmpty():
                painter.setClipRect(sel)
                painter.drawPixmap(self.rect(), self._snapshot)
                painter.setClipping(False)
                painter.setPen(QPen(QColor(255, 255, 255), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(sel.adjusted(0, 0, -1, -1))
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.pressed.emit(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event) -> None:
        self.moved.emit(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.released.emit(event.globalPosition().toPoint())

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()


class RegionCapture(QObject):
    """截图流程控制：start() 先截取每块屏幕再在每块屏幕上显示遮罩；松开后按选框跨屏拼合出内存中的 QImage。"""

    captured = Signal(object, QPoint)  # QImage (RGB888), 选框右下角（全局逻辑坐标，作为弹窗锚点）
    cancelled = Signal()

    def __init__(
        self,
        parent=None,
        snapshot: Callable[[object], QPixmap] = _grab_screen,
        screens: Callable[[], list] = _all_screens,
    ) -> None:
        super().__init__(parent)
        self._snapshot = snapshot
        self._screens = screens
        self._overlays: list[RegionOverlay] = []
        self._pixmaps: list[QPixmap] = []
        self._screen_list: list = []
        self._origin: QPoint | None = None
        self._current = QPoint()

    def start(self) -> bool:
        """已有遮罩在显示时返回 False。每块屏幕一个遮罩，覆盖全部显示器。"""
        if self._overlays:
            return False
        screens = list(self._screens())
        # 快照必须先于所有遮罩出现，否则截到的是遮罩本身
        pixmaps = [self._snapshot(screen) for screen in screens]
        self._screen_list = screens
        self._pixmaps = pixmaps
        self._origin = None
        self._overlays = []
        for screen, pixmap in zip(screens, pixmaps):
            overlay = RegionOverlay(pixmap, screen.geometry())
            overlay.pressed.connect(self._on_pressed)
            overlay.moved.connect(self._on_moved)
            overlay.released.connect(self._on_released)
            overlay.cancelled.connect(self._on_cancelled)
            self._overlays.append(overlay)
        focus = self._overlay_under_cursor()
        for overlay in self._overlays:
            overlay.show()
        if focus is not None:
            focus.raise_()
            focus.activateWindow()
            focus.setFocus()
        return True

    def cancel(self) -> None:
        """静默关闭遮罩（退出程序时使用），不发出 cancelled。"""
        self._close_all()

    def _overlay_under_cursor(self) -> RegionOverlay | None:
        pos = QCursor.pos()
        for screen, overlay in zip(self._screen_list, self._overlays):
            if screen.geometry().contains(pos):
                return overlay
        return self._overlays[0] if self._overlays else None

    def _refresh(self) -> None:
        rect = QRect(self._origin, self._current).normalized() if self._origin is not None else None
        for overlay in self._overlays:
            overlay.set_selection(rect)

    def _on_pressed(self, pos: QPoint) -> None:
        self._origin = pos
        self._current = pos
        self._refresh()

    def _on_moved(self, pos: QPoint) -> None:
        if self._origin is not None:
            self._current = pos
            self._refresh()

    def _on_released(self, pos: QPoint) -> None:
        if self._origin is None:
            return
        rect = QRect(self._origin, pos).normalized()
        self._origin = None
        if is_drag_selection(rect):
            self._on_selected(rect)
        else:
            self._on_cancelled()

    def _on_cancelled(self) -> None:
        self._close_all()
        self.cancelled.emit()

    def _on_selected(self, rect: QRect) -> None:
        pixmaps, screens = self._pixmaps, self._screen_list
        self._close_all()
        stitch = plan_stitch(rect, [(screen.geometry(), screen.devicePixelRatio()) for screen in screens])
        if not stitch.pieces:
            self.cancelled.emit()
            return
        image = stitch_images(stitch, [pixmap.toImage() for pixmap in pixmaps])
        self.captured.emit(image, rect.bottomRight())

    def _close_all(self) -> None:
        overlays = self._overlays
        self._overlays = []
        self._pixmaps = []
        self._screen_list = []
        self._origin = None
        for overlay in overlays:
            overlay.hide()
            overlay.close()
            overlay.deleteLater()
