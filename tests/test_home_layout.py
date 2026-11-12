"""首页布局：划词与截图合并为大卡片，编号 01–04，快捷键标签。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from meeting_translate.config import Settings  # noqa: E402
from meeting_translate.home_ui import DUPLEX_ONLY_TIP, HomeWindow  # noqa: E402


def _text(card, name: str) -> str:
    return card.findChild(QLabel, name).text()


def test_screenshot_card_sits_below_selection_in_one_big_card() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    window = HomeWindow(Settings())
    window.resize(1080, 780)
    window.show()
    qapp.processEvents()
    big = window.card_selection.parent()
    assert big is window.card_screenshot.parent()
    assert big.objectName() == "comboCard"
    assert not any(isinstance(w, QLabel) for w in big.children())  # 大卡片本身不设标题
    assert window.card_screenshot.y() > window.card_selection.y()
    window.deleteLater()


def test_big_card_takes_selection_column_between_meeting_and_duplex() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    window = HomeWindow(Settings())
    window.resize(1080, 780)
    window.show()
    qapp.processEvents()
    big = window.card_selection.parent()
    assert window.card_meeting.x() < big.x() < window.card_duplex.x()
    assert window.card_meeting.height() == big.height() == window.card_duplex.height()
    window.deleteLater()


def test_small_cards_fit_at_minimum_window_size() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    window = HomeWindow(Settings())
    window.resize(900, 640)
    window.show()
    qapp.processEvents()
    for card in (window.card_selection, window.card_screenshot):
        assert card.height() >= card.minimumSizeHint().height()
        assert card.width() >= card.minimumSizeHint().width()
    window.deleteLater()


def test_small_cards_have_no_bullets_and_no_button() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    window = HomeWindow(Settings())
    for card in (window.card_selection, window.card_screenshot):
        assert card.findChild(QLabel, "modeBullets") is None
        assert card.findChild(QLabel, "shortcutChip") is not None
        assert card.findChildren(QPushButton) == []
    window.deleteLater()


def test_screenshot_card_shows_configured_hotkey() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    settings = Settings(screenshot_hotkey="Ctrl+Alt+N", selection_hotkey="Alt+M")
    window = HomeWindow(settings)
    assert _text(window.card_screenshot, "shortcutChip") == "快捷键  Ctrl+Alt+N"
    assert _text(window.card_selection, "shortcutChip") == "快捷键  Alt+M"
    settings.screenshot_hotkey = "Alt+K"
    window.refresh(settings)
    assert _text(window.card_screenshot, "shortcutChip") == "快捷键  Alt+K"
    window.deleteLater()


def test_card_numbers_and_titles_are_01_to_04() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    window = HomeWindow(Settings())
    expected = {
        window.card_meeting: ("01", "会议实时翻译"),
        window.card_selection: ("02", "划词翻译"),
        window.card_screenshot: ("03", "截图翻译"),
        window.card_duplex: ("04", "语音实时转译"),
    }
    for card, (number, title) in expected.items():
        assert _text(card, "modeNumber") == number
        assert _text(card, "modeTitle") == title
    window.deleteLater()


def test_intro_says_four_entries_without_shared_model_claim() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    window = HomeWindow(Settings())
    sub = window.findChild(QLabel, "questionSub").text()
    assert "四种入口" in sub
    assert "共用同一套模型" not in sub
    window.deleteLater()


def test_duplex_badge_tip_lists_01_to_03() -> None:
    assert DUPLEX_ONLY_TIP == "仅 04 语音实时转译需要，只用 01、02、03 不必下载、安装或配置。"
