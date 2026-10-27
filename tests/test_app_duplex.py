"""App 双向分支的轻量测试；不启动 Qt 窗口。"""

from __future__ import annotations

import numpy as np
import pytest

from meeting_translate import config, lang, transcript
from meeting_translate.app import App
from meeting_translate.asr import ME, THEM
from meeting_translate.joiner import Pending
from meeting_translate.transcript import Entry, Transcript


@pytest.fixture(autouse=True)
def isolated_transcripts(tmp_path, monkeypatch):
    """Transcript 默认写到 %APPDATA%/meeting-translate/transcripts。

    这些用例用 App.__new__ 跳过 __init__，不会建目录：CI 上没有该目录就直接
    FileNotFoundError，本地则会把测试条目写进真实会议记录。统一指到 tmp_path。
    """
    target = tmp_path / "transcripts"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(transcript, "TRANSCRIPT_DIR", target)
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", target)
    return target


class FakeOverlay:
    def __init__(self) -> None:
        self.entries = []
        self.renders = 0

    def render(self) -> None:
        self.renders += 1


class FakeTranslator:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, final) -> None:
        self.submitted.append(final)


class FakePassthrough:
    def __init__(self) -> None:
        self.routed = []

    def route(self, final):
        self.routed.append(final)
        return "remote"


class FakeAsr:
    def __init__(self) -> None:
        self.frames = []

    def push(self, source, samples) -> None:
        self.frames.append((source, list(samples)))


class FakeLive:
    def __init__(self) -> None:
        self.frames = []

    def write(self, samples) -> None:
        self.frames.append(list(samples))


class FakeSettings:
    meeting_mode = "duplex"
    duplex_zh_ja = True
    duplex_ja_zh = True


def make_app() -> App:
    app = App.__new__(App)
    app.transcript = Transcript(direction="bi")
    app.transcript.add(Entry(1, 1.0, ME, "日本語", direction=lang.JA_ZH))
    app.overlay = FakeOverlay()
    app.translator = FakeTranslator()
    app._passthrough = FakePassthrough()
    app._speaker = None
    return app


def test_same_language_final_passthrough_skips_translation() -> None:
    app = make_app()
    pending = Pending(
        1, ME, "日本語", 1.0, 1.0,
        lang="ja", audio=np.ones(4, dtype=np.float32),
    )
    app._on_join_commit(pending, True)
    assert len(app._passthrough.routed) == 1
    assert app.translator.submitted == []
    assert app.transcript.entries[0].zh == "日本語"


def test_other_language_final_goes_to_translator_with_audio() -> None:
    app = make_app()
    pending = Pending(
        1, ME, "你好", 1.0, 1.0,
        lang="zh", audio=np.ones(4, dtype=np.float32),
    )
    app._on_join_commit(pending, True)
    assert app._passthrough.routed == []
    assert len(app.translator.submitted) == 1
    final = app.translator.submitted[0]
    assert final.direction == lang.ZH_JA
    assert final.audio is pending.audio


def test_switch_off_skips_translation() -> None:
    app = make_app()
    app.settings = FakeSettings()
    app.settings.duplex_zh_ja = False
    pending = Pending(1, ME, "你好", 1.0, 1.0, lang="zh")
    app._on_join_commit(pending, True)
    assert app.translator.submitted == []
    assert app.transcript.entries[0].zh == ""


def test_switch_gates_by_source_side() -> None:
    app = make_app()
    app.settings = FakeSettings()
    app.settings.duplex_zh_ja = True
    app.settings.duplex_ja_zh = False
    pending = Pending(1, THEM, "こんにちは", 1.0, 1.0, lang="ja")
    app._on_join_commit(pending, True)
    assert app.translator.submitted == []
    assert app.transcript.entries[0].zh == ""


def test_capture_routes_raw_audio_when_switch_off() -> None:
    app = make_app()
    app.settings = FakeSettings()
    app.settings.duplex_zh_ja = False
    app.settings.duplex_ja_zh = False
    app.asr = FakeAsr()
    app._direct_remote = FakeLive()
    app._direct_local = FakeLive()
    App._on_capture_audio(app, ME, [0.1, 0.2])
    App._on_capture_audio(app, THEM, [0.3, 0.4])
    assert len(app.asr.frames) == 2
    assert app._direct_remote.frames == [[0.1, 0.2]]
    assert app._direct_local.frames == [[0.3, 0.4]]


def test_speech_start_stops_only_same_side_tts() -> None:
    app = make_app()

    class Speaker:
        def __init__(self) -> None:
            self.stops: list[str | None] = []

        def stop(self, direction: str | None = None) -> None:
            self.stops.append(direction)

    speaker = Speaker()
    app._speaker = speaker
    app._on_speech_start(ME)
    assert speaker.stops == [lang.JA_ZH]
    app._on_speech_start(THEM)
    assert speaker.stops == [lang.JA_ZH, lang.ZH_JA]
