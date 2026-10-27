"""双向同传的语音输出：把译文按方向送到对应 TTS 引擎和播放设备。"""

from __future__ import annotations

import logging
import queue
import re
import threading
from typing import Callable

from . import lang
from .duplex_audio import Player
from .tts_client import TtsAudio, TtsClient

log = logging.getLogger(__name__)

# 方向 -> (sidecar 引擎, 播放目标)
_ROUTE = {
    lang.JA_ZH: ("melo", "local"),   # 对方日语 -> 中文 TTS -> 我的耳机
    lang.ZH_JA: ("sbv2", "remote"),  # 我中文 -> 日语 TTS -> CABLE-B
}


class TtsSpeaker:
    """单线程串行合成，避免多个请求打乱对话顺序。

    每个方向有 generation：stop(direction) 后，正在合成的旧结果丢弃，队列里
    同方向的旧块也清掉；另一个方向不受影响。

    队列不丢句：译文比播放快时先排队，保证每句都完整播完，宁可延迟增加也
    不跳过句子。真正需要打断时由 stop() 清队列。
    """

    def __init__(
        self,
        client: TtsClient,
        local: Player,
        remote: Player,
        on_error: Callable[[Exception], None] | None = None,
        on_event: Callable[[str, str, str], None] | None = None,
        timeout: float = 120.0,
        max_queue: int = 2,
        max_chars: int = 120,
        volume: float = 0.8,
    ) -> None:
        self.client = client
        self.local = local
        self.remote = remote
        self._on_error = on_error
        self._on_event = on_event
        self._timeout = timeout
        self._max_chars = max(10, max_chars)
        self._volume = max(0.0, min(1.0, float(volume)))
        self._queue: queue.Queue[tuple[str, str, str, str, int] | None] = queue.Queue()
        self._generation = {lang.JA_ZH: 0, lang.ZH_JA: 0}
        self._thread: threading.Thread | None = None
        self._closed = False
        self._last: dict[str, TtsAudio] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="duplex-tts", daemon=True)
        self._thread.start()

    def submit(self, text: str, direction: str) -> None:
        if self._closed or not text or direction not in _ROUTE:
            return
        if text.startswith("（翻译失败）"):
            return
        engine, target = _ROUTE[direction]
        self.start()
        generation = self._generation[direction]
        for chunk in self._chunk_text(text):
            self._put((chunk, engine, target, direction, generation))

    def _put(self, item: tuple[str, str, str, str, int]) -> None:
        # 不设上限、不丢最旧：跳句比多等几秒更糟。
        self._queue.put_nowait(item)

    def _chunk_text(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= self._max_chars:
            return [text]
        # 先按句末切；只有单句过长时才按逗号/顿号切，避免把正常一句话
        # 拆成多个独立合成导致韵律重置、块间空隙和“电音/卡顿”听感。
        sentences = [p for p in re.split(r"(?<=[。！？!?])", text) if p]
        chunks: list[str] = []
        buf = ""

        def add(part: str) -> None:
            nonlocal buf
            while len(part) > self._max_chars:
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.append(part[: self._max_chars])
                part = part[self._max_chars :]
            if len(buf) + len(part) <= self._max_chars:
                buf += part
            else:
                if buf:
                    chunks.append(buf)
                buf = part

        for sentence in sentences:
            if len(sentence) <= self._max_chars:
                add(sentence)
                continue
            for clause in re.split(r"(?<=[，,、；;])", sentence):
                if clause:
                    add(clause)
        if buf:
            chunks.append(buf)
        return chunks or [text]

    def stop(self, direction: str | None = None) -> None:
        """停一个方向或全部；当前合成结果会在返回时按 generation 丢弃。"""
        directions = [direction] if direction else list(self._generation)
        for d in directions:
            if d not in self._generation:
                continue
            self._generation[d] += 1
            player = self.local if d == lang.JA_ZH else self.remote
            player.stop()
        # 清掉队列里被停方向的旧块；保留另一个方向
        keep: list[tuple[str, str, str, str, int] | None] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            if direction is None or item[3] == direction:
                continue
            keep.append(item)
        for item in keep:
            self._put(item)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        self._queue.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def last_audio(self, direction: str) -> TtsAudio | None:
        """最近一次成功合成的音频；供「试听日语」在本机耳机回放。"""
        return self._last.get(direction)

    def _player(self, target: str) -> Player:
        return self.local if target == "local" else self.remote

    def _emit(self, direction: str, text: str, state: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(direction, text, state)
        except Exception:
            log.exception("TTS 播放状态回调失败")

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            text, engine, target, direction, generation = item
            try:
                audio = self.client.synth(
                    text, engine=engine, volume=self._volume, timeout=self._timeout
                )
            except Exception as e:  # 单句失败不影响后续
                log.exception("TTS 合成失败")
                if self._on_error is not None:
                    self._on_error(e)
                continue
            if generation != self._generation.get(direction, -1):
                continue
            self._last[direction] = audio
            self._player(target).enqueue(
                audio.samples,
                audio.sample_rate,
                on_start=lambda: self._emit(direction, text, "start"),
                on_done=lambda: self._emit(direction, text, "done"),
            )
