from __future__ import annotations

from meeting_translate.config import Settings
from meeting_translate.diagnostics import cable_checks, device_checks, mic_check
from meeting_translate.duplex_audio import CableDevices, ProbeResult, ResolvedDevices


SPEAKERS = [
    "CABLE Input (VB-Audio Virtual Cable)",
    "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)",
    "Headphones",
]
MICS = [
    "CABLE Output (VB-Audio Virtual Cable)",
    "Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)",
    "HyperX Cloud III",
]


def test_device_checks_all_present() -> None:
    settings = Settings(
        meeting_mode="duplex",
        duplex_mic_device="HyperX Cloud III",
        duplex_local_speaker="Headphones",
    )
    checks, resolved = device_checks(settings, SPEAKERS, MICS)
    assert resolved.capture == "CABLE Input (VB-Audio Virtual Cable)"
    assert resolved.remote_speaker == "Hi-Fi Cable Input (VB-Audio Hi-Fi Cable)"
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_device_checks_reports_missing_cable() -> None:
    settings = Settings(meeting_mode="duplex")
    checks, resolved = device_checks(settings, ["CABLE Input (VB-Audio Virtual Cable)"], MICS)
    assert resolved.remote_speaker is None
    assert any(not c.ok for c in checks)
    assert any("CABLE-B" in c.name or "设备缺口" in c.name for c in checks)


def test_cable_checks_uses_both_probes() -> None:
    devices = CableDevices(
        a_input="CABLE Input",
        a_output="CABLE Output",
        b_input="Hi-Fi Cable Input",
        b_output="Hi-Fi Cable Output",
    )
    resolved = ResolvedDevices(
        mic=None,
        capture=devices.a_input,
        remote_speaker=devices.b_input,
        local_speaker=None,
        detected=devices,
    )
    calls: list[str] = []

    def probe(cable: str, dev: CableDevices, seconds: float) -> ProbeResult:
        calls.append(cable)
        return ProbeResult(0.1, 0.2)

    checks = cable_checks(resolved, probe)
    assert calls == ["A", "B"]
    assert all(c.ok for c in checks)
    assert all("RMS 0.100" in c.detail for c in checks)


def test_mic_check_uses_result() -> None:
    settings = Settings(duplex_mic_device="HyperX Cloud III")
    check = mic_check(settings, lambda device, seconds: ProbeResult(0.02, 0.1))
    assert check.ok
    assert "HyperX" in check.detail
