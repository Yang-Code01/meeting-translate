"""Translator 与 Backend 的接缝：不需要 Copilot，也不联网。

这条测试钉住的是「换后端只需要实现 Backend」这个约定：
开始会议、预热、翻译、结束会议全流程都走 backend.create_session。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from meeting_translate.backend import AuthState, Session
from meeting_translate.transcript import Transcript
from meeting_translate.translator import Translator


class FakeSession:
    def __init__(self, name: str, reply: str = "你好") -> None:
        self.name = name
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
        self.started = 0
        self.stopped = 0
        self.systems: list[str] = []

    async def start(self) -> AuthState:
        self.started += 1
        return AuthState(True, login="tester")

    async def stop(self) -> None:
        self.stopped += 1

    async def login(self, token: str) -> AuthState:
        return await self.start()

    async def models(self) -> list:
        return []

    async def quota(self):
        return None

    async def create_session(self, system: str, model: str, effort, usage=None) -> Session:
        s = FakeSession(f"s{len(self.sessions)}")
        self.sessions.append(s)
        self.systems.append(system)
        return s

    def option(self, model: str):
        return None


def _final(fid: int, text: str) -> SimpleNamespace:
    now = time.time()
    return SimpleNamespace(id=fid, source="对方", ja=text, wall_time=now, mono_time=time.monotonic())


def test_meeting_round_trip_through_backend_seam(tmp_path):
    got = []
    done = threading.Event()

    def on_translation(t) -> None:
        got.append(t)
        done.set()

    backend = FakeBackend()
    tr = Translator(on_translation=on_translation, on_status=lambda s: None, on_auth=lambda s: None, backend=backend)
    tr.start().result(10)
    try:
        transcript = Transcript(path=tmp_path / "t.json")
        tr.start_meeting(transcript, "fake-model", "none", 30, 5, []).result(10)

        tr.submit(_final(1, "こんにちは"))
        assert done.wait(10), "翻译回调没有触发"
        assert got[0].texts == ["你好"]

        # 会议会话：先预热一条，再翻译一条；退出时应被关闭
        meeting = [s for s in backend.sessions if any("こんにちは" in x for x in s.sent)]
        assert meeting, "没有找到用于翻译的会话"
        assert meeting[0].sent[0].startswith("（连接测试")
    finally:
        tr.end_meeting().result(10)
        tr.shutdown()

    assert backend.started == 1
    assert backend.stopped == 1
    assert all(s.closed for s in backend.sessions if s.sent), "会话没有被关闭"


def test_switching_backend_replaces_client_and_warms_up(tmp_path):
    """设置里换服务商走的就是这条：关旧后端、启新后端、建立会话仍可用。"""
    first, second = FakeBackend(), FakeBackend()
    tr = Translator(
        on_translation=lambda t: None,
        on_status=lambda s: None,
        on_auth=lambda s: None,
        backend=first,
    )
    tr.start().result(10)
    try:
        state = tr.use_backend(second).result(10)
        assert state.ok and state.login == "tester"
        assert first.stopped == 1
        assert second.started == 1
    finally:
        tr.shutdown()
