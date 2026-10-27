"""双向同传配置与断句音频的回归测试。"""

from __future__ import annotations

import json

import numpy as np

from meeting_translate import config, lang
from meeting_translate.joiner import Joiner


def test_duplex_settings_switch_asr_and_fillers() -> None:
    s = config.Settings(meeting_mode="duplex")
    assert s.effective_engine() == "sensevoice"
    assert s.asr_language() == "auto"
    assert set(s.active_fillers()) == set(s.fillers + s.fillers_zh)


def test_v3_bidirectional_direction_migrates_to_duplex(tmp_path, monkeypatch) -> None:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"version": 3, "direction": "bi"}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", p)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    s = config.Settings.load()
    assert s.meeting_mode == "duplex"
    assert s.version == config.SETTINGS_VERSION
    assert json.loads(p.read_text(encoding="utf-8"))["meeting_mode"] == "duplex"


def test_joiner_concatenates_audio_across_pieces() -> None:
    opened = []
    committed = []
    j = Joiner(
        0.6,
        99,
        [],
        on_open=opened.append,
        on_update=lambda p: None,
        on_commit=lambda p, translate: committed.append(p),
        lang="zh",
    )
    j.add("我", "我", 1.0, 1.0, lang="zh", audio=np.ones(4, dtype=np.float32))
    j.add("我", "们", 1.1, 1.1, lang="zh", audio=np.ones(3, dtype=np.float32))
    p = opened[0]
    assert p.audio is not None
    assert p.audio.size == 7
    assert committed == []


def test_joiner_uses_per_source_language() -> None:
    seen = []
    j = Joiner(
        0.6,
        99,
        [],
        on_open=lambda p: seen.append(p.lang),
        on_update=lambda p: None,
        on_commit=lambda p, translate: None,
        lang="ja",
        langs={"我": "zh", "对方": "ja"},
    )
    j.add("我", "你", 1.0, 1.0)
    j.add("对方", "こんにちは", 1.0, 1.0)
    assert seen == ["zh", "ja"]


def test_lang_bi_is_not_a_translation_direction() -> None:
    assert lang.ZH_JA == "zh-ja"
    assert lang.JA_ZH == "ja-zh"
