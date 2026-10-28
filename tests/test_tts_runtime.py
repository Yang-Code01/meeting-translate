"""tts_runtime：找 Python、判就绪、拼 sidecar 命令。"""

from __future__ import annotations

from pathlib import Path

import pytest

from meeting_translate import tts_runtime
from meeting_translate.tts_worker import PROTOCOL_VERSION as WORKER_PROTOCOL_VERSION
from meeting_translate.tts_client import PROTOCOL_VERSION


def _fake_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """在 tmp 里伪造一个装好的运行时，返回 (venv 解释器, sidecar 脚本)。"""
    runtime = tmp_path / "runtime" / "sbv2"
    (runtime / "Scripts").mkdir(parents=True)
    python = runtime / "Scripts" / "python.exe"
    python.write_text("", encoding="utf-8")
    (runtime / tts_runtime.STAMP_NAME).write_text("{}", encoding="utf-8")

    worker = tmp_path / "worker" / "tts_worker.py"
    worker.parent.mkdir()
    worker.write_text("", encoding="utf-8")

    monkeypatch.setattr(tts_runtime, "runtime_dir", lambda: runtime)
    monkeypatch.setattr(tts_runtime, "venv_python", lambda: python)
    monkeypatch.setattr(tts_runtime, "stamp_path", lambda: runtime / tts_runtime.STAMP_NAME)
    monkeypatch.setattr(tts_runtime, "worker_script", lambda: worker)
    return python, worker


def test_worker_command_is_none_when_runtime_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime" / "sbv2"
    monkeypatch.setattr(tts_runtime, "runtime_dir", lambda: runtime)
    monkeypatch.setattr(tts_runtime, "venv_python", lambda: runtime / "Scripts" / "python.exe")
    monkeypatch.setattr(tts_runtime, "stamp_path", lambda: runtime / tts_runtime.STAMP_NAME)
    assert tts_runtime.is_ready() is False
    assert tts_runtime.worker_command() is None


def test_worker_command_runs_script_with_venv_python(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    python, worker = _fake_runtime(monkeypatch, tmp_path)
    assert tts_runtime.is_ready() is True

    command = tts_runtime.worker_command(tmp_path / "models", "auto")

    assert command is not None
    # -P 是必需的：脚本目录里有 meeting_translate/secrets.py，会把标准库 secrets 顶掉
    assert command[:3] == [str(python), "-P", str(worker)]
    assert command[3:] == ["--device", "auto", "--models-dir", str(tmp_path / "models")]


def test_worker_command_skips_models_dir_when_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    command = tts_runtime.worker_command()
    assert command is not None
    assert "--models-dir" not in command


def test_find_python_skips_unsupported_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = {("py", "-3.12"): (3, 14), ("python3.12",): (3, 12)}
    monkeypatch.setattr(tts_runtime.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(tts_runtime, "_python_version", lambda cmd: versions.get(tuple(cmd)))
    assert tts_runtime.find_python() == ["python3.12"]


def test_find_python_returns_none_when_only_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_runtime.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(tts_runtime, "_python_version", lambda cmd: (3, 14))
    assert tts_runtime.find_python() is None


def test_install_without_python_explains_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts_runtime, "find_uv", lambda: None)
    monkeypatch.setattr(tts_runtime, "find_python", lambda: None)
    with pytest.raises(tts_runtime.TtsRuntimeError) as err:
        tts_runtime.install()
    assert err.value.code == "python_missing"
    assert tts_runtime.PYTHON_DOWNLOAD_URL in str(err.value)


def test_install_prefers_uv_with_managed_python(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(cmd, timeout=None, env=None, cancel=None):  # noqa: ANN001, ANN202
        calls.append((list(cmd), env))
        return tts_runtime.subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.delenv("UV_PYTHON_INSTALL_MIRROR", raising=False)
    monkeypatch.setattr(tts_runtime, "find_uv", lambda: "uv")
    monkeypatch.setattr(tts_runtime, "find_python", lambda: pytest.fail("有 uv 时不该再找系统 Python"))
    monkeypatch.setattr(tts_runtime, "runtime_dir", lambda: tmp_path / "sbv2")
    monkeypatch.setattr(tts_runtime, "_run", fake_run)
    with pytest.raises(tts_runtime.TtsRuntimeError) as err:
        tts_runtime.install()
    assert err.value.code == "venv_failed"
    cmd, env = calls[0]
    assert cmd[:4] == ["uv", "venv", "--python", "3.12"]
    assert env is not None and env["UV_PYTHON_INSTALL_DIR"] == str(tts_runtime.uv_python_dir())
    assert env["UV_PYTHON_INSTALL_MIRROR"] == tts_runtime.UV_PYTHON_MIRROR
    # 镜像失败会去掉镜像再试一次
    assert len(calls) == 2 and calls[1][0] == cmd


def test_install_downloads_uv_only_when_asked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tts_runtime, "find_uv", lambda: None)
    monkeypatch.setattr(tts_runtime, "find_python", lambda: None)
    monkeypatch.setattr(tts_runtime, "download_uv", lambda say, cancel=None: tmp_path / "uv.exe")
    monkeypatch.setattr(tts_runtime, "runtime_dir", lambda: tmp_path / "sbv2")
    used: list[str] = []
    monkeypatch.setattr(
        tts_runtime,
        "_run",
        lambda cmd, timeout=None, env=None, cancel=None: used.append(cmd[0]) or tts_runtime.subprocess.CompletedProcess(cmd, 1, "", ""),
    )
    with pytest.raises(tts_runtime.TtsRuntimeError):
        tts_runtime.install(download_uv_if_missing=True)
    assert set(used) == {str(tmp_path / "uv.exe")}


def test_worker_script_is_the_standalone_sidecar() -> None:
    script = tts_runtime.worker_script()
    assert script is not None and script.name == "tts_worker.py"


def test_worker_and_client_share_protocol_version() -> None:
    # sidecar 现在可以脱离包独立运行（运行时 venv 里没有本项目），常量必须是同一个值
    assert WORKER_PROTOCOL_VERSION == PROTOCOL_VERSION


def test_run_kills_subprocess_when_cancelled() -> None:
    import sys
    import threading
    import time

    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(tts_runtime.TtsRuntimeError) as err:
        tts_runtime._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60, cancel=cancel)
    assert err.value.code == "cancelled"
    assert time.monotonic() - started < 10
