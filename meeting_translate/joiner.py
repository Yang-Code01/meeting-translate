"""断句合并：把 VAD 切出的片段按句末判定（日语/中文）合并成完整的句子，再送翻译。

纯逻辑，不依赖 Qt；时间由调用方传入，便于测试。
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass, field

# 以这些结尾视为没说完（助词、接续形）
INCOMPLETE_ENDINGS = (
    "て", "で", "が", "けど", "けれど", "けれども", "ので", "のに", "を", "は", "と", "に", "も",
    "し", "たら", "ば", "って", "とか", "から", "より", "へ", "や",
)
INCOMPLETE_ENDINGS_ZH = (
    "的", "和", "跟", "与", "但是", "因为", "所以", "如果", "然后", "而且", "或者", "还有", "就是",
    "把", "被", "在", "对", "给", "比", "从", "让",
)
SHORT_CHARS = 3
_END_PUNCT = "。？！?!"
_STRIP = re.compile(r"[\s、。，,.？?！!「」『』・ー〜~：:；;]")


def norm(text: str) -> str:
    return _STRIP.sub("", text)


def is_complete(text: str, lang: str = "ja") -> bool:
    t = text.strip()
    if not t:
        return False
    if t[-1] in _END_PUNCT:
        return len(norm(t)) > SHORT_CHARS
    n = norm(t)
    if len(n) <= SHORT_CHARS:
        return False
    return not n.endswith(INCOMPLETE_ENDINGS_ZH if lang == "zh" else INCOMPLETE_ENDINGS)


def is_filler(text: str, fillers: list[str]) -> bool:
    """整句只由语气词构成。"""
    n = norm(text)
    if not n:
        return True
    for f in sorted({norm(f) for f in fillers if norm(f)}, key=len, reverse=True):
        n = n.replace(f, "")
    return not n


@dataclass
class Pending:
    id: int
    source: str
    ja: str
    wall_time: float
    first_mono: float
    deadline: float = 0.0
    pieces: list[str] = field(default_factory=list)


class Joiner:
    """每个音源独立暂存一条未完句。

    on_open(p)：新开一条（界面/记录新增条目）
    on_update(p)：日文追加
    on_commit(p, translate)：定稿；translate=False 表示纯语气词，不翻译
    """

    def __init__(
        self,
        hold_seconds: float,
        hold_max_seconds: float,
        fillers: list[str],
        on_open: Callable[[Pending], None],
        on_update: Callable[[Pending], None],
        on_commit: Callable[[Pending, bool], None],
        ids: itertools.count | None = None,
        lang: str = "ja",
    ) -> None:
        self.hold = hold_seconds
        self.hold_max = hold_max_seconds
        self.fillers = fillers
        self.lang = lang
        self._on_open = on_open
        self._on_update = on_update
        self._on_commit = on_commit
        self._ids = ids or itertools.count(1)
        self._pending: dict[str, Pending] = {}

    def add(self, source: str, text: str, wall: float, mono: float) -> None:
        p = self._pending.get(source)
        if p is None:
            p = Pending(next(self._ids), source, text, wall, mono, pieces=[text])
            self._pending[source] = p
            self._on_open(p)
        else:
            p.ja += text
            p.pieces.append(text)
            self._on_update(p)
        if is_complete(p.ja, self.lang) or mono - p.first_mono >= self.hold_max:
            self._commit(source)
        else:
            p.deadline = mono + self.hold

    def tick(self, mono: float) -> None:
        for source, p in list(self._pending.items()):
            if mono >= p.deadline:
                self._commit(source)

    def flush(self) -> None:
        for source in list(self._pending):
            self._commit(source)

    def has_pending(self) -> bool:
        return bool(self._pending)

    def _commit(self, source: str) -> None:
        p = self._pending.pop(source)
        self._on_commit(p, not is_filler(p.ja, self.fillers))
