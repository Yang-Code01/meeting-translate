"""截图翻译的离线 OCR：RapidOCR（内置 PP-OCRv6 small 模型），单线程执行，不在界面线程运行。"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from .capture import image_to_rgb_array
from .ocr_layout import compose_text

# 只能在构造 RapidOCR 时设置：线程数 4、检测边长按 max 限制到 960（混合核 CPU 上避免性能退化）。
# 不传 lang_type：small 模型对日文与默认配置结果相同。
OCR_PARAMS = {
    "Det.limit_type": "max",
    "Det.limit_side_len": 960,
    "EngineConfig.onnxruntime.intra_op_num_threads": 4,
}


# 尺寸归一：长边超过 2000 等比缩小；长边小于 400 放大 2 倍；其它不变。只缩放，不锐化、二值化、去噪。
MAX_LONG_SIDE = 2000
MIN_LONG_SIDE = 400
UPSCALE_FACTOR = 2.0


def ocr_scale(width: int, height: int) -> float:
    """按选框尺寸返回缩放因子（1.0 表示不缩放）。"""
    long_side = max(width, height)
    if long_side > MAX_LONG_SIDE:
        return MAX_LONG_SIDE / long_side
    if long_side < MIN_LONG_SIDE:
        return UPSCALE_FACTOR
    return 1.0


def normalize_size(image: np.ndarray) -> np.ndarray:
    """RGB 数组（H, W, 3）按 ocr_scale 缩放；不需要缩放时原样返回。"""
    h, w = image.shape[:2]
    scale = ocr_scale(w, h)
    if scale == 1.0:
        return image
    tw, th = max(1, round(w * scale)), max(1, round(h * scale))
    src = np.ascontiguousarray(image, dtype=np.uint8)
    # QImage 不持有这块字节：buf 必须活到 scaled 完成之后，否则读到已释放的内存
    buf = src.tobytes()
    qimg = QImage(buf, w, h, w * 3, QImage.Format.Format_RGB888)
    scaled = qimg.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return image_to_rgb_array(scaled)


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
_ENGINE_LOCK = threading.Lock()  # 界面线程与 OCR 线程都可能首次调用
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")


def default_engine() -> OcrEngine:
    """进程内单例（加锁，多线程首次调用也只构造一次）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = RapidOcrEngine()
        return _ENGINE


def recognize_text(image: np.ndarray, engine: OcrEngine) -> str:
    """截图 → 文本：先做尺寸归一，再过质量门槛，最后横排排版（见 ocr_layout）。门槛不通过抛 RecognitionRejected；引擎异常原样抛出。"""
    return compose_text(engine.recognize(normalize_size(image)))


def submit_recognition(image: np.ndarray, engine: OcrEngine | None = None) -> Future[str]:
    """在单线程执行器上识别；调用方通过 Future 回到界面线程取结果。"""
    return _EXECUTOR.submit(recognize_text, image, engine or default_engine())


def warm_up() -> Future:
    """用一张空白小图触发一次识别，把模型加载提前到启动时。"""
    blank = np.full((64, 256, 3), 255, dtype=np.uint8)
    return _EXECUTOR.submit(lambda: default_engine().recognize(blank))
