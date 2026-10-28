"""与「详细」弹窗同一套深色样式的提示 / 确认框，替代原生 QMessageBox。

用法：
    info(parent, "标题", "正文")
    warn(parent, "标题", "正文", detail="补充说明")
    if ask(parent, "标题", "正文", yes="继续", no="取消"): ...
    choice = choose(parent, "标题", "正文", ["自动安装", "打开下载页", "关闭"])
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .resource_details import DETAIL_QSS

KINDS = {
    # kind: (徽标字符, 前景色, 背景色, 边框色)
    "info": ("i", "#8fb3ff", "#1a2336", "#2e4170"),
    "warning": ("!", "#f0b75a", "#2a2416", "#4a3d1f"),
    "question": ("?", "#8fb3ff", "#1a2336", "#2e4170"),
}

MESSAGE_QSS = DETAIL_QSS + """
QLabel#msgBadge {
    font-size: 18px; font-weight: 800; border-radius: 17px;
    min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px;
}
QLabel#msgText { color: #eaf0fb; font-size: 14px; font-weight: 600; }
QLabel#msgDetail { color: #c9d2e3; font-size: 13px; }
QPushButton#detailPrimary {
    color: white; background: #4f7cff; border: none; border-radius: 9px;
    padding: 9px 18px; font-weight: 700;
}
QPushButton#detailPrimary:hover { background: #6a91ff; }
"""


def _to_html(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


class MessageDialog(QDialog):
    """标题 + 图标徽标 + 主文案 + 可选补充说明 / 步骤列表 + 一排按钮。"""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        *,
        detail: str = "",
        steps: list[str] | None = None,
        kind: str = "info",
        buttons: list[str] | None = None,
        default: int = 0,
        on_top: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("detailDialog")
        self.setWindowTitle(title)
        self.setStyleSheet(MESSAGE_QSS)
        self.setMinimumWidth(460)
        self.setMaximumWidth(680)
        if on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.clicked: str | None = None

        glyph, fg, bg, border = KINDS.get(kind, KINDS["info"])
        badge = QLabel(glyph)
        badge.setObjectName("msgBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(f"color:{fg}; background:{bg}; border:1px solid {border};")

        title_lbl = QLabel(title)
        title_lbl.setObjectName("detailTitle")
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(title_lbl, 1)

        text_lbl = QLabel(_to_html(text))
        text_lbl.setObjectName("msgText")
        text_lbl.setWordWrap(True)
        text_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)
        lay.addLayout(head)
        lay.addWidget(text_lbl)

        body = _to_html(detail) if detail else ""
        if steps:
            items = "".join(f"<li style='margin-bottom:4px'>{_to_html(s)}</li>" for s in steps)
            body += f"<ol style='margin:4px 0 0 0'>{items}</ol>"
        if body:
            detail_lbl = QLabel(body)
            detail_lbl.setObjectName("msgDetail")
            detail_lbl.setWordWrap(True)
            detail_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(detail_lbl)

        labels = buttons or ["确定"]
        self.buttons: dict[str, QPushButton] = {}
        row = QHBoxLayout()
        row.addStretch(1)
        for i, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setObjectName("detailPrimary" if i == default else "detailGhost")
            btn.setAutoDefault(i == default)
            btn.setDefault(i == default)
            btn.clicked.connect(lambda _=False, label=label: self._pick(label))
            row.addWidget(btn)
            self.buttons[label] = btn
        lay.addSpacing(6)
        lay.addLayout(row)
        # 自动换行的 QLabel 会让默认尺寸偏高，按实际宽度重算高度，避免标题下空一大块
        width = 580
        self.resize(width, lay.totalHeightForWidth(width))

    def _pick(self, label: str) -> None:
        self.clicked = label
        self.accept()


def choose(parent, title: str, text: str, buttons: list[str], **kwargs) -> str | None:
    """弹窗并返回被点的按钮文字；关掉窗口返回 None。"""
    dlg = MessageDialog(parent, title, text, buttons=buttons, **kwargs)
    dlg.exec()
    return dlg.clicked


def info(parent, title: str, text: str, **kwargs) -> None:
    choose(parent, title, text, ["确定"], kind="info", **kwargs)


def warn(parent, title: str, text: str, **kwargs) -> None:
    choose(parent, title, text, ["确定"], kind="warning", **kwargs)


def ask(parent, title: str, text: str, *, yes: str = "是", no: str = "否", **kwargs) -> bool:
    kwargs.setdefault("kind", "question")
    return choose(parent, title, text, [yes, no], **kwargs) == yes
