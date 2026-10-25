"""窗口摆放：屏幕外的锚点不能把对话框带出屏幕。"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize

from meeting_translate.ui import place_beside


def test_place_beside_pulls_dialog_back_when_anchor_is_offscreen():
    screen = QRect(0, 0, 1707, 960)
    anchor = QRect(3438, 763, 770, 401)  # 悬浮窗落在已拔掉的显示器上
    size = QSize(760, 620)
    point = place_beside(anchor, size, screen)
    assert screen.contains(QRect(point, size))


def test_place_beside_centers_when_window_does_not_fit():
    screen = QRect(0, 0, 800, 500)
    anchor = QRect(10, 10, 780, 480)
    size = QSize(760, 620)
    point = place_beside(anchor, size, screen)
    assert point.x() >= screen.left()
    assert point.y() >= screen.top()
