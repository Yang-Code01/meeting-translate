"""深色提示框：ask / choose 按点的按钮返回结果。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate import message_dialog as msg  # noqa: E402


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _click_later(label: str) -> None:
    def click() -> None:
        dlg = next(w for w in QApplication.topLevelWidgets() if isinstance(w, msg.MessageDialog) and w.isVisible())
        dlg.buttons[label].click()

    QTimer.singleShot(0, click)


def test_ask_returns_true_only_for_yes(qapp) -> None:
    _click_later("继续")
    assert msg.ask(None, "t", "x", yes="继续", no="取消") is True
    _click_later("取消")
    assert msg.ask(None, "t", "x", yes="继续", no="取消") is False


def test_choose_returns_clicked_label(qapp) -> None:
    _click_later("打开下载页")
    assert msg.choose(None, "t", "x", ["自动安装", "打开下载页", "关闭"], kind="warning") == "打开下载页"
