"""双向记录的 Markdown/JSON 往返与按方向取上下文。"""

from __future__ import annotations

from meeting_translate import lang
from meeting_translate.transcript import Entry, Transcript


def _entry(eid: int, source: str, text: str, translated: str, direction: str, ts: float) -> Entry:
    return Entry(
        eid,
        ts,
        source,
        text,
        translated,
        direction=direction,
        src_lang="zh" if direction == lang.ZH_JA else "ja",
        dst_lang="ja" if direction == lang.ZH_JA else "zh",
    )


def test_bi_markdown_roundtrip_keeps_direction_and_languages(tmp_path):
    t = Transcript(path=tmp_path / "meeting.json", direction="bi")
    base = t.started.timestamp()
    t.add(_entry(1, "我", "你好", "こんにちは", lang.ZH_JA, base))
    t.add(_entry(2, "对方", "おはよう", "早上好", lang.JA_ZH, base + 1))

    md = t.to_markdown()
    assert "> 模式：中日双向同传" in md
    assert "【我·中→日】你好" in md
    assert "【对方·日→中】おはよう" in md

    loaded = Transcript.load_markdown(t.md_path)
    assert loaded.direction == "bi"
    assert [(e.source, e.ja, e.zh, e.direction) for e in loaded.entries] == [
        ("我", "你好", "こんにちは", lang.ZH_JA),
        ("对方", "おはよう", "早上好", lang.JA_ZH),
    ]
    assert loaded.last(5, direction=lang.ZH_JA)[0].ja == "你好"
    assert loaded.last(5, direction=lang.JA_ZH)[0].ja == "おはよう"


def test_bi_json_roundtrip(tmp_path):
    t = Transcript(path=tmp_path / "meeting.json", direction="bi")
    t.add(_entry(1, "我", "你好", "こんにちは", lang.ZH_JA, t.started.timestamp()))

    loaded = Transcript.load(t.json_path)
    assert loaded.direction == "bi"
    assert loaded.entries[0].direction == lang.ZH_JA
    assert loaded.entries[0].src_lang == "zh"
    assert loaded.entries[0].dst_lang == "ja"


def test_single_direction_markdown_still_uses_old_header(tmp_path):
    t = Transcript(path=tmp_path / "meeting.json", direction=lang.JA_ZH)
    t.add(Entry(1, t.started.timestamp(), "对方", "こんにちは", "你好"))
    md = t.to_markdown()
    assert "> 模式" not in md
    loaded = Transcript.load_markdown(t.md_path)
    assert loaded.direction == lang.JA_ZH
    assert loaded.entries[0].direction is None
