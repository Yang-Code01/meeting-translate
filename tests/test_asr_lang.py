"""ASR 语言标记：SenseVoice 的 <|zh|> 映射与 Final 默认字段。"""

from __future__ import annotations

from meeting_translate.asr import Final, normalize_language


def test_normalize_sensevoice_language_tags():
    assert normalize_language("<|zh|>") == "zh"
    assert normalize_language("<|ja|>") == "ja"
    assert normalize_language("<|en|>") == "en"
    assert normalize_language("", "ja") == "ja"
    assert normalize_language(None, "auto") == ""


def test_final_has_optional_language_and_audio():
    f = Final(1, "对方", "こんにちは", 0.0, 0.0)
    assert f.lang == ""
    assert f.audio is None
