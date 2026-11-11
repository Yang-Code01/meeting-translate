"""OCR 尺寸归一：期望值来自规格（2000/400 阈值、2 倍放大、等比缩小），不依赖实现。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate.ocr import normalize_size  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


def solid(w: int, h: int, rgb: tuple[int, int, int]) -> np.ndarray:
    return np.full((h, w, 3), rgb, dtype=np.uint8)


def test_small_solid_region_is_upscaled_twice_with_the_same_colour():
    out = normalize_size(solid(100, 50, (10, 20, 30)))
    assert out.shape == (100, 200, 3)
    assert (out == [10, 20, 30]).all()


def test_long_side_above_2000_is_scaled_down_to_2000_keeping_aspect_ratio():
    out = normalize_size(solid(2400, 10, (200, 100, 50)))
    assert out.shape == (8, 2000, 3)
    assert (out == [200, 100, 50]).all()


def test_mid_sized_region_is_returned_unchanged():
    image = solid(500, 200, (1, 2, 3))
    assert normalize_size(image) is image


def test_left_and_right_halves_keep_their_colours_after_upscaling():
    image = solid(100, 20, (255, 0, 0))
    image[:, 50:] = (0, 0, 255)
    out = normalize_size(image)
    assert out.shape == (40, 200, 3)
    assert out[0, 0].tolist() == [255, 0, 0]
    assert out[0, -1].tolist() == [0, 0, 255]
    assert out[-1, 0].tolist() == [255, 0, 0]


def test_default_engine_is_built_once_even_when_called_from_several_threads(monkeypatch):
    import threading
    import time

    from meeting_translate import ocr

    built: list = []

    class SlowEngine:
        def __init__(self) -> None:
            time.sleep(0.05)  # 拉长 check-then-set 的竞争窗口
            built.append(self)

    monkeypatch.setattr(ocr, "_ENGINE", None)
    monkeypatch.setattr(ocr, "RapidOcrEngine", SlowEngine)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(ocr.default_engine())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
    assert all(engine is built[0] for engine in results)
