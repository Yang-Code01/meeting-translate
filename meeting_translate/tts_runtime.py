"""日语 TTS 独立运行时：用用户机器上的 Python 3.12 建一个只装 TTS 依赖的 venv。

为什么不能直接装进主环境：Style-Bert-VITS2 2.7.0 要 `numpy<2`，而主程序（sherpa-onnx /
PySide6 一侧）跑在 `numpy>=2`，两个约束塞不进同一个解释器。ONNX 推理路径不需要 torch，
所以这个环境只有 onnxruntime（约 70MB）加若干纯 Python 依赖，装完约 400MB。

装好以后 TTS 子进程就是
`%APPDATA%/meeting-translate/runtime/sbv2/Scripts/python.exe worker/tts_worker.py`，
主程序不必 import style_bert_vits2。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

from .config import APP_DIR, resource_dir

log = logging.getLogger(__name__)

#: 运行时要求的最低 / 最高 Python（3.14 上 numba、llvmlite 还没有轮子）
MIN_PYTHON = (3, 12)
MAX_PYTHON = (3, 13)

#: 我们自己构建的 style-bert-vits2 wheel（PyPI 上只有 2.5.0，会拖进 torch）
WHEEL_NAME = "style_bert_vits2-2.7.0-py3-none-any.whl"
WHEEL_URL = "https://github.com/Yang-Code01/meeting-translate/releases/download/tts-runtime-v1/" + WHEEL_NAME
# setuptools<81：pyopenjtalk 还在 import pkg_resources，setuptools 81+ 已经删掉了
PACKAGES = ("onnxruntime", "sherpa-onnx==1.13.8", "setuptools<81")
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
PYTHON_DOWNLOAD_URL = "https://www.python.org/downloads/release/python-31210/"

STAMP_NAME = "runtime.json"
NO_PYTHON_MESSAGE = (
    "没有找到可用的 Python 3.12。\n\n"
    "日语语音合成需要 Python 3.12 才有的科学计算轮子（3.13/3.14 目前装不全）。\n"
    f"请先安装 Python 3.12：{PYTHON_DOWNLOAD_URL}\n"
    "装的时候勾选「Add python.exe to PATH」，装完回到这里重新点「安装运行时」。"
)

_CHECK_CODE = (
    "import importlib.metadata as m\n"
    "import numpy, onnxruntime, sherpa_onnx, style_bert_vits2.tts_model\n"
    "print('numpy', numpy.__version__, '| ort', onnxruntime.__version__,\n"
    "      '| sherpa', m.version('sherpa-onnx'), '| sbv2', m.version('style-bert-vits2'))\n"
)

_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("py", "-3.12"),
    ("python3.12",),
    ("py", "-3.13"),
    ("python3.13",),
    ("python",),
    ("py", "-3"),
)


class TtsRuntimeError(RuntimeError):
    def __init__(self, message: str, code: str = "runtime_error") -> None:
        super().__init__(message)
        self.code = code


def runtime_dir() -> Path:
    return APP_DIR / "runtime" / "sbv2"


def venv_python() -> Path:
    if os.name == "nt":
        return runtime_dir() / "Scripts" / "python.exe"
    return runtime_dir() / "bin" / "python"


def stamp_path() -> Path:
    return runtime_dir() / STAMP_NAME


def worker_script() -> Path | None:
    """独立脚本形式的 TTS sidecar；打包版在 `worker/` 下，源码运行时就在包旁边。"""
    for candidate in (resource_dir() / "worker" / "tts_worker.py", Path(__file__).with_name("tts_worker.py")):
        if candidate.is_file():
            return candidate
    return None


def is_ready() -> bool:
    """装好了并且校验过一次（校验结果落在 runtime.json）。"""
    return venv_python().is_file() and stamp_path().is_file()


def ready_info() -> dict[str, str]:
    try:
        return json.loads(stamp_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def describe() -> str:
    """给界面用的一句话状态。"""
    if not is_ready():
        return "未安装"
    info = ready_info()
    detail = info.get("versions") or ""
    return f"已就绪（Python {info.get('python', '?')}{'，' + detail if detail else ''}）"


def _run(cmd: Sequence[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **kwargs,  # type: ignore[arg-type]
    )


def _python_version(cmd: Sequence[str]) -> tuple[int, int] | None:
    probe = [*cmd, "-c", "import sys;sys.stdout.write('%d.%d' % sys.version_info[:2])"]
    try:
        result = _run(probe, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"(\d+)\.(\d+)", result.stdout or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def find_python() -> list[str] | None:
    """找一个 3.12/3.13 解释器；找不到返回 None（Windows 商店的假 python 会在探测里失败）。"""
    seen: set[str] = set()
    for candidate in _CANDIDATES:
        found = shutil.which(candidate[0])
        if not found or found in seen:
            continue
        seen.add(found)
        version = _python_version(list(candidate))
        if version is not None and MIN_PYTHON <= version < MAX_PYTHON:
            return list(candidate)
    return None


def python_version_text(cmd: Sequence[str] | None = None) -> str:
    version = _python_version(list(cmd)) if cmd else None
    return f"{version[0]}.{version[1]}" if version else "?"


def _tail(result: subprocess.CompletedProcess[str], lines: int = 12) -> str:
    text = (result.stderr or "") + (result.stdout or "")
    chunks = [line for line in text.splitlines() if line.strip()]
    return "\n".join(chunks[-lines:]) or f"退出码 {result.returncode}"


def install(on_log: Callable[[str], None] | None = None, timeout: float = 1800.0) -> dict[str, str]:
    """建 venv、装依赖、校验 import，最后写 runtime.json。失败抛 TtsRuntimeError。"""
    say = on_log or (lambda _text: None)
    python = find_python()
    if python is None:
        raise TtsRuntimeError(NO_PYTHON_MESSAGE, "python_missing")

    target = runtime_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        say("清理旧的运行时目录…")
        shutil.rmtree(target, ignore_errors=True)

    version = python_version_text(python)
    say(f"正在用 {python[0]} {version} 创建独立环境…")
    created = _run([*python, "-m", "venv", str(target)], timeout=600)
    if created.returncode != 0 or not venv_python().is_file():
        raise TtsRuntimeError("创建 Python 环境失败：\n" + _tail(created), "venv_failed")

    say("正在安装依赖（首次约 400MB，网络慢的话要几分钟）…")
    command = [
        str(venv_python()),
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--progress-bar",
        "off",
        "-i",
        PIP_INDEX,
        WHEEL_URL,
        *PACKAGES,
    ]
    tail: list[str] = []
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **kwargs,  # type: ignore[arg-type]
    )
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-20]
        if re.match(r"^(Collecting|Downloading|Installing|Successfully|ERROR|Building)", line):
            say(line[:120])
        if time.monotonic() > deadline:
            proc.kill()
            raise TtsRuntimeError("安装依赖超时（20 分钟）。请检查网络后重试。", "install_timeout")
    if proc.wait() != 0:
        raise TtsRuntimeError("安装依赖失败：\n" + "\n".join(tail[-12:]), "install_failed")

    say("正在校验运行时…")
    check = _run([str(venv_python()), "-c", _CHECK_CODE], timeout=300)
    if check.returncode != 0:
        raise TtsRuntimeError("运行时校验失败：\n" + _tail(check), "check_failed")

    info = {
        "python": version,
        "versions": (check.stdout or "").strip(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wheel": WHEEL_URL,
    }
    stamp_path().write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    say("运行时已就绪")
    return info


def worker_command(models_dir: Path | None = None, device: str = "auto") -> list[str] | None:
    """装好运行时就用独立解释器跑 sidecar；没装返回 None 交给 tts_client 兜底。"""
    if not is_ready():
        return None
    script = worker_script()
    if script is None:
        log.warning("找不到 tts_worker.py，运行时无法启动 sidecar")
        return None
    # -P：不要把脚本目录塞到 sys.path[0]。那个目录里有 meeting_translate/secrets.py，
    # 会把标准库 secrets 顶掉，numpy 一 import 就报 `cannot import name randbits`。
    command = [str(venv_python()), "-P", str(script), "--device", device]
    if models_dir is not None:
        command += ["--models-dir", str(models_dir)]
    return command
