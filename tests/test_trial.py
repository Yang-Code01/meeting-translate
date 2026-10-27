from __future__ import annotations

import numpy as np

from meeting_translate.trial import TrialResult, resample_audio


def test_resample_changes_length_and_keeps_float32() -> None:
    data = np.sin(np.linspace(0, 1, 4800, endpoint=False)).astype(np.float32)
    out = resample_audio(data, 48000, 16000)
    assert out.dtype == np.float32
    assert out.size == 1600
    assert np.max(np.abs(out)) <= 1.0


def test_resample_empty_is_empty() -> None:
    out = resample_audio(np.array([], dtype=np.float32), 48000, 16000)
    assert out.size == 0


def test_trial_result_fields() -> None:
    audio = np.zeros(16, dtype=np.float32)
    result = TrialResult("你好", "こんにちは", audio, 16000, 1.25)
    assert result.chinese == "你好"
    assert result.japanese == "こんにちは"
    assert result.audio is audio
    assert result.latency == 1.25


def test_has_speech_text_rejects_punctuation_only() -> None:
    from meeting_translate.trial import has_speech_text

    assert not has_speech_text("。，！？")
    assert has_speech_text("你好")
    assert has_speech_text("こんにちは")
