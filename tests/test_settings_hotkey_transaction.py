from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from meeting_translate import config
import meeting_translate.app as app_module
from meeting_translate.app import App, IDLE
from meeting_translate.config import Settings
from meeting_translate.hotkey import parse


class Field:
    def __init__(self, value: str) -> None:
        self.value = value

    def text(self) -> str:
        return self.value


class Choice:
    def __init__(self, value) -> None:
        self.value = value

    def currentData(self):
        return self.value

    def currentText(self) -> str:
        return self.value


class FakeDialog:
    def __init__(self, settings: Settings, hotkeys: dict[str, str], opacity: float = 0.4) -> None:
        self.settings = settings
        self.hotkey = Field(hotkeys["hotkey"])
        self.selection_hotkey = Field(hotkeys["selection_hotkey"])
        self.screenshot_hotkey = Field(hotkeys["screenshot_hotkey"])
        self.provider = Choice(settings.provider)
        self.model = Choice(settings.model)
        self.effort = Choice(settings.reasoning_effort)
        self.opacity = opacity
        self.applied = False
        self.accepted = False

    def apply(self, *, hotkeys: dict[str, str] | None = None) -> None:
        self.applied = True
        values = hotkeys or {
            "hotkey": self.hotkey.text(),
            "selection_hotkey": self.selection_hotkey.text(),
            "screenshot_hotkey": self.screenshot_hotkey.text(),
        }
        for name, value in values.items():
            setattr(self.settings, name, value)
        self.settings.opacity = self.opacity
        self.settings.save()

    def accept(self) -> None:
        self.accepted = True


class FakeGlobalHotkey:
    failures: set[str] = set()
    active: dict[tuple[int, int], "FakeGlobalHotkey"] = {}

    def __init__(self, spec, callback) -> None:
        self.spec = spec
        self.callback = callback
        self.error = None
        self.stopped = False
        self._combination = parse(spec)

    def start(self) -> None:
        if self.spec in self.failures:
            self.error = f"快捷键 {self.spec} 已被其他程序占用"
        elif self._combination in self.active:
            self.error = f"快捷键 {self.spec} 已被占用"
        else:
            self.active[self._combination] = self

    def stop(self) -> None:
        self.stopped = True
        if self.active.get(self._combination) is self:
            del self.active[self._combination]


class FakeSignal:
    def emit(self) -> None:
        pass


