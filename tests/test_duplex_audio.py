from __future__ import annotations

import time

import numpy as np
import pytest

from meeting_translate import audio
from meeting_translate.asr import Final
from meeting_translate.config import Settings
from meeting_translate.duplex_audio import (
    PassthroughRouter,
    Player,
    ProbeResult,
    detect_cable_devices,
    make_tone,
    probe_cable,
    resolve_devices,
)

SPEAKERS = [
    "CABLE-A Input (VB-Audio Virtual Cable)",
    "CABLE-B Input (VB-Audio Virtual Cable)",
    "Headphones (Realtek Audio)",
]
MICS = [
    "CABLE-A Output (VB-Audio Virtual Cable)",
    "CABLE-B Output (VB-Audio Virtual Cable)",
    "Microphone (Realtek Audio)",
]


def test_detect_cable_devices_maps_all_four_endpoints() -> None:
    devices = detect_cable_devices(SPEAKERS, MICS)
    assert devices.a_input == "CABLE-A Input (VB-Audio Virtual Cable)"
    assert devices.a_output == "CABLE-A Output (VB-Audio Virtual Cable)"
    assert devices.b_input == "CABLE-B Input (VB-Audio Virtual Cable)"
    assert devices.b_output == "CABLE-B Output (VB-Audio Virtual Cable)"
    assert devices.capture == devices.a_input
    assert devices.remote_speaker == devices.b_input
    assert devices.complete


def test_detect_cable_devices_reports_missing() -> None:
    devices = detect_cable_devices(["Speakers"], ["Microphone"])
    assert devices.a_input is None
    assert devices.b_input is None
    assert not devices.complete
    assert devices.missing == (
        "CABLE-A Input",
        "CABLE-A Output",
        "CABLE-B Input",
        "CABLE-B Output",
    )


def test_resolve_devices_prefers_settings_and_falls_back() -> None:
    settings = Settings(
        duplex_mic_device="Microphone (Realtek Audio)",
        duplex_local_speaker="Headphones (Realtek Audio)",
    )
    resolved = resolve_devices(settings, SPEAKERS, MICS)
    assert resolved.mic == "Microphone (Realtek Audio)"
    assert resolved.capture == "CABLE-A Input (VB-Audio Virtual Cable)"
    assert resolved.remote_speaker == "CABLE-B Input (VB-Audio Virtual Cable)"
    assert resolved.local_speaker == "Headphones (Realtek Audio)"
    assert resolved.missing == ()


def test_resolve_devices_auto_picks_physical_output_for_local_tts() -> None:
    settings = Settings()
    resolved = resolve_devices(settings, SPEAKERS, MICS)
    assert resolved.local_speaker == "Headphones (Realtek Audio)"


def test_resolve_devices_rejects_wrong_endpoint() -> None:
    settings = Settings(duplex_capture_device="CABLE-A Output (VB-Audio Virtual Cable)")
    resolved = resolve_devices(settings, SPEAKERS, MICS)
    assert resolved.capture == "CABLE-A Input (VB-Audio Virtual Cable)"
    assert "duplex_capture_device" in resolved.missing


def test_make_tone_shape_fade_and_peak() -> None:
    tone = make_tone(seconds=0.5, volume=0.2)
    assert tone.dtype == np.float32
    assert tone.size == 24000
    assert abs(float(tone[0])) < 1e-6
    assert abs(float(tone[-1])) < 1e-6
    assert float(np.max(np.abs(tone))) <= 0.201
    assert 0.02 < float(np.sqrt(np.mean(tone * tone))) < 0.16


def test_probe_result_threshold() -> None:
    assert not ProbeResult(0.001, 0.002).ok
    assert ProbeResult(0.01, 0.02).ok


def test_probe_cable_plays_and_records(monkeypatch: pytest.MonkeyPatch) -> None:
    devices = detect_cable_devices(SPEAKERS, MICS)
    played: list[str] = []
    monkeypatch.setattr("meeting_translate.duplex_audio.record_level", lambda device, seconds: ProbeResult(0.05, 0.1))
    monkeypatch.setattr("meeting_translate.duplex_audio.play_tone", lambda device, seconds: played.append(device))
    result = probe_cable("A", devices, seconds=0.1)
    assert result.ok
    assert played == [devices.a_input]
    with pytest.raises(ValueError):
        probe_cable("C", devices)


def test_passthrough_router_routes_target_language_only() -> None:
    class FakePlayer:
        def __init__(self) -> None:
            self.calls: list[tuple[np.ndarray, int]] = []

        def enqueue(self, samples: np.ndarray, sample_rate: int) -> None:
            self.calls.append((np.asarray(samples), sample_rate))

    local = FakePlayer()
    remote = FakePlayer()
    router = PassthroughRouter(local, remote)  # type: ignore[arg-type]
    data = np.ones(160, dtype=np.float32)

    assert router.route(Final(1, audio.ME, "ja", 1.0, 1.0, lang="ja", audio=data)) == "remote"
    assert router.route(Final(2, audio.THEM, "zh", 1.0, 1.0, lang="zh", audio=data)) == "local"
    assert router.route(Final(3, audio.ME, "zh", 1.0, 1.0, lang="zh", audio=data)) is None
    assert router.route(Final(4, audio.ME, "ja", 1.0, 1.0, lang="ja")) is None
    assert len(remote.calls) == 1
    assert len(local.calls) == 1
    assert remote.calls[0][1] == audio.SAMPLE_RATE
    assert local.calls[0][1] == audio.SAMPLE_RATE


def test_player_enqueues_and_stops_cleanly() -> None:
    class RecordingPlayer(Player):
        def __init__(self) -> None:
            super().__init__(None)
            self.played: list[np.ndarray] = []

        def _play_clip(self, samples: np.ndarray, sample_rate: int, generation: int, on_start=None, on_done=None) -> None:
            if on_start is not None:
                on_start()
            self.played.append(np.asarray(samples).copy())
            if on_done is not None:
                on_done()

    player = RecordingPlayer()
    player.enqueue(np.ones(160, dtype=np.float32))
    deadline = time.monotonic() + 1.0
    while not player.played and time.monotonic() < deadline:
        time.sleep(0.01)
    player.stop()
    player.close()
    assert player.played
    assert player.played[0].shape == (160,)


def test_detect_cable_devices_supports_free_cable_plus_hifi() -> None:
    speakers = [
        "CABLE In 16ch (VB-Audio Virtual Cable)",
        "CABLE Input (VB-Audio Virtual Cable)",
        "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)",
        "Headphones (Realtek Audio)",
    ]
    mics = [
        "CABLE Output (VB-Audio Virtual Cable)",
        "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)",
        "Microphone (Realtek Audio)",
    ]
    devices = detect_cable_devices(speakers, mics)
    assert devices.a_input == "CABLE Input (VB-Audio Virtual Cable)"
    assert devices.a_output == "CABLE Output (VB-Audio Virtual Cable)"
    assert devices.b_input == "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)"
    assert devices.b_output == "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)"
    assert devices.complete
