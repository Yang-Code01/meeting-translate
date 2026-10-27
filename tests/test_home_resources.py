"""首页资源卡与划词端点测试按钮。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate.config import Settings  # noqa: E402
from meeting_translate.home_ui import HomeWindow  # noqa: E402
from meeting_translate.ui import ProfileDialog  # noqa: E402


def test_resource_cards_emit_primary_actions() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    window = HomeWindow(Settings())
    actions: list[str] = []
    window.model_requested.connect(actions.append)
    window.cable_requested.connect(actions.append)
    window.set_model_state("未安装", primary="下载")
    window.model_card.btn_primary.click()
    window.set_model_state("已安装", primary="")
    assert window.model_card.btn_primary.isHidden()
    window.set_cable_state("未安装", primary="下载安装")
    window.cable_card.btn_primary.click()
    assert actions == ["下载", "下载安装"]
    window.deleteLater()


def test_profile_dialog_has_test_button() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    dialog = ProfileDialog({"name": "x", "base_url": "https://example.com/v1", "api_key": "dpapi:x"})
    assert dialog.btn_test.text() == "测试连接"
    assert dialog.btn_test.isEnabled()
    dialog.deleteLater()
