"""识别结果的质量门槛与排版：纯函数，输入为 OCR 行，输出为文本；不满足门槛时抛 RecognitionRejected（异常文案即提示）。"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ocr import OcrLine

NOT_RECOGNIZED_MESSAGE = "没有识别到日文，请重新框选"
VERTICAL_MESSAGE = "竖排文字暂不支持，请框选横向区域"

MIN_CHARS = 2  # 去掉空白后的最少字符数
MIN_MEAN_SCORE = 0.45  # 非空识别行的平均置信度下限（含）
VERTICAL_RATIO = 1.5  # 框高 > 框宽 × 1.5 判为竖向框
VERTICAL_SHARE = 0.6  # 竖向框占比超过该值即判为竖排
ROW_TOLERANCE = 0.6  # 同一行的纵向中心容差 = 行高中位数 × 该系数


class RecognitionRejected(Exception):
    """识别结果不满足门槛：不送翻译，异常消息即展示给用户的提示。"""


def compose_text(lines: Sequence[OcrLine]) -> str:
    """门槛检查后按阅读顺序排版：同一行内按左边界拼接（无分隔符），行与行之间换行。"""
    kept = [line for line in lines if line.text.strip()]
    if not kept or _char_count(kept) < MIN_CHARS:
        raise RecognitionRejected(NOT_RECOGNIZED_MESSAGE)
    if sum(line.score for line in kept) / len(kept) < MIN_MEAN_SCORE:
        raise RecognitionRejected(NOT_RECOGNIZED_MESSAGE)

    bounds = [_bounds(line) for line in kept]
    tall = sum(1 for x0, y0, x1, y1 in bounds if (y1 - y0) > (x1 - x0) * VERTICAL_RATIO)
    if tall / len(kept) > VERTICAL_SHARE:
        raise RecognitionRejected(VERTICAL_MESSAGE)

    tolerance = statistics.median(y1 - y0 for _, y0, _, y1 in bounds) * ROW_TOLERANCE
    placed = sorted(zip(kept, bounds), key=lambda item: _center_y(item[1]))
    rows: list[list[tuple[OcrLine, tuple[float, float, float, float]]]] = []
    anchor = 0.0  # 当前行第一个框的纵向中心
    for item in placed:
        cy = _center_y(item[1])
        if not rows or cy - anchor > tolerance:
            rows.append([])
            anchor = cy
        rows[-1].append(item)

    return "\n".join(
        "".join(line.text.strip() for line, _ in sorted(row, key=lambda item: item[1][0])) for row in rows
    )


def _char_count(lines: Sequence[OcrLine]) -> int:
    return sum(len("".join(line.text.split())) for line in lines)


def _bounds(line: OcrLine) -> tuple[float, float, float, float]:
    xs = [x for x, _ in line.box]
    ys = [y for _, y in line.box]
    return min(xs), min(ys), max(xs), max(ys)


def _center_y(bound: tuple[float, float, float, float]) -> float:
    return (bound[1] + bound[3]) / 2
