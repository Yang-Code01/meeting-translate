"""截图翻译热键：默认值、v5→v6 迁移、设置对话框读写。"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate import config  # noqa: E402
from meeting_translate.config import Settings  # noqa: E402
from meeting_translate.ui import SettingsDialog  # noqa: E402


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config, "MODEL_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    return tmp_path / "config.json"


def test_fresh_install_defaults_screenshot_hotkey_to_alt_n(cfg_path):
    s = Settings.load()
    assert s.screenshot_hotkey == "Alt+N"
    assert s.selection_hotkey == "Alt+M"


def test_v5_config_gains_screenshot_hotkey_and_keeps_other_fields(cfg_path):
    cfg_path.write_text(
        json.dumps(
            {
                "version": 5,
                "selection_hotkey": "Alt+K",
                "selection_font_family": "SimSun",
                "selection_chinese_px": 14,
                "font_color": "#ffcc00",
            }
        ),
        encoding="utf-8",
    )
    s = Settings.load()
    assert s.version == 6
    assert s.screenshot_hotkey == "Alt+N"
    assert s.selection_hotkey == "Alt+K"
    assert s.selection_font_family == "SimSun"
    assert s.selection_chinese_px == 14
    assert s.font_color == "#ffcc00"
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["screenshot_hotkey"] == "Alt+N"


def test_user_set_screenshot_hotkey_survives_reload(cfg_path):
    s = Settings()
    s.screenshot_hotkey = "Ctrl+Shift+S"
    s.save()
    assert Settings.load().screenshot_hotkey == "Ctrl+Shift+S"


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_settings_dialog_shows_and_saves_screenshot_hotkey(cfg_path, qapp):
    s = Settings()
    dlg = SettingsDialog(s, [])
    assert dlg.screenshot_hotkey.text() == "Alt+N"
    assert dlg.selection_hotkey.text() == "Alt+M"

    dlg.screenshot_hotkey.setText("Ctrl+Shift+S")
    dlg.apply()
    assert s.screenshot_hotkey == "Ctrl+Shift+S"
    assert s.selection_hotkey == "Alt+M"
    assert Settings.load().screenshot_hotkey == "Ctrl+Shift+S"
    dlg.deleteLater()


def test_save_button_requests_application_without_closing_dialog(cfg_path, qapp):
    dlg = SettingsDialog(Settings(), [])
    requests = []
    dlg.save_requested.connect(lambda: requests.append("save"))
    dlg.show()

    dlg.btn_save.click()

    assert requests == ["save"]
    assert dlg.isVisible()
    dlg.close()


def test_settings_dialog_blank_screenshot_hotkey_keeps_previous(cfg_path, qapp):
    s = Settings(screenshot_hotkey="Alt+P")
    dlg = SettingsDialog(s, [])
    dlg.screenshot_hotkey.setText("   ")
    dlg.apply()
    assert s.screenshot_hotkey == "Alt+P"
    dlg.deleteLater()
