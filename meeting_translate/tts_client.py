"""TTS sidecar 客户端：JSON-lines 协议、请求超时、崩溃重启。

sidecar 的 stdout 只允许出现协议 JSON；日志走 stderr。协议见 `tts_worker.py`。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT = 120.0


class TtsError(RuntimeError):
    def __init__(self, message: str, code: str = "tts_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TtsAudio:
    samples: np.ndarray
    sample_rate: int

    @property
    def duration(self) -> float:
        return float(self.samples.size) / self.sample_rate if self.sample_rate else 0.0


def default_worker_command(models_dir: Path | None = None, device: str = "auto") -> list[str]:
    """开发环境用当前 Python；打包后可被 sidecar exe/环境变量覆盖。"""
    override = os.environ.get("MEETING_TRANSLATE_TTS_WORKER")
    if override:
        return shlex.split(override, posix=False)
    python = os.environ.get("MEETING_TRANSLATE_TTS_PYTHON") or sys.executable
    command = [python, "-m", "meeting_translate.tts_worker", "--device", device]
    if models_dir is not None:
        command += ["--models-dir", str(models_dir)]
    if getattr(sys, "frozen", False):
        sidecar = Path(sys.executable).with_name("tts_worker.exe")
        if sidecar.exists():
            command = [str(sidecar), "--device", device]
            if models_dir is not None:
                command += ["--models-dir", str(models_dir)]
    return command


class TtsClient:
    """一个 TTS 子进程。

    - `synth()` 串行发送请求（sidecar 本身也是串行合成）；
    - 超时不会杀进程，但会抛 `TtsError`，迟到的结果会被丢弃；
    - 进程崩溃后，下一次 `synth()` 会尝试自动重启一次。
    """

    def __init__(
        self,
        command: Sequence[str] | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        on_stderr: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.command = list(command) if command is not None else default_worker_command()
        self.cwd = str(cwd) if cwd is not None else None
        self.env = env
        self.timeout = timeout
        self._on_stderr = on_stderr
        self._on_event = on_event

        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._ids = 0
        self._ready = threading.Event()
        self._ready_info: dict[str, Any] = {}
        self._closed = False
        self._last_error = ""

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._proc is not None and self._proc.poll() is None

    @property
    def ready_info(self) -> dict[str, Any]:
        return dict(self._ready_info)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self, timeout: float | None = None) -> None:
        with self._state_lock:
            if self._closed:
                raise TtsError("TTS 客户端已关闭", "closed")
            if self._proc is not None and self._proc.poll() is None:
                if self._ready.wait(timeout=5 if timeout is None else timeout):
                    return
                raise TtsError("TTS sidecar 未就绪", "not_ready")
            self._start_locked()
        if not self._ready.wait(timeout=self.timeout if timeout is None else timeout):
            self._kill()
            raise TtsError(f"TTS sidecar 启动超时：{self._last_error or '无输出'}", "start_timeout")

    def _start_locked(self) -> None:
        self._ready.clear()
        self._ready_info = {}
        self._last_error = ""
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log.info("启动 TTS sidecar：%s", " ".join(self.command))
        try:
            self._proc = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **kwargs,
            )
        except OSError as e:
            raise TtsError(f"无法启动 TTS sidecar：{e}", "spawn_failed") from e
        self._reader = threading.Thread(target=self._read_stdout, name="tts-stdout", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, name="tts-stderr", daemon=True)
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.warning("TTS sidecar 非 JSON 输出：%s", line[:300])
                continue
            self._dispatch(event)
        self._on_process_exit(proc.returncode, proc)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            self._last_error = line[-500:]
            log.debug("TTS sidecar: %s", line)
            if self._on_stderr is not None:
                try:
                    self._on_stderr(line)
                except Exception:
                    log.exception("TTS stderr 回调失败")

    def _on_process_exit(self, code: int | None, proc: subprocess.Popen[str] | None = None) -> None:
        with self._state_lock:
            if proc is not None and self._proc is not proc:
                return
        self._ready.clear()
        error = TtsError(f"TTS sidecar 已退出（code={code}）：{self._last_error}", "exited")
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if self._on_event is not None and kind not in ("result", "error", "pong"):
            try:
                self._on_event(event)
            except Exception:
                log.exception("TTS event 回调失败")
        if kind == "ready":
            version = int(event.get("version", 0))
            if version != PROTOCOL_VERSION:
                self._last_error = f"协议版本不匹配：{version}"
                self._ready.set()
                return
            self._ready_info = event
            self._ready.set()
            return
        if kind in ("result", "error", "pong"):
            request_id = event.get("id")
            if not isinstance(request_id, int):
                return
            with self._state_lock:
                future = self._pending.pop(request_id, None)
            if future is None or future.done():
                return
            if kind == "error":
                future.set_exception(TtsError(str(event.get("message", "TTS 合成失败")), str(event.get("code", "synth_failed"))))
            else:
                future.set_result(event)

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise TtsError("TTS sidecar 未启动", "not_started")
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise TtsError(f"写入 TTS sidecar 失败：{e}", "write_failed") from e

    def _next_id(self) -> int:
        with self._state_lock:
            self._ids += 1
            return self._ids

    def synth(
        self,
        text: str,
        engine: str = "sbv2",
        voice: str = "koharune_ami",
        style: str = "Neutral",
        speed: float = 1.0,
        volume: float = 1.0,
        timeout: float | None = None,
    ) -> TtsAudio:
        """合成一句；返回 float32 单声道。失败会抛 `TtsError`。"""
        if not text.strip():
            return TtsAudio(np.zeros(0, dtype=np.float32), 16000)
        if not self.ready:
            self.start()
        request_id = self._next_id()
        future: Future = Future()
        with self._state_lock:
            self._pending[request_id] = future
        try:
            with self._request_lock:
                self._send(
                    {
                        "type": "synth",
                        "id": request_id,
                        "engine": engine,
                        "text": text,
                        "voice": voice,
                        "style": style,
                        "speed": float(speed),
                        "volume": float(volume),
                    }
                )
            event = future.result(timeout=self.timeout if timeout is None else timeout)
        except FutureTimeout as e:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise TtsError(f"TTS 合成超时（{text[:20]}…）", "timeout") from e
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        raw = event.get("samples")
        if not isinstance(raw, str):
            raise TtsError("TTS 返回缺少 samples", "bad_result")
        try:
            data = np.frombuffer(base64.b64decode(raw), dtype=np.float32).copy()
        except (ValueError, TypeError) as e:
            raise TtsError(f"TTS 返回音频解码失败：{e}", "bad_result") from e
        sample_rate = int(event.get("sample_rate", 0))
        if sample_rate <= 0:
            raise TtsError("TTS 返回采样率无效", "bad_result")
        if data.size == 0:
            raise TtsError("TTS 返回空音频", "bad_result")
        if volume != 1.0:
            data = np.clip(data * float(volume), -1.0, 1.0).astype(np.float32)
        return TtsAudio(np.ascontiguousarray(data, dtype=np.float32), sample_rate)

    def warm(self, engine: str = "sbv2", timeout: float = 180.0) -> float:
        """让 sidecar 加载并预热一个引擎；返回耗时秒数。"""
        if not self.ready:
            self.start()
        request_id = self._next_id()
        future: Future = Future()
        with self._state_lock:
            self._pending[request_id] = future
        try:
            with self._request_lock:
                self._send({"type": "warm", "id": request_id, "engine": engine})
            event = future.result(timeout=timeout)
        except FutureTimeout as e:
            raise TtsError(f"TTS 预热超时（{engine}）", "timeout") from e
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        return float(event.get("ms", 0)) / 1000.0

    def ping(self, timeout: float = 5.0) -> bool:
        if not self.ready:
            self.start()
        request_id = self._next_id()
        future: Future = Future()
        with self._state_lock:
            self._pending[request_id] = future
        try:
            self._send({"type": "ping", "id": request_id})
            event = future.result(timeout=timeout)
            return event.get("type") == "pong"
        except Exception:
            return False
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass
        self._proc = None

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                self._send({"type": "shutdown"})
            except TtsError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._kill()
        self._proc = None
        self._ready.clear()
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(TtsError("TTS 客户端已关闭", "closed"))
