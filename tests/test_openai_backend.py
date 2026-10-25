"""OpenAI 兼容后端的契约测试：本地假端点，不联网、不需要真 key。"""

from __future__ import annotations

import asyncio

import pytest

from meeting_translate.openai_backend import OpenAICompatBackend, OpenAIProfile
from meeting_translate.usage import Usage

REPLY = "你好"


def _profile(srv, **kw) -> OpenAIProfile:
    fields = dict(
        name="测试端点",
        base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
        api_key="sk-test",
        model="deepseek-chat",
    )
    fields.update(kw)
    return OpenAIProfile(**fields)


def _run(coro_factory):
    async def main():
        return await coro_factory()

    return asyncio.run(main())


def test_request_carries_system_prompt_key_and_model(openai_stub):
    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub), on_status=lambda s: None, retry_delay=0)
        await backend.start()
        try:
            s = await backend.create_session("你是翻译", "deepseek-chat")
            return await s.send("こんにちは")
        finally:
            await backend.stop()

    assert _run(scenario) == REPLY
    req = openai_stub.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["auth"] == "Bearer sk-test"
    assert req["body"]["model"] == "deepseek-chat"
    assert req["body"]["messages"] == [
        {"role": "system", "content": "你是翻译"},
        {"role": "user", "content": "你是翻译\n\nこんにちは"},
    ]


def test_history_accumulates_across_turns(openai_stub):
    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub), on_status=lambda s: None, retry_delay=0)
        await backend.start()
        try:
            s = await backend.create_session("sys", "deepseek-chat")
            await s.send("一句目")
            await s.send("二句目")
        finally:
            await backend.stop()

    _run(scenario)
    second = openai_stub.requests[1]["body"]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[2]["content"] == REPLY


def test_rate_limit_is_retried_once(openai_stub):
    openai_stub.responses.append((429, {"error": "rate limited"}))

    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub), on_status=lambda s: None, retry_delay=0)
        await backend.start()
        try:
            s = await backend.create_session("sys", "deepseek-chat")
            return await s.send("こんにちは")
        finally:
            await backend.stop()

    assert _run(scenario) == REPLY
    assert len(openai_stub.requests) == 2


def test_tokens_are_accumulated_per_request(openai_stub):
    usage = Usage()

    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub), on_status=lambda s: None, retry_delay=0)
        await backend.start()
        try:
            s = await backend.create_session("sys", "deepseek-chat", usage=usage)
            await s.send("一")
            await s.send("二")
        finally:
            await backend.stop()

    _run(scenario)
    assert usage.tokens == (20, 4)


def test_client_error_rolls_back_the_turn(openai_stub):
    openai_stub.responses.append((400, {"error": {"message": "bad model"}}))

    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub), on_status=lambda s: None, retry_delay=0)
        await backend.start()
        try:
            s = await backend.create_session("sys", "deepseek-chat")
            with pytest.raises(RuntimeError) as e:
                await s.send("こんにちは")
            assert "400" in str(e.value)
            assert [m["role"] for m in s.messages] == ["system"]
            await s.send("二句目")
        finally:
            await backend.stop()

    _run(scenario)
    assert [m["role"] for m in openai_stub.requests[1]["body"]["messages"]] == ["system", "user"]


def test_models_are_listed_from_endpoint(openai_stub):
    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub), on_status=lambda s: None, retry_delay=0)
        await backend.start()
        try:
            return await backend.models()
        finally:
            await backend.stop()

    assert [m.id for m in _run(scenario)] == ["deepseek-chat", "deepseek-reasoner"]


def test_blank_model_falls_back_to_first_listed(openai_stub):
    """端点配了 key 没填模型名时，不能把空串发给服务端（400 model_unavailable）。"""

    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub, model=""), on_status=lambda s: None, retry_delay=0)
        await backend.start()
        try:
            s = await backend.create_session("sys", "")
            return await s.send("こんにちは")
        finally:
            await backend.stop()

    assert _run(scenario) == REPLY
    assert openai_stub.requests[-1]["body"]["model"] == "deepseek-chat"


