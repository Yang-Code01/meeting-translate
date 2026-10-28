"""日语 TTS 独立运行时：用 Python 3.12 建一个只装 TTS 依赖的 venv。

优先用 uv：PATH 上的 uv，或者下载到 `runtime/uv/uv.exe` 的那份。uv 会自动下载一份独立的
Python 3.12（放在 `runtime/python`），用户机器上不需要预先装 Python。没有 uv 时退回
系统 Python 3.12 + pip；两者都没有时抛 `python_missing`，界面上再给「自动下载 uv」的入口。

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
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Sequence

from .config import APP_DIR, resource_dir

log = logging.getLogger(__name__)

#: 运行时接受的 Python 区间 [MIN, MAX)：numpy<2 与 pyopenjtalk-dict 都只到 3.12
MIN_PYTHON = (3, 12)
MAX_PYTHON = (3, 13)
UV_PYTHON_REQUEST = "3.12"
#: uv.exe 来源：先从 PyPI 镜像取 uv 的 wheel（国内快），不行再去 GitHub
UV_GITHUB_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
#: uv 下载独立 Python 用的镜像（官方源在国内很慢），失败会不带镜像再试一次
UV_PYTHON_MIRROR = "https://registry.npmmirror.com/-/binary/python-build-standalone"

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
    "点「自动下载并安装」：程序会下载 uv（约 15MB），由它自动下载独立的 Python 3.12 并装好运行时，"
    "不影响系统里的 Python。\n"
    f"也可以自己安装 Python 3.12（{PYTHON_DOWNLOAD_URL}，勾选「Add python.exe to PATH」）后在下载列表里重新下载「语音运行时」。"
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


def bundled_uv() -> Path:
    return APP_DIR / "runtime" / "uv" / ("uv.exe" if os.name == "nt" else "uv")


def uv_python_dir() -> Path:
    """uv 下载的 Python 放在这里，venv 依赖它，不能删。"""
    return APP_DIR / "runtime" / "python"


def find_uv() -> str | None:
    """PATH 上的 uv 优先，其次是我们下载过的那份。"""
    found = shutil.which("uv")
    if found:
        return found
    local = bundled_uv()
    return str(local) if local.is_file() else None


def _pypi_uv_wheel_url(index: str = PIP_INDEX) -> str:
    """从 PyPI simple 页面挑最新的 uv Windows wheel（wheel 里就是 uv.exe）。"""
    page_url = index.rstrip("/") + "/uv/"
    request = urllib.request.Request(page_url, headers={"User-Agent": "meeting-translate"})
    with urllib.request.urlopen(request, timeout=30) as response:
        page = response.read().decode("utf-8", "replace")
    found = re.findall(r'href="([^"#]*/uv-(\d+)\.(\d+)\.(\d+)-py3-none-win_amd64\.whl)', page)
    if not found:
        raise OSError("镜像上没有 uv 的 Windows wheel")
    href = max(found, key=lambda m: (int(m[1]), int(m[2]), int(m[3])))[0]
    return urllib.parse.urljoin(page_url, href)


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise TtsRuntimeError("已暂停", "cancelled")


def _kill_on_cancel(proc: subprocess.Popen, cancel: threading.Event | None) -> threading.Event:
    """后台盯着 cancel：一旦置位就结束子进程。返回的事件在进程结束后由调用方置位，让盯梢线程退出。"""
    finished = threading.Event()
    if cancel is not None:

        def watch() -> None:
            while not finished.wait(0.3):
                if cancel.is_set():
                    proc.kill()
                    return

        threading.Thread(target=watch, daemon=True).start()
    return finished


def _fetch(
    url: str, dest: Path, label: str, say: Callable[[str], None], cancel: threading.Event | None = None
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "meeting-translate"})
    with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:
        total = int(response.headers.get("Content-Length", "0") or 0)
        done = 0
        last = 0.0
        while True:
            _check_cancel(cancel)
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if total and now - last > 0.5:
                last = now
                say(f"{label} {done * 100 // total}%")


def download_uv(on_log: Callable[[str], None] | None = None, cancel: threading.Event | None = None) -> Path:
    """下载 uv.exe 到 runtime/uv：先 PyPI 镜像，再 GitHub。都失败抛 TtsRuntimeError。"""
    say = on_log or (lambda _text: None)
    target = bundled_uv()
    target.parent.mkdir(parents=True, exist_ok=True)
    archive = target.parent / "uv-download.zip"
    errors: list[str] = []
    try:
        for source in ("pypi", "github"):
            try:
                url = _pypi_uv_wheel_url() if source == "pypi" else UV_GITHUB_URL
                _fetch(url, archive, "正在下载 uv…", say, cancel)
                with zipfile.ZipFile(archive) as zf:
                    member = next((n for n in zf.namelist() if Path(n).name == target.name), None)
                    if member is None:
                        raise OSError("压缩包里没有 " + target.name)
                    target.write_bytes(zf.read(member))
                return target
            except (OSError, zipfile.BadZipFile) as ex:
                _check_cancel(cancel)
                log.warning("从 %s 下载 uv 失败：%s", source, ex)
                errors.append(f"{source}: {ex}")
    finally:
        archive.unlink(missing_ok=True)
    raise TtsRuntimeError("下载 uv 失败：\n" + "\n".join(errors) + "\n请检查网络后重试。", "uv_download_failed")


def _uv_env(mirror: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    env["UV_PYTHON_INSTALL_DIR"] = str(uv_python_dir())
    env["UV_PYTHON_DOWNLOADS"] = "automatic"
    if mirror:
        env.setdefault("UV_PYTHON_INSTALL_MIRROR", UV_PYTHON_MIRROR)
    env.pop("VIRTUAL_ENV", None)
    return env


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


def _run(
    cmd: Sequence[str],
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    cancel: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if cancel is None:
        return subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            **kwargs,  # type: ignore[arg-type]
        )
    with subprocess.Popen(
        list(cmd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **kwargs,  # type: ignore[arg-type]
    ) as proc:
        finished = _kill_on_cancel(proc, cancel)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        finally:
            finished.set()
    _check_cancel(cancel)
    return subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)


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


def install(
    on_log: Callable[[str], None] | None = None,
    timeout: float = 1800.0,
    download_uv_if_missing: bool = False,
    cancel: threading.Event | None = None,
) -> dict[str, str]:
    """建 venv、装依赖、校验 import，最后写 runtime.json。失败抛 TtsRuntimeError。

    有 uv 就用 uv（自动下载 Python 3.12）；否则用系统 Python 3.12 + pip；
    都没有时，`download_uv_if_missing=True` 会先下载 uv，否则抛 `python_missing`。
    cancel 置位时尽快停下（结束子进程）并抛 code=`cancelled`；再装会从头来。
    """
    say = on_log or (lambda _text: None)
    uv = find_uv()
    python = None if uv else find_python()
    if uv is None and python is None:
        if not download_uv_if_missing:
            raise TtsRuntimeError(NO_PYTHON_MESSAGE, "python_missing")
        uv = str(download_uv(say, cancel))

    target = runtime_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        say("清理旧的运行时目录…")
        shutil.rmtree(target, ignore_errors=True)

    env: dict[str, str] | None = None
    if uv:
        env = _uv_env()
        say(f"正在用 uv 准备 Python {UV_PYTHON_REQUEST}（没有的话会自动下载，约 20MB）…")
        venv_cmd = [uv, "venv", "--python", UV_PYTHON_REQUEST, "--no-project", str(target)]
        created = _run(venv_cmd, timeout=900, env=env, cancel=cancel)
        if created.returncode != 0 and "UV_PYTHON_INSTALL_MIRROR" in env:
            log.warning("经镜像准备 Python 失败，改用官方源：%s", _tail(created))
            say("镜像不可用，改用官方源下载 Python…")
            env = _uv_env(mirror=False)
            created = _run(venv_cmd, timeout=900, env=env, cancel=cancel)
    else:
        assert python is not None
        say(f"正在用 {python[0]} {python_version_text(python)} 创建独立环境…")
        created = _run([*python, "-m", "venv", str(target)], timeout=600, cancel=cancel)
    if created.returncode != 0 or not venv_python().is_file():
        raise TtsRuntimeError("创建 Python 环境失败：\n" + _tail(created), "venv_failed")
    version = python_version_text([str(venv_python())])

    say("正在安装依赖（首次约 400MB，网络慢的话要几分钟）…")
    if uv:
        command = [uv, "pip", "install", "--python", str(venv_python()), "--no-cache", "-i", PIP_INDEX]
    else:
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
        ]
    command += [WHEEL_URL, *PACKAGES]
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
        env=env,
        **kwargs,  # type: ignore[arg-type]
    )
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    finished = _kill_on_cancel(proc, cancel)
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-20]
            if re.match(r"^\s*(Collecting|Downloading|Downloaded|Installing|Installed|Successfully|ERROR|error|Building|Resolved|Prepared)", line):
                say(line.strip()[:120])
            if time.monotonic() > deadline:
                proc.kill()
                raise TtsRuntimeError("安装依赖超时（30 分钟）。请检查网络后重试。", "install_timeout")
        code = proc.wait()
    finally:
        finished.set()
    _check_cancel(cancel)
    if code != 0:
        raise TtsRuntimeError("安装依赖失败：\n" + "\n".join(tail[-12:]), "install_failed")

    say("正在校验运行时…")
    check = _run([str(venv_python()), "-c", _CHECK_CODE], timeout=300, cancel=cancel)
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
