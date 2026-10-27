"""语音识别：可插拔引擎 + 每个音源一个 VAD 切句器 + 单线程识别工作者。"""

from __future__ import annotations

import difflib
import itertools
import logging
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import sherpa_onnx

from .audio import ME, SAMPLE_RATE, THEM
from .config import APP_DIR, REAZON_DIR_NAME, SILERO_VAD_NAME, find_model

log = logging.getLogger(__name__)

PARTIAL_INTERVAL = 0.5
PRE_ROLL_SECONDS = 0.3
ECHO_WINDOW_SECONDS = 4.0
ECHO_SIMILARITY = 0.75


class ModelMissing(Exception):
    """所需模型不存在（Whisper 需要先下载）。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class AsrEngine(Protocol):
    partial_interval: float  # 部分识别的最小间隔（秒）
    language: str  # 引擎配置的识别语言；auto 表示由模型自动判断

    def decode(self, samples: np.ndarray) -> str: ...

    def decode_result(self, samples: np.ndarray) -> tuple[str, str]: ...


class _OfflineEngine:
    def __init__(self, rec: sherpa_onnx.OfflineRecognizer, post: Callable[[str], str] | None = None,
                 partial_interval: float = PARTIAL_INTERVAL, language: str = "ja") -> None:
        self._rec = rec
        self._post = post
        self.partial_interval = partial_interval
        self.language = language

    def decode(self, samples: np.ndarray) -> str:
        return self.decode_result(samples)[0]

    def decode_result(self, samples: np.ndarray) -> tuple[str, str]:
        s = self._rec.create_stream()
        s.accept_waveform(SAMPLE_RATE, samples)
        self._rec.decode_stream(s)
        text = s.result.text.strip()
        if self._post:
            text = self._post(text)
        raw = getattr(s.result, "lang", "") or ""
        return text, normalize_language(raw, self.language)


def normalize_language(value: str | None, default: str = "ja") -> str:
    """SenseVoice 的 lang 可能是 `<|zh|>`；统一成 zh/ja/en，未知时用默认值。"""
    v = (value or "").strip().strip("<|>").lower()
    if v.startswith("zh"):
        return "zh"
    if v.startswith("ja"):
        return "ja"
    if v.startswith("en"):
        return "en"
    return "" if default == "auto" else default


_CJK_SPACE = re.compile(r"\s+(?=[^\x00-\x7f])|(?<=[^\x00-\x7f])\s+")


def strip_cjk_spaces(text: str) -> str:
    """SenseVoice 的日语输出按词加空格，去掉与非 ASCII 字符相邻的空格，保留英文词间空格。"""
    return _CJK_SPACE.sub("", text)


def _reazon(hotwords: list[str], threads: int) -> AsrEngine:
    d = find_model(REAZON_DIR_NAME)
    if d is None:
        raise ModelMissing(REAZON_DIR_NAME)
    kw: dict = dict(
        encoder=str(d / "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=str(d / "decoder-epoch-99-avg-1.int8.onnx"),
        joiner=str(d / "joiner-epoch-99-avg-1.int8.onnx"),
        tokens=str(d / "tokens.txt"),
        num_threads=threads,
    )
    if hotwords:
        hw = APP_DIR / "hotwords.txt"
        hw.write_text("\n".join(hotwords) + "\n", encoding="utf-8")
        kw.update(
            decoding_method="modified_beam_search",
            hotwords_file=str(hw),
            hotwords_score=2.0,
            modeling_unit="cjkchar",
        )
    return _OfflineEngine(sherpa_onnx.OfflineRecognizer.from_transducer(**kw), language="ja")


def whisper_dir_name(size: str) -> str:
    return f"whisper-{size}"


def whisper_files(size: str) -> list[str]:
    return [f"{size}-encoder.int8.onnx", f"{size}-decoder.int8.onnx", f"{size}-tokens.txt"]


SENSEVOICE_DIR_NAME = "sensevoice"
SENSEVOICE_FILES = ["model.int8.onnx", "tokens.txt"]


def downloadable(name: str) -> tuple[str, str, list[str]] | None:
    """可下载模型：(hf 仓库, 本地目录名, 文件列表)；内置模型返回 None。"""
    from .config import SENSEVOICE_REPO, WHISPER_REPO

    if name.startswith("whisper-"):
        size = name.removeprefix("whisper-")
        return WHISPER_REPO.format(size=size), whisper_dir_name(size), whisper_files(size)
    if name == "sensevoice":
        return SENSEVOICE_REPO, SENSEVOICE_DIR_NAME, SENSEVOICE_FILES
    return None


def _complete_dir(dir_name: str, files: list[str]) -> Path | None:
    d = find_model(dir_name)
    return d if d is not None and all((d / f).exists() for f in files) else None


def _sensevoice(threads: int, language: str) -> AsrEngine:
    d = _complete_dir(SENSEVOICE_DIR_NAME, SENSEVOICE_FILES)
    if d is None:
        raise ModelMissing("sensevoice")
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(d / "model.int8.onnx"), tokens=str(d / "tokens.txt"), num_threads=threads, language=language, use_itn=True
    )
    return _OfflineEngine(rec, strip_cjk_spaces, language=language)


def _whisper(size: str, threads: int) -> AsrEngine:
    d = _complete_dir(whisper_dir_name(size), whisper_files(size))
    if d is None:
        raise ModelMissing(f"whisper-{size}")
    enc, dec, tok = (str(d / f) for f in whisper_files(size))
    rec = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=enc, decoder=dec, tokens=tok, language="ja", task="transcribe", num_threads=threads
    )
    return _OfflineEngine(rec, language="ja")


def create_engine(name: str, hotwords: list[str], threads: int, language: str = "ja") -> AsrEngine:
    """language 为识别语言（ja | zh），目前只有 SenseVoice 区分；ReazonSpeech/Whisper 固定日语。"""
    if name == "reazon":
        return _reazon(hotwords, threads)
    if name.startswith("whisper-"):
        return _whisper(name.removeprefix("whisper-"), threads)
    if name == "sensevoice":
        return _sensevoice(threads, language)
    raise ValueError(f"未知的 ASR 引擎: {name}")


@dataclass
class Final:
    id: int
    source: str
    ja: str
    wall_time: float
    mono_time: float
    lang: str = ""  # zh | ja | en；空 = 未知
    direction: str | None = None  # ja-zh | zh-ja；双向会议时逐条携带
    audio: np.ndarray | None = None  # 16k 单声道，供同语言直通/调试


class _Segmenter:
    """单个音源：VAD 判定句子边界，说话期间定期输出部分结果。"""

    def __init__(self, source: str, max_seconds: float, silence_seconds: float,
                 partial_interval: float = PARTIAL_INTERVAL) -> None:
        cfg = sherpa_onnx.VadModelConfig()
        vad_path = find_model(SILERO_VAD_NAME)
        if vad_path is None:
            raise ModelMissing(SILERO_VAD_NAME)
        cfg.silero_vad.model = str(vad_path)
        cfg.silero_vad.min_silence_duration = silence_seconds
        cfg.silero_vad.min_speech_duration = 0.2
        cfg.silero_vad.max_speech_duration = max_seconds
        cfg.sample_rate = SAMPLE_RATE
        self.source = source
        self._partial_interval = partial_interval
        self._vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
        self._pre_roll: deque[np.ndarray] = deque(maxlen=int(PRE_ROLL_SECONDS * SAMPLE_RATE / 1600) + 1)
        self._buf: list[np.ndarray] = []
        self._last_partial = 0.0
        self._speaking = False

    def feed(self, samples: np.ndarray) -> tuple[np.ndarray | None, list[np.ndarray], bool]:
        """返回 (需要部分识别的音频, 已定稿的句子音频列表, 本次是否刚开始说话)。"""
        self._vad.accept_waveform(samples)
        finals: list[np.ndarray] = []
        while not self._vad.empty():
            finals.append(np.asarray(self._vad.front.samples, dtype=np.float32))
            self._vad.pop()
        if finals:
            self._buf.clear()

        speech = self._vad.is_speech_detected()
        started = speech and not self._speaking
        self._speaking = speech
        partial = None
        if speech:
            if not self._buf:
                self._buf.extend(self._pre_roll)
            self._buf.append(samples)
            now = time.monotonic()
            if now - self._last_partial >= self._partial_interval and len(self._buf) >= 3:
                self._last_partial = now
                partial = np.concatenate(self._buf)
        else:
            self._buf.clear()
        self._pre_roll.append(samples)
        return partial, finals, started

    def flush(self) -> list[np.ndarray]:
        self._speaking = False
        self._vad.flush()
        out = []
        while not self._vad.empty():
            out.append(np.asarray(self._vad.front.samples, dtype=np.float32))
            self._vad.pop()
        self._buf.clear()
        return out


_PUNCT = re.compile(r"[\s、。，,.？?！!「」『』・ー：:；;]")


def _norm(text: str) -> str:
    return _PUNCT.sub("", text)


class AsrWorker:
    """单线程识别两路音频，避免 CPU 争用；定稿时对麦克风回声做文本去重。"""

    def __init__(
        self,
        engine: AsrEngine,
        sources: list[str],
        max_seconds: float,
        on_partial: Callable[[str, str], None],
        on_final: Callable[[Final], None],
        silence_seconds: float = 0.6,
        on_speech_start: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine
        self._segs = {s: _Segmenter(s, max_seconds, silence_seconds, engine.partial_interval) for s in sources}
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_speech_start = on_speech_start
        self._q: queue.Queue[tuple[str, np.ndarray | None] | None] = queue.Queue()
        self._recent: deque[tuple[str, str, float]] = deque(maxlen=20)
        self._ids = itertools.count(1)
        self._thread = threading.Thread(target=self._run, name="asr", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def push(self, source: str, samples: np.ndarray) -> None:
        self._q.put((source, samples))

    def flush_source(self, source: str) -> None:
        """把某个音源正在说的半句定稿（用于会议中关闭麦克风）。"""
        self._q.put((source, None))

    def stop(self) -> None:
        """停止并把正在说的句子定稿。"""
        self._q.put(None)
        self._thread.join(timeout=10)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            source, samples = item
            if samples is None:
                try:
                    for audio in self._segs[source].flush():
                        self._emit_final(source, audio)
                    self._on_partial(source, "")
                except Exception:
                    log.exception("识别失败")
                continue
            # 积压时跳过部分识别，优先保证定稿
            backlog = self._q.qsize() > 5
            try:
                partial, finals, started = self._segs[source].feed(samples)
                if started and self._on_speech_start is not None:
                    self._on_speech_start(source)
                for seg in finals:
                    self._emit_final(source, seg)
                if partial is not None and not backlog:
                    text = self._engine.decode(partial)
                    if text:
                        self._on_partial(source, text)
            except Exception:
                log.exception("识别失败")
        for source, seg in self._segs.items():
            for audio in seg.flush():
                self._emit_final(source, audio)

    def _emit_final(self, source: str, audio: np.ndarray) -> None:
        decode_result = getattr(self._engine, "decode_result", None)
        if decode_result is not None:
            text, lang = decode_result(audio)
        else:  # 兼容只实现 decode 的旧引擎/测试替身
            text = self._engine.decode(audio)
            lang = getattr(self._engine, "language", "")
        self._on_partial(source, "")
        if not text:
            return
        now = time.monotonic()
        if source == ME and self._is_echo(text, now):
            log.info("丢弃疑似回声: %s", text)
            return
        self._recent.append((source, _norm(text), now))
        self._on_final(Final(next(self._ids), source, text, time.time(), now, lang=lang, audio=audio))

    def _is_echo(self, text: str, now: float) -> bool:
        n = _norm(text)
        for src, other, t in reversed(self._recent):
            if now - t > ECHO_WINDOW_SECONDS:
                break
            if src == THEM and difflib.SequenceMatcher(None, n, other).ratio() >= ECHO_SIMILARITY:
                return True
        return False


def model_ready(name: str) -> bool:
    try:
        if name == "reazon":
            return find_model(REAZON_DIR_NAME) is not None
        spec = downloadable(name)
        return spec is not None and _complete_dir(spec[1], spec[2]) is not None
    except Exception:
        return False


def model_target_dir(name: str) -> Path:
    from .config import MODEL_DIR

    spec = downloadable(name)
    assert spec is not None
    return MODEL_DIR / spec[1]
