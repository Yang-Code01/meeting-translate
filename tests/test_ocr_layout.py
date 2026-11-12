"""识别结果的门槛与横排排版：纯函数，输入为假 OCR 行。"""

from __future__ import annotations

import numpy as np
import pytest

from meeting_translate.ocr import OcrLine
from meeting_translate.ocr_layout import (
    NOT_RECOGNIZED_MESSAGE,
    VERTICAL_MESSAGE,
    RecognitionRejected,
    compose_text,
)


def box(text: str, x: float, y: float, w: float, h: float, score: float = 0.95) -> OcrLine:
    return OcrLine(text, score, ((x, y), (x + w, y), (x + w, y + h), (x, y + h)))


def rejected_message(lines) -> str:
    with pytest.raises(RecognitionRejected) as info:
        compose_text(lines)
    return str(info.value)


# 正常拼接与排版


def test_boxes_in_one_row_are_joined_by_x_position_without_separator():
    lines = [box("テスト", 40, 0, 30, 10), box("日本", 0, 0, 30, 10)]
    assert compose_text(lines) == "日本テスト"


def test_rows_are_separated_by_newlines_in_top_to_bottom_order():
    lines = [box("二行目", 0, 20, 30, 10), box("一行目", 0, 0, 30, 10)]
    assert compose_text(lines) == "一行目\n二行目"


def test_small_vertical_jitter_within_tolerance_keeps_boxes_in_one_row():
    # 行高中位数 10，容差 6：中心相差 5 仍同一行，相差 7 另起一行
    lines = [
        box("A", 0, 0, 10, 10),
        box("B", 20, 5, 10, 10),
        box("C", 40, 12, 10, 10),
    ]
    assert compose_text(lines) == "AB\nC"


def test_row_tolerance_uses_median_height_not_a_single_outlier():
    # 中位数 10 → 容差 6；一个超高的框不应放大容差
    lines = [
        box("A", 0, 0, 10, 10),
        box("B", 20, 7, 10, 10),  # 中心差 7 > 6：另起一行
        box("C", 0, 40, 10, 200, score=0.95),  # 高度 200 但为离群值，中位数仍为 10
    ]
    assert compose_text(lines) == "A\nB\nC"


def test_blank_and_whitespace_only_boxes_are_dropped_from_layout():
    lines = [box("   ", 0, 0, 10, 10), box("日本語", 0, 20, 30, 10), box("", 0, 40, 10, 10)]
    assert compose_text(lines) == "日本語"


def test_text_inside_a_line_is_stripped_of_surrounding_whitespace():
    assert compose_text([box(" 日本語 ", 0, 0, 30, 10)]) == "日本語"


# 没有识别到日文：空白、单字符、低置信度


def test_no_lines_is_rejected_as_no_japanese():
    assert rejected_message([]) == NOT_RECOGNIZED_MESSAGE


def test_whitespace_only_text_is_rejected_as_no_japanese():
    assert rejected_message([box("  ", 0, 0, 30, 10), box("\t", 0, 20, 30, 10)]) == NOT_RECOGNIZED_MESSAGE


def test_a_single_character_is_rejected_as_no_japanese():
    assert rejected_message([box("日", 0, 0, 30, 10)]) == NOT_RECOGNIZED_MESSAGE


def test_fewer_than_two_characters_after_removing_whitespace_is_rejected():
    assert rejected_message([box("日 ", 0, 0, 30, 10)]) == NOT_RECOGNIZED_MESSAGE


def test_two_characters_are_accepted():
    assert compose_text([box("日本", 0, 0, 30, 10)]) == "日本"


def test_mean_confidence_below_threshold_is_rejected_as_no_japanese():
    lines = [box("日本語", 0, 0, 30, 10, score=0.3), box("テスト", 0, 20, 30, 10, score=0.5)]
    assert rejected_message(lines) == NOT_RECOGNIZED_MESSAGE


def test_mean_confidence_is_averaged_over_lines_not_taken_from_the_worst_line():
    lines = [box("日本語", 0, 0, 30, 10, score=0.2), box("テスト", 0, 20, 30, 10, score=0.9)]
    assert compose_text(lines) == "日本語\nテスト"


def test_mean_confidence_exactly_at_threshold_is_accepted():
    lines = [box("日本語", 0, 0, 30, 10, score=0.44), box("テスト", 0, 20, 30, 10, score=0.46)]
    assert compose_text(lines) == "日本語\nテスト"


# 竖排


def _vertical(text: str, x: float) -> OcrLine:
    return box(text, x, 0, 10, 30)  # 高 30 > 宽 10 × 1.5


def _horizontal(text: str, y: float) -> OcrLine:
    return box(text, 0, y, 30, 10)


def test_vertical_share_above_threshold_is_rejected_as_vertical():
    # 4 个竖框 / 5 个框 = 0.8 > 0.6
    lines = [_vertical("日", 0), _vertical("本", 20), _vertical("語", 40), _vertical("字", 60), _horizontal("x", 100)]
    assert rejected_message(lines) == VERTICAL_MESSAGE


def test_vertical_share_exactly_at_threshold_is_not_vertical():
    # 3 个竖框 / 5 个框 = 0.6，不超过 0.6
    lines = [_vertical("日", 0), _vertical("本", 20), _vertical("語", 40), _horizontal("字幕", 100), _horizontal("x", 120)]
    assert compose_text(lines) == "日本語\n字幕\nx"


def test_a_box_must_be_more_than_one_and_a_half_times_taller_than_wide_to_count_as_vertical():
    # 高 15、宽 10：15 不大于 10 × 1.5，不计入竖排
    lines = [box("日本", 0, 0, 10, 15), box("語字", 0, 30, 10, 15)]
    assert compose_text(lines) == "日本\n語字"


# 通过 ocr.recognize_text 的端到端（假引擎）


def test_recognize_text_applies_the_gates_from_the_engine_output():
    from meeting_translate import ocr

    class FakeEngine:
        def recognize(self, image):
            return [box("日", 0, 0, 30, 10)]

    with pytest.raises(RecognitionRejected) as info:
        ocr.recognize_text(np.zeros((10, 30, 3), dtype=np.uint8), FakeEngine())
    assert str(info.value) == NOT_RECOGNIZED_MESSAGE
