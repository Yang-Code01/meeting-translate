"""Windows 默认播放设备的读取/切换/恢复。

只处理 eRender（播放）方向。双向同传开始时把系统默认输出切到会议软件
实际写入的虚拟扬声器（CABLE/Hi-Fi），结束后恢复原来的耳机/扬声器。
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from typing import Callable

from pycaw.constants import EDataFlow, ERole
from pycaw.pycaw import AudioUtilities

log = logging.getLogger(__name__)

RENDER_ID_PREFIX = "{0.0.0."
DEFAULT_ROLES = (ERole.eConsole, ERole.eMultimedia, ERole.eCommunications)


class AudioSwitchError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutputDevice:
    id: str
    name: str


@dataclass(frozen=True)
class SwitchResult:
    changed: bool
    previous: OutputDevice | None
    target: OutputDevice


def _device(ptr) -> OutputDevice:
    dev = AudioUtilities.CreateDevice(ptr)
    return OutputDevice(str(dev.id), str(dev.FriendlyName))


def active_outputs() -> list[OutputDevice]:
    """当前可用的播放端点（按名称排序）。"""
    result: list[OutputDevice] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        devices = AudioUtilities.GetAllDevices()
    for dev in devices:
        try:
            if not str(dev.id).startswith(RENDER_ID_PREFIX):
                continue
            if dev.state.name != "Active":
                continue
            result.append(OutputDevice(str(dev.id), str(dev.FriendlyName)))
        except Exception:
            log.debug("跳过无法读取的音频端点", exc_info=True)
    return sorted(result, key=lambda d: d.name.casefold())


def _default_for(role: ERole) -> OutputDevice | None:
    try:
        enum = AudioUtilities.GetDeviceEnumerator()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ptr = enum.GetDefaultAudioEndpoint(EDataFlow.eRender.value, role.value)
            return _device(ptr)
    except Exception:
        log.debug("读取默认播放设备失败：%s", role.name, exc_info=True)
        return None


def snapshot_defaults() -> dict[ERole, OutputDevice | None]:
    return {role: _default_for(role) for role in DEFAULT_ROLES}


def set_defaults(device_id: str, roles: tuple[ERole, ...] = DEFAULT_ROLES) -> None:
    AudioUtilities.SetDefaultDevice(device_id, list(roles))


def find_output(name: str, devices: list[OutputDevice] | None = None) -> OutputDevice | None:
    """按完整名称/关键字找播放端点；优先精确匹配，避免 Input/Output 选反。"""
    items = active_outputs() if devices is None else list(devices)
    wanted = name.strip().casefold()
    if not wanted:
        return None
    for dev in items:
        if dev.name.casefold() == wanted:
            return dev
    # 用户手选/旧驱动可能只给简称；允许包含匹配，但只匹配播放端点。
    for dev in items:
        if wanted in dev.name.casefold() or dev.name.casefold() in wanted:
            return dev
    return None


def _label(dev: OutputDevice | None) -> str:
    return dev.name if dev else "未知设备"


class DefaultOutputSwitch:
    """切换默认输出，并保存原设备用于恢复。"""

    def __init__(
        self,
        snapshot_fn: Callable[[], dict[ERole, OutputDevice | None]] = snapshot_defaults,
        set_fn: Callable[..., None] = set_defaults,
        find_fn: Callable[[str], OutputDevice | None] = find_output,
    ) -> None:
        self._snapshot_fn = snapshot_fn
        self._set_fn = set_fn
        self._find_fn = find_fn
        self._previous: dict[ERole, OutputDevice | None] | None = None
        self._target: OutputDevice | None = None

    @property
    def active(self) -> bool:
        return self._previous is not None

    @property
    def previous(self) -> OutputDevice | None:
        if not self._previous:
            return None
        for role in (ERole.eMultimedia, ERole.eConsole, ERole.eCommunications):
            dev = self._previous.get(role)
            if dev is not None:
                return dev
        return None

    def switch_to(self, name: str) -> SwitchResult:
        target = self._find_fn(name)
        if target is None:
            raise AudioSwitchError(f"找不到播放设备：{name}")
        previous = self._snapshot_fn()
        prev = previous.get(ERole.eMultimedia) or previous.get(ERole.eConsole)
        if all(dev is not None and dev.id == target.id for dev in previous.values()):
            return SwitchResult(False, prev, target)
        # 已切换过就不要覆盖最初的原设备。
        if self._previous is None:
            self._previous = previous
        self._set_fn(target.id)
        self._target = target
        log.info("默认输出切换：%s -> %s", _label(prev), target.name)
        return SwitchResult(True, prev, target)

    def restore(self) -> OutputDevice | None:
        previous = self._previous
        self._previous = None
        self._target = None
        if not previous:
            return None
        first = None
        for role, dev in previous.items():
            if dev is None:
                continue
            try:
                self._set_fn(dev.id, (role,))
                if first is None:
                    first = dev
            except Exception:
                log.exception("恢复默认播放设备失败：%s", dev.name)
        if first is not None:
            log.info("默认输出恢复：%s", first.name)
        return first
