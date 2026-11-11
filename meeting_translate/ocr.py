"""截图翻译的离线 OCR：RapidOCR（内置 PP-OCRv6 small 模型），单线程执行，不在界面线程运行。"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import numpy as np

# 只能在构造 RapidOCR 时设置：线程数 4、检测边长按 max 限制到 960（混合核 CPU 上避免性能退化）。
# 不传 lang_type：small 模型对日文与默认配置结果相同。
OCR_PARAMS = {
    "Det.limit_type": "max",
    "Det.limit_side_len": 960,
    "EngineConfig.onnxruntime.intra_op_num_threads": 4,
}


@dataclass(frozen=True)
class OcrLine:
    text: str
    score: float
    box: tuple[tuple[float, float], ...]  # 四个角点


class OcrEngine(Protocol):
    def recognize(self, image: np.ndarray) -> list[OcrLine]: ...


class RapidOcrEngine:
    """RapidOCR 封装：首次识别时才构造（加载模型）。输入为 RGB 数组（H, W, 3）。"""

    def __init__(self) -> None:
        self._impl = None

    def recognize(self, image: np.ndarray) -> list[OcrLine]:
        if self._impl is None:
            from rapidocr import RapidOCR

            self._impl = RapidOCR(params=OCR_PARAMS)
        # RapidOCR 按 OpenCV 习惯读取 BGR
        out = self._impl(np.ascontiguousarray(image[:, :, ::-1]))
        if not out.txts:
            return []
        return [
            OcrLine(str(text), float(score), tuple((float(x), float(y)) for x, y in box))
            for text, score, box in zip(out.txts, out.scores, out.boxes)
        ]


_ENGINE: OcrEngine | None = None
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")


def default_engine() -> OcrEngine:
    """进程内单例。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOcrEngine()
    return _ENGINE


def recognize_text(image: np.ndarray, engine: OcrEngine) -> str:
    """截图 → 文本：识别行去掉首尾空白后按顺序用换行连接，空行丢弃。引擎异常原样抛出。"""
    texts = (line.text.strip() for line in engine.recognize(image))
    return "\n".join(text for text in texts if text)


def submit_recognition(image: np.ndarray, engine: OcrEngine | None = None) -> Future[str]:
    """在单线程执行器上识别；调用方通过 Future 回到界面线程取结果。"""
    return _EXECUTOR.submit(recognize_text, image, engine or default_engine())


def warm_up() -> Future:
    """用一张空白小图触发一次识别，把模型加载提前到启动时。"""
    blank = np.full((64, 256, 3), 255, dtype=np.uint8)
    return _EXECUTOR.submit(lambda: default_engine().recognize(blank))
