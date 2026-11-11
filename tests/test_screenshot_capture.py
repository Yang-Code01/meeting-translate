"""区域截取：快照先于遮罩、拖框确认、取消（点击/Esc）。使用 offscreen Qt。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate.capture import RegionCapture, RegionOverlay, image_to_rgb_array  # noqa: E402


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def overlays_visible() -> list:
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, RegionOverlay) and w.isVisible()]


def make_capture(qapp, log: list):
    def snapshot(screen):
        # 快照时遮罩尚未出现
        log.append(("snapshot", len(overlays_visible())))
        return QPixmap(qapp.primaryScreen().geometry().size())

    capture = RegionCapture(snapshot=snapshot)
    results: list = []
    capture.captured.connect(lambda image, anchor: results.append(("captured", image, anchor)))
    capture.cancelled.connect(lambda: results.append(("cancelled",)))
    return capture, results


def test_snapshot_is_taken_before_the_overlay_appears(qapp):
    log: list = []
    capture, _ = make_capture(qapp, log)
    assert capture.start()
    assert log == [("snapshot", 0)]
    assert len(overlays_visible()) == 1
    capture.cancel()


def test_dragging_and_releasing_confirms_the_selected_region_in_memory(qapp):
    log: list = []
    capture, results = make_capture(qapp, log)
    capture.start()
    overlay = overlays_visible()[0]
    origin = overlay.rect().topLeft() + QPoint(10, 20)
    end = overlay.rect().topLeft() + QPoint(109, 69)
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, origin)
    QTest.mouseMove(overlay, end)
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    kind, image, anchor = results[0]
    assert kind == "captured"
    assert (image.width(), image.height()) == (100, 50)
    assert anchor == end  # 锚点是选框右下角
    assert overlays_visible() == []


def test_a_click_without_drag_cancels(qapp):
    capture, results = make_capture(qapp, [])
    capture.start()
    overlay = overlays_visible()[0]
    p = overlay.rect().topLeft() + QPoint(30, 30)
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
    assert results == [("cancelled",)]
    assert overlays_visible() == []


def test_escape_cancels_and_a_new_capture_can_start_afterwards(qapp):
    capture, results = make_capture(qapp, [])
    capture.start()
    overlay = overlays_visible()[0]
    QTest.keyClick(overlay, Qt.Key.Key_Escape)
    assert results == [("cancelled",)]
    assert capture.start()
    capture.cancel()


def test_captured_pixels_come_from_the_snapshot_not_from_the_dimmed_overlay(qapp):
    red = QPixmap(qapp.primaryScreen().geometry().size())
    red.fill(QColor(255, 0, 0))
    capture = RegionCapture(snapshot=lambda screen: red)
    results: list = []
    capture.captured.connect(lambda image, anchor: results.append(image))
    capture.start()
    overlay = overlays_visible()[0]
    origin = overlay.rect().topLeft() + QPoint(10, 10)
    end = overlay.rect().topLeft() + QPoint(19, 19)
    QTest.mousePress(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, origin)
    QTest.mouseRelease(overlay, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
    assert image_to_rgb_array(results[0])[0, 0].tolist() == [255, 0, 0]


def test_image_to_rgb_array_keeps_channel_order_and_size():
    img = QImage(3, 2, QImage.Format.Format_ARGB32)
    img.fill(QColor(10, 20, 30))
    arr = image_to_rgb_array(img)
    assert arr.shape == (2, 3, 3)
    assert arr[1, 2].tolist() == [10, 20, 30]


def fake_screen(x: int, y: int, w: int, h: int, color: QColor):
    """假屏幕：只提供 geometry / devicePixelRatio，快照是纯色图。"""
    from types import SimpleNamespace

    geometry = QRect(x, y, w, h)
    return SimpleNamespace(geometry=lambda: geometry, devicePixelRatio=lambda: 1.0, color=color)


def solid_snapshot(screen) -> QPixmap:
    pixmap = QPixmap(screen.geometry().size())
    pixmap.fill(screen.color)
    return pixmap


def test_one_overlay_per_screen_is_aligned_with_that_screen(qapp):
    left = fake_screen(-100, 0, 100, 80, QColor(255, 0, 0))
    right = fake_screen(0, 0, 120, 80, QColor(0, 0, 255))
    capture = RegionCapture(snapshot=solid_snapshot, screens=lambda: [left, right])
    capture.start()
    overlays = sorted(overlays_visible(), key=lambda o: o.geometry().x())
    assert [o.geometry() for o in overlays] == [QRect(-100, 0, 100, 80), QRect(0, 0, 120, 80)]
    capture.cancel()


def test_a_selection_dragged_across_two_screens_is_stitched_from_both_snapshots(qapp):
    left = fake_screen(0, 0, 100, 100, QColor(255, 0, 0))
    right = fake_screen(100, 0, 100, 100, QColor(0, 0, 255))
    capture = RegionCapture(snapshot=solid_snapshot, screens=lambda: [left, right])
    results: list = []
    capture.captured.connect(lambda image, anchor: results.append((image, anchor)))
    capture.start()
    first = next(o for o in overlays_visible() if o.geometry().x() == 0)
    # 按下在左屏，松开在右屏：鼠标隐式抓取，事件仍投递给左屏遮罩（局部坐标可越界）
    QTest.mousePress(first, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(90, 10))
    QTest.mouseMove(first, QPoint(109, 19))
    QTest.mouseRelease(first, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(109, 19))
    image, anchor = results[0]
    assert (image.width(), image.height()) == (20, 10)
    rgb = image_to_rgb_array(image)
    assert rgb[0, 0].tolist() == [255, 0, 0]  # 左屏部分
    assert rgb[0, 19].tolist() == [0, 0, 255]  # 右屏部分
    assert anchor == QPoint(109, 19)
    assert overlays_visible() == []


def test_screen_index_at_picks_the_screen_containing_the_point():
    from meeting_translate.capture import screen_index_at

    geometries = [QRect(-100, 0, 100, 80), QRect(0, 0, 120, 80)]
    assert screen_index_at(QPoint(-50, 10), geometries) == 0
    assert screen_index_at(QPoint(60, 10), geometries) == 1
    assert screen_index_at(QPoint(500, 500), geometries) is None


def test_keyboard_focus_follows_the_cursor_to_another_screen(qapp):
    # Esc 只在持有键盘焦点的遮罩上生效：光标跨屏后焦点必须跟过去
    left = fake_screen(0, 0, 100, 100, QColor(255, 0, 0))
    right = fake_screen(100, 0, 100, 100, QColor(0, 0, 255))
    capture = RegionCapture(snapshot=solid_snapshot, screens=lambda: [left, right])
    results: list = []
    capture.cancelled.connect(lambda: results.append("cancelled"))
    capture.start()
    left_overlay = next(o for o in overlays_visible() if o.geometry().x() == 0)
    right_overlay = next(o for o in overlays_visible() if o.geometry().x() == 100)
    assert capture.focused_overlay is left_overlay
    right_overlay.moved.emit(QPoint(150, 10))
    assert capture.focused_overlay is right_overlay
    QTest.keyClick(right_overlay, Qt.Key.Key_Escape)
    assert results == ["cancelled"]
