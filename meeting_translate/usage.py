"""用量账本：本场会议的消耗累计。

两种后端记两种量：Copilot 记 nanoAIC（1 AIC = 1e9 nanoAIC），
OpenAI 兼容端点记 token（每次请求的增量累加）。两者互不换算。
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class Usage:
    def __init__(self, on_change: Callable[[float], None] | None = None) -> None:
        self._by_session: dict[str, int] = {}
        self._tokens_in: dict[str, int] = {}
        self._tokens_out: dict[str, int] = {}
        self._lock = threading.Lock()
        self._on_change = on_change

    # ---- Copilot：nanoAIC ----

    def update(self, session_id: str, nano: int) -> None:
        """会话内是累计值，取最大。"""
        with self._lock:
            if nano <= self._by_session.get(session_id, 0):
                return
            self._by_session[session_id] = nano
            total = self.aic_unlocked()
        if self._on_change:
            self._on_change(total)

    def aic_unlocked(self) -> float:
        return sum(self._by_session.values()) / 1e9

    @property
    def aic(self) -> float:
        with self._lock:
            return self.aic_unlocked()

    # ---- OpenAI 兼容端点：token ----

    def add_tokens(self, session_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        """每次请求的增量，直接累加。"""
        with self._lock:
            self._tokens_in[session_id] = self._tokens_in.get(session_id, 0) + int(prompt_tokens or 0)
            self._tokens_out[session_id] = self._tokens_out.get(session_id, 0) + int(completion_tokens or 0)

    @property
    def tokens(self) -> tuple[int, int]:
        """(输入 token, 输出 token)"""
        with self._lock:
            return sum(self._tokens_in.values()), sum(self._tokens_out.values())
