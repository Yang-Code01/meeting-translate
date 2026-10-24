# PyInstaller spec：单 exe，内置 ReazonSpeech/SenseVoice/VAD 模型与 Copilot 运行时。
# 构建: uv run pyinstaller meeting-translate.spec --noconfirm
from pathlib import Path

from copilot._cli_download import ensure_runtime_wrapper
from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = Path(SPECPATH)
runtime_dir = Path(ensure_runtime_wrapper()).parent  # %LOCALAPPDATA%\github-copilot-sdk\cli\<ver>\prebuilds\win32-x64

datas = [
    (str(ROOT / "models"), "models"),
    (str(runtime_dir), "copilot_runtime"),
]
binaries = collect_dynamic_libs("sherpa_onnx")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["copilot.generated.rpc"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MeetingTranslate",
    console=False,
    upx=False,
    runtime_tmpdir=None,
)
