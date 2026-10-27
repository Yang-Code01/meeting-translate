"""双向同传的 TTS 输出：方向路由、切块、队列与打断。"""

from __future__ import annotations

import threading
import time

import numpy as np

from meeting_translate import lang
from meeting_translate.duplex_speech import TtsSpeaker
from meeting_translate.tts_client import TtsAudio


class FakeClient:
    def __init__(self, block: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.block = block
        self.started = threading.Event()
        self.release = threading.Event()

    def synth(self, text: str, engine: str = "sbv2", **kw) -> TtsAudio:
        self.calls.append((text, engine))
        self.started.set()
        if self.block:
            self.release.wait(timeout=3)
        return TtsAudio(np.ones(8, dtype=np.float32), 16000)


class FakePlayer:
    def __init__(self) -> None:
        self.clips: list[np.ndarray] = []
        self.stops = 0

    def enqueue(self, samples: np.ndarray, sample_rate: int, on_start=None, on_done=None) -> None:
        self.clips.append(np.asarray(samples))
        if on_start is not None:
            on_start()
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        self.stops += 1


def wait_for(pred, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def test_routes_by_direction_and_strips_failed_translation() -> None:
    client = FakeClient()
    local, remote = FakePlayer(), FakePlayer()
    speaker = TtsSpeaker(client, local, remote)
    speaker.submit("こんにちは。", lang.JA_ZH)
    speaker.submit("你好。", lang.ZH_JA)
    speaker.submit("（翻译失败）", lang.ZH_JA)
    wait_for(lambda: len(local.clips) == 1 and len(remote.clips) == 1)
    speaker.close()
    assert client.calls[0][1] == "melo"
    assert client.calls[1][1] == "sbv2"
    assert len(client.calls) == 2


def test_stop_discards_inflight_result() -> None:
    client = FakeClient(block=True)
    local, remote = FakePlayer(), FakePlayer()
    speaker = TtsSpeaker(client, local, remote)
    speaker.submit("你好。", lang.ZH_JA)
    assert client.started.wait(timeout=2)
    speaker.stop(lang.ZH_JA)
    client.release.set()
    time.sleep(0.1)
    speaker.close()
    assert remote.clips == []
    assert remote.stops >= 1


def test_chunk_text_splits_long_text_and_keeps_short_text() -> None:
    speaker = TtsSpeaker(FakeClient(), FakePlayer(), FakePlayer(), max_chars=12)
    assert speaker._chunk_text("短句") == ["短句"]
    chunks = speaker._chunk_text("第一句话。第二句话，第三句话！第四句话？")
    assert len(chunks) >= 2
    assert all(len(c) <= 12 for c in chunks)
    assert "".join(chunks) == "第一句话。第二句话，第三句话！第四句话？"


def test_last_audio_exposes_latest_tts_result() -> None:
    client = FakeClient()
    local, remote = FakePlayer(), FakePlayer()
    speaker = TtsSpeaker(client, local, remote)
    speaker.submit("こんにちは。", lang.ZH_JA)
    wait_for(lambda: speaker.last_audio(lang.ZH_JA) is not None)
    audio = speaker.last_audio(lang.ZH_JA)
    speaker.close()
    assert audio is not None
    assert audio.sample_rate == 16000


def test_play_events_report_text_and_state() -> None:
    client = FakeClient()
    local, remote = FakePlayer(), FakePlayer()
    events: list[tuple[str, str, str]] = []
    speaker = TtsSpeaker(client, local, remote, on_event=lambda d, t, s: events.append((d, t, s)))
    speaker.submit("こんにちは。", lang.ZH_JA)
    wait_for(lambda: any(e[2] == "done" for e in events))
    speaker.close()
    assert (lang.ZH_JA, "こんにちは。", "start") in events
    assert (lang.ZH_JA, "こんにちは。", "done") in events


def test_queue_keeps_all_sentences() -> None:
    client = FakeClient(block=True)
    local, remote = FakePlayer(), FakePlayer()
    speaker = TtsSpeaker(client, local, remote, max_queue=2)
    for i in range(5):
        speaker.submit(f"第{i}句。", lang.ZH_JA)
    assert client.started.wait(timeout=2)
    client.release.set()
    wait_for(lambda: len(remote.clips) >= 5, timeout=3)
    speaker.close()
    # 不跳句：5 句都要合成并交给播放器。
    assert len(client.calls) == 5
