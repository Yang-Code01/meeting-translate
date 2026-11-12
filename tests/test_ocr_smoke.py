"""截图翻译 OCR 冒烟测试：使用真实 RapidOCR 与随 wheel 内置的 PP-OCRv6 small 模型（不 mock，不 skip）。

- 生成一张含日文的图（代码渲染，不依赖磁盘图片），识别结果应包含关键片段。
- 内置模型必须存在于 rapidocr 包内，且识别过程不得发起任何下载（断网环境可用）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rapidocr
from PIL import Image, ImageDraw, ImageFont
from rapidocr.utils.download_file import DownloadFile

from meeting_translate import ocr

# Windows 自带的日文字体（CI 的 windows-latest 镜像同样自带）。
JAPANESE_FONTS = [
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\msmincho.ttc",
]
PACKAGE_DIR = Path(rapidocr.__file__).resolve().parent
BUNDLED_MODELS = ["PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"]


def render_japanese(text: str) -> np.ndarray:
    font_path = next((p for p in JAPANESE_FONTS if Path(p).exists()), None)
    assert font_path, f"找不到日文字体，请在以下路径之一安装或修改测试：{JAPANESE_FONTS}"
    font = ImageFont.truetype(font_path, 56)
    img = Image.new("RGB", (900, 180), "white")
    ImageDraw.Draw(img).text((30, 50), text, font=font, fill="black")
    return np.asarray(img, dtype=np.uint8)


def test_real_rapidocr_reads_japanese_text_rendered_in_memory():
    image = render_japanese("こんにちは、世界")
    text = ocr.recognize_text(image, ocr.RapidOcrEngine())
    assert "こんにちは" in text
    assert "世界" in text


def test_recognition_does_not_download_models(monkeypatch):
    # RapidOCR 每次构造都会调用 DownloadFile.run：内置文件校验通过时在本地直接返回。
    # 这里封住 HTTP 请求层：若内置模型缺失或校验失败而发起下载，测试即失败。
    def refuse_http(cls, url, logger):
        raise AssertionError(f"识别过程不应发起模型下载：{url}")

    monkeypatch.setattr(DownloadFile, "_make_http_request", classmethod(refuse_http))
    text = ocr.recognize_text(render_japanese("こんにちは、世界"), ocr.RapidOcrEngine())
    assert "世界" in text


def test_default_models_are_bundled_inside_the_rapidocr_package():
    assert (PACKAGE_DIR / "default_models.yaml").is_file()
    for name in BUNDLED_MODELS:
        assert (PACKAGE_DIR / "models" / name).is_file(), f"缺少内置模型：{name}"
