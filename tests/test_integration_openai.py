"""Translator + OpenAICompatBackend 端到端：批次翻译真的走 /chat/completions。"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from meeting_translate.openai_backend import OpenAICompatBackend, OpenAIProfile
from meeting_translate.transcript import Transcript
from meeting_translate.translator import Translator


def _final(fid: int, text: str) -> SimpleNamespace:
    now = time.time()
    return SimpleNamespace(id=fid, source="对方", ja=text, wall_time=now, mono_time=time.monotonic())


def test_batch_translation_runs_through_openai_backend(openai_stub, tmp_path):
    openai_stub.reply = "1. 你好\n2. 再见"
    got: list = []
    done = threading.Event()
    backend = OpenAICompatBackend(
        OpenAIProfile(
            name="测试端点",
            base_url=f"http://127.0.0.1:{openai_stub.server_address[1]}/v1",
            api_key="sk-test",
            model="deepseek-chat",
        ),
        on_status=lambda s: None,
        retry_delay=0,
    )
    tr = Translator(
        on_translation=lambda t: (got.append(t), done.set()),
        on_status=lambda s: None,
        on_auth=lambda s: None,
        backend=backend,
    )
    tr.start().result(10)
    try:
        tr.start_meeting(Transcript(path=tmp_path / "t.json"), "deepseek-chat", "none", 30, 5, []).result(10)
        f1, f2 = _final(1, "こんにちは"), _final(2, "さようなら")
        # 同一轮回调里放入两句，保证被合并成一次请求
        tr._loop.call_soon_threadsafe(lambda: (tr._queue.put_nowait(f1), tr._queue.put_nowait(f2)))
        assert done.wait(10), "没有等到译文"
        assert got[0].texts == ["你好", "再见"]
    finally:
        tr.end_meeting().result(10)
        tr.shutdown()

    payload = openai_stub.requests[-1]["body"]
    assert payload["messages"][0]["role"] == "system"
    assert "【对方】こんにちは" in payload["messages"][-1]["content"]


def _running_translator(openai_stub) -> Translator:
    backend = OpenAICompatBackend(
        OpenAIProfile(
            name="运行中端点",
            base_url=f"http://127.0.0.1:{openai_stub.server_address[1]}/v1",
            api_key="sk-test",
            model="deepseek-chat",
        ),
        on_status=lambda s: None,
        retry_delay=0,
    )
    tr = Translator(
        on_translation=lambda t: None,
        on_status=lambda s: None,
        on_auth=lambda s: None,
        backend=backend,
    )
    tr.start().result(10)
    return tr


def test_list_models_for_asks_the_profile_being_edited(openai_stub):
    """设置窗口里还没保存的端点，要能问它自己，而不是问正在运行的那条。"""
    tr = _running_translator(openai_stub)
    try:
        opts = tr.list_models_for(
            {
                "name": "草稿端点",
                "base_url": f"http://127.0.0.1:{openai_stub.server_address[1]}/v1",
                "api_key": "sk-new",
                "model": "",
            }
        ).result(10)
    finally:
        tr.shutdown()
    assert [o.id for o in opts] == ["deepseek-chat", "deepseek-reasoner"]


def test_list_models_for_reports_missing_fields(openai_stub):
    tr = _running_translator(openai_stub)
    try:
        with pytest.raises(RuntimeError, match="接口地址"):
            tr.list_models_for({"name": "草稿端点", "base_url": "", "api_key": "sk-new"}).result(10)
    finally:
        tr.shutdown()


class _RejectingSession:
    async def send(self, text, timeout=None):
        raise RuntimeError("400 请求被拒绝：model_unavailable")

    async def close(self):
        pass


def test_prime_failure_is_raised_and_reported():
    """预热失败必须能传到界面：以前只写日志，用户只看到「会话已建立」。"""
    statuses: list[str] = []
    tr = Translator(on_translation=lambda t: None, on_status=statuses.append, on_auth=lambda s: None)
    with pytest.raises(RuntimeError, match="400 请求被拒绝"):
        asyncio.run(tr._prime(_RejectingSession()))
    assert any("翻译服务返回错误" in s for s in statuses)
