"""COM 初始化顺序的回归测试（Windows 专属）。

soundcard 导入时会把线程设成 MTA；一旦发生在 Qt 之前，原生文件对话框会卡死。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_PROBE = textwrap.dedent(
    """
    import ctypes
    import meeting_translate.audio  # noqa: F401

    apt = ctypes.c_int()
    qual = ctypes.c_int()
    ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(apt), ctypes.byref(qual))
    print(apt.value)
    """
)

APTTYPE_MAINSTA = 3


@pytest.mark.skipif(sys.platform != "win32", reason="COM 只在 Windows 上存在")
def test_audio_import_keeps_the_gui_thread_in_sta():
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == str(APTTYPE_MAINSTA)


_MTA_PROBE = textwrap.dedent(
    """
    import ctypes
    ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # 先占成 MTA，模拟第三方库

    import meeting_translate.qtcom as qtcom

    print(qtcom.init_sta())
    print(qtcom.gui_thread_is_sta())
    """
)


@pytest.mark.skipif(sys.platform != "win32", reason="COM 只在 Windows 上存在")
def test_init_sta_reports_false_when_com_is_already_mta():
    out = subprocess.run(
        [sys.executable, "-c", _MTA_PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "False"]