def test_start_fails_loudly_when_no_model_can_be_resolved(openai_stub):
    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub, model=""), on_status=lambda s: None, retry_delay=0)

        async def empty():
            return []

        backend.models = empty  # type: ignore[method-assign]
        return await backend.start()

    state = _run(scenario)
    assert not state.ok
    assert "模型名" in (state.message or "")


def test_session_refuses_blank_model_instead_of_sending_it(openai_stub):
    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub, model=""), on_status=lambda s: None, retry_delay=0)

        async def empty():
            return []

        backend.models = empty  # type: ignore[method-assign]
        with pytest.raises(RuntimeError) as e:
            await backend.create_session("sys", "")
        return str(e.value)

    assert "模型名" in _run(scenario)
    assert openai_stub.requests == []


def test_loopback_endpoints_bypass_the_system_proxy():
    from meeting_translate.openai_backend import is_loopback

    assert is_loopback("http://127.0.0.1:8407/v1")
    assert is_loopback("http://127.1.2.3:8080/v1")
    assert is_loopback("http://localhost:11434/v1")
    assert is_loopback("http://[::1]:8407/v1")
    assert not is_loopback("https://api.deepseek.com")
    assert not is_loopback("http://192.168.1.20:8080/v1")


def test_reasoning_payload_matches_the_provider():
    from meeting_translate.openai_backend import reasoning_payload

    assert reasoning_payload("high", "https://api.deepseek.com") == {
        "reasoning_effort": "high",
        "thinking": {"type": "enabled"},
    }
    assert reasoning_payload("max", "http://127.0.0.1:8407/v1") == {"reasoning_effort": "max"}
    assert reasoning_payload("none", "https://api.deepseek.com") == {"thinking": {"type": "disabled"}}
    assert reasoning_payload("none", "http://127.0.0.1:8407/v1") == {}
    assert reasoning_payload("default", "https://api.deepseek.com") == {}
    assert reasoning_payload(None, "https://api.deepseek.com") == {}


def test_session_sends_the_selected_reasoning_effort(openai_stub):
    async def scenario():
        backend = OpenAICompatBackend(_profile(openai_stub), on_status=lambda s: None, retry_delay=0)
        await backend.start()
        try:
            s = await backend.create_session("sys", "deepseek-chat", "high")
            return await s.send("こんにちは")
        finally:
            await backend.stop()

    assert _run(scenario) == REPLY
    body = openai_stub.requests[0]["body"]
    assert body["reasoning_effort"] == "high"
    assert "thinking" not in body


def test_reasoning_payload_can_disable_thinking():
    from meeting_translate.openai_backend import reasoning_payload

    assert reasoning_payload("none", "http://127.0.0.1:8407/v1", True) == {
        "thinking": {"type": "disabled"}
    }
    assert reasoning_payload("low", "http://127.0.0.1:8407/v1", True) == {"reasoning_effort": "low"}


def test_model_metadata_drives_effort_choices_and_clamping(openai_stub):
    openai_stub.model_data = {
        "data": [
            {
                "id": "deepseek-chat",
                "reasoning_supported_efforts": ["low", "high", "xhigh"],
                "can_disable_thinking": True,
                "reasoning_default_effort": "high",
            }
        ]
    }
    statuses: list[str] = []

    async def scenario():
        backend = OpenAICompatBackend(
            _profile(openai_stub), on_status=statuses.append, retry_delay=0
        )
        await backend.start()
        try:
            options = await backend.models()
            assert options[0].can_disable_thinking is True
            assert options[0].default_effort == "high"
            assert options[0].efforts == ["low", "high", "xhigh"]
            session = await backend.create_session("sys", "deepseek-chat", "medium")
            await session.send("こんにちは")
            body = openai_stub.requests[-1]["body"]
            assert body["reasoning_effort"] == "low"
            assert "thinking" not in body
            session = await backend.create_session("sys", "deepseek-chat", "none")
            await session.send("こんにちは")
            body = openai_stub.requests[-1]["body"]
            assert body["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in body
        finally:
            await backend.stop()

    _run(scenario)
    assert any("不支持 effort=medium" in s for s in statuses)
