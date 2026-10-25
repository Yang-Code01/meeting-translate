"""Windows COM 线程模式的初始化顺序。

Qt 的窗口（尤其原生文件对话框）要求 GUI 线程是 STA。soundcard 在导入时
（mediafoundation._COMLibrary）会把当前线程设成 MTA，之后再让 Qt 初始化
OLE 就会失败（0x80010106），打开「选择会议记录」这类原生对话框时界面会卡死。
所以必须在导入 soundcard 之前，先把 GUI 线程占成 STA。
"""

from __future__ import annotations

import ctypes
import sys

_COINIT_APARTMENTTHREADED = 0x2
# APTTYPE_STA = 0，APTTYPE_MAINSTA = 3；MTA 是 1。
_STA_APARTMENTS = (0, 3)

_initialized = False
_sta = False


def _apartment_type() -> int:
    apt = ctypes.c_int()
    qual = ctypes.c_int()
    ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(apt), ctypes.byref(qual))
    return apt.value


def init_sta() -> bool:
    """把当前线程初始化成 STA，返回它现在是不是单线程套间。"""
    global _initialized, _sta
    if _initialized:
        return _sta
    if sys.platform != "win32":
        return False
    _initialized = True
    try:
        ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        _sta = _apartment_type() in _STA_APARTMENTS
    except OSError:  # pragma: no cover - ole32 在 Windows 上总是存在
        _sta = False
    return _sta


def gui_thread_is_sta() -> bool:
    return _sta
