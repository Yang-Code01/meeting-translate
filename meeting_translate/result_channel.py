"""结果弹窗通道：请求编号 + 弹窗展示，与输入来源（划词、截图…）无关。

规则：每次 begin() 分配新编号并把它设为最新；只有最新请求的结果或错误
才会刷新弹窗，较早的结果由调用方自行处理（如只写历史）。
"""

from __future__ import annotations

from typing import Protocol

from PySide6.QtCore import QPoint


class ResultPopup(Protocol):
    def show_loading(self, anchor: QPoint) -> None: ...

    def show_result(self, chinese: str, hiragana: str, anchor: QPoint) -> None: ...

    def show_error(self, message: str, anchor: QPoint) -> None: ...


class ResultChannel:
    def __init__(self, popup: ResultPopup) -> None:
        self._popup = popup
        self._latest = 0

    def begin(self, anchor: QPoint) -> int:
        """开始一次新请求：编号设为最新，弹窗进入加载状态，返回编号。"""
        self._latest += 1
        self._popup.show_loading(anchor)
        return self._latest

    def is_latest(self, request_id: int) -> bool:
        return request_id == self._latest

    def show_result(self, request_id: int, chinese: str, hiragana: str, anchor: QPoint) -> bool:
        if not self.is_latest(request_id):
            return False
        self._popup.show_result(chinese, hiragana, anchor)
        return True

    def show_error(self, request_id: int, message: str, anchor: QPoint) -> bool:
        if not self.is_latest(request_id):
            return False
        self._popup.show_error(message, anchor)
        return True
