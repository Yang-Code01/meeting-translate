"""设置页的「测试麦克风」（录几秒显示电平）与「播放测试音」（检查耳机能不能听到）。"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from . import audio

TEST_SECONDS = 6
SILENT_PEAK = 0.01  # 正常说话 peak 一般 > 0.05；低于这个基本等于没声音

LEVEL_QSS = """
QProgressBar { background:#171a21; border:1px solid #2a3140; border-radius:5px; max-height:10px; }
QProgressBar::chunk { border-radius:4px;
  background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #3fb950, stop:0.7 #d4b106, stop:1 #e06c75); }
"""


def level_percent(samples: np.ndarray) -> int:
    """RMS 换算成 -60dB..0dB 的 0..100。"""
    if samples.size == 0:
        return 0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    if rms <= 1e-6:
        return 0
    db = 20 * math.log10(rms)
    return max(0, min(100, int((db + 60) / 60 * 100)))


class _Relay(QObject):
    chunk = Signal(int, float)  # 电平百分比, 本块 peak


class MicTestWidget(QWidget):
    """按钮 + 电平条 + 结论。device_fn 返回当前下拉框选的设备（None=系统默认）。"""

    def __init__(
        self,
        device_fn: Callable[[], str | None],
        capture_factory: Callable[..., audio.Capture] = audio.Capture,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._device_fn = device_fn
        self._factory = capture_factory
        self._capture: audio.Capture | None = None
        self._peak = 0.0
        self._chunks = 0
        self._device_label = ""
        self._relay = _Relay()
        self._relay.chunk.connect(self._on_chunk, Qt.ConnectionType.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.stop)

        self.button = QPushButton("测试麦克风")
        self.button.setToolTip(f"用上面选中的麦克风录 {TEST_SECONDS} 秒（不保存），对着它说几句话看电平条有没有动")
        self.button.clicked.connect(self.toggle)
        self.button.setMinimumWidth(104)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setStyleSheet(LEVEL_QSS)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setStyleSheet("color:#8991a4; font-size:12px;")
        self.result.hide()

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.button)
        row.addWidget(self.meter, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addLayout(row)
        lay.addWidget(self.result)

    @property
    def running(self) -> bool:
        return self._capture is not None

    def toggle(self) -> None:
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.running:
            return
        if audio.audio_service_running() is False:
            self._show(audio.AUDIO_SERVICE_HELP.split("\n\n")[0] + " 先按「环境自检」里的方法启动服务。", error=True)
            return
        device = self._device_fn()
        if device == "__off__":
            device = None
        self._device_label = device or f"系统默认：{audio.default_microphone_name() or '未知'}"
        self._peak = 0.0
        self._chunks = 0
        self.meter.setValue(0)
        self._show(f"正在录音（{self._device_label}）… 请对着麦克风说话")
        relay = self._relay

        def on_audio(_source: str, samples: np.ndarray) -> None:
            relay.chunk.emit(level_percent(samples), float(np.max(np.abs(samples))) if samples.size else 0.0)

        capture = self._factory(on_audio, None, device, True)
        capture.set_enabled(audio.THEM, False)
        self._capture = capture
        capture.start()
        self.button.setText("停止")
        self._timer.start(TEST_SECONDS * 1000)

    def stop(self) -> None:
        capture, self._capture = self._capture, None
        self._timer.stop()
        self.button.setText("测试麦克风")
        if capture is None:
            return
        capture.stop()
        self.meter.setValue(0)
        self._show(self._verdict(), error=self._peak < SILENT_PEAK)

    def _on_chunk(self, percent: int, peak: float) -> None:
        if not self.running:
            return
        self._chunks += 1
        self._peak = max(self._peak, peak)
        self.meter.setValue(percent)

    def _verdict(self) -> str:
        name = self._device_label
        virtual = audio.is_virtual_device(name)
        if self._chunks == 0:
            text = f"【失败】打不开这个麦克风（{name}）：设备被占用/已拔出，或 Windows 音频服务异常。详见日志。"
        elif self._peak < SILENT_PEAK:
            text = (
                f"【没声音】没听到声音（{name}，峰值 {self._peak:.3f}）。"
                "检查：麦克风是否插好/未静音；Windows「设置 → 隐私 → 麦克风」是否允许桌面应用访问；"
                "这里是否选对了设备。"
            )
        else:
            text = f"【正常】有声音输入（{name}，峰值 {self._peak:.2f}）。"
        if virtual:
            text += "\n【注意】这是虚拟声卡，录到的是其他程序送进来的声音，不是你的麦克风；请改选真实麦克风。"
        return text

    def _show(self, text: str, error: bool = False) -> None:
        color = "#e06c75" if error else "#8991a4"
        self.result.setStyleSheet(f"color:{color}; font-size:12px;")
        self.result.setText(text)
        self.result.show()
        # 换行标签在表单里改文字后不会自动长高，手动按宽度算一次高度。
        width = self.result.width() if self.result.width() > 50 else max(200, self.width())
        self.result.setMinimumHeight(self.result.heightForWidth(width))
        self.updateGeometry()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.result.isVisible() and self.result.width() > 50:
            self.result.setMinimumHeight(self.result.heightForWidth(self.result.width()))

    def hideEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().hideEvent(event)


def make_chime(rate: int = 48000, volume: float = 0.25) -> np.ndarray:
    """「叮-咚-叮」三音提示音（约 1.6 秒），比单调 440Hz 更容易分辨是不是本程序在响。"""
    parts = []
    for freq, seconds in ((660.0, 0.45), (880.0, 0.45), (1320.0, 0.7)):
        n = int(rate * seconds)
        t = np.arange(n, dtype=np.float32) / rate
        env = np.exp(-3.5 * t / seconds).astype(np.float32)
        attack = min(n, int(rate * 0.01))
        env[:attack] *= np.linspace(0.0, 1.0, attack, dtype=np.float32)
        parts.append(np.sin(2 * np.pi * freq * t).astype(np.float32) * env * volume)
    return np.ascontiguousarray(np.concatenate(parts), dtype=np.float32)


class _PlayRelay(QObject):
    done = Signal(str)  # 空串=成功，否则是错误信息


class SpeakerTestWidget(QWidget):
    """「播放测试音」按钮 + 实际设备说明 + 虚拟声卡警告。

    device_fn 返回下拉框选中的设备（None=自动）；resolve_fn 把 None 解析成实际会用的设备名。
    """

    def __init__(
        self,
        device_fn: Callable[[], str | None],
        resolve_fn: Callable[[str | None], str | None],
        play_fn: Callable[[str | None, np.ndarray, int], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._device_fn = device_fn
        self._resolve_fn = resolve_fn
        self._play_fn = play_fn or _play_on
        self._relay = _PlayRelay()
        self._relay.done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)
        self._playing = False
        self.button = QPushButton("播放测试音")
        self.button.setMinimumWidth(104)
        self.button.setToolTip("在选中的设备上播放一段「叮-咚-叮」，听得到就说明中文 TTS 也能听到")
        self.button.clicked.connect(self.play)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.button)
        row.addWidget(self.info, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(row)
        self.refresh()

    def _target(self) -> str | None:
        device = self._device_fn()
        return device if device else self._resolve_fn(None)

    def refresh(self) -> None:
        """下拉框变化时调用：显示实际会播放到哪，选到虚拟声卡时标红。"""
        if self._playing:
            return
        target = self._target()
        if audio.is_virtual_device(target):
            self._show(
                f"【注意】{target} 是虚拟声卡，声音会送进会议软件而不是你的耳机，你会听不到中文。"
                "请改选「自动」或你的耳机/扬声器。",
                error=True,
            )
        else:
            self._show(f"将播放到：{target or '系统默认输出'}")

    def play(self) -> None:
        if self._playing:
            return
        target = self._target()
        self._playing = True
        self.button.setEnabled(False)
        self.button.setText("播放中…")
        self._show(f"正在播放到：{target or '系统默认输出'} … 听到「叮-咚-叮」了吗？")
        relay = self._relay
        play_fn = self._play_fn

        def run() -> None:
            try:
                play_fn(target, make_chime(), 48000)
                relay.done.emit("")
            except Exception as e:  # noqa: BLE001
                relay.done.emit(str(e) or type(e).__name__)

        threading.Thread(target=run, name="speaker-test", daemon=True).start()

    def _on_done(self, error: str) -> None:
        self._playing = False
        self.button.setEnabled(True)
        self.button.setText("播放测试音")
        target = self._target()
        if error:
            self._show(f"【失败】无法在 {target or '系统默认输出'} 上播放：{error}", error=True)
        elif audio.is_virtual_device(target):
            self.refresh()
        else:
            self._show(
                f"已播放到：{target or '系统默认输出'}。听不到就换一个设备再试，"
                "并检查耳机是否插好、Windows 音量合成器里本程序是否静音。"
            )

    def _show(self, text: str, error: bool = False) -> None:
        color = "#e06c75" if error else "#8991a4"
        self.info.setStyleSheet(f"color:{color}; font-size:12px;")
        self.info.setText(text)


def _play_on(device: str | None, samples: np.ndarray, rate: int) -> None:
    import ctypes

    ctypes.windll.ole32.CoInitializeEx(None, 0)
    spk = audio.sc.get_speaker(device) if device else audio.sc.default_speaker()
    with spk.player(samplerate=rate, channels=2) as player:
        player.play(np.repeat(samples[:, None], 2, axis=1))
