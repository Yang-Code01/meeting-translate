"""截图翻译的错误与降级路径：OCR 失败、翻译失败、未配置、热键冲突。

只用假对象与假 OCR 引擎；不加载真实模型。错误文案与划词路径保持一致。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402
from concurrent.futures import Future, wait  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

import meeting_translate.app as app_module  # noqa: E402
from meeting_translate import ocr  # noqa: E402
from meeting_translate.app import App  # noqa: E402
from meeting_translate.ocr import OcrLine  # noqa: E402
from meeting_translate.result_channel import ResultChannel  # noqa: E402

MODEL_MISSING = "OCR 模型文件缺失"
NO_MODEL_NAME = "没有可用的模型名：请在端点里填写模型名，或确认该端点的 /models 能返回列表"


class FlakyEngine:
    """前 failures 次识别抛出 error，之后返回一行日文。"""

    def __init__(self, failures: int = 0, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or RuntimeError("识别中途出错")

    def recognize(self, image):
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        return [OcrLine("日本語", 0.99, ((0, 0), (1, 0), (1, 1), (0, 1)))]


class FakePopup:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def show_loading(self, anchor):
        self.calls.append(("loading", anchor))

    def show_result(self, chinese, hiragana, anchor):
        self.calls.append(("result", chinese, hiragana, anchor))

    def show_error(self, message, anchor):
        self.calls.append(("error", message, anchor))


class FakeTranslator:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.futures: list[Future] = []

    def translate_selected(self, text, model, effort):
        self.sent.append(text)
        fut: Future = Future()
        self.futures.append(fut)
        return fut


class FakeSettings:
    provider = "copilot"
    model = "gpt-test"
    reasoning_effort = "low"
    screenshot_hotkey = "Alt+N"
    selection_hotkey = "Alt+M"


class FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def showMessage(self, title, text, icon, ms):
        self.messages.append((title, text))


class FakeSignal:
    def __init__(self) -> None:
        self.emitted = 0

    def emit(self) -> None:
        self.emitted += 1


class FakeGlobalHotkey:
    def __init__(self, spec, callback, error: str | None = None) -> None:
        self.spec = spec
        self.callback = callback
        self.error = error

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture
def app(monkeypatch):
    """只带截图与划词错误路径需要的属性的 App；_watch 只记录，由 drain 回放。"""
    a = App.__new__(App)
    a.settings = FakeSettings()
    a.selection_popup = FakePopup()
    a.translator = FakeTranslator()
    a.tray = FakeTray()
    a._pending_selection_history = []
    a.result_channel = ResultChannel(a.selection_popup)
    a.status_messages: list[str] = []
    a._set_status = a.status_messages.append
    a.watched: list[tuple] = []
    a._watch = lambda fut, tag: a.watched.append((fut, tag))
    monkeypatch.setattr(ocr, "_ENGINE", FlakyEngine())
    return a


def drain(app) -> None:
    """回放后台任务：OCR 在真实执行器上跑，等它完成；翻译结果由测试手动设置。"""
    ocr_futures = [fut for fut, _ in app.watched if not any(fut is t for t in app.translator.futures)]
    if ocr_futures:
        wait(ocr_futures, timeout=5)
    progressed = True
    while progressed:
        progressed = False
        for entry in list(app.watched):
            fut, (_, callback) = entry
            if fut.done():
                app.watched.remove(entry)
                # 与 App._watch 一致：异常作为结果对象传给回调
                callback(fut.exception() or fut.result())
                progressed = True


def image() -> QImage:
    return QImage(40, 20, QImage.Format.Format_RGB888)


def blank() -> np.ndarray:
    return np.full((20, 40, 3), 255, dtype=np.uint8)


# ---- OCR 引擎层 ----


def test_model_load_failure_is_raised_to_the_caller_and_the_next_call_retries_the_load(monkeypatch):
    attempts = {"n": 0}

    class FlakyRapidOCR:
        def __init__(self, params=None) -> None:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError(MODEL_MISSING)

        def __call__(self, img):
            return SimpleNamespace(
                txts=("日本語",), scores=(0.99,), boxes=([[0, 0], [1, 0], [1, 1], [0, 1]],)
            )

    monkeypatch.setitem(sys.modules, "rapidocr", types.SimpleNamespace(RapidOCR=FlakyRapidOCR))
    engine = ocr.RapidOcrEngine()
    with pytest.raises(RuntimeError, match=MODEL_MISSING):
        engine.recognize(blank())
    assert engine.recognize(blank())[0].text == "日本語"
    assert attempts["n"] == 2


def test_a_failed_recognition_does_not_stop_the_next_one_on_the_worker():
    engine = FlakyEngine(failures=1)
    first = ocr.submit_recognition(blank(), engine)
    # 失败只作为 Future 的异常返回，不抛到调用方线程
    assert isinstance(first.exception(timeout=5), RuntimeError)
    assert ocr.submit_recognition(blank(), engine).result(timeout=5) == "日本語"


# ---- 应用层：截图流程 ----


def test_model_load_failure_shows_the_error_in_the_popup_and_selection_still_works(monkeypatch, app):
    monkeypatch.setattr(ocr, "_ENGINE", FlakyEngine(failures=99, error=RuntimeError(MODEL_MISSING)))
    anchor = QPoint(5, 5)
    app._on_screenshot_captured(image(), anchor)
    drain(app)
    kind, message, _ = app.selection_popup.calls[-1]
    assert kind == "error" and message == f"识别失败：{MODEL_MISSING}"
    assert app.translator.sent == []

    app._on_selection_captured("日本語", anchor, "")
    drain(app)
    assert app.translator.sent == ["日本語"]
    app.translator.futures[0].set_result(SimpleNamespace(chinese="日语", hiragana="にほんご"))
    drain(app)
    assert app.selection_popup.calls[-1] == ("result", "日语", "にほんご", anchor)


def test_one_failed_screenshot_does_not_affect_the_next_one(monkeypatch, app):
    app._on_screenshot_captured(image(), QPoint(1, 1))
    drain(app)
    # 第一次识别失败（来自引擎）
    monkeypatch.setattr(ocr, "_ENGINE", FlakyEngine(failures=1, error=RuntimeError("偶发错误")))
    app._on_screenshot_captured(image(), QPoint(2, 2))
    drain(app)
    assert app.selection_popup.calls[-1] == ("error", "识别失败：偶发错误", QPoint(2, 2))

    anchor = QPoint(3, 3)
    app._on_screenshot_captured(image(), anchor)
    drain(app)
    assert app.translator.sent[-1] == "日本語"
    app.translator.futures[-1].set_result(SimpleNamespace(chinese="日语", hiragana="にほんご"))
    drain(app)
    assert app.selection_popup.calls[-1] == ("result", "日语", "にほんご", anchor)


# ---- 应用层：翻译服务错误与未配置，与划词一致 ----


def _translation_error_popup(app, trigger, error: Exception):
    anchor = QPoint(7, 7)
    trigger(anchor)
    drain(app)
    app.translator.futures[-1].set_exception(error)
    drain(app)
    return app.selection_popup.calls[-1], app.tray.messages[-1]


def test_translation_service_error_shows_the_same_popup_error_as_selection(app):
    error = RuntimeError("服务返回 500")
    shot_popup, shot_tray = _translation_error_popup(
        app, lambda anchor: app._on_screenshot_captured(image(), anchor), error
    )
    sel_popup, sel_tray = _translation_error_popup(
        app, lambda anchor: app._on_selection_captured("日本語", anchor, ""), error
    )
    assert shot_popup == ("error", "翻译失败：服务返回 500", QPoint(7, 7))
    assert shot_popup[:2] == sel_popup[:2]
    assert shot_tray == sel_tray


def test_unconfigured_translation_service_gives_the_same_hint_as_selection(app):
    error = RuntimeError(NO_MODEL_NAME)
    shot_popup, _ = _translation_error_popup(app, lambda anchor: app._on_screenshot_captured(image(), anchor), error)
    sel_popup, _ = _translation_error_popup(app, lambda anchor: app._on_selection_captured("日本語", anchor, ""), error)
    assert shot_popup[0] == "error" and NO_MODEL_NAME in shot_popup[1]
    assert shot_popup[:2] == sel_popup[:2]


# ---- 热键注册失败：与划词一致的提示 ----


def test_screenshot_hotkey_conflict_shows_the_same_status_as_selection(monkeypatch, app):
    conflict = "快捷键 Alt+N 已被其他程序占用"
    monkeypatch.setattr(app_module, "GlobalHotkey", lambda spec, cb: FakeGlobalHotkey(spec, cb, error=conflict))
    app.bridge = SimpleNamespace(screenshot_hotkey=FakeSignal(), selection_hotkey=FakeSignal())
    app.screenshot_hotkey = None
    app.selection_hotkey = None

    app._register_screenshot_hotkey()
    assert app.status_messages == [conflict]

    conflict_sel = "快捷键 Alt+M 已被其他程序占用"
    monkeypatch.setattr(app_module, "GlobalHotkey", lambda spec, cb: FakeGlobalHotkey(spec, cb, error=conflict_sel))
    app.status_messages.clear()
    app._register_selection_hotkey()
    assert app.status_messages == [conflict_sel]
    assert app.screenshot_hotkey is not None  # 注册失败不影响对象存在，程序继续运行
