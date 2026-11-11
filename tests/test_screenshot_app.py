"""截图翻译的应用层接线：OCR 文本交给划词翻译路径，共享最新请求编号，热键替换。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from concurrent.futures import Future, wait  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

import meeting_translate.app as app_module  # noqa: E402
from meeting_translate import ocr  # noqa: E402
from meeting_translate.app import App  # noqa: E402
from meeting_translate.ocr import OcrLine  # noqa: E402
from meeting_translate.ocr_layout import NO_JAPANESE_MESSAGE, VERTICAL_MESSAGE  # noqa: E402
from meeting_translate.result_channel import ResultChannel  # noqa: E402
from meeting_translate.translator import SelectionTranslation  # noqa: E402


class FakeEngine:
    def __init__(self, texts=(), error: Exception | None = None) -> None:
        self.texts = list(texts)
        self.error = error

    def recognize(self, image):
        if self.error:
            raise self.error
        return [OcrLine(t, 0.99, ((0, i), (1, i), (1, i + 1), (0, i + 1))) for i, t in enumerate(self.texts)]


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


class FakeSignal:
    def __init__(self) -> None:
        self.emitted = 0

    def emit(self) -> None:
        self.emitted += 1


class FakeGlobalHotkey:
    def __init__(self, spec, callback) -> None:
        self.spec = spec
        self.callback = callback
        self.stopped = False
        self.error = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def app(monkeypatch):
    """只带截图流程需要的属性的 App；_watch 只记录，由测试按完成情况回放。"""
    a = App.__new__(App)
    a.settings = FakeSettings()
    a.selection_popup = FakePopup()
    a.translator = FakeTranslator()
    a._pending_selection_history = []
    a.result_channel = ResultChannel(a.selection_popup)
    a.status_messages: list[str] = []
    a._set_status = a.status_messages.append
    a.watched: list[tuple] = []
    a._watch = lambda fut, tag: a.watched.append((fut, tag))
    monkeypatch.setattr(ocr, "_ENGINE", FakeEngine(["日本語"]))
    return a


def drain(app) -> None:
    """回放后台任务（按提交顺序）。OCR 在真实执行器上跑，这里等它完成；翻译结果由测试手动设置，不等待。"""
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


def test_recognized_text_goes_to_the_selection_translation_path_and_shows_the_result(app):
    anchor = QPoint(300, 200)
    app._on_screenshot_captured(image(), anchor)
    assert app.selection_popup.calls == [("loading", anchor)]
    drain(app)  # OCR 完成 → 交给 translate_selected
    assert app.translator.sent == ["日本語"]
    app.translator.futures[0].set_result(SelectionTranslation("日语", "にほんご"))
    drain(app)
    assert app.selection_popup.calls[-1] == ("result", "日语", "にほんご", anchor)
    assert [r.source for r in app._pending_selection_history] == ["日本語"]


def test_an_empty_recognition_shows_an_error_and_does_not_translate(monkeypatch, app):
    monkeypatch.setattr(ocr, "_ENGINE", FakeEngine([]))
    app._on_screenshot_captured(image(), QPoint(5, 5))
    drain(app)
    assert app.translator.sent == []
    assert app.selection_popup.calls[-1] == ("error", NO_JAPANESE_MESSAGE, QPoint(5, 5))


def test_a_single_character_recognition_is_rejected_without_translation(monkeypatch, app):
    monkeypatch.setattr(ocr, "_ENGINE", FakeEngine(["日"]))
    app._on_screenshot_captured(image(), QPoint(5, 5))
    drain(app)
    assert app.translator.sent == []
    assert app.selection_popup.calls[-1][:2] == ("error", NO_JAPANESE_MESSAGE)


def test_a_vertical_region_is_rejected_without_translation(monkeypatch, app):
    class VerticalEngine:
        def recognize(self, image):
            return [OcrLine("日本語", 0.99, ((x, 0), (x + 10, 0), (x + 10, 30), (x, 30))) for x in (0, 20, 40)]

    monkeypatch.setattr(ocr, "_ENGINE", VerticalEngine())
    app._on_screenshot_captured(image(), QPoint(5, 5))
    drain(app)
    assert app.translator.sent == []
    assert app.selection_popup.calls[-1][:2] == ("error", VERTICAL_MESSAGE)


def test_an_ocr_failure_shows_an_error_and_the_app_keeps_working(monkeypatch, app):
    monkeypatch.setattr(ocr, "_ENGINE", FakeEngine(error=RuntimeError("模型损坏")))
    app._on_screenshot_captured(image(), QPoint(5, 5))
    drain(app)
    assert app.translator.sent == []
    kind, message, _ = app.selection_popup.calls[-1]
    assert kind == "error" and "模型损坏" in message


def test_only_the_latest_request_refreshes_the_popup_but_every_result_is_kept_for_history(monkeypatch, app):
    monkeypatch.setattr(ocr, "_ENGINE", FakeEngine(["旧文"]))
    app._on_screenshot_captured(image(), QPoint(1, 1))
    drain(app)
    monkeypatch.setattr(ocr, "_ENGINE", FakeEngine(["新文"]))
    app._on_screenshot_captured(image(), QPoint(2, 2))
    drain(app)
    app.selection_popup.calls.clear()
    app.translator.futures[1].set_result(SelectionTranslation("新译", "しん"))
    app.translator.futures[0].set_result(SelectionTranslation("旧译", "きゅう"))
    drain(app)
    assert app.selection_popup.calls == [("result", "新译", "しん", QPoint(2, 2))]
    assert {r.chinese for r in app._pending_selection_history} == {"新译", "旧译"}


def test_screenshot_hotkey_is_registered_and_replaced_without_touching_selection_hotkey(monkeypatch, app):
    monkeypatch.setattr(app_module, "GlobalHotkey", FakeGlobalHotkey)
    app.bridge = SimpleNamespace(screenshot_hotkey=FakeSignal())
    selection = FakeGlobalHotkey("Alt+M", lambda: None)
    app.selection_hotkey = selection
    app.screenshot_hotkey = None

    app._register_screenshot_hotkey()
    first = app.screenshot_hotkey
    assert first.spec == "Alt+N"
    first.callback()
    assert app.bridge.screenshot_hotkey.emitted == 1

    app.settings.screenshot_hotkey = "Ctrl+Alt+K"
    app._register_screenshot_hotkey()
    assert first.stopped
    assert app.screenshot_hotkey.spec == "Ctrl+Alt+K"
    assert not selection.stopped


def test_alternating_screenshot_and_selection_triggers_refresh_only_the_last_one(monkeypatch, app):
    """截图与划词共用一个弹窗通道：较早触发的截图结果晚到时只写历史，不刷新弹窗。"""
    app._active_model = lambda: "gpt-test"
    app.tray = SimpleNamespace(showMessage=lambda *a: None)
    anchor_shot, anchor_sel = QPoint(1, 1), QPoint(2, 2)
    monkeypatch.setattr(ocr, "_ENGINE", FakeEngine(["截图原文"]))
    app._on_screenshot_captured(image(), anchor_shot)
    drain(app)  # 截图 OCR 完成 → 翻译已提交（未完成）
    app._on_selection_captured("划词原文", anchor_sel, "")  # 之后触发划词
    assert app.selection_popup.calls[-1] == ("loading", anchor_sel)
    assert app.translator.sent == ["截图原文", "划词原文"]

    app.selection_popup.calls.clear()
    app.translator.futures[1].set_result(SelectionTranslation("划词译", "せん"))  # 最新结果先到
    drain(app)
    app.translator.futures[0].set_result(SelectionTranslation("截图译", "しゃ"))  # 较早的截图结果后到
    drain(app)

    assert app.selection_popup.calls == [("result", "划词译", "せん", anchor_sel)]
    assert {r.source for r in app._pending_selection_history} == {"截图原文", "划词原文"}
