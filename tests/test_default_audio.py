"""默认播放设备查找/切换/恢复的纯逻辑测试；不碰真实系统设备。"""

from __future__ import annotations

import pytest

from meeting_translate.default_audio import (
    AudioSwitchError,
    DefaultOutputSwitch,
    OutputDevice,
    find_output,
)
from pycaw.constants import ERole

HEADPHONE = OutputDevice("headphone-id", "耳机 (HyperX Cloud III)")
CABLE = OutputDevice("cable-id", "CABLE In 16ch (VB-Audio Virtual Cable)")


def test_find_output_prefers_exact_and_accepts_short_name() -> None:
    devices = [HEADPHONE, CABLE]
    assert find_output("CABLE In 16ch (VB-Audio Virtual Cable)", devices) == CABLE
    assert find_output("cable in 16ch", devices) == CABLE
    assert find_output("不存在的设备", devices) is None


def test_switch_saves_previous_and_restores() -> None:
    previous = {
        ERole.eConsole: HEADPHONE,
        ERole.eMultimedia: HEADPHONE,
        ERole.eCommunications: HEADPHONE,
    }
    calls: list[tuple[str, tuple[ERole, ...] | None]] = []

    def set_fn(device_id: str, roles: tuple[ERole, ...] | None = None) -> None:
        calls.append((device_id, roles))

    switch = DefaultOutputSwitch(
        snapshot_fn=lambda: dict(previous),
        set_fn=set_fn,
        find_fn=lambda name: CABLE if "CABLE" in name.upper() else None,
    )
    result = switch.switch_to("CABLE")
    assert result.changed
    assert result.previous == HEADPHONE
    assert result.target == CABLE
    assert calls[0] == ("cable-id", None)

    restored = switch.restore()
    assert restored == HEADPHONE
    assert {c[0] for c in calls[1:]} == {"headphone-id"}
    assert not switch.active


def test_switch_noop_when_already_target_does_not_restore() -> None:
    previous = {
        ERole.eConsole: CABLE,
        ERole.eMultimedia: CABLE,
        ERole.eCommunications: CABLE,
    }
    calls: list[str] = []

    def set_fn(device_id: str, roles: tuple[ERole, ...] | None = None) -> None:
        calls.append(device_id)

    switch = DefaultOutputSwitch(
        snapshot_fn=lambda: dict(previous),
        set_fn=set_fn,
        find_fn=lambda name: CABLE,
    )
    result = switch.switch_to("CABLE")
    assert not result.changed
    assert not switch.active
    assert calls == []
    assert switch.restore() is None


def test_missing_target_raises() -> None:
    switch = DefaultOutputSwitch(
        snapshot_fn=lambda: {},
        set_fn=lambda *a, **k: None,
        find_fn=lambda name: None,
    )
    with pytest.raises(AudioSwitchError):
        switch.switch_to("CABLE")

def test_pending_restore_roundtrip(tmp_path) -> None:
    from meeting_translate.default_audio import clear_pending, load_pending, save_pending

    path = tmp_path / "pending.json"
    assert load_pending(path) is None
    save_pending(path, None)
    assert not path.exists()
    save_pending(path, HEADPHONE)
    assert load_pending(path) == HEADPHONE
    clear_pending(path)
    assert load_pending(path) is None
    path.write_text("{broken", encoding="utf-8")
    assert load_pending(path) is None
