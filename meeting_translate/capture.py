"""截图翻译的区域截取：按下热键时先截屏，再显示压暗遮罩供拖框；截取结果只在内存中。"""

from __future__ import annotations

from collections.abc import Callable

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


class RegionOverlay(QWidget):
    """覆盖一块屏幕的压暗遮罩：显示快照，拖出的矩形保持原亮度。松开即确认，Esc 或误触取消。"""

    selected = Signal(QRect)  # 全局逻辑坐标
    cancelled = Signal()

    def __init__(self, snapshot: QPixmap, geometry: QRect) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool,
        )
        self._snapshot = snapshot
        self._origin: QPoint | None = None
        self._current = QPoint()
        self._finished = False
        self.setGeometry(geometry)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _local_selection(self) -> QRect | None:
        if self._origin is None:
            return None
        return QRect(self._origin, self._current).normalized()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self._snapshot)
        painter.fillRect(self.rect(), _DIM)
        sel = self._local_selection()
        if sel is not None:
            painter.setClipRect(sel)
            painter.drawPixmap(self.rect(), self._snapshot)
            painter.setClipping(False)
            painter.setPen(QPen(QColor(255, 255, 255), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(sel.adjusted(0, 0, -1, -1))
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._origin is None or event.button() != Qt.MouseButton.LeftButton:
            return
        rect = QRect(self._origin, event.position().toPoint()).normalized()
        self._origin = None
        if is_drag_selection(rect):
            self._finish(rect.translated(self.geometry().topLeft()))
        else:
            self._finish(None)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)

    def _finish(self, rect: QRect | None) -> None:
        if self._finished:
            return
        self._finished = True
        self.hide()
        self.close()
        if rect is None:
            self.cancelled.emit()
        else:
            self.selected.emit(rect)


class RegionCapture(QObject):
    """截图流程控制：start() 先截屏再显示遮罩；确认后从快照里截出选框内容（内存中的 QImage）。"""

    captured = Signal(object, QPoint)  # QImage (RGB888), 选框右下角（全局逻辑坐标，作为弹窗锚点）
    cancelled = Signal()

    def __init__(self, parent=None, snapshot: Callable[[object], QPixmap] = _grab_screen) -> None:
        super().__init__(parent)
        self._snapshot = snapshot
        self._overlay: RegionOverlay | None = None
        self._pixmap: QPixmap | None = None
        self._screen = None

    def start(self) -> bool:
        """已有遮罩在显示时返回 False。目前只支持光标所在的单屏。"""
        if self._overlay is not None:
            return False
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        # 快照必须先于遮罩出现，否则截到的是遮罩本身
        pixmap = self._snapshot(screen)
        self._pixmap = pixmap
        self._screen = screen
        overlay = RegionOverlay(pixmap, screen.geometry())
        overlay.selected.connect(self._on_selected)
        overlay.cancelled.connect(self._on_cancelled)
        self._overlay = overlay
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()
        overlay.setFocus()
        return True

    def cancel(self) -> None:
        """静默关闭遮罩（退出程序时使用），不发出 cancelled。"""
        overlay = self._overlay
        self._reset()
        if overlay is not None:
            overlay.hide()
            overlay.close()
            overlay.deleteLater()

    def _reset(self) -> None:
        self._overlay = None
        self._pixmap = None
        self._screen = None

    def _on_cancelled(self) -> None:
        self._close_current()
        self.cancelled.emit()

    def _on_selected(self, rect: QRect) -> None:
        pixmap, screen = self._pixmap, self._screen
        self._close_current()
        if pixmap is None or screen is None:
            return
        phys = physical_rect(rect, screen.geometry(), screen.devicePixelRatio())
        image = pixmap.toImage().copy(phys).convertToFormat(QImage.Format.Format_RGB888)
        self.captured.emit(image, rect.bottomRight())

    def _close_current(self) -> None:
        overlay = self._overlay
        self._reset()
        if overlay is not None:
            overlay.deleteLater()
