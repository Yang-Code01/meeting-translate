"""音频采集：WASAPI 环回（对方）与麦克风（我），统一输出 16kHz 单声道 float32。"""

from __future__ import annotations

import logging
import os
import threading
import warnings
from collections.abc import Callable

import numpy as np

# soundcard 导入时 cffi.dlopen('ole32') 经 ctypes.util.find_library 只搜索 PATH；
# 某些启动方式下 PATH 不含 System32，会导致导入失败。
_system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
if _system32.lower() not in (p.rstrip("\\").lower() for p in os.environ.get("PATH", "").split(os.pathsep)):
    os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _system32

from .qtcom import init_sta  # noqa: E402

# soundcard 导入时会把当前线程的 COM 设成 MTA；Qt 需要 STA，否则原生对话框会卡死。
init_sta()

import soundcard as sc  # noqa: E402

SAMPLE_RATE = 16000
BLOCK = 1600  # 100ms

THEM = "对方"
ME = "我"

log = logging.getLogger(__name__)


def list_speakers() -> list[str]:
    return [s.name for s in sc.all_speakers()]


def list_microphones() -> list[str]:
    return [m.name for m in sc.all_microphones(include_loopback=False)]


def _loopback(name: str | None):
    spk = sc.get_speaker(name) if name else sc.default_speaker()
    return sc.get_microphone(id=str(spk.name), include_loopback=True)


def _mic(name: str | None):
    return sc.get_microphone(name) if name else sc.default_microphone()


class Capture:
    """为每个音源开一个线程，持续把 100ms 音频块交给 on_audio(source, samples)。

    麦克风可在采集中途通过 set_enabled 单独开关。
    """

    def __init__(
        self,
        on_audio: Callable[[str, np.ndarray], None],
        loopback_device: str | None,
        mic_device: str | None,
        mic_enabled: bool,
    ) -> None:
        self._on_audio = on_audio
        self._factories: dict[str, Callable[[], object]] = {
            THEM: lambda: _loopback(loopback_device),
            ME: lambda: _mic(mic_device),
        }
        self._enabled = {THEM: True, ME: mic_enabled}
        self._running: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._started = False

    def start(self) -> None:
        self._started = True
        for source, on in self._enabled.items():
            if on:
                self._spawn(source)

    def stop(self) -> None:
        self._started = False
        for source in list(self._running):
            self._halt(source)

    def set_enabled(self, source: str, on: bool) -> None:
        """开关单个音源；关闭时等待采集线程退出，之后不会再有该音源的音频块。"""
        self._enabled[source] = on
        if not self._started:
            return
        if on:
            if source not in self._running:
                self._spawn(source)
        else:
            self._halt(source)

    def _spawn(self, source: str) -> None:
        stop = threading.Event()
        t = threading.Thread(
            target=self._run, args=(source, self._factories[source], stop), name=f"capture-{source}", daemon=True
        )
        self._running[source] = (t, stop)
        t.start()

    def _halt(self, source: str) -> None:
        item = self._running.pop(source, None)
        if item is None:
            return
        t, stop = item
        stop.set()
        t.join(timeout=2)

    def _run(self, source: str, factory: Callable[[], object], stop: threading.Event) -> None:
        # soundcard 在环回无声时会发出 data discontinuity 警告，属正常现象
        warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
        import ctypes

        ctypes.windll.ole32.CoInitializeEx(None, 0)  # 采集线程需要初始化 COM（MTA）
        try:
            dev = factory()
            with dev.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK) as rec:
                while not stop.is_set():
                    data = rec.record(numframes=BLOCK)
                    self._on_audio(source, np.ascontiguousarray(data[:, 0], dtype=np.float32))
        except Exception:
            log.exception("音频采集失败: %s", source)
