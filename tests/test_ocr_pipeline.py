"""截图→文本流水线：用假 OCR 引擎做测试，不加载真实模型。"""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace

import numpy as np
import pytest

from meeting_translate import ocr
from meeting_translate.ocr import OcrLine
from meeting_translate.ocr_layout import RecognitionRejected


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


def test_recognized_rows_are_joined_in_order_with_newlines():
    engine = FakeEngine([line("こんにちは", 0), line("世界", 10)])
    assert ocr.recognize_text(blank(), engine) == "こんにちは\n世界"


def test_blank_lines_are_dropped_and_no_lines_is_rejected_before_translation():
    assert ocr.recognize_text(blank(), FakeEngine([line("  ", 0), line("日本語", 10)])) == "日本語"
    with pytest.raises(RecognitionRejected):
        ocr.recognize_text(blank(), FakeEngine([]))


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
