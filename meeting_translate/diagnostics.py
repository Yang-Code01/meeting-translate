"""运行环境自检：设备、CABLE 回采、麦克风、TTS 引擎。

自检只读取设备并播放短测试音，不启动会议、不改设置。放在 QThread 里跑，
避免 1~20 秒的采集/模型加载卡住主界面。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import MODEL_DIR, Settings
from .duplex_audio import CableDevices, ProbeResult, ResolvedDevices, probe_cable, record_level, resolve_devices
from .tts_client import TtsClient, default_worker_command
from .tts_models import ModelManager
from .trial import Recorder, TrialResult, TrialWorker


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    warning: bool = False


def device_checks(
    settings: Settings,
    speakers: list[str] | None = None,
    microphones: list[str] | None = None,
) -> tuple[list[Check], ResolvedDevices]:
    """检查路由设备是否齐全；返回检查项和解析后的设备名。"""
    resolved = resolve_devices(settings, speakers, microphones)
    detected = resolved.detected
    mic = resolved.mic or settings.mic_device
    local = resolved.local_speaker or settings.duplex_local_speaker

    checks = [
        Check("CABLE-A 播放端点（对方声音）", bool(resolved.capture), resolved.capture or "未找到 CABLE Input"),
        Check("CABLE-A 录音回采", bool(detected.a_output), detected.a_output or "未找到 CABLE Output"),
        Check("CABLE-B 播放端点（日语 TTS）", bool(resolved.remote_speaker), resolved.remote_speaker or "未找到 Hi-Fi Cable Input"),
        Check("CABLE-B 录音端点（会议软件麦克风）", bool(detected.b_output), detected.b_output or "未找到 Hi-Fi Cable Output"),
        Check("物理麦克风", True, mic or "系统默认输入（可用性由下方采集测试确认）"),
    ]
    if local:
        checks.append(Check("本地耳机", True, local))
    else:
        checks.append(Check("本地耳机", True, "未指定，使用系统默认输出", warning=True))
    if resolved.missing:
        checks.append(Check("设备缺口", False, "、".join(resolved.missing)))
    return checks, resolved


def cable_checks(
    resolved: ResolvedDevices,
    probe: Callable[[str, CableDevices, float], ProbeResult] = probe_cable,
) -> list[Check]:
    """播放测试音并从对应录音端点回采，验证 A/B 两条虚拟声卡链路。"""
    devices = CableDevices(
        a_input=resolved.capture,
        a_output=resolved.detected.a_output,
        b_input=resolved.remote_speaker,
        b_output=resolved.detected.b_output,
    )
    checks: list[Check] = []
    for label, cable in (("CABLE-A 回采", "A"), ("CABLE-B 回采", "B")):
        try:
            result = probe(cable, devices, 0.8)
        except Exception as e:  # noqa: BLE001
            checks.append(Check(label, False, str(e)))
            continue
        checks.append(
            Check(
                label,
                result.ok,
                f"RMS {result.rms:.3f} / peak {result.peak:.3f}",
            )
        )
    return checks


def _sounddevice_level(device: str | None, seconds: float) -> ProbeResult:
    import numpy as np
    import sounddevice as sd

    devices = sd.query_devices()
    index = None
    if device:
        for i, item in enumerate(devices):
            if item["max_input_channels"] > 0 and device in item["name"]:
                index = i
                break
    if index is None:
        index = sd.default.device[0]
    if index is None or index < 0:
        raise RuntimeError("没有可用的默认麦克风")

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

    data = sd.rec(
        int(seconds * rate),
        samplerate=rate,
        channels=1,
        dtype="float32",
        device=index,
        blocking=True,
    )
    flat = np.asarray(data, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        return ProbeResult(0.0, 0.0)
    return ProbeResult(float(np.sqrt(np.mean(flat * flat))), float(np.max(np.abs(flat))))


def mic_level(device: str | None, seconds: float = 1.2) -> ProbeResult:
    """优先 soundcard；HyperX 等设备会断言失败，回退 sounddevice。"""
    try:
        return record_level(device, seconds)
    except Exception as first:
        try:
            return _sounddevice_level(device, seconds)
        except Exception as second:  # noqa: BLE001
            raise RuntimeError(f"soundcard: {first}; sounddevice: {second}") from second


def mic_check(settings: Settings, level: Callable[[str | None, float], ProbeResult] = mic_level) -> Check:
    device = settings.duplex_mic_device
    try:
        result = level(device, 1.2)
    except Exception as e:  # noqa: BLE001
        return Check("麦克风输入", False, str(e))
    return Check(
        "麦克风输入",
        result.ok,
        f"RMS {result.rms:.3f} / peak {result.peak:.3f}（{device or '系统默认'}）",
    )


def tts_checks(settings: Settings, progress: Callable[[str], None] | None = None) -> list[Check]:
    """检查模型是否齐全，并用短句各合成一次；不播放，避免与会议抢占设备。"""
    root = Path(os.environ.get("MEETING_TRANSLATE_TTS_DIR") or (MODEL_DIR / "tts"))
    manager = ModelManager(root)
    missing = manager.missing("melo", verify_hash=False) + manager.missing("sbv2", verify_hash=False)
    if missing:
        names = "、".join(str(p.name) for p in missing[:3])
        return [
            Check(
                "TTS 模型",
                False,
                f"缺少 {len(missing)} 个文件（{names}…）；双向 TTS 暂不可用",
                warning=True,
            )
        ]

    if progress:
        progress("正在加载日文 TTS…")
    client: TtsClient | None = None
    checks: list[Check] = []
    try:
        command = None if os.environ.get("MEETING_TRANSLATE_TTS_WORKER") else default_worker_command(root, "auto")
        client = TtsClient(command)
        client.start()
        started = time.monotonic()
        client.warm("sbv2")
        ja = client.synth("こんにちは。今日は会議です。", engine="sbv2", voice="koharune_ami")
        checks.append(
            Check(
                "日文 TTS（小春音アミ）",
                ja.samples.size > 0,
                f"{ja.samples.size / ja.sample_rate:.2f}s 音频 / 总计 {time.monotonic() - started:.1f}s",
            )
        )
        if progress:
            progress("正在加载中文 TTS…")
        started = time.monotonic()
        client.warm("melo")
        zh = client.synth("你好，今天的会议开始了。", engine="melo")
        checks.append(
            Check(
                "中文 TTS（MeloTTS）",
                zh.samples.size > 0,
                f"{zh.samples.size / zh.sample_rate:.2f}s 音频 / 总计 {time.monotonic() - started:.1f}s",
            )
        )
    except Exception as e:  # noqa: BLE001
        checks.append(Check("TTS 引擎", False, str(e), warning=True))
    finally:
        if client is not None:
            client.close()
    return checks


def run_checks(
    settings: Settings,
    progress: Callable[[str], None] | None = None,
) -> list[Check]:
    checks, resolved = device_checks(settings)
    if progress:
        progress("正在测试 CABLE-A/B 回采…")
    checks.extend(cable_checks(resolved))
    if progress:
        progress("正在测试麦克风…")
    checks.append(mic_check(settings))
    if progress:
        progress("正在测试 TTS 引擎（首次加载约 10~20 秒）…")
    checks.extend(tts_checks(settings, progress))
    return checks


DIAG_QSS = """
QDialog#diagnostics { background: #0f1115; }
QLabel#diagTitle { color: #f4f6fb; font-size: 22px; font-weight: 800; }
QLabel#diagSub { color: #8991a4; font-size: 12px; }
QListWidget {
    background: #171a21; color: #dce3f2; border: 1px solid #272d3a;
    border-radius: 12px; padding: 8px; font-size: 13px;
}
QProgressBar {
    background: #171a21; border: 1px solid #272d3a; border-radius: 7px;
    height: 12px; text-align: center; color: #8991a4;
}
QProgressBar::chunk { background: #4f7cff; border-radius: 6px; }
QPushButton#diagPrimary {
    color: white; background: #4f7cff; border: none; border-radius: 9px;
    padding: 9px 18px; font-weight: 700;
}
QPushButton#diagGhost {
    color: #dce3f2; background: #202635; border: 1px solid #313a4f;
    border-radius: 9px; padding: 9px 16px;
}
QLabel#diagStatus { color: #8991a4; font-size: 12px; }
QLabel#trialHint { color: #8991a4; font-size: 12px; }
QLabel#trialText { color: #dce3f2; font-size: 14px; line-height: 1.5; }
QTabWidget::pane { border: 1px solid #272d3a; border-radius: 10px; top: -1px; }
QTabBar::tab {
    color: #8991a4; background: #151922; border: 1px solid #272d3a;
    padding: 8px 18px; border-top-left-radius: 8px; border-top-right-radius: 8px;
}
QTabBar::tab:selected { color: #f4f6fb; background: #202635; border-bottom-color: #202635; }
"""


class DiagnosticsWorker(QThread):
    check = Signal(object)
    progress = Signal(str)

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings

    def run(self) -> None:  # noqa: D401
        try:
            for item in run_checks(self.settings, self.progress.emit):
                if self.isInterruptionRequested():
                    break
                self.check.emit(item)
        except Exception as e:  # noqa: BLE001
            self.check.emit(Check("自检异常", False, str(e)))
        finally:
            self.progress.emit("自检完成")


class DiagnosticsDialog(QDialog):
    """一键自检窗口：检查设备、回采、麦克风、TTS。"""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.worker: DiagnosticsWorker | None = None
        self.recorder: Recorder | None = None
        self.trial_worker: TrialWorker | None = None
        self.trial_result: TrialResult | None = None
        self.setObjectName("diagnostics")
        self.setWindowTitle("环境自检 / 试一句")
        self.resize(760, 680)
        self.setStyleSheet(DIAG_QSS)

        title = QLabel("环境自检")
        title.setObjectName("diagTitle")
        sub = QLabel("不启动会议；会播放 0.8 秒测试音，并可能首次加载 TTS 模型。请在安静环境下运行。")
        sub.setObjectName("diagSub")
        sub.setWordWrap(True)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setSpacing(2)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.status = QLabel("点击「开始自检」")
        self.status.setObjectName("diagStatus")

        self.start_btn = QPushButton("开始自检")
        self.start_btn.setObjectName("diagPrimary")
        self.start_btn.clicked.connect(self.start)
        self.copy_btn = QPushButton("复制结果")
        self.copy_btn.setObjectName("diagGhost")
        self.copy_btn.clicked.connect(self.copy_result)
        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("diagGhost")
        self.close_btn.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(self.copy_btn)
        buttons.addWidget(self.close_btn)
        buttons.addWidget(self.start_btn)

        test_page = QWidget()
        test_lay = QVBoxLayout(test_page)
        test_lay.setContentsMargins(0, 8, 0, 0)
        test_lay.setSpacing(10)
        test_lay.addWidget(self.list, 1)
        test_lay.addWidget(self.progress)
        test_lay.addLayout(buttons)

        trial_page = QWidget()
        trial_lay = QVBoxLayout(trial_page)
        trial_lay.setContentsMargins(0, 8, 0, 0)
        trial_lay.setSpacing(10)
        hint = QLabel(
            "按「开始录音」说一句中文，再点「结束并翻译」。\n"
            "不会启动会议，也不经过 CABLE；只用默认麦克风、当前模型服务和本地日文 TTS。"
        )
        hint.setObjectName("trialHint")
        hint.setWordWrap(True)
        self.record_btn = QPushButton("开始录音")
        self.record_btn.setObjectName("diagPrimary")
        self.record_btn.clicked.connect(self.toggle_record)
        self.play_btn = QPushButton("播放日语")
        self.play_btn.setObjectName("diagGhost")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self.play_trial)
        trial_buttons = QHBoxLayout()
        trial_buttons.addWidget(self.record_btn)
        trial_buttons.addWidget(self.play_btn)
        trial_buttons.addStretch(1)
        self.trial_original = QLabel("原文：—")
        self.trial_original.setObjectName("trialText")
        self.trial_original.setWordWrap(True)
        self.trial_original.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.trial_translation = QLabel("日文：—")
        self.trial_translation.setObjectName("trialText")
        self.trial_translation.setWordWrap(True)
        self.trial_translation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.trial_status = QLabel("等待录音")
        self.trial_status.setObjectName("diagStatus")
        trial_lay.addWidget(hint)
        trial_lay.addLayout(trial_buttons)
        trial_lay.addWidget(self.trial_original)
        trial_lay.addWidget(self.trial_translation)
        trial_lay.addWidget(self.trial_status)
        trial_lay.addStretch(1)

        self.tabs = QTabWidget()
        self.tabs.addTab(test_page, "环境自检")
        self.tabs.addTab(trial_page, "试一句")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(12)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addWidget(self.tabs, 1)

    def start(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if self.recorder is not None and self.recorder.active:
            self.status.setText("先结束「试一句」录音")
            return
        self.list.clear()
        self.start_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.status.setText("正在自检…")
        self.worker = DiagnosticsWorker(self.settings, self)
        self.worker.check.connect(self._add_check)
        self.worker.progress.connect(self.status.setText)
        self.worker.finished.connect(self._done)
        self.worker.start()

    def toggle_record(self) -> None:
        if self.recorder is not None and self.recorder.active:
            self._stop_record()
        else:
            self._start_record()

    def _start_record(self) -> None:
        if (self.worker is not None and self.worker.isRunning()) or (
            self.trial_worker is not None and self.trial_worker.isRunning()
        ):
            self.trial_status.setText("上一项还在运行，请稍候…")
            return
        self.trial_result = None
        self.play_btn.setEnabled(False)
        self.trial_original.setText("原文：—")
        self.trial_translation.setText("日文：—")
        self.recorder = Recorder(self.settings.duplex_mic_device)
        try:
            self.recorder.start()
        except Exception as e:  # noqa: BLE001
            self.recorder = None
            self.trial_status.setText(f"录音启动失败：{e}")
            return
        self.record_btn.setText("结束并翻译")
        self.trial_status.setText("录音中…说完后点「结束并翻译」")

    def _stop_record(self) -> None:
        assert self.recorder is not None
        recorder = self.recorder
        self.recorder = None
        self.record_btn.setText("开始录音")
        try:
            audio = recorder.stop()
        except Exception as e:  # noqa: BLE001
            self.trial_status.setText(f"录音失败：{e}")
            return
        if audio.size < 16000 * 0.35:
            self.trial_status.setText("录音太短，请按住说完整一句")
            return
        self.record_btn.setEnabled(False)
        self.play_btn.setEnabled(False)
        self.trial_status.setText("准备识别…")
        self.trial_worker = TrialWorker(self.settings, audio, self)
        self.trial_worker.stage.connect(self.trial_status.setText)
        self.trial_worker.done.connect(self._trial_done)
        self.trial_worker.failed.connect(self._trial_failed)
        self.trial_worker.finished.connect(self._trial_finished)
        self.trial_worker.start()

    def _trial_done(self, result: TrialResult) -> None:
        self.trial_result = result
        self.trial_original.setText("原文：" + result.chinese)
        self.trial_translation.setText("日文：" + result.japanese)
        self.trial_status.setText(f"完成，用时 {result.latency:.1f}s；点「播放日语」试听")
        self.play_btn.setEnabled(True)

    def _trial_failed(self, message: str) -> None:
        self.trial_status.setText("试一句失败：" + message)

    def _trial_finished(self) -> None:
        self.record_btn.setEnabled(True)

    def play_trial(self) -> None:
        if self.trial_result is None:
            return
        try:
            import sounddevice as sd

            sd.play(self.trial_result.audio, self.trial_result.sample_rate)
            self.trial_status.setText("正在播放日语…")
        except Exception as e:  # noqa: BLE001
            self.trial_status.setText(f"播放失败：{e}")

    def _add_check(self, item: Check) -> None:
        if item.ok:
            icon, color = "✓", QColor("#5ee0a0")
        elif item.warning:
            icon, color = "!", QColor("#f0c45c")
        else:
            icon, color = "✗", QColor("#ff7b7b")
        label = QListWidgetItem(f"{icon}  {item.name}")
        label.setForeground(color)
        if item.detail:
            label.setText(f"{icon}  {item.name}\n     {item.detail}")
        self.list.addItem(label)
        self.list.scrollToBottom()

    def _done(self) -> None:
        self.start_btn.setEnabled(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        failures = [
            self.list.item(i).text().splitlines()[0]
            for i in range(self.list.count())
            if self.list.item(i).text().startswith("✗")
        ]
        if failures:
            self.status.setText(f"自检完成：{len(failures)} 项失败")
        else:
            self.status.setText("自检完成")

    def copy_result(self) -> None:
        lines = [self.list.item(i).text() for i in range(self.list.count())]
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText("\n".join(lines))
        self.status.setText("结果已复制")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.recorder is not None and self.recorder.active:
            self.recorder.close()
            self.recorder = None
        if self.worker is not None and self.worker.isRunning():
            self.status.setText("自检仍在运行，请稍候…")
            event.ignore()
            return
        if self.trial_worker is not None and self.trial_worker.isRunning():
            self.trial_status.setText("试一句仍在运行，请稍候…")
            event.ignore()
            return
        event.accept()
