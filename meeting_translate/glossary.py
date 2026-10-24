"""用户术语表：同时用于翻译提示词和 ASR 热词。

文件格式（UTF-8，每行一条，# 开头为注释）：
    取引種別 = 取引種別      # 译文保留日文原词
    申込金額                 # 省略译文时等同于保留原词
    リリース = 发布
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import GLOSSARY_FILE

TEMPLATE = """\
# 术语表：每行「日文 = 中文译法」。省略「= 译法」表示译文中保留日文原词。
# 日文词同时作为语音识别热词，提高识别准确率。
# 修改后在下一次「开始」时生效。
取引種別
申込金額
"""


@dataclass(frozen=True)
class Term:
    ja: str
    zh: str


def load(path: Path = GLOSSARY_FILE) -> list[Term]:
    if not path.exists():
        path.write_text(TEMPLATE, encoding="utf-8")
    terms: list[Term] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        ja, _, zh = line.partition("=")
        ja, zh = ja.strip(), zh.strip()
        if ja:
            terms.append(Term(ja, zh or ja))
    return terms


def to_prompt(terms: list[Term], direction: str = "ja-zh") -> str:
    if not terms:
        return ""
    if direction == "zh-ja":
        lines = [f"- {t.zh} → {t.ja}" for t in terms]
        return "术语表（中文说法 → 日文译法，必须严格按此译法）：\n" + "\n".join(lines)
    lines = [f"- {t.ja} → {t.zh}" for t in terms]
    return "术语表（必须严格按此译法；译法与原词相同时保留日文原词）：\n" + "\n".join(lines)
