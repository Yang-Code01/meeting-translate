# PyInstaller spec：单 exe，内置 ReazonSpeech/SenseVoice/VAD 模型与 Copilot 运行时。
# 构建: uv run pyinstaller meeting-translate.spec --noconfirm
from pathlib import Path

from copilot._cli_download import ensure_runtime_wrapper
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH)
runtime_dir = Path(ensure_runtime_wrapper()).parent  # %LOCALAPPDATA%\github-copilot-sdk\cli\<ver>\prebuilds\win32-x64

datas = [
    (str(ROOT / "models"), "models"),
    # 独立运行时 venv 里的解释器要能直接跑这个脚本（不能 import 包里的代码）
    (str(ROOT / "meeting_translate" / "tts_worker.py"), "worker"),
    (str(runtime_dir), "copilot_runtime"),
    (str(ROOT / "assets"), "assets"),
]
datas += collect_data_files("sounddevice")
binaries = collect_dynamic_libs("sherpa_onnx")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["copilot.generated.rpc", "comtypes.gen", "pycaw"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# onefile 每次启动要先把 400MB 解压到临时目录（约 15 秒），这期间由 bootloader 显示启动画面；
# 主窗口出来后 app.close_splash() 关掉它。
# always_on_top 默认为 True，会一直盖在其他窗口上；关掉后用户切到别的窗口时它能被盖住。
splash = Splash(
    str(ROOT / "assets" / "splash.png"),
    binaries=a.binaries,
    datas=a.datas,
    always_on_top=False,
)
exe = EXE(
    pyz,
    splash,
    splash.binaries,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MeetingTranslate",
    icon=str(ROOT / "assets" / "app.ico"),
    console=False,
    upx=False,
    runtime_tmpdir=None,
)
