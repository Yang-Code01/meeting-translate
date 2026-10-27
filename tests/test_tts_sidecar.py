from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from meeting_translate.tts_client import PROTOCOL_VERSION, TtsClient, TtsError

ROOT = Path(__file__).resolve().parents[1]


def _worker_command() -> list[str]:
    return [sys.executable, "-m", "meeting_translate.tts_worker", "--stub"]


def test_worker_stub_protocol() -> None:
    proc = subprocess.Popen(
        _worker_command(),
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready["type"] == "ready"
        assert ready["version"] == PROTOCOL_VERSION
        assert ready["engines"] == ["stub"]

        proc.stdin.write(
            json.dumps({"type": "synth", "id": 1, "text": "你好，世界。", "engine": "stub"}) + "\n"
        )
        proc.stdin.flush()
        result = json.loads(proc.stdout.readline())
        assert result["type"] == "result"
        assert result["id"] == 1
        assert result["sample_rate"] == 16000
        samples = np.frombuffer(base64.b64decode(result["samples"]), dtype=np.float32)
        assert samples.size > 0
        assert np.max(np.abs(samples)) <= 1.0

        proc.stdin.write(json.dumps({"type": "ping", "id": 2}) + "\n")
        proc.stdin.flush()
        pong = json.loads(proc.stdout.readline())
        assert pong == {"type": "pong", "id": 2}

        proc.stdin.write(json.dumps({"type": "synth", "id": 3, "text": "", "engine": "stub"}) + "\n")
        proc.stdin.flush()
        error = json.loads(proc.stdout.readline())
        assert error["type"] == "error"
        assert error["id"] == 3
        assert error["code"] == "empty_text"

        proc.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
        proc.stdin.flush()
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_client_stub_roundtrip_and_volume() -> None:
    client = TtsClient(command=_worker_command(), cwd=ROOT, timeout=20)
    try:
        client.start()
        assert client.ready
        assert client.ready_info["engines"] == ["stub"]

        audio = client.synth("你好，世界。", engine="stub")
        assert audio.sample_rate == 16000
        assert audio.samples.dtype == np.float32
        assert audio.samples.size > 0
        assert float(np.max(np.abs(audio.samples))) <= 1.0

        quiet = client.synth("你好，世界。", engine="stub", volume=0.5)
        assert float(np.max(np.abs(quiet.samples))) <= 0.11
        assert client.warm("stub") >= 0
        assert client.ping()
    finally:
        client.close()


def test_client_maps_worker_error() -> None:
    client = TtsClient(command=_worker_command(), cwd=ROOT, timeout=20)
    try:
        assert client.synth("", engine="stub").samples.size == 0
        with pytest.raises(TtsError):
            client.synth("hello", engine="does-not-exist")
    finally:
        client.close()


def test_client_auto_starts_and_reports_exit() -> None:
    client = TtsClient(command=_worker_command(), cwd=ROOT, timeout=20)
    try:
        assert not client.ready
        client.synth("自动启动", engine="stub")
        assert client.ready
        assert client.pid
    finally:
        client.close()
    assert not client.ready
