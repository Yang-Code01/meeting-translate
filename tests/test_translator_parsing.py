"""翻译链路的纯函数回归：批次解析、校正格式解析、推理强度回退、用量累计。"""

from __future__ import annotations

from meeting_translate.backend import ModelOption, clamp_effort
from meeting_translate.translator import Usage, _parse_batch, _parse_corrected


def test_parse_batch_single_sentence_returns_whole_reply():
    assert _parse_batch("  你好  ", 1) == ["你好"]


def test_parse_batch_maps_numbered_lines():
    reply = "1. 你好\n2. 再见"
    assert _parse_batch(reply, 2) == ["你好", "再见"]


def test_parse_corrected_separates_source_and_translation():
    reply = "1原：会議は三時からです\n1訳：会议从三点开始"
    assert _parse_corrected(reply, 1) == (["会議は三時からです"], ["会议从三点开始"])


def test_parse_corrected_strips_speaker_tag():
    reply = "1原：【对方】資料を送ります\n1译：【对方】我把资料发过去"
    sources, texts = _parse_corrected(reply, 1)
    assert sources == ["資料を送ります"]
    assert texts == ["我把资料发过去"]


def test_parse_corrected_accepts_japanese_translation_marker():
    reply = "1原：会議は三時からです\n1訳：会议从三点开始"
    assert _parse_corrected(reply, 1) == (["会議は三時からです"], ["会议从三点开始"])


def test_parse_corrected_returns_none_without_translation_lines():
    assert _parse_corrected("会議は三時からです", 1) is None


def _option(supports_effort: bool, efforts: list[str]) -> ModelOption:
    return ModelOption(
        id="m",
        name="M",
        input_price=1.0,
        output_price=2.0,
        supports_effort=supports_effort,
        efforts=efforts,
        enabled=True,
    )


def test_clamp_effort_keeps_supported_value():
    assert clamp_effort(_option(True, ["low", "high"]), "high") == ("high", "")


def test_clamp_effort_drops_value_when_model_lacks_support():
    effort, note = clamp_effort(_option(False, []), "high")
    assert effort is None
    assert "不支持推理强度" in note


def test_clamp_effort_falls_back_to_first_supported_value():
    effort, note = clamp_effort(_option(True, ["low"]), "high")
    assert effort == "low"
    assert "已改用 low" in note


def test_usage_keeps_max_per_session_and_sums_sessions():
    seen: list[float] = []
    usage = Usage(seen.append)
    usage.update("a", 1_000_000_000)
    usage.update("a", 500_000_000)  # 会话内是累计值，旧的更小应被忽略
    usage.update("b", 2_000_000_000)
    assert usage.aic == 3.0
    assert seen[-1] == 3.0
