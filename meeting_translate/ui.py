"""PySide6 界面：置顶悬浮字幕窗、设置对话框、登录对话框、模型下载。"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import audio, lang, stats
from .asr import downloadable, model_ready, model_target_dir
from .config import HF_BASE, Settings
from .transcript import Entry, fmt_aic
from .translator import ModelOption

MAX_VISIBLE_ENTRIES = 40
ENGINES = [
    ("sensevoice", "SenseVoice（内置）"),
    ("reazon", "ReazonSpeech（内置，支持词表）"),
    ("whisper-small", "Whisper small"),
    ("whisper-medium", "Whisper medium"),
]
EFFORTS = ["none", "low", "medium", "high", "default"]
SPEAKER_COLORS = {audio.THEM: "#7fd1ff", audio.ME: "#ffd27f"}
BUTTON_STYLE = (
    "QPushButton{color:#eee;background:rgba(255,255,255,30);border:none;padding:2px 8px;border-radius:4px}"
    "QPushButton:hover{background:rgba(255,255,255,70)}QPushButton:disabled{color:#777}"
)
MIC_ON_STYLE = (
    "QPushButton{color:white;background:rgba(46,160,67,200);border:none;padding:2px 8px;border-radius:4px}"
    "QPushButton:hover{background:rgba(46,160,67,255)}"
)
DIALOG_GAP = 8
FRAME_ALLOWANCE = QSize(16, 40)  # 对话框标题栏/边框，显示前取不到真实值


def place_beside(anchor: QRect, size: QSize, screen: QRect, gap: int = DIALOG_GAP) -> QPoint:
    """在 screen 内选 anchor 四周空间最大且放得下 size 的一侧；都放不下时在屏幕居中。"""
    w, h = size.width(), size.height()
    spaces = {
        "right": screen.right() - anchor.right() - gap,
        "left": anchor.left() - screen.left() - gap,
        "below": screen.bottom() - anchor.bottom() - gap,
        "above": anchor.top() - screen.top() - gap,
    }
    need = {"right": w, "left": w, "below": h, "above": h}
    fitting = [k for k in spaces if spaces[k] >= need[k]]

    def clamp(v: int, lo: int, hi: int) -> int:
        return max(lo, min(v, hi))

    if not fitting:
        return QPoint(screen.left() + max(0, (screen.width() - w) // 2), screen.top() + max(0, (screen.height() - h) // 2))
    side = max(fitting, key=lambda k: spaces[k] - need[k])
    if side in ("right", "left"):
        x = anchor.right() + gap + 1 if side == "right" else anchor.left() - gap - w
        y = clamp(anchor.top(), screen.top(), screen.bottom() - h + 1)
    else:
        y = anchor.bottom() + gap + 1 if side == "below" else anchor.top() - gap - h
        x = clamp(anchor.left(), screen.left(), screen.right() - w + 1)
    return QPoint(x, y)


class Overlay(QWidget):
    toggle_clicked = Signal()
    stop_clicked = Signal()
    settings_clicked = Signal()
    mic_toggled = Signal(bool)
    retry_clicked = Signal()
    geometry_changed = Signal(list)

    def __init__(self, settings: Settings) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("会议翻译")
        self._opacity = settings.opacity
        self._font_size = settings.font_size
        self._drag: QPoint | None = None
        self._progress_token = 0
        self.entries: list[Entry] = []
        self.partials: dict[str, str] = {}

        self.status = QLabel("启动中…")
        self.latency = QLabel("")
        self.usage = QLabel("")
        self.btn_mic = QPushButton()
        self.btn_mic.setCheckable(True)
        self.btn_toggle = QPushButton("▶ 开始")
        self.btn_stop = QPushButton("■ 结束")
        self.btn_settings = QPushButton("⚙")
        self.btn_hide = QPushButton("—")
        self.btn_stop.setEnabled(False)
        for w in (self.status, self.latency, self.usage):
            w.setStyleSheet("color:#ddd;")
        for b in (self.btn_mic, self.btn_toggle, self.btn_stop, self.btn_settings, self.btn_hide):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(BUTTON_STYLE)
        self.set_mic(settings.mic_enabled)
        self.btn_mic.toggled.connect(self._on_mic_toggled)
        self.btn_toggle.clicked.connect(self.toggle_clicked)
        self.btn_stop.clicked.connect(self.stop_clicked)
        self.btn_settings.clicked.connect(self.settings_clicked)
        self.btn_hide.clicked.connect(self.hide)

        bar = QHBoxLayout()
        bar.addWidget(self.status, 1)
        bar.addWidget(self.latency)
        bar.addWidget(self.usage)
        for b in (self.btn_mic, self.btn_toggle, self.btn_stop, self.btn_settings, self.btn_hide):
            bar.addWidget(b)

        self.progress = QFrame()
        self.progress.setStyleSheet(
            "QFrame{background:rgba(43,125,233,60);border-radius:6px}QLabel{color:#eee;background:transparent}"
        )
        self.progress_text = QLabel("")
        # 长路径/长错误信息不撑宽悬浮窗，完整内容放在 tooltip
        self.progress_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet(
            "QProgressBar{background:rgba(255,255,255,40);border:none;border-radius:3px}"
            "QProgressBar::chunk{background:#2b7de9;border-radius:3px}"
        )
        self.btn_retry = QPushButton("重试")
        self.btn_close_progress = QPushButton("✕")
        for b in (self.btn_retry, self.btn_close_progress):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(BUTTON_STYLE)
        self.btn_retry.clicked.connect(self.retry_clicked)
        self.btn_close_progress.clicked.connect(self.hide_progress)
        prow = QHBoxLayout()
        prow.addWidget(self.progress_text, 1)
        prow.addWidget(self.btn_retry)
        prow.addWidget(self.btn_close_progress)
        play = QVBoxLayout(self.progress)
        play.setContentsMargins(8, 4, 4, 6)
        play.addLayout(prow)
        play.addWidget(self.progress_bar)
        self.progress.hide()

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.view.setStyleSheet("background:transparent;color:white;")
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 2)
        lay.addLayout(bar)
        lay.addWidget(self.progress)
        lay.addWidget(self.view, 1)
        lay.addLayout(grip_row)
        self.setGeometry(*settings.window_geometry)
        self.apply_style(settings)

    def apply_style(self, settings: Settings) -> None:
        self._opacity = settings.opacity
        self._font_size = settings.font_size
        self.view.setFont(QFont("Microsoft YaHei UI", settings.font_size))
        self.update()
        self.render()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(20, 20, 24, int(255 * self._opacity)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(self.rect(), 10, 10)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        if self._drag is not None:
            self._drag = None
            self._emit_geometry()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._emit_geometry()

    def _emit_geometry(self) -> None:
        g = self.geometry()
        self.geometry_changed.emit([g.x(), g.y(), g.width(), g.height()])

    def set_running(self, running: bool, active: bool) -> None:
        """running: 正在采集；active: 有进行中的会议（含暂停）。"""
        self.btn_toggle.setText("⏸ 暂停" if running else ("▶ 继续" if active else "▶ 开始"))
        self.btn_stop.setEnabled(active)

    def set_usage(self, text: str, warn: bool) -> None:
        self.usage.setText(text)
        self.usage.setStyleSheet("color:#ff6b6b;font-weight:bold;" if warn else "color:#ddd;")

    def set_mic(self, on: bool) -> None:
        """同步按钮外观，不发出 mic_toggled。"""
        self.btn_mic.blockSignals(True)
        self.btn_mic.setChecked(on)
        self.btn_mic.blockSignals(False)
        self._style_mic(on)

    def _on_mic_toggled(self, on: bool) -> None:
        self._style_mic(on)
        self.mic_toggled.emit(on)

    def _style_mic(self, on: bool) -> None:
        self.btn_mic.setText("🎤 麦克风：开" if on else "🎤 麦克风：关")
        self.btn_mic.setToolTip(
            "正在监听你的麦克风，识别结果标记为【我】。点击关闭。"
            if on
            else "未监听你的麦克风（只翻译对方声音）。点击开启。"
        )
        self.btn_mic.setStyleSheet(MIC_ON_STYLE if on else BUTTON_STYLE)

    # ---- 会后整理进度卡片 ----

    def show_progress(self, text: str, value: int, maximum: int) -> None:
        self._progress_token += 1
        self.progress_text.setText(text)
        self.progress_text.setToolTip(text)
        self.progress_text.setStyleSheet("")
        self.progress_bar.setRange(0, max(maximum, 0))  # maximum=0 显示忙碌动画
        self.progress_bar.setValue(value)
        self.progress_bar.show()
        self.btn_retry.hide()
        self.btn_close_progress.hide()
        self.progress.show()

    def show_progress_done(self, text: str, hide_after_ms: int = 8000) -> None:
        self.show_progress(text, 1, 1)
        self.btn_close_progress.show()
        token = self._progress_token
        QTimer.singleShot(hide_after_ms, lambda: token == self._progress_token and self.hide_progress())

    def show_progress_error(self, text: str) -> None:
        self._progress_token += 1
        self.progress_text.setText(text)
        self.progress_text.setToolTip(text)
        self.progress_text.setStyleSheet("color:#ff8a8a")
        self.progress_bar.hide()
        self.btn_retry.show()
        self.btn_close_progress.show()
        self.progress.show()

    def hide_progress(self) -> None:
        self._progress_token += 1
        self.progress.hide()

    def clear(self) -> None:
        self.entries.clear()
        self.partials.clear()
        self.render()

    def render(self) -> None:
        ja_size = max(9, int(self._font_size * 0.75))
        rows = []
        for e in self.entries[-MAX_VISIBLE_ENTRIES:]:
            color = SPEAKER_COLORS.get(e.source, "#ccc")
            ja = f"<span style='color:{color};font-size:{ja_size}pt'>【{e.source}】{html.escape(e.ja)}</span>"
            if e.zh == "":  # 语气词：只显示不翻译
                rows.append(f"<div style='margin-bottom:6px'>{ja}</div>")
                continue
            zh = html.escape(e.zh) if e.zh else "<span style='color:#888'>…</span>"
            rows.append(
                f"<div style='margin-bottom:6px'>{ja}<br>"
                f"<span style='font-size:{self._font_size}pt'>{zh}</span></div>"
            )
        for src, text in self.partials.items():
            if text:
                color = SPEAKER_COLORS.get(src, "#ccc")
                rows.append(
                    f"<div style='color:{color};font-size:{ja_size}pt'><i>【{src}】{html.escape(text)} …</i></div>"
                )
        self.view.setHtml("".join(rows))
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())


_RUBY_RE = re.compile(r"([\u3400-\u9fff\uf900-\ufaff々〆ヶ]+)[（(]([\u3041-\u309f\u30a0-\u30ffー]+)[）)]")
_NO_LINE_START = set("、。，．,.！？!?）)」』】〕〉》ー…・：；ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ々")


class RubyLabel(QWidget):
    """把「漢字（かな）」形式的文本绘制成假名标在汉字正上方的注音（QLabel 不支持 <ruby>）。"""

    def __init__(self, max_width: int, parent=None) -> None:
        super().__init__(parent)
        self._max_width = max_width
        self._tokens: list[tuple[str, str]] = []
        self._lines: list[list[tuple[str, str, int, int]]] = []
        self._base_font = QFont(self.font())
        self._ruby_font = QFont(self.font())
        self._base_color = QColor("#dddddd")
        self._ruby_color = QColor("#ffe082")
        self._show_base = True
        self.configure(10, 10, "#ffe082", "#dddddd", True)
        self.hide()

    def configure(self, kana_px: int, kanji_px: int, kana_color: str, kanji_color: str, show_kanji: bool) -> None:
        self._ruby_font.setPixelSize(kana_px)
        self._ruby_font.setWeight(QFont.Weight.Bold)
        self._base_font.setPixelSize(kanji_px)
        self._base_font.setWeight(QFont.Weight.Bold)
        self._ruby_color, self._base_color = QColor(kana_color), QColor(kanji_color)
        self._show_base = show_kanji
        self._relayout()

    def setText(self, text: str) -> None:
        tokens: list[tuple[str, str]] = []
        pos = 0
        for m in _RUBY_RE.finditer(text):
            tokens.extend((ch, "") for ch in text[pos : m.start()])
            tokens.append((m.group(1), m.group(2)))
            pos = m.end()
        tokens.extend((ch, "") for ch in text[pos:])
        self._tokens = tokens
        self._relayout()

    def clear(self) -> None:
        self.setText("")

    def set_max_width(self, max_width: int) -> None:
        max_width = max(60, max_width)
        if max_width != self._max_width:
            self._max_width = max_width
            self._relayout()

    def _token_width(self, base: str, ruby: str, fm: QFontMetrics, rm: QFontMetrics) -> int:
        if not self._show_base:
            # 只显示假名：有读音的词用读音替换，其余字符按假名字体原样显示
            return rm.horizontalAdvance(ruby or base)
        return max(fm.horizontalAdvance(base), rm.horizontalAdvance(ruby) + 8 if ruby else 0)

    def _relayout(self) -> None:
        fm, rm = QFontMetrics(self._base_font), QFontMetrics(self._ruby_font)
        lines: list[list[tuple[str, str, int, int]]] = [[]]
        x = widest = 0
        for base, ruby in self._tokens:
            if base == "\n":
                lines.append([])
                x = 0
                continue
            w = self._token_width(base, ruby, fm, rm)
            if lines[-1] and x + w > self._max_width:
                carried = []
                # 避头：标点、小写假名等不能出现在行首，把前一个字一并带到下一行
                if base in _NO_LINE_START and len(lines[-1]) > 1:
                    carried.append(lines[-1].pop())
                lines.append([])
                x = 0
                for cb, cr, _cx, cw in carried:
                    lines[-1].append((cb, cr, x, cw))
                    x += cw
            lines[-1].append((base, ruby, x, w))
            x += w
            widest = max(widest, x)
        self._lines = lines
        self._ruby_h = rm.height()
        self._base_h = fm.height() if self._show_base else 0
        self._line_h = self._ruby_h + self._base_h + 4
        self.setFixedSize(widest, self._line_h * len(lines) if self._tokens else 0)
        self.setVisible(bool(self._tokens))
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        center = Qt.AlignmentFlag.AlignCenter
        for i, line in enumerate(self._lines):
            top = i * self._line_h
            for base, ruby, x, w in line:
                if not self._show_base:
                    p.setFont(self._ruby_font)
                    p.setPen(self._ruby_color)
                    p.drawText(QRect(x, top, w, self._ruby_h), center, ruby or base)
                    continue
                if ruby:
                    p.setFont(self._ruby_font)
                    p.setPen(self._ruby_color)
                    p.drawText(QRect(x, top, w, self._ruby_h), center, ruby)
                p.setFont(self._base_font)
                p.setPen(self._base_color)
                p.drawText(QRect(x, top + self._ruby_h, w, self._base_h), center, base)
        p.end()


class ColorButton(QPushButton):
    """显示当前颜色、点击后弹出取色对话框的按钮。"""

    colorChanged = Signal(str)

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(90)
        self.clicked.connect(self._pick)
        self._color = ""
        self.set_color(color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        c = QColor(color)
        self._color = c.name() if c.isValid() else "#ffffff"
        fg = "#000" if QColor(self._color).lightness() > 128 else "#fff"
        self.setText(self._color)
        self.setStyleSheet(f"background:{self._color};color:{fg};border:1px solid #888;padding:2px 6px")
        self.colorChanged.emit(self._color)

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self, "选择颜色")
        if c.isValid():
            self.set_color(c.name())


class SelectionResultPopup(QWidget):
    user_resized = Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool,
        )
        self.setObjectName("selectionPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("划词/句翻译")
        self.setMinimumSize(300, 120)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet(
            "QFrame#selectionPanel{background:#202024;border:1px solid #888;border-radius:10px}"
            "QLabel{color:#eee;background:transparent}"
            "QPushButton{color:#eee;background:transparent;border:none;padding:2px 8px}"
            "QPushButton:hover{background:rgba(255,255,255,50);border-radius:4px}"
            "QScrollArea{background:transparent;border:none}"
            "QScrollBar:vertical{background:transparent;width:8px;margin:0}"
            "QScrollBar::handle:vertical{background:#666;border-radius:4px;min-height:24px}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent}"
        )

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.chinese = QLabel("")
        self.chinese.setWordWrap(True)
        self.chinese.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.reading_title = QLabel("读音：")
        self.reading_title.hide()
        self.hiragana = RubyLabel(780)
        self.btn_close = QPushButton("✕")
        self.btn_close.setToolTip("关闭")
        self.btn_close.clicked.connect(self.hide)

        self.chinese.setCursor(Qt.CursorShape.IBeamCursor)
        self.btn_close.setCursor(Qt.CursorShape.ArrowCursor)

        header = QHBoxLayout()
        self._title = QLabel("划词/句翻译")
        header.addWidget(self._title, 1)
        header.addWidget(self.btn_close)

        # 内容放进滚动区域：长文本时窗口大小受限，超出部分滚动查看
        self.body = QWidget()
        self.body.setAutoFillBackground(False)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        for w in (self.message, self.chinese, self.reading_title, self.hiragana):
            body_layout.addWidget(w)
        body_layout.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.body)
        self.body.setAutoFillBackground(False)  # setWidget 会把它打开，导致浅色底
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.viewport().installEventFilter(self)

        self.grip = QSizeGrip(self)
        self.grip.setToolTip("拖动调整大小（会被记住）")
        self.grip.installEventFilter(self)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        footer.addWidget(self.grip, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)

        # 透明顶层窗口上的普通 QWidget 不会绘制样式表背景，需用 QFrame 承载不透明底色
        panel = QFrame()
        panel.setObjectName("selectionPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        content = QVBoxLayout(panel)
        content.setContentsMargins(14, 8, 4, 4)
        content.setSpacing(4)
        content.addLayout(header)
        content.addWidget(self.scroll, 1)
        content.addLayout(footer)
        self._drag_offset: QPoint | None = None
        self._user_moved = False
        self._saved_size: QSize | None = None
        self.hide()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self.hiragana.set_max_width(self.scroll.viewport().width() - 4)
        elif obj is self.grip and event.type() == QEvent.Type.MouseButtonRelease:
            self._saved_size = self.size()
            self.user_resized.emit(self.width(), self.height())
        return super().eventFilter(obj, event)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._drag_offset is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            self._user_moved = True
            e.accept()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(e)

    def apply_style(self, settings: Settings) -> None:
        self.chinese.setStyleSheet(
            f"color:{settings.selection_chinese_color};font-size:{settings.selection_chinese_px}px"
        )
        self.hiragana.configure(
            settings.selection_kana_px,
            settings.selection_kanji_px,
            settings.selection_kana_color,
            settings.selection_kanji_color,
            settings.selection_show_kanji,
        )
        size = settings.selection_popup_size
        self._saved_size = QSize(size[0], size[1]) if len(size) == 2 else None
        if self.isVisible():
            self._fit(self._available())

    def _available(self) -> QRect:
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)

    def _fit(self, avail: QRect) -> None:
        """有记住的大小就用它，否则按内容自动计算（不超过屏幕的 60%）。"""
        if self._saved_size is not None:
            self.resize(self._saved_size.boundedTo(avail.size()))
            return
        max_w = min(820, int(avail.width() * 0.6))
        max_h = int(avail.height() * 0.6)
        chrome_w = 14 + 4 + 12  # 面板左右边距 + 滚动条预留
        inner_max = max_w - chrome_w
        self.hiragana.set_max_width(inner_max - 4)
        self.body.ensurePolished()
        for label in (self.message, self.chinese, self.reading_title):
            label.ensurePolished()
        natural = max(
            [self.hiragana.width() + 4]
            + [
                label.fontMetrics().horizontalAdvance(label.text())
                for label in (self.message, self.chinese, self.reading_title)
                if label.isVisibleTo(self) and label.text()
            ]
        )
        inner_w = max(240, min(inner_max, natural))
        self.hiragana.set_max_width(inner_w - 4)
        body_h = self.body.heightForWidth(inner_w) if self.body.hasHeightForWidth() else -1
        if body_h <= 0:
            body_h = self.body.sizeHint().height()
        # 直接按各部分尺寸计算；窗口可见时布局的 sizeHint 有缓存，不可靠
        header_h = max(self._title.sizeHint().height(), self.btn_close.sizeHint().height())
        chrome_h = 8 + 4 + header_h + self.grip.sizeHint().height() + 2 * 4 + 2
        width = inner_w + chrome_w + 2
        self.resize(max(300, min(width, max_w)), max(120, min(body_h + chrome_h, max_h)))

    def show_loading(self, anchor: QPoint) -> None:
        self.message.setText("正在翻译…")
        self.message.setStyleSheet("color:#aaa")
        self.message.show()
        self._clear_result()
        self._user_moved = False
        self._show_at(anchor)

    def show_result(self, chinese: str, hiragana: str, anchor: QPoint) -> None:
        self.message.clear()
        self.message.hide()
        self.chinese.setText(f"中文：{chinese}")
        self.reading_title.show()
        self.hiragana.setText(hiragana)
        self._show_at(anchor)

    def _clear_result(self) -> None:
        self.chinese.clear()
        self.reading_title.hide()
        self.hiragana.clear()

    def show_error(self, message: str, anchor: QPoint) -> None:
        self.message.setText(message)
        self.message.setStyleSheet("color:#ff8a8a")
        self.message.show()
        self._clear_result()
        self._show_at(anchor)

    def _show_at(self, anchor: QPoint) -> None:
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else self._available()
        self._fit(avail)
        self.scroll.verticalScrollBar().setValue(0)
        # 用户在等待期间拖动过弹窗时，结果就地更新，不再跳回选区旁
        if self._user_moved and self.isVisible():
            self.raise_()
            return
        if screen is not None:
            target = QRect(anchor, QSize(1, 1))
            self.move(place_beside(target, self.size(), avail))
        else:
            self.move(QCursor.pos())
        self.show()
        self.raise_()


REFERENCE_MODEL = "gpt-6-luna"


def sorted_models(opts: list[ModelOption]) -> tuple[list[ModelOption], str | None]:
    """按估算 AIC 升序（同价按延迟，无价格的排最后），返回 (排序后列表, 推荐模型 id)。"""
    st = stats.get()

    def key(o: ModelOption):
        lat = st.latency(o.id)[0]
        return (o.price is None, o.price or 0, lat is None, lat or 0, o.name)

    ordered = sorted(opts, key=key)
    best = next((o.id for o in ordered if o.enabled and o.price is not None), None)
    return ordered, best


def model_label(o: ModelOption, opts: list[ModelOption], best: str | None) -> str:
    st = stats.get()
    parts = [("★推荐 " if o.id == best else "") + o.name]
    if o.price is not None:
        ref = next((x for x in opts if x.id == REFERENCE_MODEL), None)
        ratio = f"（luna×{o.price / ref.price:.3g}）" if ref and ref.price else ""
        parts.append(f"约 {fmt_aic(st.aic_per_hour(o.price))} AIC/时{ratio}")
    else:
        parts.append("成本未知")
    lat, measured = st.latency(o.id)
    parts.append(f"{'平均' if measured else '约'} {lat:.1f}s" if lat is not None else "未测")
    if not o.enabled:
        parts.append("不可用")
    return "｜".join(parts)


def model_tooltip(o: ModelOption) -> str:
    if o.input_price is None:
        return o.id
    return f"{o.id}\n输入 {o.input_price:g} / 输出 {o.output_price:g} AIC 每百万 token"


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: Settings,
        models: list[ModelOption],
        parent=None,
        on_refresh: Callable[[], None] | None = None,
        on_bench: Callable[[str, str], None] | None = None,
        on_save_selection_history: Callable[[], None] | None = None,
        selection_history_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.s = settings
        self._on_refresh = on_refresh
        self._on_bench = on_bench
        form = QFormLayout(self)

        self.speaker = QComboBox()
        self.speaker.addItem("系统默认", None)
        for n in audio.list_speakers():
            self.speaker.addItem(n, n)
        self._select(self.speaker, settings.loopback_device)

        self.mic = QComboBox()
        self.mic.addItem("不翻译麦克风", "__off__")
        self.mic.addItem("系统默认", None)
        for n in audio.list_microphones():
            self.mic.addItem(n, n)
        self._select(self.mic, settings.mic_device if settings.mic_enabled else "__off__")

        self.direction = QComboBox()
        for d in lang.DIRECTIONS.values():
            note = "（SenseVoice 识别" + ("）" if model_ready("sensevoice") else "，需下载约 230MB）")
            self.direction.addItem(d.label + (note if d.key == lang.ZH_JA else ""), d.key)
        self._select(self.direction, settings.direction)
        self.direction_note = QLabel("")
        self.direction_note.setStyleSheet("color:#888")
        direction_row = QHBoxLayout()
        direction_row.addWidget(self.direction, 1)
        direction_row.addWidget(self.direction_note)

        self.engine = QComboBox()
        for key, label in ENGINES:
            self.engine.addItem(label + ("" if model_ready(key) else "（需下载）"), key)
        self._select(self.engine, settings.asr_engine)

        self.model = QComboBox()
        self.model.setMinimumWidth(460)
        self.btn_refresh = QPushButton("刷新")
        self.btn_bench = QPushButton("测速")
        self.btn_refresh.clicked.connect(self._refresh)
        self.btn_bench.clicked.connect(self._bench)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        model_row.addWidget(self.btn_refresh)
        model_row.addWidget(self.btn_bench)
        self.model_info = QLabel("")
        self.model_info.setWordWrap(True)
        self._models: list[ModelOption] = []
        self.set_models(models)

        self.effort = QComboBox()
        self.effort.addItems(EFFORTS)
        self.effort.setCurrentText(settings.reasoning_effort)

        self.rotate = QSpinBox()
        self.rotate.setRange(5, 240)
        self.rotate.setSuffix(" 分钟")
        self.rotate.setValue(settings.session_rotate_minutes)

        self.asr_correct = QCheckBox("翻译时由 LLM 结合上下文校正识别原文")
        self.asr_correct.setToolTip("同一次请求内完成，不增加请求次数；输出略长，延迟稍增。下一次「开始」时生效")
        self.asr_correct.setChecked(settings.asr_correct)
        self.silence = self._dspin(0.2, 2.0, 0.1, settings.silence_seconds)
        self.hold = self._dspin(0.5, 10.0, 0.5, settings.hold_seconds)
        self.hold_max = self._dspin(5.0, 60.0, 1.0, settings.hold_max_seconds)
        self.fillers = QLineEdit("、".join(settings.fillers))
        self.fillers.setToolTip("单独成句时只显示不翻译，用「、」或逗号分隔")
        self.fillers_zh = QLineEdit("、".join(settings.fillers_zh))
        self.fillers_zh.setToolTip("中→日时使用；单独成句时只显示不翻译，用「、」或逗号分隔")

        self.font = QSpinBox()
        self.font.setRange(9, 48)
        self.font.setValue(settings.font_size)

        self.opacity = QDoubleSpinBox()
        self.opacity.setRange(0.2, 1.0)
        self.opacity.setSingleStep(0.05)
        self.opacity.setValue(settings.opacity)

        self.hotkey = QLineEdit(settings.hotkey)
        self.selection_hotkey = QLineEdit(settings.selection_hotkey)

        self.chinese_size = self._px_spin(settings.selection_chinese_px)
        self.chinese_color = ColorButton(settings.selection_chinese_color)
        chinese_row = QHBoxLayout()
        chinese_row.addWidget(QLabel("字号"))
        chinese_row.addWidget(self.chinese_size)
        chinese_row.addWidget(QLabel("颜色"))
        chinese_row.addWidget(self.chinese_color)
        chinese_row.addStretch(1)
        self.kana_size = self._px_spin(settings.selection_kana_px)
        self.kana_color = ColorButton(settings.selection_kana_color)
        kana_row = QHBoxLayout()
        kana_row.addWidget(QLabel("字号"))
        kana_row.addWidget(self.kana_size)
        kana_row.addWidget(QLabel("颜色"))
        kana_row.addWidget(self.kana_color)
        kana_row.addStretch(1)
        self.kanji_size = self._px_spin(settings.selection_kanji_px)
        self.kanji_color = ColorButton(settings.selection_kanji_color)
        self.show_kanji = QCheckBox("显示汉字（取消则只显示假名）")
        self.show_kanji.setChecked(settings.selection_show_kanji)
        self.show_kanji.toggled.connect(self._sync_kanji_enabled)
        kanji_row = QHBoxLayout()
        kanji_row.addWidget(QLabel("字号"))
        kanji_row.addWidget(self.kanji_size)
        kanji_row.addWidget(QLabel("颜色"))
        kanji_row.addWidget(self.kanji_color)
        kanji_row.addWidget(self.show_kanji)
        kanji_row.addStretch(1)

        self.show_remaining = QCheckBox("账户剩余 %（低于 10% 时总会以红色显示）")
        self.show_remaining.setChecked(settings.show_quota_remaining)
        self.show_used = QCheckBox("账户已用 AIC（本计费周期）")
        self.show_used.setChecked(settings.show_quota_used)
        self.show_meeting = QCheckBox("本场 AIC")
        self.show_meeting.setChecked(settings.show_meeting_aic)
        display_box = QVBoxLayout()
        for w in (self.show_remaining, self.show_used, self.show_meeting):
            display_box.addWidget(w)

        self.btn_save_selection_history = QPushButton("保存划词/句翻译历史")
        if on_save_selection_history is not None:
            self.btn_save_selection_history.clicked.connect(on_save_selection_history)
        else:
            self.btn_save_selection_history.setEnabled(False)
        self.selection_history_path = QLineEdit(str(selection_history_path or ""))
        self.selection_history_path.setReadOnly(True)
        self.selection_history_path.setToolTip(str(selection_history_path or ""))
        history_row = QHBoxLayout()
        history_row.addWidget(self.btn_save_selection_history)
        history_row.addWidget(self.selection_history_path, 1)
        self.btn_open_selection_history = QPushButton("打开")
        self.btn_open_selection_history.setToolTip("用系统默认程序打开历史文件")
        self.btn_open_selection_history.clicked.connect(self._open_selection_history)
        history_row.addWidget(self.btn_open_selection_history)

        self.save_dir = QLineEdit(settings.save_dir or "")
        self.save_dir.setReadOnly(True)
        self.save_dir.setPlaceholderText("未设置（首次结束会议时询问）")
        btn_browse = QPushButton("选择…")
        btn_browse.clicked.connect(self._browse_save_dir)
        btn_reset_dir = QPushButton("清除")
        btn_reset_dir.setToolTip("清除后，下次结束会议时重新询问保存位置")
        btn_reset_dir.clicked.connect(self.save_dir.clear)
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_dir, 1)
        save_row.addWidget(btn_browse)
        save_row.addWidget(btn_reset_dir)

        form.addRow("对方声音（扬声器环回）", self.speaker)
        form.addRow("我的声音（麦克风）", self.mic)
        form.addRow("翻译方向", direction_row)
        form.addRow("语音识别引擎（日→中）", self.engine)
        form.addRow("识别纠错", self.asr_correct)
        form.addRow("翻译模型", model_row)
        form.addRow("", self.model_info)
        form.addRow("推理强度", self.effort)
        form.addRow("会话轮换间隔", self.rotate)
        form.addRow("断句：静音判定", self.silence)
        form.addRow("断句：未说完最多等待", self.hold)
        form.addRow("断句：合并上限", self.hold_max)
        form.addRow("语气词·日语（不翻译）", self.fillers)
        form.addRow("语气词·中文（不翻译）", self.fillers_zh)
        form.addRow("译文字号", self.font)
        form.addRow("背景不透明度", self.opacity)
        form.addRow("开始/暂停快捷键", self.hotkey)
        form.addRow("划词/句翻译快捷键", self.selection_hotkey)
        form.addRow("划词/句翻译：中文译文", chinese_row)
        form.addRow("划词/句翻译：假名", kana_row)
        form.addRow("划词/句翻译：汉字", kanji_row)
        form.addRow("显示设置：悬浮窗显示", display_box)
        form.addRow("翻译历史", history_row)
        form.addRow("会议记录保存位置", save_row)
        form.addRow(
            QLabel(
                "<small>模型与推理强度、麦克风开关在会议中修改会立即生效；"
                "设备、翻译方向、识别引擎、识别纠错、静音判定在下一次「开始」时生效。</small>"
            )
        )
        self.dirty_hint = QLabel("")
        self.dirty_hint.setStyleSheet("color:#d9822b")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        button_row = QHBoxLayout()
        button_row.addWidget(self.dirty_hint, 1, Qt.AlignmentFlag.AlignRight)
        button_row.addWidget(buttons)
        form.addRow(button_row)

        self._getters: dict[str, Callable[[], object]] = {
            "speaker": self.speaker.currentData,
            "mic": self.mic.currentData,
            "direction": self.direction.currentData,
            "engine": self.engine.currentData,
            "asr_correct": self.asr_correct.isChecked,
            "model": lambda: self.model.currentData() or self.s.model,
            "effort": self.effort.currentText,
            "rotate": self.rotate.value,
            "silence": self.silence.value,
            "hold": self.hold.value,
            "hold_max": self.hold_max.value,
            "fillers": self.fillers.text,
            "fillers_zh": self.fillers_zh.text,
            "font": self.font.value,
            "opacity": self.opacity.value,
            "hotkey": self.hotkey.text,
            "selection_hotkey": self.selection_hotkey.text,
            "chinese_size": self.chinese_size.value,
            "chinese_color": self.chinese_color.color,
            "kana_size": self.kana_size.value,
            "kanji_size": self.kanji_size.value,
            "kana_color": self.kana_color.color,
            "kanji_color": self.kanji_color.color,
            "show_kanji": self.show_kanji.isChecked,
            "show_remaining": self.show_remaining.isChecked,
            "show_used": self.show_used.isChecked,
            "show_meeting": self.show_meeting.isChecked,
            "save_dir": self.save_dir.text,
        }
        self._baseline = {k: f() for k, f in self._getters.items()}
        for w in (self.speaker, self.mic, self.direction, self.engine, self.model, self.effort):
            w.currentIndexChanged.connect(self._update_dirty)
        for w in (
            self.rotate, self.silence, self.hold, self.hold_max, self.font, self.opacity,
            self.chinese_size, self.kana_size, self.kanji_size,
        ):
            w.valueChanged.connect(self._update_dirty)
        for w in (self.chinese_color, self.kana_color, self.kanji_color):
            w.colorChanged.connect(self._update_dirty)
        for w in (self.fillers, self.fillers_zh, self.hotkey, self.selection_hotkey, self.save_dir):
            w.textChanged.connect(self._update_dirty)
        for w in (self.show_remaining, self.show_used, self.show_meeting, self.asr_correct, self.show_kanji):
            w.toggled.connect(self._update_dirty)
        self.direction.currentIndexChanged.connect(self._sync_engine_enabled)
        self._meeting_active = False
        self._sync_engine_enabled()
        self._sync_kanji_enabled()

    def is_dirty(self) -> bool:
        return any(f() != self._baseline[k] for k, f in self._getters.items())

    def _update_dirty(self) -> None:
        self.dirty_hint.setText("设置已修改，点击 OK 后生效" if self.is_dirty() else "")

    def _sync_engine_enabled(self) -> None:
        self.engine.setEnabled(self.direction.currentData() == lang.JA_ZH)
        self.engine.setToolTip("" if self.engine.isEnabled() else "中→日固定使用 SenseVoice 识别中文")

    def _sync_kanji_enabled(self) -> None:
        on = self.show_kanji.isChecked()
        self.kanji_size.setEnabled(on)
        self.kanji_color.setEnabled(on)

    def set_meeting_active(self, active: bool) -> None:
        """会议进行中锁定翻译方向。"""
        self._meeting_active = active
        if active:
            self._select(self.direction, self.s.direction)
        self.direction.setEnabled(not active)
        self.direction_note.setText("会议进行中，结束后可切换" if active else "")

    def sync_mic(self, on: bool) -> None:
        """悬浮窗按钮开关麦克风后同步下拉框，且不算作对话框里的修改。"""
        value = (self.s.mic_device if on else "__off__")
        self.mic.blockSignals(True)
        self._select(self.mic, value)
        self.mic.blockSignals(False)
        self._baseline["mic"] = self.mic.currentData()
        self._update_dirty()

    @staticmethod
    def _px_spin(value: int) -> QSpinBox:
        w = QSpinBox()
        w.setRange(8, 72)
        w.setSuffix(" px")
        w.setValue(value)
        return w

    @staticmethod
    def _dspin(lo: float, hi: float, step: float, value: float) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setDecimals(1)
        w.setSuffix(" 秒")
        w.setValue(value)
        return w

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _open_selection_history(self) -> None:
        path = Path(self.selection_history_path.text())
        if not path.is_file():
            QMessageBox.information(self, "翻译历史", "历史文件尚未创建，请先点击「保存划词/句翻译历史」。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "翻译历史", f"无法打开文件：\n{path}")

    def _browse_save_dir(self) -> None:
        start = self.save_dir.text() or str(Path.home() / "Documents")
        d = QFileDialog.getExistingDirectory(self, "选择会议记录保存位置", start)
        if d:
            self.save_dir.setText(str(Path(d)))

    def set_models(self, models: list[ModelOption]) -> None:
        current = self.model.currentData() or self.s.model
        self._models = models
        ordered, best = sorted_models(models)
        self.model.clear()
        for o in ordered:
            self.model.addItem(model_label(o, models, best), o.id)
            self.model.setItemData(self.model.count() - 1, model_tooltip(o), Qt.ItemDataRole.ToolTipRole)
        if self.model.findData(current) < 0:
            self.model.addItem(current, current)
        self._select(self.model, current)
        self.model_info.setText(
            "<small>成本按本机历史会议估算（无历史时按每小时 600 句估算）；"
            "延迟：「平均」为本机实测，「约」为内置参考值。</small>"
            if models
            else "<small>正在获取模型列表…</small>"
        )

    def _refresh(self) -> None:
        if self._on_refresh:
            self.model_info.setText("<small>正在刷新模型列表…</small>")
            self._on_refresh()

    def _bench(self) -> None:
        model = self.model.currentData()
        if self._on_bench and model:
            self.btn_bench.setEnabled(False)
            self.model_info.setText(f"<small>正在测速 {model}（预热后测 3 句，约 15 秒）…</small>")
            self._on_bench(model, self.effort.currentText())

    def bench_done(self, model: str, result) -> None:
        self.btn_bench.setEnabled(True)
        if isinstance(result, Exception):
            self.model_info.setText(f"<small style='color:#c33'>测速失败：{html.escape(str(result))}</small>")
            return
        self.set_models(self._models)
        self.model_info.setText(f"<small>{html.escape(model)} 实测平均 {result:.2f}s（已记入统计）</small>")

    def apply(self) -> None:
        s = self.s
        s.loopback_device = self.speaker.currentData()
        mic = self.mic.currentData()
        s.mic_enabled = mic != "__off__"
        if s.mic_enabled:
            s.mic_device = mic  # 关闭时保留设备选择，悬浮窗按钮重新开启时沿用
        s.asr_engine = self.engine.currentData()
        s.asr_correct = self.asr_correct.isChecked()
        if not self._meeting_active:
            s.direction = self.direction.currentData()
        s.model = self.model.currentData() or s.model
        s.reasoning_effort = self.effort.currentText()
        s.session_rotate_minutes = self.rotate.value()
        s.silence_seconds = self.silence.value()
        s.hold_seconds = self.hold.value()
        s.hold_max_seconds = max(self.hold_max.value(), s.hold_seconds)
        s.fillers = [f.strip() for f in re.split(r"[、,，\s]+", self.fillers.text()) if f.strip()]
        s.fillers_zh = [f.strip() for f in re.split(r"[、,，\s]+", self.fillers_zh.text()) if f.strip()]
        s.font_size = self.font.value()
        s.opacity = self.opacity.value()
        s.hotkey = self.hotkey.text().strip() or s.hotkey
        s.selection_hotkey = self.selection_hotkey.text().strip() or s.selection_hotkey
        s.selection_chinese_px = self.chinese_size.value()
        s.selection_chinese_color = self.chinese_color.color()
        s.selection_kana_px = self.kana_size.value()
        s.selection_kanji_px = self.kanji_size.value()
        s.selection_kana_color = self.kana_color.color()
        s.selection_kanji_color = self.kanji_color.color()
        s.selection_show_kanji = self.show_kanji.isChecked()
        s.show_quota_remaining = self.show_remaining.isChecked()
        s.show_quota_used = self.show_used.isChecked()
        s.show_meeting_aic = self.show_meeting.isChecked()
        s.save_dir = self.save_dir.text() or None
        s.save()


class LoginDialog(QDialog):
    token_submitted = Signal(str)
    recheck_requested = Signal()

    def __init__(self, message: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("登录 GitHub Copilot")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"未检测到 Copilot 登录状态。<br><small>{html.escape(message)}</small>"))

        lay.addWidget(QLabel("<b>方式一：使用 Copilot CLI 登录（推荐）</b>"))
        cli = shutil.which("copilot")
        btn_cli = QPushButton("打开控制台执行 copilot login" if cli else "未找到 copilot CLI（请先安装）")
        btn_cli.setEnabled(bool(cli))
        btn_cli.clicked.connect(self._run_cli_login)
        btn_recheck = QPushButton("我已完成登录，重新检测")
        btn_recheck.clicked.connect(self.recheck_requested)
        lay.addWidget(btn_cli)
        lay.addWidget(btn_recheck)

        lay.addWidget(
            QLabel(
                "<b>方式二：粘贴 fine-grained Personal Access Token</b><br>"
                "<small>需要 <i>Copilot Requests</i> 权限。"
                "<a href='https://github.com/settings/personal-access-tokens/new'>创建 Token</a></small>"
            )
        )
        lay.itemAt(lay.count() - 1).widget().setOpenExternalLinks(True)
        row = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("github_pat_…")
        btn_token = QPushButton("使用 Token 登录")
        btn_token.clicked.connect(lambda: self.token_submitted.emit(self.token.text()))
        row.addWidget(self.token, 1)
        row.addWidget(btn_token)
        lay.addLayout(row)
        self.info = QLabel("")
        lay.addWidget(self.info)

    def _run_cli_login(self) -> None:
        subprocess.Popen(["cmd", "/k", "copilot login"], creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.info.setText("请在弹出的控制台中完成登录，然后点击「重新检测」。")


class _Downloader(QThread):
    progress = Signal(int, str)
    done = Signal(str)  # 空字符串 = 成功，否则为错误信息

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        spec = downloadable(self.name)
        assert spec is not None
        repo, _, files = spec
        target = model_target_dir(self.name)
        target.mkdir(parents=True, exist_ok=True)
        try:
            for name in files:
                dest = target / name
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                # hf-mirror 会对 Python 默认 User-Agent 的重定向请求返回 403
                req = urllib.request.Request(
                    f"{HF_BASE}/{repo}/resolve/main/{name}", headers={"User-Agent": "meeting-translate"}
                )
                with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                    total = int(r.headers.get("Content-Length", 0)) or 1
                    got = 0
                    while chunk := r.read(1 << 20):
                        if self._cancel:
                            raise InterruptedError("已取消")
                        f.write(chunk)
                        got += len(chunk)
                        self.progress.emit(min(100, int(got * 100 / total)), name)
                tmp.replace(dest)
            self.done.emit("")
        except Exception as e:
            self.done.emit(str(e))


MODEL_LABELS = {
    "whisper-small": "Whisper small",
    "whisper-medium": "Whisper medium",
    "sensevoice": "SenseVoice（中文识别）",
}


def download_model(name: str, parent: QWidget | None, on_done: Callable[[bool], None]) -> None:
    label = MODEL_LABELS.get(name, name)
    dlg = QProgressDialog(f"正在下载 {label} 模型…", "取消", 0, 100, parent)
    dlg.setWindowTitle("下载模型")
    dlg.setMinimumDuration(0)
    th = _Downloader(name)
    th.progress.connect(lambda p, n: (dlg.setValue(p), dlg.setLabelText(f"正在下载 {n}")))
    dlg.canceled.connect(th.cancel)

    def finished(err: str) -> None:
        dlg.close()
        if err:
            QMessageBox.warning(parent, "下载失败", err)
        on_done(not err)

    th.done.connect(finished)
    th.start()
    dlg._thread = th  # 保持引用


class Bridge(QObject):
    """后台线程 → Qt 主线程的信号桥。"""

    partial = Signal(str, str)
    final = Signal(object)
    translated = Signal(object)
    status = Signal(str)
    auth = Signal(object)
    hotkey = Signal()
    selection_hotkey = Signal()
    usage = Signal(float)
    progress = Signal(str, int, int)  # 会后整理：(阶段说明, 已完成步数, 总步数)
    job_done = Signal(object, object)  # (tag, result 或 Exception)；tag 可为 ("cb", 回调)


def open_path(p: Path) -> None:
    import os

    os.startfile(p)  # type: ignore[attr-defined]
