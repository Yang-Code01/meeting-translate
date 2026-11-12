"""结果弹窗标题同时涵盖划词与截图翻译。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate.ui import SelectionResultPopup  # noqa: E402


def test_result_popup_title_names_both_selection_and_screenshot() -> None:
    QApplication.instance() or QApplication([])
    popup = SelectionResultPopup()
    assert popup.windowTitle() == "划词 / 截图翻译"
    assert popup._title.text() == "划词 / 截图翻译"
