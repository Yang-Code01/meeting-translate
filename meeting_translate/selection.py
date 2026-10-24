"""跨应用选区读取与手动保存的划词翻译历史。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QMimeData, QObject, QPoint, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_VK_CONTROL, _VK_C, _KEYEVENTF_KEYUP = 0x11, 0x43, 0x0002
_user32.GetAsyncKeyState.restype = ctypes.c_short
# Shift / Ctrl / Alt / 左右 Win：快捷键触发时用户往往还按着，此时发 Ctrl+C 会变成 Ctrl+Alt+C 等组合
_MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)
_HOTKEY_SETTLE_MS = 50
_RELEASE_TIMEOUT_MS = 2000
_POLL_INTERVAL_MS = 30
_CAPTURE_TIMEOUT_MS = 1000


@dataclass(frozen=True)
class SelectionRecord:
    created_at: datetime
    source: str
    chinese: str
    hiragana: str


class SelectionCapture(QObject):
    captured = Signal(str, QPoint, str)  # text, cursor position, clipboard restoration warning
    failed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pending = False
        self._original: QMimeData | None = None
        self._sequence = 0
        self._deadline = 0.0
        self._release_deadline = 0.0
        self._anchor = QPoint()

    def start(self) -> bool:
        if self._pending:
            return False
        self._anchor = QCursor.pos()
        self._pending = True
        self._release_deadline = time.monotonic() + _RELEASE_TIMEOUT_MS / 1000
        QTimer.singleShot(_HOTKEY_SETTLE_MS, self._wait_modifiers_released)
        return True

    def _wait_modifiers_released(self) -> None:
        if not self._pending:
            return
        if _modifiers_held():
            if time.monotonic() < self._release_deadline:
                QTimer.singleShot(_POLL_INTERVAL_MS, self._wait_modifiers_released)
                return
            self._finish(error="请松开 Alt 等按键后再试。")
            return
        self._begin_copy()

    def _begin_copy(self) -> None:
        if not self._pending:
            return
        try:
            clipboard = QApplication.clipboard()
            self._original = _copy_mime_data(clipboard.mimeData())
            self._sequence = int(_user32.GetClipboardSequenceNumber())
            self._deadline = time.monotonic() + _CAPTURE_TIMEOUT_MS / 1000
            _send_copy_shortcut()
        except Exception:
            self._finish(error="无法读取所选文字。请确认应用允许复制后重试。")
            log.exception("发送复制快捷键失败")
            return
        QTimer.singleShot(_POLL_INTERVAL_MS, self._poll)

    def cancel(self) -> None:
        if self._pending:
            self._finish(error=None, emit_failure=False)

    def _poll(self) -> None:
        if not self._pending:
            return
        sequence = int(_user32.GetClipboardSequenceNumber())
        if sequence != self._sequence:
            data = QApplication.clipboard().mimeData()
            text = data.text().strip() if data is not None and data.hasText() else ""
            if text:
                self._finish(text=text)
            else:
                self._finish(error="没有读取到文字选区。请先选中日文，再按快捷键。")
            return
        if time.monotonic() >= self._deadline:
            self._finish(error="没有读取到文字选区。请先选中日文，再按快捷键。")
            return
        QTimer.singleShot(_POLL_INTERVAL_MS, self._poll)

    def _finish(self, text: str | None = None, error: str | None = None, emit_failure: bool = True) -> None:
        self._pending = False
        warning = ""
        original, self._original = self._original, None
        if original is not None:
            try:
                QApplication.clipboard().setMimeData(original)
            except Exception as e:
                warning = f"；恢复剪贴板失败：{e}"
                log.exception("恢复剪贴板失败")
        if text is not None:
            self.captured.emit(text, self._anchor, warning)
        elif error and emit_failure:
            self.failed.emit(error + warning)


def _copy_mime_data(source: QMimeData | None) -> QMimeData:
    copied = QMimeData()
    if source is None:
        return copied
    for mime_type in source.formats():
        copied.setData(mime_type, source.data(mime_type))
    if source.hasText():
        copied.setText(source.text())
    return copied


def _modifiers_held() -> bool:
    return any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS)


def _send_copy_shortcut() -> None:
    try:
        _user32.keybd_event(_VK_CONTROL, 0, 0, 0)
        _user32.keybd_event(_VK_C, 0, 0, 0)
    finally:
        _user32.keybd_event(_VK_C, 0, _KEYEVENTF_KEYUP, 0)
        _user32.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)


def _code_block(label: str, value: str) -> str:
    longest = max((len(match.group()) for match in re.finditer(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    lines = "\n".join(f"  {line}" for line in value.splitlines())
    return f"- {label}：\n  {fence}text\n{lines}\n  {fence}\n"


_HISTORY_HEADER = "# 划词/句翻译历史\n\n"
_SOURCE_BLOCK_RE = re.compile(r"^- 原文：\n  (`{3,})text\n(.*?)\n  \1\n", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class HistorySaveResult:
    added: int
    skipped: int  # 与已有记录或本批其他记录原文相同而跳过的条数
    cleaned: int  # 从历史文件中清理掉的已有重复条数


def _source_key(source: str) -> str:
    return " ".join(source.split())


def _entry_source(entry: str) -> str | None:
    m = _SOURCE_BLOCK_RE.search(entry)
    if not m:
        return None
    return "\n".join(line[2:] if line.startswith("  ") else line for line in m.group(2).split("\n"))


def _split_entries(text: str) -> tuple[str, list[str]]:
    """拆成（文件头, 各条目）；代码块内容有两格缩进，行首的 "## " 只会是条目标题。"""
    parts = re.split(r"(?m)^(?=## )", text)
    return parts[0], [p if p.endswith("\n") else p + "\n" for p in parts[1:]]


def _format_record(record: SelectionRecord) -> str:
    return (
        f"## {record.created_at:%Y-%m-%d %H:%M:%S}\n"
        + _code_block("原文", record.source)
        + _code_block("中文", record.chinese)
        + _code_block("读音（汉字注平假名）", record.hiragana)
        + "\n"
    )


def append_history(path: Path, records: list[SelectionRecord]) -> HistorySaveResult:
    """按原文去重后写入：跳过已记录过的原文，并清理文件里已有的重复条目（保留最早一条）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    head, entries = _split_entries(text)
    seen: set[str] = set()
    kept: list[str] = []
    for entry in entries:
        source = _entry_source(entry)
        key = _source_key(source) if source is not None else None
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        kept.append(entry)
    cleaned = len(entries) - len(kept)

    new_chunks: list[str] = []
    for record in records:
        key = _source_key(record.source)
        if key in seen:
            continue
        seen.add(key)
        new_chunks.append(_format_record(record))
    result = HistorySaveResult(len(new_chunks), len(records) - len(new_chunks), cleaned)
    if not new_chunks and not cleaned:
        return result

    if cleaned or not text.strip():
        body = (head if head.strip() else _HISTORY_HEADER) + "".join(kept) + "".join(new_chunks)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8", newline="\n")
        tmp.replace(path)
        return result
    separator = "" if text.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8", newline="\n") as history:
        history.write(separator + "".join(new_chunks))
    return result
