"""会议记录：内存中的双语条目，实时落盘为 md（阅读）+ json（临时，会后整理用）。

会议进行中两者都写在 %APPDATA% 的临时目录；结束后 md 移到用户设定的保存目录，
json 在整理完成或用户放弃整理后删除。历史会议可从 md 解析回来重新整理。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import lang
from .config import TRANSCRIPT_DIR

_MD_TITLE = re.compile(r"^# 会议记录 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
_MD_USAGE = re.compile(r"^> 本场用量：([\d.]+) AIC（模型 (.*)）")
_MD_USAGE_TOKENS = re.compile(r"^> 本场用量：([\d,]+) 输入 / ([\d,]+) 输出 token（模型 (.*)）")
_MD_DIRECTION = re.compile(r"^> 翻译方向：(\S+)")
_MD_MODE = re.compile(r"^> 模式：中日双向同传")
_MD_ENTRY = re.compile(r"^- `(\d{2}:\d{2}:\d{2})` 【(.+?)】(.*?)\s*$")
_MD_ZH = re.compile(r"^  → (.*)$")
_STEM_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{6})")


@dataclass
class Entry:
    id: int
    wall_time: float
    source: str
    ja: str
    zh: str | None = None
    asr_raw: str | None = None  # LLM 校正前的识别原文（未校正为 None）
    direction: str | None = None  # ja-zh | zh-ja；双向会议时逐条记录
    src_lang: str | None = None  # zh | ja | en ...
    dst_lang: str | None = None


class Transcript:
    def __init__(self, started: datetime | None = None, path: Path | None = None, direction: str = lang.JA_ZH) -> None:
        self.started = started or datetime.now()
        self.direction = direction  # 字段 ja/zh 分别为原文/译文，见 lang.py
        stem = self.started.strftime("%Y-%m-%d_%H%M%S")
        self.json_path: Path | None = path or TRANSCRIPT_DIR / f"{stem}.json"
        self.md_path = self.json_path.with_suffix(".md")
        self.entries: list[Entry] = []
        self._by_id: dict[int, Entry] = {}
        self._lock = threading.Lock()
        self.usage_aic = 0.0
        self.usage_tokens: tuple[int, int] | None = None  # 自定义端点：输入/输出 token
        self.models: list[str] = []

    @classmethod
    def load(cls, json_path: Path) -> "Transcript":
        data = json.loads(json_path.read_text(encoding="utf-8"))
        t = cls(datetime.fromisoformat(data["started"]), json_path, data.get("direction", lang.JA_ZH))
        for e in data["entries"]:
            t._add(Entry(**e))
        t.usage_aic = data.get("usage_aic", 0.0)
        t.usage_tokens = tuple(data["usage_tokens"]) if data.get("usage_tokens") else None
        t.models = data.get("models", [])
        return t

    @classmethod
    def load_markdown(cls, md_path: Path) -> "Transcript":
        """从 to_markdown() 写出的双语记录解析；不再关联 json。"""
        lines = md_path.read_text(encoding="utf-8").splitlines()
        m = _MD_TITLE.match(lines[0]) if lines else None
        if m is None:
            raise ValueError("不是原始会议记录（缺少「# 会议记录」标题）；整理稿不能再次整理")
        started = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        if (m := _STEM_TIME.search(md_path.stem)) is not None:
            started = datetime.strptime(m.group(1), "%Y-%m-%d_%H%M%S")  # 文件名精确到秒，优先使用
        t = cls(started, md_path.with_suffix(".json"))
        t.json_path = None
        t.md_path = md_path
        day = started.date()
        last: Entry | None = None
        for line in lines[1:]:
            if m := _MD_DIRECTION.match(line):
                t.direction = lang.from_label(m.group(1))
            elif _MD_MODE.match(line):
                t.direction = "bi"
            elif m := _MD_USAGE_TOKENS.match(line):
                t.usage_tokens = (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", "")))
                t.models = [x.strip() for x in m.group(3).split(",") if x.strip() and x.strip() != "—"]
            elif m := _MD_USAGE.match(line):
                t.usage_aic = float(m.group(1))
                t.models = [x.strip() for x in m.group(2).split(",") if x.strip() and x.strip() != "—"]
            elif m := _MD_ENTRY.match(line):
                ts = datetime.combine(day, datetime.strptime(m.group(1), "%H:%M:%S").time())
                if ts < started.replace(second=0):  # 跨午夜
                    day = day + timedelta(days=1)
                    ts = ts + timedelta(days=1)
                source = m.group(2)
                entry_direction = None
                if "·" in source:
                    source, label = source.split("·", 1)
                    entry_direction = lang.from_label(label)
                elif t.direction == "bi":
                    entry_direction = lang.ZH_JA if source == "我" else lang.JA_ZH
                last = Entry(
                    len(t.entries) + 1, ts.timestamp(), source, m.group(3),
                    direction=entry_direction,
                    src_lang="zh" if entry_direction == lang.ZH_JA else "ja",
                    dst_lang="ja" if entry_direction == lang.ZH_JA else "zh",
                )
                t._add(last)
            elif last is not None and (m := _MD_ZH.match(line)):
                zh = m.group(1)
                last.zh = "" if zh == "—" else (None if zh == "（未翻译）" else zh)
                last = None
        return t

    def _add(self, e: Entry) -> None:
        self.entries.append(e)
        self._by_id[e.id] = e

    def add(self, e: Entry) -> None:
        if e.direction and not e.src_lang:
            e.src_lang = "zh" if e.direction == lang.ZH_JA else "ja"
            e.dst_lang = "ja" if e.direction == lang.ZH_JA else "zh"
        with self._lock:
            self._add(e)
        self.save()

    def set_ja(self, entry_id: int, ja: str) -> None:
        with self._lock:
            if entry_id in self._by_id:
                self._by_id[entry_id].ja = ja
        self.save()

    def set_usage(self, aic: float, model: str | None = None, tokens: tuple[int, int] | None = None) -> None:
        with self._lock:
            self.usage_aic = aic
            if tokens is not None:
                self.usage_tokens = tokens
            if model and model not in self.models:
                self.models.append(model)

    def set_translation(self, entry_id: int, zh: str, corrected: str | None = None) -> None:
        with self._lock:
            if entry_id in self._by_id:
                e = self._by_id[entry_id]
                e.zh = zh
                if corrected and corrected != e.ja:
                    if e.asr_raw is None:
                        e.asr_raw = e.ja
                    e.ja = corrected
        self.save()

    def since(self, wall_time: float, direction: str | None = None) -> list[Entry]:
        with self._lock:
            items = [e for e in self.entries if e.wall_time >= wall_time]
            if direction is not None:
                items = [e for e in items if (e.direction or self.direction) == direction]
            return items

    def last(self, n: int, direction: str | None = None) -> list[Entry]:
        with self._lock:
            items = [e for e in self.entries if e.zh]
            if direction is not None:
                items = [e for e in items if (e.direction or self.direction) == direction]
            return items[-n:]

    def to_markdown(self) -> str:
        lines = [f"# 会议记录 {self.started:%Y-%m-%d %H:%M}", ""]
        if self.direction == "bi":
            lines += ["> 模式：中日双向同传", ""]
        elif self.direction != lang.JA_ZH:
            lines += [f"> 翻译方向：{lang.get(self.direction).label}", ""]
        if self.usage_tokens:
            tin, tout = self.usage_tokens
            lines += [
                f"> 本场用量：{tin:,} 输入 / {tout:,} 输出 token（模型 {', '.join(self.models) or '—'}）",
                "",
            ]
        elif self.usage_aic:
            lines += [f"> 本场用量：{fmt_aic(self.usage_aic)} AIC（模型 {', '.join(self.models) or '—'}）", ""]
        for e in self.entries:
            ts = datetime.fromtimestamp(e.wall_time).strftime("%H:%M:%S")
            if e.direction:
                d = lang.get(e.direction)
                lines.append(f"- `{ts}` 【{e.source}·{d.label}】{e.ja}  ")
            else:
                lines.append(f"- `{ts}` 【{e.source}】{e.ja}  ")
            lines.append(f"  → {'—' if e.zh == '' else (e.zh or '（未翻译）')}")
        return "\n".join(lines) + "\n"

    def save(self) -> None:
        with self._lock:
            data = {
                "started": self.started.isoformat(),
                "direction": self.direction,
                "usage_aic": self.usage_aic,
                "usage_tokens": self.usage_tokens,
                "models": self.models,
                "entries": [asdict(e) for e in self.entries],
            }
            md = self.to_markdown()
            json_path = self.json_path
            md_path = self.md_path
        if json_path is not None:
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        md_path.write_text(md, encoding="utf-8")

    def move_markdown(self, folder: Path) -> None:
        """把 md 写到 folder 并删除临时目录里的旧 md。"""
        old = self.md_path
        with self._lock:
            self.md_path = folder / old.name
        self.save()
        if old.resolve() != self.md_path.resolve():
            old.unlink(missing_ok=True)

    def discard_json(self) -> None:
        """整理完成或放弃整理后删除临时 json；之后只维护 md。"""
        with self._lock:
            path, self.json_path = self.json_path, None
        if path is not None:
            path.unlink(missing_ok=True)

    def discard(self) -> None:
        """空会议：删除全部落盘文件。"""
        self.discard_json()
        self.md_path.unlink(missing_ok=True)


def fmt_aic(v: float) -> str:
    if v >= 10:
        return f"{v:.1f}"
    if v >= 0.1:
        return f"{v:.2f}"
    return f"{v:.4f}"


def list_meetings() -> list[Path]:
    return sorted(TRANSCRIPT_DIR.glob("*.json"), reverse=True)
