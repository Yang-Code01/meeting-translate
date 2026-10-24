"""全局快捷键：Win32 RegisterHotKey，在独立线程的消息循环中监听。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}


def parse(spec: str) -> tuple[int, int]:
    """'Ctrl+Alt+J' → (modifiers, virtual key)。"""
    mods, vk = MOD_NOREPEAT, 0
    for part in (p.strip().lower() for p in spec.split("+")):
        if part in _MODS:
            mods |= _MODS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"无法识别的按键: {part}")
    if not vk:
        raise ValueError("快捷键缺少主键")
    return mods, vk


class GlobalHotkey:
    def __init__(self, spec: str, callback: Callable[[], None]) -> None:
        self._mods, self._vk = parse(spec)
        self._spec = spec
        self._callback = callback
        self._tid = 0
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self.error: str | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(2)

    def stop(self) -> None:
        if self._tid:
            _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(timeout=2)

    def _run(self) -> None:
        self._tid = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, 1, self._mods, self._vk):
            self.error = f"快捷键 {self._spec} 已被其他程序占用"
            log.warning(self.error)
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self._callback()
        finally:
            _user32.UnregisterHotKey(None, 1)
