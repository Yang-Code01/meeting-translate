"""自检里的「试一句」：录中文 → 识别 → 翻成日语 → 本地 TTS 播放。

这条链路不依赖 Teams / CABLE，只验证「麦克风 + ASR + 翻译服务 + 日文 TTS」
是否真的能走通。录音在 GUI 线程启动 InputStream，识别/翻译/合成放到 QThread。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QThread, Signal

from .asr import SAMPLE_RATE, create_engine
from .config import MODEL_DIR, Settings
from .providers import make_backend
from .translator import _parse_batch, system_prompt
from .tts_client import TtsClient, default_worker_command
from .tts_models import ModelManager


@dataclass(frozen=True)
class TrialResult:
    chinese: str
    japanese: str
    audio: np.ndarray
    sample_rate: int
    latency: float


def has_speech_text(text: str) -> bool:
    """ASR 在静音上可能只吐标点；没有字母/汉字/假名就视为没听清。"""
    return any(ch.isalnum() or 0x4E00 <= ord(ch) <= 0x9FFF or 0x3040 <= ord(ch) <= 0x30FF for ch in text)


def resample_audio(samples: np.ndarray, source_rate: int, target_rate: int = SAMPLE_RATE) -> np.ndarray:
    """线性重采样；输入输出都是 float32 单声道。"""
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    if data.size == 0 or source_rate == target_rate:
        return data
    n = max(1, int(round(data.size * target_rate / source_rate)))
    x_old = np.linspace(0.0, 1.0, data.size, endpoint=False, dtype=np.float64)
    x_new = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float64)
    return np.interp(x_new, x_old, data).astype(np.float32)


class Recorder:
    """默认输入（或指定麦克风）录音；停止后返回 16k 单声道。"""

    def __init__(self, device: str | None = None) -> None:
        self.device = device
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._rate = SAMPLE_RATE
        self._index: int | None = None

    @property
    def active(self) -> bool:
        return self._stream is not None

    def _pick(self) -> tuple[int, int]:
        import sounddevice as sd

        devices = sd.query_devices()
        index: int | None = None
        if self.device:
            for i, item in enumerate(devices):
                if item["max_input_channels"] > 0 and self.device in item["name"]:
                    index = i
                    break
        if index is None:
            index = sd.default.device[0]
        if index is None or index < 0:
            raise RuntimeError("没有可用的默认麦克风")
        for rate in (48000, 44100, 16000):
            try:
                sd.check_input_settings(device=index, channels=1, samplerate=rate, dtype="float32")
                return index, rate
            except Exception:
                continue
        raise RuntimeError("麦克风不支持 48k/44.1k/16k 单声道")

    def _callback(self, indata, frames, time_info, status) -> None:
        # status 只表示 overflow 等音频警告；保留已经拿到的数据即可。
        del frames, time_info, status
        self._chunks.append(np.asarray(indata[:, 0], dtype=np.float32).copy())

    def start(self) -> None:
        if self._stream is not None:
            raise RuntimeError("录音已开始")
        import sounddevice as sd

        self._index, self._rate = self._pick()
        self._chunks = []
        self._stream = sd.InputStream(
            samplerate=self._rate,
            channels=1,
            dtype="float32",
            device=self._index,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream is None:
            raise RuntimeError("没有正在进行的录音")
        stream = self._stream
        self._stream = None
        try:
            stream.stop()
        finally:
            stream.close()
        if not self._chunks:
            raise RuntimeError("没有录到声音")
        data = np.concatenate(self._chunks).astype(np.float32, copy=False)
        self._chunks = []
        return resample_audio(data, self._rate)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
            self._stream = None
        self._chunks = []


async def translate_once(settings: Settings, text: str) -> str:
    """用当前模型服务把一句中文翻成日语；不写会议记录。"""
    backend = make_backend(settings, lambda _msg: None)
    session = None
    try:
        auth = await backend.start()
        if not auth.ok:
            raise RuntimeError(auth.message or "翻译服务未就绪")
        model = settings.model or getattr(backend, "default_model", lambda: "")()
        if not model:
            raise RuntimeError("没有选择翻译模型")
        session = await backend.create_session(
            system_prompt("zh-ja", settings.asr_correct),
            model,
            settings.reasoning_effort,
        )
        reply = await session.send(f"【我】{text}", timeout=90)
        parsed = _parse_batch(reply, 1)
        return parsed[0].strip()
    finally:
        if session is not None:
            await session.close()
        await backend.stop()


def synthesize_ja(settings: Settings, text: str) -> tuple[np.ndarray, int]:
    root = Path(os.environ.get("MEETING_TRANSLATE_TTS_DIR") or (MODEL_DIR / "tts"))
    manager = ModelManager(root)
    missing = manager.missing("sbv2", verify_hash=False)
    if missing:
        raise RuntimeError(f"日文 TTS 模型缺少 {len(missing)} 个文件")
    command = None if os.environ.get("MEETING_TRANSLATE_TTS_WORKER") else default_worker_command(root, "auto")
    client = TtsClient(command)
    try:
        client.start()
        client.warm("sbv2")
        audio = client.synth(
            text,
            engine="sbv2",
            voice=settings.duplex_tts_voice,
            style=settings.duplex_tts_style,
            speed=settings.duplex_tts_speed,
        )
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)
    finally:
        client.close()


class TrialWorker(QThread):
    stage = Signal(str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, settings: Settings, audio: np.ndarray, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.audio = audio

    def run(self) -> None:  # noqa: D401
        started = time.monotonic()
        try:
            self.stage.emit("正在识别中文…")
            engine = create_engine("sensevoice", [], self.settings.asr_threads, language="zh")
            chinese = engine.decode(self.audio).strip()
            if not has_speech_text(chinese):
                raise RuntimeError("没有识别到中文，请靠近麦克风重说")

            self.stage.emit("正在翻译成日语…")
            japanese = asyncio.run(translate_once(self.settings, chinese))
            if not japanese:
                raise RuntimeError("翻译结果为空")

            self.stage.emit("正在合成日语语音…")
            audio, rate = synthesize_ja(self.settings, japanese)
            if audio.size == 0:
                raise RuntimeError("日语 TTS 没有产生音频")
            self.done.emit(TrialResult(chinese, japanese, audio, rate, time.monotonic() - started))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
