"""麦克风兜底选择、麦克风测试控件、退出时的音频清理。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate import app as app_mod  # noqa: E402
from meeting_translate import audio, mic_test  # noqa: E402
from meeting_translate.app import App, IDLE  # noqa: E402

DEVICES = [
    {"name": "CABLE Output (VB-Audio Point)", "max_input_channels": 2, "hostapi": 0},  # WDM-KS
    {"name": "ジャックマイク (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 0},  # WDM-KS
    {"name": "ジャックマイク (Realtek(R) Aud", "max_input_channels": 2, "hostapi": 1},  # MME 截断名
    {"name": "ジャックマイク (Realtek(R) Audio)", "max_input_channels": 2, "hostapi": 2},  # WASAPI
    {"name": "スピーカー", "max_input_channels": 0, "hostapi": 2},
]
HOSTAPIS = [
    {"name": "Windows WDM-KS", "devices": [0, 1], "default_input_device": 0},
    {"name": "MME", "devices": [2], "default_input_device": 2},
    {"name": "Windows WASAPI", "devices": [3, 4], "default_input_device": 3},
]


def test_sounddevice_pick_skips_wdm_ks_and_prefers_wasapi() -> None:
    assert audio.pick_sounddevice_input(DEVICES, HOSTAPIS, "ジャックマイク") == 3
    assert audio.pick_sounddevice_input(DEVICES, HOSTAPIS, None) == 3
    only_mme = [HOSTAPIS[0], HOSTAPIS[1]]
    assert audio.pick_sounddevice_input(DEVICES, only_mme, None) == 2
    assert audio.pick_sounddevice_input(DEVICES, [HOSTAPIS[0]], None) is None


def test_virtual_device_detection() -> None:
    assert audio.is_virtual_device("Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)")
    assert audio.is_virtual_device("CABLE-B Output (VB-Audio Cable B)")
    assert not audio.is_virtual_device("ジャックマイク (Realtek(R) Audio)")
    assert not audio.is_virtual_device(None)


class FakeCapture:
    chunks: list[np.ndarray] = []

    def __init__(self, on_audio, loopback, mic, mic_on) -> None:
        self.on_audio, self.mic = on_audio, mic
        self.enabled = {audio.THEM: True, audio.ME: mic_on}
        self.stopped = False

    def set_enabled(self, source, on) -> None:
        self.enabled[source] = on

    def start(self) -> None:
        for c in self.chunks:
            self.on_audio(audio.ME, c)

    def stop(self) -> None:
        self.stopped = True


def _run_test(monkeypatch, device, chunks) -> mic_test.MicTestWidget:
    qapp = QApplication.instance() or QApplication([])
    monkeypatch.setattr(audio, "audio_service_running", lambda: True)
    monkeypatch.setattr(audio, "default_microphone_name", lambda: "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)")
    FakeCapture.chunks = chunks
    made: list[FakeCapture] = []

    def factory(*a):
        made.append(FakeCapture(*a))
        return made[-1]

    w = mic_test.MicTestWidget(lambda: device, capture_factory=factory)
    w.start()
    assert made[0].enabled[audio.THEM] is False  # 只录麦克风，不开环回
    assert w.button.text() == "停止"
    qapp.processEvents()
    w.stop()
    assert made[0].stopped and not w.running
    return w


def test_mic_test_reports_sound(monkeypatch) -> None:
    w = _run_test(monkeypatch, "ジャックマイク", [np.full(1600, 0.2, np.float32)])
    assert w.result.text().startswith("【正常】")
    assert "虚拟声卡" not in w.result.text()


def test_mic_test_reports_silence_and_virtual_default(monkeypatch) -> None:
    w = _run_test(monkeypatch, None, [np.zeros(1600, np.float32)])
    text = w.result.text()
    assert text.startswith("【没声音】")
    assert "系统默认：Hi-Fi Cable" in text and "虚拟声卡" in text


def test_mic_test_reports_open_failure(monkeypatch) -> None:
    w = _run_test(monkeypatch, "坏设备", [])
    assert w.result.text().startswith("【失败】")


def test_level_percent_scale() -> None:
    assert mic_test.level_percent(np.zeros(10, np.float32)) == 0
    assert mic_test.level_percent(np.ones(10, np.float32)) == 100
    assert 0 < mic_test.level_percent(np.full(10, 0.01, np.float32)) < 100


def _shutdown_app(tmp_path, monkeypatch) -> tuple[App, list[str]]:
    import threading

    calls: list[str] = []
    app = App.__new__(App)
    app.state = IDLE
    app._shutdown_lock = threading.Lock()
    app._audio_switch = None
    app._restore_output_name = None
    app._stop_capture = lambda: calls.append("capture")  # type: ignore[method-assign]
    app._close_tts = lambda: calls.append("tts")  # type: ignore[method-assign]
    monkeypatch.setattr(app_mod, "PENDING_OUTPUT_FILE", tmp_path / "pending.json")
    return app, calls


def test_shutdown_audio_closes_everything_even_when_idle(tmp_path, monkeypatch) -> None:
    app, calls = _shutdown_app(tmp_path, monkeypatch)

    class Switch:
        def restore(self):
            calls.append("restore")
            return audio_dev

    from meeting_translate.default_audio import OutputDevice, save_pending

    audio_dev = OutputDevice("hp", "耳机")
    save_pending(app_mod.PENDING_OUTPUT_FILE, audio_dev)
    app._audio_switch = Switch()  # type: ignore[assignment]
    app.shutdown_audio()
    assert calls == ["capture", "tts", "restore"]  # 先关流，再切回默认输出
    assert not app_mod.PENDING_OUTPUT_FILE.exists()
    app.shutdown_audio()  # 可重复调用
    assert calls == ["capture", "tts", "restore", "capture", "tts"]


def test_failed_restore_keeps_pending_for_next_launch(tmp_path, monkeypatch) -> None:
    app, _ = _shutdown_app(tmp_path, monkeypatch)
    from meeting_translate.default_audio import OutputDevice, save_pending

    save_pending(app_mod.PENDING_OUTPUT_FILE, OutputDevice("hp", "耳机"))
    app._restore_output_name = "耳机"
    monkeypatch.setattr(app_mod, "find_output", lambda name: None)
    app.shutdown_audio()
    assert app_mod.PENDING_OUTPUT_FILE.exists()


def test_speaker_test_warns_virtual_and_plays_on_resolved_device() -> None:
    qapp = QApplication.instance() or QApplication([])
    played: list[str | None] = []
    selected = {"v": "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)"}
    w = mic_test.SpeakerTestWidget(
        lambda: selected["v"], lambda _n: "ヘッドフォン (Realtek(R) Audio)", lambda d, s, r: played.append(d)
    )
    assert "虚拟声卡" in w.info.text()
    selected["v"] = None  # 自动 → 解析成真实耳机
    w.refresh()
    assert "ヘッドフォン" in w.info.text() and "虚拟声卡" not in w.info.text()
    w.play()
    assert not w.button.isEnabled()
    for _ in range(50):
        qapp.processEvents()
        if w.button.isEnabled():
            break
        import time

        time.sleep(0.01)
    assert played == ["ヘッドフォン (Realtek(R) Audio)"]
    assert w.button.isEnabled() and w.info.text().startswith("已播放到")


def test_speaker_test_reports_play_error() -> None:
    qapp = QApplication.instance() or QApplication([])

    def boom(*_a):
        raise RuntimeError("设备被占用")

    w = mic_test.SpeakerTestWidget(lambda: "耳机", lambda _n: None, boom)
    w.play()
    import time

    for _ in range(50):
        qapp.processEvents()
        if w.button.isEnabled():
            break
        time.sleep(0.01)
    assert w.info.text().startswith("【失败】") and "设备被占用" in w.info.text()


def test_physical_speaker_prefers_japanese_headphone_over_display_audio() -> None:
    from meeting_translate.duplex_audio import _physical_speaker

    names = [
        "IG27Q (2- HD Audio Driver for Display Audio)",
        "CABLE Input (VB-Audio Virtual Cable)",
        "ヘッドフォン (Realtek(R) Audio)",
        "スピーカー (Realtek(R) Audio)",
        "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)",
    ]
    assert _physical_speaker(names) == "ヘッドフォン (Realtek(R) Audio)"
    assert _physical_speaker([names[0], names[3]]) == "スピーカー (Realtek(R) Audio)"
    assert _physical_speaker([names[0], names[1]]) == names[0]
