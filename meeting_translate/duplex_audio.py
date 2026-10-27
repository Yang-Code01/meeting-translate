"""双向同传的音频路由：CABLE-A/B 识别、测试音、播放队列、原声直通。

约定（与 README / 设置项保持一致）：

- CABLE-A Input（扬声器）由会议软件播放对方声音；App 环回采集它。
- CABLE-B Input（扬声器）由 App 播放日语 TTS；会议软件用 CABLE-B Output 当麦克风。
- 若没有 A+B 包，也支持免费组合：标准 ``CABLE Input/Output`` 当 A，
  ``Hi-Fi Cable Input/Output`` 当 B（见 detect_cable_devices）。
- CABLE-A Output / CABLE-B Output 是驱动暴露的录音端点，只用于测试音回采。
- 本地耳机是 App 播放中文 TTS 的扬声器；物理麦克风是本地输入。
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np

from . import audio
from .config import Settings

TONE_RATE = 48000
TONE_FREQ = 440.0
PLAY_BLOCK_SECONDS = 0.1
PLAY_RATE = 48000  # 统一播放格式，避免 44.1k/48k 与单声道/多声道虚拟声卡混用产生爆音

log = logging.getLogger(__name__)


def _init_com() -> None:
    """worker 线程使用 soundcard 前要初始化 COM（MTA 即可）。"""
    if sys.platform == "win32":
        ctypes.windll.ole32.CoInitializeEx(None, 0)


def _norm(name: str) -> str:
    return " ".join(name.lower().replace("_", "-").split())


def _find(names: Iterable[str], cable: str, kind: str) -> str | None:
    """在设备名里找 CABLE-A/B 的 Input/Output；兼容带驱动后缀的名字。"""
    prefix = f"cable-{cable.lower()}"
    for name in names:
        low = _norm(name)
        if low.startswith(prefix) and kind in low:
            return name
    return None


def _find_alias(names: Iterable[str], prefixes: Iterable[str], kind: str) -> str | None:
    """免费替代方案：标准 CABLE 与 Hi-Fi Cable 各当一路。"""
    for prefix in prefixes:
        for name in names:
            low = _norm(name)
            if low.startswith(prefix) and kind in low:
                return name
    return None


_VIRTUAL_HINTS = ("cable", "vb-audio", "voicemeeter", "virtual", "hi-fi", "hifi")
_PHYSICAL_HINTS = ("headphone", "headset", "earphone", "hyperx", "耳机", "耳麦")


def _physical_speaker(names: Iterable[str]) -> str | None:
    """优先选真正的耳机/扬声器，避免 Windows 默认输出被改成 CABLE 后听不到本地 TTS。"""
    speakers = list(names)
    for hint in _PHYSICAL_HINTS:
        for name in speakers:
            low = _norm(name)
            if hint in low and not any(v in low for v in _VIRTUAL_HINTS):
                return name
    for name in speakers:
        low = _norm(name)
        if not any(v in low for v in _VIRTUAL_HINTS):
            return name
    return None


@dataclass(frozen=True)
class CableDevices:
    a_input: str | None = None   # 扬声器：会议软件放对方声音，App 环回采集
    a_output: str | None = None  # 录音端点：只用于测试音回采
    b_input: str | None = None   # 扬声器：App 放日语 TTS
    b_output: str | None = None  # 录音端点：会议软件当麦克风，只用于测试

    @property
    def capture(self) -> str | None:
        """App 采集对方声音用的扬声器名（交给 audio.Capture 做环回）。"""
        return self.a_input

    @property
    def remote_speaker(self) -> str | None:
        return self.b_input

    @property
    def missing(self) -> tuple[str, ...]:
        missing = []
        for label, device in (
            ("CABLE-A Input", self.a_input),
            ("CABLE-A Output", self.a_output),
            ("CABLE-B Input", self.b_input),
            ("CABLE-B Output", self.b_output),
        ):
            if not device:
                missing.append(label)
        return tuple(missing)

    @property
    def complete(self) -> bool:
        return not self.missing


def detect_cable_devices(
    speakers: Iterable[str] | None = None,
    microphones: Iterable[str] | None = None,
) -> CableDevices:
    """按名字识别 VB-Cable A+B；设备名可带 ``(VB-Audio Virtual Cable)`` 后缀。"""
    speaker_names = list(audio.list_speakers() if speakers is None else speakers)
    mic_names = list(audio.list_microphones() if microphones is None else microphones)
    return CableDevices(
        a_input=_find(speaker_names, "a", "input")
        or _find_alias(speaker_names, ("cable input", "cable in 16ch"), "input"),
        a_output=_find(mic_names, "a", "output") or _find_alias(mic_names, ("cable output",), "output"),
        b_input=_find(speaker_names, "b", "input")
        or _find_alias(speaker_names, ("hi-fi cable input", "hifi cable input", "voicemeeter input"), "input"),
        b_output=_find(mic_names, "b", "output")
        or _find_alias(mic_names, ("hi-fi cable output", "hifi cable output", "voicemeeter output"), "output"),
    )


@dataclass(frozen=True)
class ResolvedDevices:
    mic: str | None
    capture: str | None
    remote_speaker: str | None
    local_speaker: str | None
    detected: CableDevices
    missing: tuple[str, ...] = ()


def resolve_devices(
    settings: Settings,
    speakers: Iterable[str] | None = None,
    microphones: Iterable[str] | None = None,
) -> ResolvedDevices:
    """设置优先、检测兜底；返回实际要交给 soundcard 的设备名。

    ``capture`` 是扬声器名（CABLE-A Input），不是录音端点；``audio.Capture``
    会对它做 loopback 采集。设备名不存在时回退到检测值，并记在 missing 里。
    """
    speaker_names = list(audio.list_speakers() if speakers is None else speakers)
    mic_names = list(audio.list_microphones() if microphones is None else microphones)
    detected = detect_cable_devices(speaker_names, mic_names)
    missing: list[str] = []

    def pick(configured: str | None, available: Iterable[str], fallback: str | None, label: str) -> str | None:
        if configured:
            if configured in available:
                return configured
            missing.append(label)
        return fallback

    mic = pick(settings.duplex_mic_device, mic_names, None, "duplex_mic_device")
    capture = pick(settings.duplex_capture_device, speaker_names, detected.capture, "duplex_capture_device")
    remote = pick(settings.duplex_remote_speaker, speaker_names, detected.remote_speaker, "duplex_remote_speaker")
    local = pick(
        settings.duplex_local_speaker,
        speaker_names,
        _physical_speaker(speaker_names),
        "duplex_local_speaker",
    )
    if not capture and "CABLE-A Input" not in missing:
        missing.append("CABLE-A Input")
    if not remote and "CABLE-B Input" not in missing:
        missing.append("CABLE-B Input")
    for item in detected.missing:
        if item not in missing:
            missing.append(item)
    return ResolvedDevices(mic, capture, remote, local, detected, tuple(missing))


def make_tone(seconds: float = 1.0, volume: float = 0.2, freq: float = TONE_FREQ, sample_rate: int = TONE_RATE) -> np.ndarray:
    """440Hz 正弦测试音，首尾各 20ms 淡入淡出，避免爆音。"""
    n = max(1, int(sample_rate * max(0.01, seconds)))
    t = np.arange(n, dtype=np.float32) / sample_rate
    wave = np.sin(2 * np.pi * freq * t).astype(np.float32) * float(volume)
    fade = min(n // 2, max(1, int(sample_rate * 0.02)))
    env = np.ones(n, dtype=np.float32)
    env[:fade] = np.linspace(0.0, 1.0, fade, dtype=np.float32)
    env[-fade:] = np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return np.ascontiguousarray(wave * env, dtype=np.float32)


def _fade(samples: np.ndarray, sample_rate: int, milliseconds: float = 8.0) -> np.ndarray:
    """给播放块首尾加极短淡入淡出，消除切块边界的爆音/电音感。"""
    data = np.array(samples, dtype=np.float32, copy=True).reshape(-1)
    n = min(data.size // 2, max(1, int(sample_rate * milliseconds / 1000.0)))
    if n < 2:
        return data
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    data[:n] *= ramp
    data[-n:] *= ramp[::-1]
    return data


def _resample(samples: np.ndarray, source_rate: int, target_rate: int = PLAY_RATE) -> np.ndarray:
    """线性重采样到统一播放采样率；语音场景足够，避免设备端隐式转换。"""
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    if data.size == 0 or source_rate <= 0 or source_rate == target_rate:
        return data
    n = max(1, int(round(data.size * target_rate / source_rate)))
    x = np.arange(n, dtype=np.float64) * (source_rate / target_rate)
    return np.interp(x, np.arange(data.size, dtype=np.float64), data).astype(np.float32)


def _stereo(samples: np.ndarray) -> np.ndarray:
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    return np.repeat(data[:, None], 2, axis=1)


def _speaker(name: str | None):
    return audio.sc.get_speaker(name) if name else audio.sc.default_speaker()


def _microphone(name: str | None):
    return audio.sc.get_microphone(name) if name else audio.sc.default_microphone()


def play_tone(device: str | None, seconds: float = 1.0, volume: float = 0.15) -> None:
    """同步播放测试音；调用方应放到后台线程，避免卡住 UI。"""
    samples = make_tone(seconds, volume).reshape(-1, 1)
    with _speaker(device).player(samplerate=TONE_RATE, channels=1) as player:
        player.play(samples)


@dataclass(frozen=True)
class ProbeResult:
    rms: float
    peak: float

    @property
    def ok(self) -> bool:
        return self.peak >= 0.003


def record_level(device: str | None, seconds: float = 1.0) -> ProbeResult:
    """录一段并返回 RMS/峰值；用于测试音回采。"""
    _init_com()
    n = max(1, int(TONE_RATE * max(0.05, seconds)))
    with _microphone(device).recorder(samplerate=TONE_RATE, channels=1, blocksize=int(TONE_RATE * PLAY_BLOCK_SECONDS)) as rec:
        data = rec.record(numframes=n)
    flat = np.asarray(data, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        return ProbeResult(0.0, 0.0)
    return ProbeResult(float(np.sqrt(np.mean(flat * flat))), float(np.max(np.abs(flat))))


def probe_cable(cable: str, devices: CableDevices, seconds: float = 1.0) -> ProbeResult:
    """播放测试音并从对应录音端点回采，验证虚拟声卡路径。

    CABLE-A: 播到 A Input，从 A Output 回采。
    CABLE-B: 播到 B Input，从 B Output 回采。
    """
    key = cable.upper()
    if key == "A":
        play_device, record_device = devices.a_input, devices.a_output
    elif key == "B":
        play_device, record_device = devices.b_input, devices.b_output
    else:
        raise ValueError("cable 必须是 A 或 B")
    if not play_device or not record_device:
        raise ValueError(f"CABLE-{key} 的播放/录音端点不完整")

    result: dict[str, ProbeResult] = {}
    started = threading.Event()

    def record() -> None:
        _init_com()
        started.set()
        result["value"] = record_level(record_device, seconds + 0.25)

    thread = threading.Thread(target=record, name=f"cable-probe-{key}", daemon=True)
    thread.start()
    started.wait(timeout=2)
    time.sleep(0.12)
    play_tone(play_device, seconds)
    thread.join(timeout=seconds + 3)
    return result.get("value", ProbeResult(0.0, 0.0))


class Player:
    """单设备播放队列；块级播放，stop() 后最多 100ms 静音。

    队列不丢音频：TTS 合成比播放快时先排队，保证句子完整；真正打断由
    stop() 清队列。generation 用来让 stop() 立刻作废正在播的旧块。
    """

    def __init__(
        self,
        speaker: str | None,
        on_error: Callable[[Exception], None] | None = None,
        max_queue: int = 8,
    ) -> None:
        self.speaker = speaker
        self._on_error = on_error
        self._q: queue.Queue[tuple[np.ndarray, int, Callable[[], None] | None, Callable[[], None] | None]] = queue.Queue()
        self._stop = threading.Event()
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._stop.clear()
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="duplex-player", daemon=True)
            self._thread.start()

    def enqueue(
        self,
        samples: np.ndarray | None,
        sample_rate: int = audio.SAMPLE_RATE,
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if samples is None:
            return
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        if data.size == 0:
            return
        self.start()
        item = (np.ascontiguousarray(data), int(sample_rate), on_start, on_done)
        self._q.put_nowait(item)

    def stop(self) -> None:
        """立即作废当前块并清空队列；播放线程保留，下一次 enqueue 可复用。"""
        self._generation += 1
        self._stop.set()
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        self.stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.5)
        self._thread = None

    def _run(self) -> None:
        _init_com()
        while not self._stop.is_set():
            try:
                samples, rate, on_start, on_done = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            generation = self._generation
            try:
                self._play_clip(samples, rate, generation, on_start, on_done)
            except Exception as e:  # 设备拔出/被独占时不让播放线程死掉
                log.exception("音频播放失败")
                if self._on_error is not None:
                    self._on_error(e)

    def _play_clip(
        self,
        samples: np.ndarray,
        sample_rate: int,
        generation: int,
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        samples = _stereo(_fade(_resample(samples, sample_rate), PLAY_RATE))
        block = max(1, int(PLAY_RATE * PLAY_BLOCK_SECONDS))
        if generation != self._generation or self._stop.is_set():
            return
        if on_start is not None:
            on_start()
        with _speaker(self.speaker).player(samplerate=PLAY_RATE, channels=2) as player:
            for start in range(0, samples.shape[0], block):
                if generation != self._generation or self._stop.is_set():
                    return
                player.play(samples[start:start + block])
        if generation == self._generation and not self._stop.is_set() and on_done is not None:
            on_done()


class LivePlayer:
    """连续播放原始音频块（用于关闭翻译时的原声直通）。

    和 Player 的区别：Player 每句话重开一次设备；LivePlayer 保持一条
    连续播放流，适合 100ms 一块的麦克风/环回音频，避免频繁开流造成卡顿。
    """

    def __init__(
        self,
        speaker: str | None,
        on_error: Callable[[Exception], None] | None = None,
        max_queue: int = 8,
    ) -> None:
        self.speaker = speaker
        self._on_error = on_error
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=max(1, max_queue))
        self._stop = threading.Event()
        self._locked = threading.Lock()
        self._closed = False
        self._failed = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._locked:
            if self._closed or self._failed:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="duplex-live", daemon=True)
            self._thread.start()

    def write(self, samples: np.ndarray | None, sample_rate: int = audio.SAMPLE_RATE) -> None:
        if self._closed or self._failed or samples is None:
            return
        data = _resample(samples, sample_rate, PLAY_RATE)
        if data.size == 0:
            return
        self.start()
        try:
            self._q.put_nowait(np.ascontiguousarray(data))
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(np.ascontiguousarray(data))
            except queue.Full:
                pass

    def stop(self) -> None:
        """清空待播块并结束当前播放流；下一次 write 会重新开流。"""
        self._stop.set()
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None
        self._stop.clear()

    def close(self) -> None:
        self._closed = True
        self.stop()

    def _run(self) -> None:
        _init_com()
        try:
            with _speaker(self.speaker).player(samplerate=PLAY_RATE, channels=2) as player:
                while not self._stop.is_set():
                    try:
                        data = self._q.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    player.play(_stereo(data))
        except Exception as e:
            log.exception("原声直通播放失败")
            self._failed = True
            if self._on_error is not None:
                self._on_error(e)


class PassthroughRouter:
    """同语言直通：把 ASR 定稿的原始音频块送回对应设备。

    - 我这边检测到日语（目标语言）→ 原声播到 CABLE-B，对方直接听到。
    - 对方检测到中文（目标语言）→ 原声播到耳机，我直接听到。
    这是定稿后回放，延迟高于实时直通；P4 若需要更低延迟再改成边采边路由。
    """

    def __init__(self, local: Player, remote: Player) -> None:
        self.local = local
        self.remote = remote

    def route(self, final) -> str | None:
        data = getattr(final, "audio", None)
        if data is None or len(data) == 0:
            return None
        source = getattr(final, "source", "")
        detected = getattr(final, "lang", "")
        if source == audio.ME and detected == "ja":
            self.remote.enqueue(data, audio.SAMPLE_RATE)
            return "remote"
        if source == audio.THEM and detected == "zh":
            self.local.enqueue(data, audio.SAMPLE_RATE)
            return "local"
        return None
