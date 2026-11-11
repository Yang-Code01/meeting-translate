"""截图→文本流水线：用假 OCR 引擎做测试，不加载真实模型。"""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace

import numpy as np

from meeting_translate import ocr
from meeting_translate.ocr import OcrLine


class FakeEngine:
    def __init__(self, lines=()) -> None:
        self.lines = list(lines)
        self.threads: list[threading.Thread] = []

    def recognize(self, image):
        self.threads.append(threading.current_thread())
        return self.lines


def line(text: str, y: int = 0) -> OcrLine:
    return OcrLine(text, 0.95, ((0, y), (10, y), (10, y + 5), (0, y + 5)))


def blank() -> np.ndarray:
    return np.full((20, 40, 3), 255, dtype=np.uint8)


def test_recognized_lines_are_joined_in_order_with_newlines():
    engine = FakeEngine([line("こんにちは", 0), line("世界", 10)])
    assert ocr.recognize_text(blank(), engine) == "こんにちは\n世界"


def test_blank_lines_are_dropped_and_no_lines_gives_empty_text():
    assert ocr.recognize_text(blank(), FakeEngine([line("  ", 0), line("日本語", 10)])) == "日本語"
    assert ocr.recognize_text(blank(), FakeEngine([])) == ""


def test_recognition_runs_on_a_worker_thread_not_the_caller_thread():
    engine = FakeEngine([line("日本語")])
    assert ocr.submit_recognition(blank(), engine).result() == "日本語"
    assert engine.threads[0] is not threading.main_thread()
    assert engine.threads[0] is not threading.current_thread()


def test_all_recognitions_share_one_worker_thread():
    engine = FakeEngine([line("日本語")])
    for _ in range(3):
        ocr.submit_recognition(blank(), engine).result()
    assert len({t.ident for t in engine.threads}) == 1


def test_rapidocr_is_built_once_with_thread_count_and_detection_limits(monkeypatch):
    built: dict = {}

    class FakeRapidOCR:
        def __init__(self, params=None) -> None:
            built["params"] = params

        def __call__(self, img):
            built["shape"] = img.shape
            return SimpleNamespace(txts=None, scores=None, boxes=None)

    monkeypatch.setitem(sys.modules, "rapidocr", types.SimpleNamespace(RapidOCR=FakeRapidOCR))
    engine = ocr.RapidOcrEngine()
    assert engine.recognize(blank()) == []
    assert built["params"] == {
        "Det.limit_type": "max",
        "Det.limit_side_len": 960,
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
    }
    assert built["shape"] == (20, 40, 3)


class ShapeRecordingEngine:
    """记录传给 OCR 引擎的图像，用于断言尺寸归一结果。"""

    def __init__(self) -> None:
        self.images: list[np.ndarray] = []

    def recognize(self, image):
        self.images.append(image)
        return []


def solid(w: int, h: int, value: int = 200) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def test_long_side_over_2000_is_scaled_down_proportionally():
    engine = ShapeRecordingEngine()
    ocr.recognize_text(solid(3000, 1500), engine)
    assert engine.images[0].shape == (1000, 2000, 3)


def test_portrait_selection_over_2000_is_scaled_by_its_long_side():
    engine = ShapeRecordingEngine()
    ocr.recognize_text(solid(1500, 3000), engine)
    assert engine.images[0].shape == (2000, 1000, 3)


def test_long_side_of_exactly_2000_is_left_alone():
    engine = ShapeRecordingEngine()
    ocr.recognize_text(solid(2000, 800), engine)
    assert engine.images[0].shape == (800, 2000, 3)


def test_long_side_under_400_is_doubled():
    engine = ShapeRecordingEngine()
    ocr.recognize_text(solid(300, 200), engine)
    assert engine.images[0].shape == (400, 600, 3)


def test_long_side_of_exactly_400_is_left_alone():
    engine = ShapeRecordingEngine()
    ocr.recognize_text(solid(400, 200), engine)
    assert engine.images[0].shape == (200, 400, 3)


def test_typical_selection_is_passed_through_at_its_own_size():
    engine = ShapeRecordingEngine()
    ocr.recognize_text(solid(1000, 600), engine)
    assert engine.images[0].shape == (600, 1000, 3)


def test_normalization_only_resizes_and_does_not_binarize_or_sharpen():
    # 纯色图放大后仍为原色（不二值化、不锐化）；灰度中间值保持不变
    engine = ShapeRecordingEngine()
    ocr.recognize_text(solid(300, 200, value=128), engine)
    out = engine.images[0]
    assert out.dtype == np.uint8
    assert np.all(out == 128)


def test_scale_factor_rule_is_a_pure_function_of_the_size():
    assert ocr.ocr_scale(3000, 1500) == 2000 / 3000
    assert ocr.ocr_scale(300, 200) == 2.0
    assert ocr.ocr_scale(1000, 600) == 1.0
    assert ocr.ocr_scale(2000, 2000) == 1.0
    assert ocr.ocr_scale(400, 100) == 1.0


def test_warm_up_returns_without_waiting_for_the_model(monkeypatch):
    release = threading.Event()

    class SlowEngine:
        def recognize(self, image):
            release.wait(timeout=5)
            return []

    monkeypatch.setattr(ocr, "_ENGINE", SlowEngine())
    fut = ocr.warm_up()  # 模型加载/推理在后台执行器上，调用方立即返回
    assert not fut.done()
    release.set()
    fut.result(timeout=5)


def test_warm_up_runs_on_the_ocr_worker_not_the_caller(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(ocr, "_ENGINE", engine)
    ocr.warm_up().result(timeout=5)
    assert len(engine.threads) == 1
    assert engine.threads[0] is not threading.current_thread()
