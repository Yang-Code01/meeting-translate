"""结果弹窗通道：请求编号规则与输入来源无关，只有最新请求能刷新弹窗。"""

from __future__ import annotations

from PySide6.QtCore import QPoint

from meeting_translate.result_channel import ResultChannel


class FakePopup:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def show_loading(self, anchor) -> None:
        self.calls.append(("loading", anchor))

    def show_result(self, chinese, hiragana, anchor) -> None:
        self.calls.append(("result", chinese, hiragana, anchor))

    def show_error(self, message, anchor) -> None:
        self.calls.append(("error", message, anchor))


def test_begin_shows_loading_and_returns_increasing_ids() -> None:
    popup = FakePopup()
    channel = ResultChannel(popup)
    anchor = QPoint(3, 4)
    first = channel.begin(anchor)
    second = channel.begin(anchor)
    assert second == first + 1
    assert popup.calls == [("loading", anchor), ("loading", anchor)]
    assert channel.is_latest(second)
    assert not channel.is_latest(first)


def test_latest_result_is_shown() -> None:
    popup = FakePopup()
    channel = ResultChannel(popup)
    anchor = QPoint(1, 1)
    rid = channel.begin(anchor)
    assert channel.show_result(rid, "猫", "ねこ", anchor) is True
    assert popup.calls[-1] == ("result", "猫", "ねこ", anchor)


def test_stale_result_is_dropped() -> None:
    popup = FakePopup()
    channel = ResultChannel(popup)
    old = channel.begin(QPoint(1, 1))
    channel.begin(QPoint(2, 2))
    before = list(popup.calls)
    assert channel.show_result(old, "一", "いち", QPoint(1, 1)) is False
    assert popup.calls == before


def test_latest_error_is_shown_and_stale_error_dropped() -> None:
    popup = FakePopup()
    channel = ResultChannel(popup)
    old = channel.begin(QPoint(1, 1))
    new = channel.begin(QPoint(2, 2))
    before = list(popup.calls)
    assert channel.show_error(old, "翻译失败：x", QPoint(1, 1)) is False
    assert popup.calls == before
    assert channel.show_error(new, "翻译失败：y", QPoint(2, 2)) is True
    assert popup.calls[-1] == ("error", "翻译失败：y", QPoint(2, 2))
