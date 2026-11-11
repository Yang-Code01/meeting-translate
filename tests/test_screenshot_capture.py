"""区域截取：快照先于遮罩、拖框确认、取消（点击/Esc）。使用 offscreen Qt。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
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
