"""会后整理：清洗双语全文（分块）→ 纪要 + 待办（译文语言）→ 写出「AI 标题_生成时间.md」。"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import glossary, lang
from .transcript import Transcript, fmt_aic
from .translator import Translator, Usage

CHUNK_ENTRIES = 120
TIMEOUT = 600.0
TITLE_MAX = 30

CLEAN_PROMPT = """\
你在整理{src}会议的「语音识别 + 机器翻译」记录。请：
1. 修正明显的语音识别错误（同音错字、漏字），补全标点；
2. 把同一说话方连续的碎句合并成通顺的段落；
3. 重新给出准确、通顺的{dst}译文；
4. 专有术语遵循术语表；不确定的内容保留原样，不要编造。
输出 Markdown，每段格式严格如下（段与段之间空一行）：
**【说话方】** {src}原文
> {dst}译文
只输出整理后的正文。"""

MINUTES_PROMPT = """\
根据下面整理后的{src}会议双语记录，用{dst}输出（专有术语保留原词）。

第一行输出「标题：」加上概括本次会议主题的标题（{dst}，20 字以内，不含日期），空一行后输出：

## {d.minutes}
### {d.topics}
（逐条列出）
### {d.conclusions}
（逐条列出已达成的结论/决定；没有则写「{d.none}」）

## {d.todos}
{d.todo_header}
|---|---|---|
（逐行列出；记录中没提到负责人或期限时写「{d.unclear}」；没有待办则写一行「{d.none}」）

除标题行外只输出以上 Markdown。"""

_TITLE_LINE = re.compile(r"^\s*(?:\*\*)?(?:标题|標題|タイトル)\s*[:：]\s*(.+?)(?:\*\*)?\s*$")
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def split_title(reply: str) -> tuple[str | None, str]:
    """从纪要回复中取出首行标题，返回 (标题, 去掉标题后的正文)。"""
    lines = reply.strip().splitlines()
    for i, line in enumerate(lines[:3]):
        if m := _TITLE_LINE.match(line):
            return m.group(1).strip(" #「」『』\"'"), "\n".join(lines[i + 1 :]).strip()
    return None, reply.strip()


def safe_title(title: str | None, fallback: str) -> str:
    t = _BAD_CHARS.sub(" ", title or "").strip(" .")
    t = re.sub(r"\s+", " ", t)[:TITLE_MAX].strip(" .")
    return t or fallback


def output_path(folder: Path, title: str, now: datetime) -> Path:
    path = folder / f"{title}_{now:%Y-%m-%d_%H%M%S}.md"
    n = 2
    while path.exists():
        path = folder / f"{title}_{now:%Y-%m-%d_%H%M%S}_{n}.md"
        n += 1
    return path


def _chunks(t: Transcript) -> list[str]:
    out = []
    for i in range(0, len(t.entries), CHUNK_ENTRIES):
        part = t.entries[i : i + CHUNK_ENTRIES]
        out.append("\n".join(f"【{e.source}】{e.ja}\n→ {e.zh or ''}" for e in part))
    return out


def summarize(
    transcript: Transcript,
    model: str,
    on_progress: Callable[[str, int, int], None],
    usage: Usage | None = None,
) -> Callable[[Translator], object]:
    """返回供 Translator.run 执行的协程工厂。

    on_progress(阶段说明, 已完成步数, 总步数)；usage：计入哪个用量账本（本场会议的或新建的）。
    """
    usage = usage or Usage()
    d = lang.get(transcript.direction)

    async def run(tr: Translator) -> Path:
        terms = glossary.to_prompt(glossary.load(), d.key)
        chunks = _chunks(transcript)
        if not chunks:
            raise ValueError("会议记录为空")
        total = len(chunks) + 2  # 清洗各块 + 纪要 + 写文件

        translate_aic = transcript.usage_aic
        u0 = usage.aic
        cleaned: list[str] = []
        on_progress("整理中：建立整理会话", 0, total)
        s = await tr.new_session(CLEAN_PROMPT.format(src=d.src, dst=d.dst) + "\n\n" + terms, model, "low", usage)
        try:
            for i, c in enumerate(chunks, 1):
                on_progress(f"整理中：清洗全文 {i}/{len(chunks)}", i - 1, total)
                cleaned.append(await s.send(c, timeout=TIMEOUT))
        finally:
            await s.close()

        on_progress("整理中：生成纪要、待办与标题", len(chunks), total)
        full = "\n\n".join(cleaned)
        s = await tr.new_session(MINUTES_PROMPT.format(src=d.src, dst=d.dst, d=d) + "\n\n" + terms, model, "medium", usage)
        try:
            title, minutes = split_title(await s.send(full, timeout=TIMEOUT))
        finally:
            await s.close()

        summarize_aic = usage.aic - u0
        total_aic = translate_aic + summarize_aic
        on_progress("整理中：写出整理文件", len(chunks) + 1, total)
        tokens = usage.tokens if total_aic <= 0 and usage.tokens != (0, 0) else None
        transcript.set_usage(total_aic, model, tokens)
        transcript.save()
        now = datetime.now()
        title = safe_title(title, d.fallback_title)
        path = output_path(transcript.md_path.parent, title, now)
        generated = d.generated.format(now=f"{now:%Y-%m-%d %H:%M}", model=model, source=transcript.md_path.name)
        used = d.usage.format(total=fmt_aic(total_aic), tr=fmt_aic(translate_aic), sm=fmt_aic(summarize_aic))
        path.write_text(
            f"# {title}\n\n"
            f"> {d.doc_title} {transcript.started:%Y-%m-%d %H:%M}（{d.label}）\n"
            f"> {generated}\n"
            f"> {used}\n\n"
            f"{minutes}\n\n## {d.fulltext}\n\n{full}\n",
            encoding="utf-8",
        )
        on_progress(f"整理完成：{path.name}", total, total)
        return path

    return run
