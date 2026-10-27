"""双向会议：一个 Translator 实例按 direction 分流会话与回调。"""

from __future__ import annotations

import threading
import time

from meeting_translate import lang
from meeting_translate.asr import Final
from meeting_translate.backend import AuthState
from meeting_translate.transcript import Transcript
from meeting_translate.translator import Translator


class FakeSession:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str, timeout: float | None = None) -> str:
        self.sent.append(text)
        return self.reply

    async def close(self) -> None:
        self.closed = True


class FakeBackend:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.systems: list[str] = []

    async def start(self) -> AuthState:
        return AuthState(True, login="tester")

    async def stop(self) -> None:
        pass

    async def login(self, token: str) -> AuthState:
        return await self.start()

    async def models(self) -> list:
        return []

    async def quota(self):
        return None

    async def create_session(self, system: str, model: str, effort, usage=None):
        reply = "日本語訳" if "把中文会议发言翻译成日语" in system else "中文译文"
        s = FakeSession(reply)
        self.sessions.append(s)
        self.systems.append(system)
        return s

    def option(self, model: str):
        return None


def _final(fid: int, source: str, text: str, direction: str) -> Final:
    return Final(fid, source, text, time.time(), time.monotonic(), lang="zh" if direction == lang.ZH_JA else "ja", direction=direction)


def test_two_directions_get_separate_sessions_and_callbacks(tmp_path):
    backend = FakeBackend()
    got: list = []
    done = threading.Event()

    def on_translation(t) -> None:
        got.append(t)
        if len(got) == 2:
            done.set()

    tr = Translator(on_translation=on_translation, on_status=lambda s: None, on_auth=lambda s: None, backend=backend)
    tr.start().result(10)
    try:
        transcript = Transcript(path=tmp_path / "bi.json", direction="bi")
        tr.start_meeting(transcript, "fake-model", "none", 30, 5, [], directions=[lang.JA_ZH, lang.ZH_JA]).result(10)

        tr.submit(_final(1, "对方", "こんにちは", lang.JA_ZH))
        tr.submit(_final(2, "我", "你好", lang.ZH_JA))
        assert done.wait(10), "双向翻译回调没有触发"

        by_direction = {t.direction: t for t in got}
        assert by_direction[lang.JA_ZH].texts == ["中文译文"]
        assert by_direction[lang.ZH_JA].texts == ["日本語訳"]
        assert len(backend.sessions) == 2  # 两个方向各一个会议会话
        assert any("日语" in s and "简体中文" in s for s in backend.systems)
        assert any("中文" in s and "日语" in s for s in backend.systems)
    finally:
        tr.end_meeting().result(10)
        tr.shutdown()
