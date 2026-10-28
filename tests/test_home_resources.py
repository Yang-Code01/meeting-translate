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


def test_duplex_card_locked_until_blockers_cleared() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    window = HomeWindow(Settings())
    assert window.card_duplex.locked  # 首次检测前也不让进
    window.set_duplex_blockers(["语音模型", "虚拟声卡"])
    assert not window.card_duplex._button.isEnabled()
    assert "语音模型、虚拟声卡" in window.card_duplex._lock.text()
    window.set_duplex_blockers([])
    assert window.card_duplex._button.isEnabled()
    assert not window.card_duplex.locked
    window.deleteLater()


class _Home:
    def __init__(self) -> None:
        self.model: list[tuple[str, dict]] = []
        self.blockers: list[list[str]] = []

    def set_model_state(self, status: str, **kwargs) -> None:
        self.model.append((status, kwargs))

    def set_cable_state(self, *_args, **_kwargs) -> None:
        pass

    def set_teams_state(self, *_args, **_kwargs) -> None:
        pass

    def set_duplex_blockers(self, blockers: list[str]) -> None:
        self.blockers.append(list(blockers))


def _bare_app(monkeypatch, tmp_path, *, cable: bool):
    from meeting_translate import app as app_module
    from meeting_translate.tts_models import ModelManager

    class _Devices:
        complete = cable
        missing: list[str] = [] if cable else ["CABLE-B"]

    app = app_module.App.__new__(app_module.App)
    app.home = _Home()
    app.model_manager = ModelManager(tmp_path / "tts")
    app._model_busy = False
    app._model_dialog = None
    app._model_jobs = {}
    app._model_paused = set()
    monkeypatch.setattr(app_module.duplex_audio, "detect_cable_devices", lambda: _Devices())
    return app_module, app


def test_model_card_reports_runtime_and_blockers(monkeypatch, tmp_path) -> None:
    from meeting_translate import tts_runtime

    app_module, app = _bare_app(monkeypatch, tmp_path, cable=False)
    monkeypatch.setattr(tts_runtime, "is_ready", lambda: False)
    monkeypatch.setattr(tts_runtime, "find_uv", lambda: "uv")
    app_module.App._refresh_resources(app)
    status, kwargs = app.home.model[-1]
    assert status.startswith("缺语音运行时") and "检测到 uv" in status
    assert kwargs["primary"] == "下载"
    assert app.home.blockers[-1] == ["语音运行时", "语音模型", "虚拟声卡"]

    monkeypatch.setattr(tts_runtime, "is_ready", lambda: True)
    app_module.App._refresh_resources(app)
    assert app.home.model[-1][0].startswith("运行时已就绪 · 模型缺")
    assert app.home.blockers[-1] == ["语音模型", "虚拟声卡"]


def test_entering_duplex_is_refused_while_blocked(monkeypatch, tmp_path) -> None:
    from meeting_translate import tts_runtime

    app_module, app = _bare_app(monkeypatch, tmp_path, cable=True)
    monkeypatch.setattr(tts_runtime, "is_ready", lambda: True)
    shown: list[str] = []
    monkeypatch.setattr(app_module.msg, "info", lambda _p, _t, text, **_kw: shown.append(text))
    app.state = app_module.IDLE
    app.settings = Settings()
    app_module.App._on_home_mode(app, "duplex")
    assert shown and "语音模型" in shown[0]
    assert app.settings.meeting_mode != "duplex"

def test_slogan_rotates_with_fade() -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    from meeting_translate.home_ui import SloganLabel

    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    label = SloganLabel(tuple("甲乙丙丁戊己庚辛壬癸"), interval_ms=200)
    label.show()
    loop = QEventLoop()
    QTimer.singleShot(1200, loop.quit)
    loop.exec()
    assert label._index > 0 and label.text() == "甲乙丙丁戊己庚辛壬癸"[label._index]
    label.hide()
    assert label._effect.opacity() == 1.0  # 隐藏时复位，再显示不会半透明
    label.deleteLater()


def test_tray_click_opens_home_when_idle(monkeypatch) -> None:
    from PySide6.QtWidgets import QSystemTrayIcon

    from meeting_translate import app as app_module

    calls: list[str] = []

    class _Overlay:
        visible = False

        def isVisible(self) -> bool:  # noqa: N802
            return self.visible

        def raise_(self) -> None:
            pass

        def activateWindow(self) -> None:  # noqa: N802
            pass

    app = app_module.App.__new__(app_module.App)
    app.state = app_module.IDLE
    app.overlay = _Overlay()
    monkeypatch.setattr(app, "_show_home", lambda: calls.append("home"), raising=False)
    monkeypatch.setattr(app, "_show_overlay", lambda: calls.append("overlay"), raising=False)
    trigger = QSystemTrayIcon.ActivationReason.Trigger
    app_module.App._on_tray_activated(app, trigger)
    app.overlay.visible = True
    app_module.App._on_tray_activated(app, trigger)
    app_module.App._on_tray_activated(app, QSystemTrayIcon.ActivationReason.Context)
    assert calls == ["home", "overlay"]
