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


def default_microphone_name() -> str | None:
    try:
        return str(sc.default_microphone().name)
    except Exception:
        return None


_VIRTUAL_HINTS = ("cable", "hi-fi", "vb-audio", "voicemeeter", "virtual")


def is_virtual_device(name: str | None) -> bool:
    """虚拟声卡（VB-Cable/Hi-Fi Cable/Voicemeeter）录到的是别的程序的声音，不是你的嗓子。"""
    low = (name or "").casefold()
    return any(h in low for h in _VIRTUAL_HINTS)


AUDIO_SERVICE_HELP = (
    "Windows 音频服务（Windows Audio / Audiosrv）没有运行，所有麦克风和扬声器都不可用。\n\n"
    "恢复方法（任选其一）：\n"
    "1. Win+R 输入 services.msc → 找到「Windows Audio」→ 右键「启动」\n"
    "2. 以管理员身份打开 PowerShell，执行：Start-Service Audiosrv\n"
    "3. 重启电脑"
)


def audio_service_running() -> bool | None:
    """Windows Audio 服务是否在运行；查不到（非 Windows/权限异常）返回 None。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _Status(ctypes.Structure):
        _fields_ = [(n, wintypes.DWORD) for n in (
            "type", "state", "controls", "exit_code", "svc_exit_code", "checkpoint", "wait_hint")]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenSCManagerW.restype = wintypes.HANDLE
    advapi.OpenServiceW.restype = wintypes.HANDLE
    advapi.OpenServiceW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD)
    advapi.QueryServiceStatus.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Status))
    advapi.CloseServiceHandle.argtypes = (wintypes.HANDLE,)
    scm = advapi.OpenSCManagerW(None, None, 0x0001)  # SC_MANAGER_CONNECT
    if not scm:
        return None
    try:
        svc = advapi.OpenServiceW(scm, "Audiosrv", 0x0004)  # SERVICE_QUERY_STATUS
        if not svc:
            return None
        try:
            status = _Status()
            if not advapi.QueryServiceStatus(svc, ctypes.byref(status)):
                return None
            return status.state == 4  # SERVICE_RUNNING
        finally:
            advapi.CloseServiceHandle(svc)
    finally:
        advapi.CloseServiceHandle(scm)


# 阻塞读只在 WASAPI/MME 上可用；WDM-KS 会报「Blocking API not supported yet」。
SD_HOSTAPIS = ("Windows WASAPI", "MME")


def pick_sounddevice_input(devices, hostapis, wanted: str | None) -> int | None:
    """在 WASAPI（优先）/MME 里挑麦克风：先按名称，再取该 API 的默认输入。"""
    for api_name in SD_HOSTAPIS:
        api = next((a for a in hostapis if a.get("name") == api_name), None)
        if api is None:
            continue
        members = [i for i in api.get("devices", []) if devices[i]["max_input_channels"] > 0]
        if wanted:
            for i in members:
                if wanted in devices[i]["name"]:
                    return i
    for api_name in SD_HOSTAPIS:
        api = next((a for a in hostapis if a.get("name") == api_name), None)
        if api is None:
            continue
        default = api.get("default_input_device", -1)
        if default is not None and default >= 0 and devices[default]["max_input_channels"] > 0:
            return default
    return None


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
        self._mic_device = mic_device
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
        except Exception as exc:
            if source == ME and not stop.is_set():
                log.warning("soundcard 麦克风不可用，改用 sounddevice: %s", exc)
                self._run_sounddevice(stop)
            else:
                log.exception("音频采集失败: %s", source)

    def _run_sounddevice(self, stop: threading.Event) -> None:
        """soundcard 对部分麦克风（如 HyperX Cloud III）会断言失败，用 PortAudio 兜底。"""
        try:
            import sounddevice as sd
        except Exception:
            log.exception("sounddevice 不可用，麦克风保持关闭")
            return

        try:
            index = pick_sounddevice_input(sd.query_devices(), sd.query_hostapis(), self._mic_device)
            if index is None:
                if audio_service_running() is False:
                    raise RuntimeError("Windows 音频服务未运行")
                raise RuntimeError("没有可用的麦克风（WASAPI/MME 下都找不到输入设备）")

            rate = None
            for candidate in (48000, 44100, 16000):
                try:
                    sd.check_input_settings(device=index, channels=1, samplerate=candidate, dtype="float32")
                    rate = candidate
                    break
                except Exception:
                    continue
            if rate is None:
                raise RuntimeError("麦克风不支持 48k/44.1k/16k 单声道")

            blocksize = max(1, int(rate * 0.1))
            with sd.RawInputStream(
                device=index, channels=1, samplerate=rate, dtype="float32", blocksize=blocksize
            ) as stream:
                while not stop.is_set():
                    data, overflowed = stream.read(blocksize)
                    if overflowed:
                        log.debug("麦克风溢出")
                    samples = np.frombuffer(data, dtype=np.float32)
                    if rate != SAMPLE_RATE:
                        count = int(round(samples.size * SAMPLE_RATE / rate))
                        if count > 0:
                            old = np.linspace(0.0, 1.0, samples.size, endpoint=False)
                            new = np.linspace(0.0, 1.0, count, endpoint=False)
                            samples = np.interp(new, old, samples).astype(np.float32)
                    if samples.size:
                        self._on_audio(ME, np.ascontiguousarray(samples, dtype=np.float32))
        except Exception:
            log.exception("sounddevice 麦克风采集失败")
