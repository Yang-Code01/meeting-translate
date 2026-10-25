"""翻译队列的回归：批次合并、校正模式、失败占位。

用假 session 顶替 Copilot 会话，不联网、不需要登录。
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from meeting_translate.translator import Translator


class FakeSession:
    """只实现 Translator._work 用到的 send / close。"""

    def __init__(self, reply: str | None = None, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str, timeout: float | None = None) -> str:
        self.sent.append(text)
        if self.error is not None:
            raise self.error
        return self.reply

    async def close(self) -> None:
        self.closed = True


def _final(fid: int, text: str, source: str = "对方") -> SimpleNamespace:
    now = time.time()
    return SimpleNamespace(id=fid, source=source, ja=text, wall_time=now, mono_time=time.monotonic())


def _run(finals: list[SimpleNamespace], session: FakeSession, correct: bool = False):
    got: list = []
    statuses: list[str] = []
    done = threading.Event()

    def on_translation(t) -> None:
        got.append(t)
        done.set()

    tr = Translator(on_translation=on_translation, on_status=statuses.append, on_auth=lambda s: None)
    tr._thread.start()

    async def setup() -> None:
        tr._queue = asyncio.Queue()
        tr._send_lock = asyncio.Lock()
        tr._session = session
        tr._transcript = None
        tr._rotate_seconds = 0
        tr._correct = correct
        tr._model = "test-model"
        tr._context_n = 5
        for f in finals:
            tr._queue.put_nowait(f)
        tr._worker = asyncio.create_task(tr._work())

    tr._call(setup()).result(5)
    try:
        assert done.wait(10), "翻译回调没有触发"
    finally:
        async def teardown() -> None:
            tr._worker.cancel()
            try:
                await tr._worker
            except asyncio.CancelledError:
                pass

        try:
            tr._call(teardown()).result(5)
        finally:
            tr._loop.call_soon_threadsafe(tr._loop.stop)
            tr._thread.join(5)
    return got, statuses


def test_single_sentence_is_translated_and_speaker_tag_kept_in_request():
    session = FakeSession(reply="你好")
    got, _ = _run([_final(1, "こんにちは")], session)
    assert got[0].texts == ["你好"]
    assert got[0].ids == [1]
    assert got[0].sources is None
    assert "【对方】こんにちは" in session.sent[0]


def test_queued_sentences_are_batched_and_split_by_number():
    session = FakeSession(reply="1. 你好\n2. 再见")
    got, _ = _run([_final(1, "こんにちは"), _final(2, "さようなら")], session)
    assert got[0].ids == [1, 2]
    assert got[0].texts == ["你好", "再见"]
    assert len(session.sent) == 1  # 两句合并成一次请求


def test_failure_writes_placeholder_and_reports_status():
    session = FakeSession(error=RuntimeError("boom"))
    got, statuses = _run([_final(1, "こんにちは")], session)
    assert got[0].texts == ["（翻译失败）"]
    assert any("翻译失败" in s for s in statuses)


def test_correct_mode_returns_corrected_sources():
    session = FakeSession(reply="1原：会議は\n1訳：会议是")
    got, _ = _run([_final(1, "かいぎは")], session, correct=True)
    assert got[0].sources == ["会議は"]
    assert got[0].texts == ["会议是"]
