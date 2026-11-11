"""划词翻译结果流程的行为锁定：弹窗展示、加载/错误状态、锚定位置、历史写入。

用 App.__new__ 跳过 Qt 窗口初始化，只换入假弹窗、假翻译器、假托盘，
通过 _on_selection_captured / _on_selection_translated 这两个入口驱动。
"""

from __future__ import annotations

import concurrent.futures

from PySide6.QtCore import QPoint

from meeting_translate.app import App
from meeting_translate.config import Settings
from meeting_translate.result_channel import ResultChannel
from meeting_translate.translator import SelectionTranslation


class FakePopup:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def show_loading(self, anchor) -> None:
        self.calls.append(("loading", anchor))

    def show_result(self, chinese, hiragana, anchor) -> None:
        self.calls.append(("result", chinese, hiragana, anchor))

    def show_error(self, message, anchor) -> None:
        self.calls.append(("error", message, anchor))


class FakeTranslator:
    def __init__(self) -> None:
        self.futures: list[tuple[str, str, str, concurrent.futures.Future]] = []

    def translate_selected(self, text, model, effort):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self.futures.append((text, model, effort, fut))
        return fut


class FakeTray:
    def __init__(self) -> None:
        self.messages: list[tuple] = []

    def showMessage(self, title, body, icon, ms) -> None:
        self.messages.append((title, body))


def make_app() -> App:
    app = App.__new__(App)
    app.settings = Settings()
    app.selection_popup = FakePopup()  # type: ignore[assignment]
    app.translator = FakeTranslator()  # type: ignore[assignment]
    app.tray = FakeTray()  # type: ignore[assignment]
    app._pending_selection_history = []
    app.result_channel = ResultChannel(app.selection_popup)
    app._active_model = lambda: "test-model"  # type: ignore[method-assign]
    app.statuses = []
    app._set_status = app.statuses.append  # type: ignore[method-assign]
    app.watched = []

    def fake_watch(fut, tag) -> None:
        app.watched.append((fut, tag))

    app._watch = fake_watch  # type: ignore[method-assign]
    return app


def _finish(app: App, index: int, value) -> None:
    """模拟后台任务完成：调用 _watch 登记的回调（与 _on_job_done 的分派一致）。"""
    _, tag = app.watched[index]
    assert tag[0] == "cb"
    tag[1](value)


def test_capture_shows_loading_at_anchor_and_submits_translation() -> None:
    app = make_app()
    anchor = QPoint(10, 20)
    app._on_selection_captured("こんにちは", anchor, "")
    assert app.selection_popup.calls == [("loading", anchor)]
    assert app.translator.futures[0][:3] == ("こんにちは", "test-model", app.settings.reasoning_effort)
    assert app.statuses == []


def test_capture_warning_goes_to_status_without_leading_separator() -> None:
    app = make_app()
    app._on_selection_captured("テスト", QPoint(), "；剪贴板已恢复失败")
    assert app.statuses == ["剪贴板已恢复失败"]


def test_latest_result_refreshes_popup_and_records_history() -> None:
    app = make_app()
    anchor = QPoint(5, 6)
    app._on_selection_captured("猫", anchor, "")
    _finish(app, 0, SelectionTranslation("猫", "ねこ"))
    assert app.selection_popup.calls[-1] == ("result", "猫", "ねこ", anchor)
    assert len(app._pending_selection_history) == 1
    rec = app._pending_selection_history[0]
    assert (rec.source, rec.chinese, rec.hiragana) == ("猫", "猫", "ねこ")


def test_latest_error_shows_error_on_popup_and_tray() -> None:
    app = make_app()
    anchor = QPoint(1, 2)
    app._on_selection_captured("犬", anchor, "")
    err = RuntimeError("网络断开")
    _finish(app, 0, err)
    assert app.selection_popup.calls[-1] == ("error", "翻译失败：网络断开", anchor)
    assert app.tray.messages == [("划词翻译失败", "网络断开")]
    assert app._pending_selection_history == []


def test_stale_result_only_records_history_and_does_not_refresh_popup() -> None:
    app = make_app()
    app._on_selection_captured("一", QPoint(1, 1), "")
    app._on_selection_captured("二", QPoint(2, 2), "")
    newest = QPoint(2, 2)
    _finish(app, 1, SelectionTranslation("二", "に"))
    before = list(app.selection_popup.calls)
    _finish(app, 0, SelectionTranslation("一", "いち"))
    assert app.selection_popup.calls == before  # 较早结果不刷新弹窗
    assert [r.source for r in app._pending_selection_history] == ["二", "一"]
    assert app.selection_popup.calls[-1] == ("result", "二", "に", newest)


def test_stale_error_is_silent_on_popup_but_still_notifies_tray() -> None:
    app = make_app()
    app._on_selection_captured("一", QPoint(1, 1), "")
    app._on_selection_captured("二", QPoint(2, 2), "")
    _finish(app, 1, SelectionTranslation("二", "に"))
    before = list(app.selection_popup.calls)
    _finish(app, 0, ValueError("超时"))
    assert app.selection_popup.calls == before
    assert app.tray.messages == [("划词翻译失败", "超时")]
