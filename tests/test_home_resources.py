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


def test_model_card_asks_for_runtime_before_models(monkeypatch, tmp_path) -> None:
    """模型装了也跑不起来时，卡片要先要运行时而不是先要那 1.1GB。"""
    from meeting_translate import app as app_module
    from meeting_translate import tts_runtime
    from meeting_translate.tts_models import ModelManager

    calls: list[tuple[str, dict]] = []

    class _Home:
        def set_model_state(self, status: str, **kwargs) -> None:
            calls.append((status, kwargs))

        def set_cable_state(self, *_args, **_kwargs) -> None:
            pass

        def set_teams_state(self, *_args, **_kwargs) -> None:
            pass

    class _Devices:
        complete = True
        missing: list[str] = []

    app = app_module.App.__new__(app_module.App)
    app.home = _Home()
    app.model_manager = ModelManager(tmp_path / "tts")
    monkeypatch.setattr(app_module.duplex_audio, "detect_cable_devices", lambda: _Devices())

    monkeypatch.setattr(tts_runtime, "is_ready", lambda: False)
    app_module.App._refresh_resources(app)
    assert calls[-1][0].startswith("缺语音运行时")
    assert calls[-1][1]["primary"] == "安装运行时"

    monkeypatch.setattr(tts_runtime, "is_ready", lambda: True)
    app_module.App._refresh_resources(app)
    assert calls[-1][0].startswith("未安装 · 约")
    assert calls[-1][1]["primary"] == "下载"
