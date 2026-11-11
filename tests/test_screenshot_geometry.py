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


# ---- 多屏拼合：逻辑选框 → 各屏物理像素区域 + 拼合图尺寸 ----

from meeting_translate.capture import plan_stitch  # noqa: E402


def test_single_screen_at_100_percent_is_a_plain_crop():
    stitch = plan_stitch(QRect(100, 200, 300, 150), [(QRect(0, 0, 1920, 1080), 1.0)])
    assert stitch.size == (300, 150)
    assert [(p.screen, p.source, p.target) for p in stitch.pieces] == [(0, QRect(100, 200, 300, 150), QRect(0, 0, 300, 150))]


def test_single_screen_at_150_percent_maps_to_physical_pixels():
    stitch = plan_stitch(QRect(10, 20, 200, 100), [(QRect(0, 0, 1920, 1080), 1.5)])
    assert stitch.size == (300, 150)
    assert [(p.source, p.target) for p in stitch.pieces] == [(QRect(15, 30, 300, 150), QRect(0, 0, 300, 150))]


def test_selection_across_two_100_percent_screens_is_split_and_placed_side_by_side():
    screens = [(QRect(0, 0, 1000, 1080), 1.0), (QRect(1000, 0, 1000, 1080), 1.0)]
    stitch = plan_stitch(QRect(900, 100, 200, 50), screens)
    assert stitch.size == (200, 50)
    assert [(p.screen, p.source, p.target) for p in stitch.pieces] == [
        (0, QRect(900, 100, 100, 50), QRect(0, 0, 100, 50)),
        (1, QRect(0, 100, 100, 50), QRect(100, 0, 100, 50)),
    ]


def test_selection_across_100_and_150_percent_screens_uses_the_larger_scale_for_the_canvas():
    screens = [(QRect(0, 0, 1000, 1000), 1.0), (QRect(1000, 0, 1000, 1000), 1.5)]
    stitch = plan_stitch(QRect(900, 100, 200, 50), screens)
    assert stitch.size == (300, 75)
    assert [(p.screen, p.source, p.target) for p in stitch.pieces] == [
        (0, QRect(900, 100, 100, 50), QRect(0, 0, 150, 75)),
        (1, QRect(0, 150, 150, 75), QRect(150, 0, 150, 75)),
    ]


def test_left_screen_with_negative_virtual_origin_at_150_percent_next_to_primary_at_100_percent():
    screens = [(QRect(-1920, 0, 1920, 1080), 1.5), (QRect(0, 0, 1920, 1080), 1.0)]
    stitch = plan_stitch(QRect(-100, 500, 300, 100), screens)
    assert stitch.size == (450, 150)
    assert [(p.screen, p.source, p.target) for p in stitch.pieces] == [
        (0, QRect(2730, 750, 150, 150), QRect(0, 0, 150, 150)),
        (1, QRect(0, 500, 200, 100), QRect(150, 0, 300, 150)),
    ]


def test_selection_outside_every_screen_has_no_pieces():
    stitch = plan_stitch(QRect(5000, 5000, 100, 100), [(QRect(0, 0, 1920, 1080), 1.0)])
    assert stitch.pieces == ()