@pytest.fixture
def transaction(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config, "MODEL_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "GlobalHotkey", FakeGlobalHotkey)
    monkeypatch.setattr(app_module.msg, "warn", lambda *args: warnings.append(args[-1]))
    warnings: list[str] = []
    FakeGlobalHotkey.failures = set()
    FakeGlobalHotkey.active = {}

    settings = Settings()
    settings.hotkey = "Ctrl+Alt+J"
    settings.selection_hotkey = "Alt+M"
    settings.screenshot_hotkey = "Alt+N"
    settings.save()

    app = App.__new__(App)
    app.settings = settings
    app.state = IDLE
    app.overlay = SimpleNamespace(hide=lambda: None, apply_style=lambda _settings: None)
    app.selection_popup = SimpleNamespace(apply_style=lambda _settings: None)
    app._show_usage = lambda: None
    app._set_status = lambda _message: None
    app.joiner = None
    app.hotkey = FakeGlobalHotkey(settings.hotkey, FakeSignal().emit)
    app.selection_hotkey = FakeGlobalHotkey(settings.selection_hotkey, FakeSignal().emit)
    app.screenshot_hotkey = FakeGlobalHotkey(settings.screenshot_hotkey, FakeSignal().emit)
    app.bridge = SimpleNamespace(
        hotkey=FakeSignal(), selection_hotkey=FakeSignal(), screenshot_hotkey=FakeSignal()
    )
    app.hotkey.start()
    app.selection_hotkey.start()
    app.screenshot_hotkey.start()
    return app, settings, warnings


def current_values(settings: Settings) -> dict[str, str]:
    return {
        "hotkey": settings.hotkey,
        "selection_hotkey": settings.selection_hotkey,
        "screenshot_hotkey": settings.screenshot_hotkey,
    }


def dialog_values(settings: Settings, **changes: str) -> dict[str, str]:
    values = current_values(settings)
    values.update(changes)
    return values


@pytest.mark.parametrize(
    ("field", "candidate", "conflict_names"),
    [
        ("selection_hotkey", "ctrl+alt+j", ("会议实时翻译", "划词翻译")),
        ("screenshot_hotkey", "ALT+m", ("划词翻译", "截图翻译")),
        ("screenshot_hotkey", "CTRL+ALT+J", ("会议实时翻译", "截图翻译")),
    ],
)
def test_duplicate_hotkey_keeps_dialog_open_and_does_not_apply_any_setting(
    transaction, field, candidate, conflict_names
):
    app, settings, warnings = transaction
    before = config.CONFIG_FILE.read_bytes()
    original_hotkeys = (app.hotkey, app.selection_hotkey, app.screenshot_hotkey)
    values = dialog_values(settings, **{field: candidate})
    dlg = FakeDialog(settings, values)

    app._apply_settings(dlg)

    assert dlg.accepted is False
    assert dlg.applied is False
    assert config.CONFIG_FILE.read_bytes() == before
    assert current_values(settings) == {
        "hotkey": "Ctrl+Alt+J",
        "selection_hotkey": "Alt+M",
        "screenshot_hotkey": "Alt+N",
    }
    assert all(not hotkey.stopped for hotkey in original_hotkeys)
    assert all(name in warnings[-1] for name in conflict_names)


def test_legacy_hotkey_conflicts_do_not_block_unrelated_settings(transaction):
    app, settings, warnings = transaction
    settings.selection_hotkey = "Ctrl+Alt+J"
    settings.save()
    dlg = FakeDialog(settings, current_values(settings), opacity=0.6)

    app._apply_settings(dlg)

    saved = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    assert dlg.accepted is True
    assert saved["selection_hotkey"] == "Ctrl+Alt+J"
    assert saved["opacity"] == 0.6
    assert warnings == []


def test_registration_failure_rolls_back_only_that_hotkey_and_saves_other_settings(transaction):
    app, settings, warnings = transaction
    old_meeting, old_selection, old_screenshot = app.hotkey, app.selection_hotkey, app.screenshot_hotkey
    FakeGlobalHotkey.failures = {"Ctrl+Shift+S"}
    dlg = FakeDialog(
        settings,
        dialog_values(settings, hotkey="Ctrl+Shift+J", screenshot_hotkey="Ctrl+Shift+S"),
        opacity=0.35,
    )

    app._apply_settings(dlg)

    saved = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    assert dlg.accepted is True
    assert saved["hotkey"] == "Ctrl+Shift+J"
    assert saved["screenshot_hotkey"] == "Alt+N"
    assert saved["opacity"] == 0.35
    assert old_meeting.stopped is True
    assert old_screenshot.stopped is False
    assert app.hotkey.spec == "Ctrl+Shift+J"
    assert app.screenshot_hotkey is old_screenshot
    assert "截图翻译" in warnings[-1] and "Ctrl+Shift+S" in warnings[-1]


def test_hotkey_can_move_to_a_combination_released_by_another_changed_hotkey(transaction):
    app, settings, _warnings = transaction
    dlg = FakeDialog(
        settings,
        dialog_values(
            settings,
            hotkey="Alt+M",
            selection_hotkey="Ctrl+Shift+M",
        ),
    )

    app._apply_settings(dlg)

    assert settings.hotkey == "Alt+M"
    assert settings.selection_hotkey == "Ctrl+Shift+M"
    assert app.hotkey.spec == "Alt+M"
    assert app.selection_hotkey.spec == "Ctrl+Shift+M"


def test_unparseable_hotkey_is_rolled_back_while_other_settings_are_saved(transaction):
    app, settings, warnings = transaction
    old_selection = app.selection_hotkey
    dlg = FakeDialog(settings, dialog_values(settings, selection_hotkey="not a hotkey"), opacity=0.5)

    app._apply_settings(dlg)

    saved = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    assert dlg.accepted is True
    assert saved["selection_hotkey"] == "Alt+M"
    assert saved["opacity"] == 0.5
    assert app.selection_hotkey is old_selection and not old_selection.stopped
    assert "划词翻译" in warnings[-1] and "not a hotkey" in warnings[-1]
