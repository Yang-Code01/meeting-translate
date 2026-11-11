"""截图框选的纯几何：逻辑矩形 → 物理像素矩形，以及取消判定。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402

from meeting_translate.capture import is_drag_selection, physical_rect  # noqa: E402


def test_selection_on_primary_screen_at_100_percent_scales_nothing():
    screen = QRect(0, 0, 1920, 1080)
    assert physical_rect(QRect(100, 200, 300, 150), screen, 1.0) == QRect(100, 200, 300, 150)


def test_selection_on_screen_with_negative_virtual_origin_is_relative_to_that_screen_at_150_percent():
    # 左侧副屏：虚拟桌面坐标 x 从 -1920 开始；DPR 1.5
    screen = QRect(-1920, 0, 1920, 1080)
    assert physical_rect(QRect(-1900, 100, 200, 50), screen, 1.5) == QRect(30, 150, 300, 75)


def test_physical_rect_is_clipped_to_the_screen():
    screen = QRect(0, 0, 1000, 500)
    assert physical_rect(QRect(900, 400, 300, 300), screen, 1.0) == QRect(900, 400, 100, 100)


def test_a_click_without_drag_is_not_a_selection():
    assert not is_drag_selection(QRect(50, 50, 1, 1))
    assert not is_drag_selection(QRect(50, 50, 3, 80))
    assert not is_drag_selection(QRect(50, 50, 80, 3))


def test_a_drag_of_four_pixels_in_both_directions_is_a_selection():
    assert is_drag_selection(QRect(50, 50, 4, 4))
