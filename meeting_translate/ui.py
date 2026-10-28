"""PySide6 界面：置顶悬浮字幕窗、设置对话框、登录对话框、模型下载。"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import threading
import urllib.request

import httpx
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
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
    QFontComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import audio, duplex_audio, lang, secrets, stats
from . import message_dialog as msg
from .asr import downloadable, model_ready, model_target_dir
from .config import HF_BASE, Settings
from .mic_test import MicTestWidget, SpeakerTestWidget
from .openai_backend import normalize_base_url
from .transcript import Entry, fmt_aic
from .translator import ModelOption

MAX_VISIBLE_ENTRIES = 40
ENGINES = [
    ("sensevoice", "SenseVoice（内置）"),
    ("reazon", "ReazonSpeech（内置，支持词表）"),
    ("whisper-small", "Whisper small"),
    ("whisper-medium", "Whisper medium"),
]
EFFORTS = ["none", "low", "medium", "high", "xhigh", "max", "default"]


def effort_choices(opt: ModelOption | None) -> list[str]:
    """模型声明的思考档位；没有声明时回退到通用列表。"""
    if opt is None:
        return list(EFFORTS)
    values: list[str] = []
    if opt.can_disable_thinking:
        values.append("none")
    for value in opt.efforts:
        if value and value not in values:
            values.append(value)
    if not values:
        return list(EFFORTS)
    if "default" not in values:
        values.append("default")
    return values


SPEAKER_COLORS = {audio.THEM: "#7fd1ff", audio.ME: "#ffd27f"}
BUTTON_STYLE = (
    "QPushButton{color:#eee;background:rgba(255,255,255,30);border:none;padding:5px 11px;border-radius:4px}"
    "QPushButton:hover{background:rgba(255,255,255,70)}QPushButton:disabled{color:#777}"
)
MIC_ON_STYLE = (
    "QPushButton{color:white;background:rgba(46,160,67,200);border:none;padding:5px 11px;border-radius:4px}"
    "QPushButton:hover{background:rgba(46,160,67,255)}"
)
DIALOG_GAP = 8
EDGE_DOUBLE_CLICK = 16  # 双击窗口边缘多少像素内切换全屏


def flexible_combo(combo: QComboBox, chars: int = 12) -> QComboBox:
    """下拉框的最小宽度按固定字符数计算。

    QComboBox 默认的最小宽度取最长条目，很长的设备名/模型名会把 QFormLayout
    的整行挤到下一行，字段就和其它行对不齐了。
    """
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    return combo


class ElidedLabel(QLabel):
    """文字过长时省略显示，不把父窗口的最小宽度撑大。

    顶栏状态/延迟/用量都是单行 QLabel；默认 QLabel 的 minimumSizeHint()
    等于整段文字宽度，状态里一旦带上长文件路径，悬浮窗就再也缩不回去。
    这里保留完整文本到 tooltip，只在绘制时按当前宽度省略。
    """

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__("", parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._apply_elide()
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        margins = self.contentsMargins()
        return QSize(
            fm.horizontalAdvance(self._full_text) + margins.left() + margins.right(),
            fm.height() + margins.top() + margins.bottom(),
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, self.sizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = max(0, self.contentsRect().width())
        elided = self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, width)
        if QLabel.text(self) != elided:
            QLabel.setText(self, elided)


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

    def clamp_point(x: int, y: int) -> QPoint:
        # 锚点可能在屏幕外（比如悬浮窗被拖到了拔掉的显示器上），
        # 算出来的位置必须收回屏幕内，否则窗口会整个消失。
        return QPoint(
            clamp(x, screen.left(), screen.right() - w + 1),
            clamp(y, screen.top(), screen.bottom() - h + 1),
        )

    if not fitting:
        return QPoint(screen.left() + max(0, (screen.width() - w) // 2), screen.top() + max(0, (screen.height() - h) // 2))
    side = max(fitting, key=lambda k: spaces[k] - need[k])
    if side in ("right", "left"):
        x = anchor.right() + gap + 1 if side == "right" else anchor.left() - gap - w
        y = clamp(anchor.top(), screen.top(), screen.bottom() - h + 1)
    else:
        y = anchor.bottom() + gap + 1 if side == "below" else anchor.top() - gap - h
        x = clamp(anchor.left(), screen.left(), screen.right() - w + 1)
    return clamp_point(x, y)


class CornerGrip(QWidget):
    """悬浮窗四角的缩放手柄；拖动时从对应的角改变窗口几何。"""

    def __init__(self, overlay: "Overlay", corner: str) -> None:
        super().__init__(overlay)
        self._overlay = overlay
        self._corner = corner
        self._start_global: QPoint | None = None
        self._start_geometry = QRect()
        self.setFixedSize(12, 12)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(
            Qt.CursorShape.SizeFDiagCursor
            if corner in ("tl", "br")
            else Qt.CursorShape.SizeBDiagCursor
        )
        self.setToolTip("拖动调整窗口大小")

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._start_global = e.globalPosition().toPoint()
            self._start_geometry = self._overlay.geometry()
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._start_global is None or not (e.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(e)
            return
        delta = e.globalPosition().toPoint() - self._start_global
        left, top = self._start_geometry.left(), self._start_geometry.top()
        right, bottom = self._start_geometry.right(), self._start_geometry.bottom()
        if "l" in self._corner:
            left += delta.x()
        if "r" in self._corner:
            right += delta.x()
        if "t" in self._corner:
            top += delta.y()
        if "b" in self._corner:
            bottom += delta.y()
        minimum = self._overlay.minimumSizeHint()
        min_w, min_h = max(1, minimum.width()), max(1, minimum.height())
        if right - left + 1 < min_w:
            if "l" in self._corner:
                left = right - min_w + 1
            else:
                right = left + min_w - 1
        if bottom - top + 1 < min_h:
            if "t" in self._corner:
                top = bottom - min_h + 1
            else:
                bottom = top + min_h - 1
        self._overlay.setGeometry(QRect(QPoint(left, top), QPoint(right, bottom)))
        e.accept()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._start_global = None
        self._overlay._emit_geometry()
        e.accept()


class Overlay(QWidget):
    toggle_clicked = Signal()
    stop_clicked = Signal()
    home_clicked = Signal()
    settings_clicked = Signal()
    mic_toggled = Signal(bool)
    retry_clicked = Signal()
    geometry_changed = Signal(list)
    zh_ja_toggled = Signal(bool)
    ja_zh_toggled = Signal(bool)
    preview_ja_clicked = Signal()

    def __init__(self, settings: Settings) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("会议翻译")
        self._opacity = settings.opacity
        self._src_size = settings.source_font_size
        self._src_color = settings.source_font_color or "#ffffff"
        self._font_size = settings.font_size
        self._font_color = settings.font_color or "#ffffff"
        self._subtitle_mode = settings.subtitle_mode
        self._drag: QPoint | None = None
        self._fullscreen = False
        self._normal_geometry: QRect | None = None
        self._progress_token = 0
        self.entries: list[Entry] = []
        self.partials: dict[str, str] = {}

        self.status = ElidedLabel("启动中…")
        self.status.setMinimumWidth(80)  # 再窄也保留一点状态提示，但不让它撑住窗口
        self.latency = ElidedLabel("")
        self.usage = ElidedLabel("")
        self.btn_mic = QPushButton()
        self.btn_mic.setCheckable(True)
        self.btn_toggle = QPushButton("▶ 开始")
        self.btn_stop = QPushButton("■ 结束")
        self.btn_home = QPushButton("主界面")
        self.btn_home.setToolTip("返回主页面，并把悬浮框最小化")
        self.btn_settings = QPushButton("⚙")
        self.btn_hide = QPushButton("—")
        self.chk_zh_ja = QCheckBox("中转日")
        self.chk_zh_ja.setToolTip("开启：我的中文→日语 TTS 给对方；关闭：麦克风原声直接送到对方")
        self.chk_ja_zh = QCheckBox("日转中")
        self.chk_ja_zh.setToolTip("开启：对方日语→中文 TTS 给我；关闭：对方原声直接送到我的耳机")
        self.btn_preview_ja = QPushButton("试听日语")
        self.btn_preview_ja.setToolTip("把最近一次「中转日」生成的日语播到本机耳机，用来随时检查效果（不会发给对方）")
        for chk in (self.chk_zh_ja, self.chk_ja_zh):
            chk.setStyleSheet("QCheckBox{color:#ddd;font-size:12px;spacing:4px;} QCheckBox::indicator{width:14px;height:14px;}")
        self.btn_stop.setEnabled(False)
        for w in (self.status, self.latency, self.usage):
            w.setStyleSheet("color:#ddd;")
        for b in (
            self.btn_mic,
            self.btn_toggle,
            self.btn_stop,
            self.btn_home,
            self.btn_settings,
            self.btn_hide,
            self.btn_preview_ja,
        ):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setStyleSheet(BUTTON_STYLE)
        for chk in (self.chk_zh_ja, self.chk_ja_zh):
            chk.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.set_mic(settings.mic_enabled)
        self.btn_mic.toggled.connect(self._on_mic_toggled)
        self.btn_toggle.clicked.connect(self.toggle_clicked)
        self.btn_stop.clicked.connect(self.stop_clicked)
        self.btn_home.clicked.connect(self.home_clicked)
        self.btn_settings.clicked.connect(self.settings_clicked)
        self.btn_hide.clicked.connect(self.hide)
        self.chk_zh_ja.toggled.connect(self.zh_ja_toggled.emit)
        self.chk_ja_zh.toggled.connect(self.ja_zh_toggled.emit)
        self.btn_preview_ja.clicked.connect(self.preview_ja_clicked)

        bar = QHBoxLayout()
        bar.addWidget(self.status, 1)
        bar.addWidget(self.latency)
        bar.addWidget(self.usage)
        for b in (
            self.btn_mic,
            self.btn_toggle,
            self.btn_stop,
            self.btn_home,
            self.btn_settings,
            self.btn_hide,
        ):
            bar.addWidget(b)

        self.duplex_bar = QWidget()
        drow = QHBoxLayout(self.duplex_bar)
        drow.setContentsMargins(2, 0, 2, 0)
        drow.setSpacing(7)
        drow.addWidget(self.chk_zh_ja)
        drow.addWidget(self.chk_ja_zh)
        drow.addWidget(self.btn_preview_ja)
        drow.addStretch(1)
        self.duplex_bar.hide()

        self.duplex_remote_now = ElidedLabel("")
        self.duplex_local_now = ElidedLabel("")
        for label in (self.duplex_remote_now, self.duplex_local_now):
            label.setStyleSheet("color:#9fc3e8;font-size:11px;")
        self.duplex_now_box = QWidget()
        nrow = QVBoxLayout(self.duplex_now_box)
        nrow.setContentsMargins(2, 0, 2, 0)
        nrow.setSpacing(1)
        nrow.addWidget(self.duplex_remote_now)
        nrow.addWidget(self.duplex_local_now)
        self.duplex_now_box.hide()

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
        for w in (self.status, self.latency, self.usage, self.view.viewport()):
            w.installEventFilter(self)
        # 自动跟随最新内容；用户往上翻时暂停跟随，拖回底部或点「回到底部」恢复
        self._follow = True
        self._rendering = False
        self._frozen_start = 0
        self._last_html: str | None = None
        sb = self.view.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll_value)
        sb.rangeChanged.connect(self._on_scroll_range)
        self.btn_bottom = QPushButton("↓ 回到底部", self.view.viewport())
        self.btn_bottom.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_bottom.setStyleSheet(
            "QPushButton{background:rgba(91,140,255,220);color:white;border:none;"
            "border-radius:11px;padding:3px 10px;font-size:12px;font-weight:bold;}"
            "QPushButton:hover{background:rgba(120,162,255,240);}"
        )
        self.btn_bottom.adjustSize()
        self.btn_bottom.clicked.connect(self.scroll_to_bottom)
        self.btn_bottom.hide()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 2)
        lay.addLayout(bar)
        lay.addWidget(self.duplex_bar)
        lay.addWidget(self.duplex_now_box)
        lay.addWidget(self.progress)
        lay.addWidget(self.view, 1)

        self._corner_grips = {name: CornerGrip(self, name) for name in ("tl", "tr", "bl", "br")}
        self.setGeometry(*settings.window_geometry)
        self.ensure_on_screen()
        self.apply_style(settings)

    def ensure_on_screen(self) -> None:
        """显示器拔插/分辨率变化后，别把窗口留在屏幕外。"""
        rect = self.frameGeometry()
        for screen in QGuiApplication.screens():
            hit = rect.intersected(screen.availableGeometry())
            if hit.width() >= 120 and hit.height() >= 60:
                return
        primary = QGuiApplication.primaryScreen()
        if primary is None:
            return
        area = primary.availableGeometry()
        self.move(
            max(area.left(), area.right() - self.width() - 24),
            max(area.top(), area.bottom() - self.height() - 60),
        )

    def apply_style(self, settings: Settings) -> None:
        self._opacity = settings.opacity
        self._src_size = settings.source_font_size
        self._src_color = settings.source_font_color or "#ffffff"
        self._font_size = settings.font_size
        self._font_color = settings.font_color or "#ffffff"
        self._subtitle_mode = settings.subtitle_mode
        # 基础字体跟随译文；原文/译文的实际大小与颜色由 HTML span 各自指定
        self.view.setFont(QFont(settings.font_family or "Microsoft YaHei UI", settings.font_size))
        self.view.setStyleSheet(f"background:transparent;color:{self._font_color};")
        self.update()
        self._last_html = None
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
        self._place_corner_grips()
        if not self._fullscreen:  # 全屏尺寸不写回配置，退出时恢复原窗口
            self._emit_geometry()

    def _place_corner_grips(self) -> None:
        if not hasattr(self, "_corner_grips"):
            return
        size = 12
        w, h = self.width(), self.height()
        positions = {"tl": (0, 0), "tr": (w - size, 0), "bl": (0, h - size), "br": (w - size, h - size)}
        for name, grip in self._corner_grips.items():
            grip.move(*positions[name])
            grip.raise_()

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton and self._is_edge_point(e.position().toPoint()):
            self.toggle_fullscreen()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.view.viewport() and event.type() == QEvent.Type.Resize:
            self._place_bottom_button()
        if (
            event.type() == QEvent.Type.MouseButtonDblClick
            and isinstance(obj, QWidget)
            and self.isAncestorOf(obj)
            and self._is_edge_point(obj.mapTo(self, event.position().toPoint()))
        ):
            self.toggle_fullscreen()
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def _is_edge_point(self, pos: QPoint) -> bool:
        return (
            pos.x() <= EDGE_DOUBLE_CLICK
            or pos.y() <= EDGE_DOUBLE_CLICK
            or pos.x() >= self.width() - 1 - EDGE_DOUBLE_CLICK
            or pos.y() >= self.height() - 1 - EDGE_DOUBLE_CLICK
        )

    def toggle_fullscreen(self) -> None:
        """双击边缘在全屏和原来的窗口大小之间切换。"""
        if self.isFullScreen():
            self._fullscreen = False
            self.showNormal()
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)
            self._normal_geometry = None
            self.ensure_on_screen()
            for grip in self._corner_grips.values():
                grip.show()
        else:
            self._normal_geometry = self.geometry()
            self._fullscreen = True
            self.showFullScreen()
            for grip in self._corner_grips.values():
                grip.hide()

    def _emit_geometry(self) -> None:
        g = self.geometry()
        self.geometry_changed.emit([g.x(), g.y(), g.width(), g.height()])

    def set_running(self, running: bool, active: bool) -> None:
        """running: 正在采集；active: 有进行中的会议（含暂停）。"""
        self.btn_toggle.setText("⏸ 暂停" if running else ("▶ 继续" if active else "▶ 开始"))
        self.btn_stop.setEnabled(active)
        # 会议进行中（含暂停）不允许从悬浮框回主界面；否则会在后台继续采集，
        # 主界面还会因为“正在会话”拒绝切换入口。只能先结束会议。
        self.btn_home.setEnabled(not active)
        self.btn_home.setToolTip(
            "翻译进行中，请先结束会议再回主界面"
            if active
            else "返回主页面，并把悬浮框最小化"
        )

    def set_duplex(self, enabled: bool, zh_ja: bool = True, ja_zh: bool = True) -> None:
        """显示/隐藏双向同传开关，并同步当前状态。"""
        self.chk_zh_ja.blockSignals(True)
        self.chk_ja_zh.blockSignals(True)
        self.chk_zh_ja.setChecked(bool(zh_ja))
        self.chk_ja_zh.setChecked(bool(ja_zh))
        self.chk_zh_ja.blockSignals(False)
        self.chk_ja_zh.blockSignals(False)
        self.duplex_bar.setVisible(bool(enabled))
        self.duplex_now_box.setVisible(bool(enabled))
        if not enabled:
            self.duplex_remote_now.setText("")
            self.duplex_local_now.setText("")

    def set_duplex_play(self, direction: str, text: str, state: str) -> None:
        """显示对方/本机实际开始播放的句子；state=start|done|direct|clear。"""
        remote = direction == lang.ZH_JA
        label = self.duplex_remote_now if remote else self.duplex_local_now
        who = "对方" if remote else "你"
        if state == "direct":
            label.setText(f"{who}听到：原声直通")
            label.setToolTip("当前方向未开启翻译，正在直接输出原始音频")
        elif state == "clear":
            label.setText("")
            label.setToolTip("")
        elif state == "done":
            label.setText(f"{who}刚听完：{text}")
            label.setToolTip(text)
        elif state == "start":
            label.setText(f"{who}听到：{text}")
            label.setToolTip(text)

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
        self._set_follow(True)
        self.render()

    # ---- 滚动跟随 ----

    def _place_bottom_button(self) -> None:
        vp = self.view.viewport()
        self.btn_bottom.move(max(0, vp.width() - self.btn_bottom.width() - 8), 6)

    def _set_follow(self, follow: bool) -> None:
        if follow == self._follow:
            return
        self._follow = follow
        if not follow:
            # 冻结可见范围的起点：翻看历史时不让顶部条目被裁掉、内容整体上跳
            self._frozen_start = max(0, len(self.entries) - MAX_VISIBLE_ENTRIES)
        self.btn_bottom.setVisible(not follow)
        if not follow:
            self._place_bottom_button()
            self.btn_bottom.raise_()

    def _on_scroll_value(self, value: int) -> None:
        if self._rendering:
            return
        sb = self.view.verticalScrollBar()
        self._set_follow(value >= sb.maximum() - 4)

    def _on_scroll_range(self, _minimum: int, maximum: int) -> None:
        if self._follow:
            prev, self._rendering = self._rendering, True
            self.view.verticalScrollBar().setValue(maximum)
            self._rendering = prev

    def scroll_to_bottom(self) -> None:
        self._set_follow(True)
        self.render()
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _visible_entries(self) -> list:
        if self._follow:
            return self.entries[-MAX_VISIBLE_ENTRIES:]
        return self.entries[min(self._frozen_start, len(self.entries)) :]

    def render(self) -> None:
        src_style = f"color:{self._src_color};font-size:{self._src_size}pt"
        zh_style = f"color:{self._font_color};font-size:{self._font_size}pt"
        rows = []
        if self._subtitle_mode:
            # 视频字幕：只保留已有译文，不显示原文、说话方标签和未完成的识别片段。
            for e in self._visible_entries():
                text = (e.zh or "").strip()
                if text:
                    rows.append(
                        f"<div style='margin-bottom:6px'>"
                        f"<span style='{zh_style}'>{html.escape(text)}</span></div>"
                    )
        else:
            for e in self._visible_entries():
                ja = f"<span style='{src_style}'>【{e.source}】{html.escape(e.ja)}</span>"
                if e.zh == "":  # 语气词：只显示不翻译
                    rows.append(f"<div style='margin-bottom:6px'>{ja}</div>")
                    continue
                zh = html.escape(e.zh) if e.zh else "<span style='color:#888'>…</span>"
                rows.append(
                    f"<div style='margin-bottom:6px'>{ja}<br>"
                    f"<span style='{zh_style}'>{zh}</span></div>"
                )
            for src, text in self.partials.items():
                if text:
                    rows.append(
                        f"<div style='{src_style}'><i>【{src}】{html.escape(text)} …</i></div>"
                    )
        html_text = "".join(rows)
        if html_text == self._last_html:
            return
        self._last_html = html_text
        sb = self.view.verticalScrollBar()
        keep = sb.value()
        # setHtml 会先把滚动条打回顶部：整段关掉重绘，避免「顶部→底部」来回跳造成闪烁
        self._rendering = True
        self.view.setUpdatesEnabled(False)
        try:
            self.view.setHtml(html_text)
            sb.setValue(sb.maximum() if self._follow else min(keep, sb.maximum()))
        finally:
            self.view.setUpdatesEnabled(True)
            self._rendering = False


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

    def configure(
        self,
        kana_px: int,
        kanji_px: int,
        kana_color: str,
        kanji_color: str,
        show_kanji: bool,
        family: str = "",
    ) -> None:
        if family:
            self._ruby_font.setFamily(family)
            self._base_font.setFamily(family)
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
        family = settings.selection_font_family or "Microsoft YaHei UI"
        for label in (self.message, self.chinese, self.reading_title, self._title):
            font = label.font()
            font.setFamily(family)
            label.setFont(font)
        self.hiragana.configure(
            settings.selection_kana_px,
            settings.selection_kanji_px,
            settings.selection_kana_color,
            settings.selection_kanji_color,
            settings.selection_show_kanji,
            family,
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


class ProfileDialog(QDialog):
    """编辑一条自定义端点（OpenAI 兼容）。key 只在提交时加密。"""

    test_done = Signal(bool, str)

    def __init__(self, profile: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("模型服务")
        self.setMinimumWidth(560)
        self._existing = dict(profile or {})
        form = QFormLayout(self)

        self.name = QLineEdit(self._existing.get("name", ""))
        self.name.setPlaceholderText("例如 DeepSeek / 公司网关 / 本地 Ollama")
        self.base_url = QLineEdit(self._existing.get("base_url", ""))
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.model_name = QLineEdit(self._existing.get("model", ""))
        self.model_name.setPlaceholderText("deepseek-chat；留空则用 /models 返回的第一项")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(
            "已保存，留空则不修改" if secrets.looks_encrypted(self._existing.get("api_key", "")) else "sk-…"
        )
        self.price_in = self._price(self._existing.get("input_price"))
        self.price_out = self._price(self._existing.get("output_price"))
        self.verify = QCheckBox("忽略证书校验（仅内网自签证书时勾选）")
        self.verify.setChecked(not self._existing.get("verify_tls", True))
        self.verify.setToolTip("只对这条端点生效。勾选后无法验证对端身份，公网端点请勿开启")

        form.addRow("名称", self.name)
        form.addRow("接口地址", self.base_url)
        form.addRow("模型名", self.model_name)
        form.addRow("API key", self.api_key)
        form.addRow("输入单价", self.price_in)
        form.addRow("输出单价", self.price_out)
        form.addRow("", self.verify)
        form.addRow(
            QLabel("<small>单价用于折算「本场花费」，可留 0 表示不折算。key 用 Windows DPAPI 加密后存进 config.json。</small>")
        )

        self.btn_test = QPushButton("测试连接")
        self.test_status = QLabel("")
        self.test_status.setStyleSheet("color:#888")
        row = QHBoxLayout()
        row.addWidget(self.btn_test)
        row.addWidget(self.test_status, 1)
        form.addRow("", row)
        self.btn_test.clicked.connect(self._test)
        self.test_done.connect(self._on_test_done)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def _price(value) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0.0, 1000.0)
        box.setDecimals(3)
        box.setSingleStep(0.1)
        box.setSuffix(" 元/百万 token")
        box.setSpecialValueText("不折算")
        box.setValue(float(value) if value else 0.0)
        return box

    def _test(self) -> None:
        base_url = normalize_base_url(self.base_url.text())
        if not base_url.startswith(("http://", "https://")):
            self.test_status.setText("接口地址要以 http:// 或 https:// 开头")
            return
        key = self.api_key.text().strip()
        if not key:
            try:
                key = secrets.unprotect(self._existing.get("api_key", ""))
            except Exception:
                key = ""
        if not key:
            self.test_status.setText("请填写 API key")
            return
        self.btn_test.setEnabled(False)
        self.test_status.setStyleSheet("color:#888")
        self.test_status.setText("测试中…")
        url = base_url.rstrip("/") + "/models"
        verify = not self.verify.isChecked()

        def work() -> None:
            try:
                response = httpx.get(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=20,
                    verify=verify,
                    follow_redirects=True,
                )
                if response.status_code >= 400:
                    self.test_done.emit(False, f"HTTP {response.status_code}：{response.text[:120]}")
                    return
                data = response.json()
                items = data.get("data") if isinstance(data, dict) else None
                count = len(items) if isinstance(items, list) else 0
                self.test_done.emit(True, f"连接成功，返回 {count} 个模型")
            except Exception as e:  # noqa: BLE001
                self.test_done.emit(False, f"连接失败：{e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        self.btn_test.setEnabled(True)
        self.test_status.setStyleSheet("color:#3fb950" if ok else "color:#e06c75")
        self.test_status.setText(message)

    def _ok(self) -> None:
        base_url = normalize_base_url(self.base_url.text())
        if not self.name.text().strip() or not base_url:
            msg.warn(self, "模型服务", "名称和接口地址都不能为空。")
            return
        if not base_url.startswith(("http://", "https://")):
            msg.warn(self, "模型服务", "接口地址要以 http:// 或 https:// 开头。")
            return
        if not self.api_key.text().strip() and not self._existing.get("api_key"):
            msg.warn(self, "模型服务", "请填写 API key。")
            return
        self.base_url.setText(base_url)  # 顺手补全协议头，保存后配置里就是可直接用的地址
        self.accept()

    def result_profile(self) -> dict:
        p = dict(self._existing)
        p["name"] = self.name.text().strip()
        p["base_url"] = normalize_base_url(self.base_url.text())
        p["model"] = self.model_name.text().strip()
        key = self.api_key.text().strip()
        if key:
            p["api_key"] = secrets.protect(key)
        p["api_key"] = p.get("api_key", "")
        p["input_price"] = self.price_in.value() or None
        p["output_price"] = self.price_out.value() or None
        p["verify_tls"] = not self.verify.isChecked()
        return p


class _PaintedArrow:
    """在控件右边缘自己画箭头。

    深色主题下 QSS 画不出三角形（`border-*` 拼出来就是一块灰方），
    系统默认箭头又呈深灰色、压在深底上几乎看不见，因此改成自绘。
    """

    # (纵向位置比例, 尖端朝向)：-1 朝上、+1 朝下
    _arrow_marks: tuple[tuple[float, int], ...] = ((0.5, 1),)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#7d8698"), 1.3))
        x = self.width() - 13.0
        for frac, tip in self._arrow_marks:
            y = self.height() * frac
            apex = QPointF(x, y + tip * 2.2)
            painter.drawLine(QPointF(x - 3.6, y - tip * 2.2), apex)
            painter.drawLine(apex, QPointF(x + 3.6, y - tip * 2.2))
        painter.end()


class ChevronCombo(_PaintedArrow, QComboBox):
    """带自绘箭头的下拉框。"""


class ChevronFontCombo(_PaintedArrow, QFontComboBox):
    """带自绘箭头的字体下拉框。"""


class ChevronSpin(_PaintedArrow, QSpinBox):
    """带自绘上下箭头的整数框。"""

    _arrow_marks = ((0.30, -1), (0.72, 1))


class ChevronDoubleSpin(_PaintedArrow, QDoubleSpinBox):
    """带自绘上下箭头的小数框。"""

    _arrow_marks = ((0.30, -1), (0.72, 1))


# 设置窗口的深色外观：与首页（HOME_QSS）、详细窗（DETAIL_QSS）同一套配色。
SETTINGS_QSS = """
QDialog { background: #0f1115; }
QLabel { color: #c3cbdb; font-size: 12px; }
QLabel#dialogTitle { color: #f4f6fb; font-size: 22px; font-weight: 800; }
QLabel#dialogSubtitle { color: #8991a4; font-size: 12px; }
QLabel#cardTitle { color: #f4f6fb; font-size: 14px; font-weight: 700; }
QLabel#cardDesc { color: #8991a4; font-size: 12px; }
QLabel#dirtyHint { color: #e0a458; font-size: 12px; }
QWidget#navColumn { background: #0b0d12; border-right: 1px solid #1c212b; }
QWidget#dialogFooter { background: #0b0d12; border-top: 1px solid #1c212b; }
QFrame#card { background: #171a21; border: 1px solid #272d3a; border-radius: 12px; }
QPushButton#navItem {
    text-align: left; padding: 8px 12px; border: 1px solid transparent;
    border-radius: 8px; background: transparent; color: #8991a4; font-size: 13px;
}
QPushButton#navItem:hover { background: #151922; color: #d7deea; }
QPushButton#navItem:checked {
    background: #171a21; color: #f4f6fb; border: 1px solid #272d3a; border-left: 2px solid #4f7cff;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QFontComboBox {
    background: #12151c; border: 1px solid #2a3040; border-radius: 8px;
    color: #e7ecf5; padding: 5px 8px; selection-background-color: #4f7cff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QFontComboBox:focus {
    border-color: #4f7cff;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QFontComboBox:disabled {
    color: #5b6478; background: #141821; border-color: #232936;
}
QComboBox::drop-down, QFontComboBox::drop-down { border: 0; width: 20px; }
QComboBox::down-arrow, QFontComboBox::down-arrow { image: none; }
QComboBox QAbstractItemView {
    background: #171a21; border: 1px solid #2a3040; color: #e7ecf5;
    selection-background-color: #2b3a5c; outline: 0;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 16px;
    border-left: 1px solid #2a3040; background: #1a1f2a; border-top-right-radius: 8px;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right; width: 16px;
    border-left: 1px solid #2a3040; background: #1a1f2a; border-bottom-right-radius: 8px;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow,
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: none; }
QCheckBox { color: #c3cbdb; spacing: 8px; }
QCheckBox::indicator {
    width: 15px; height: 15px; border: 1px solid #3a4356; border-radius: 4px; background: #12151c;
}
QCheckBox::indicator:hover { border-color: #4f7cff; }
QCheckBox::indicator:checked { background: #4f7cff; border-color: #4f7cff; }
QCheckBox:disabled { color: #5b6478; }
QPushButton {
    background: #202635; border: 1px solid #313a4f; border-radius: 8px;
    color: #d7deea; padding: 6px 14px; font-size: 12px;
}
QPushButton:hover { background: #273044; }
QPushButton:disabled { color: #5b6478; background: #171b24; border-color: #232936; }
QPushButton#primary { background: #4f7cff; border: 1px solid #4f7cff; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #6289ff; }
QPushButton#danger { background: transparent; border: 1px solid #3a2a2e; color: #d9534f; }
QPushButton#danger:hover { background: #2a1a1e; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px; }
QScrollBar::handle:vertical { background: #2b3344; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: #3a4457; }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent; height: 0; width: 0;
}
QToolTip { background: #171a21; color: #e7ecf5; border: 1px solid #2a3040; padding: 4px; }
"""


class SettingsDialog(QDialog):
    quit_requested = Signal()

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
        # 六个分区各一张表单，放进右侧的分区页面里；导航只决定哪一页可见。
        form_dev = self._form()
        form_tr = self._form()
        form_seg = self._form()
        form_ov = self._form()
        form_sel = self._form()
        form_data = self._form()

        # 启动失败等严重错误挂在标题下面：不切分区也能看到。
        self.service_error = QLabel("")
        self.service_error.setWordWrap(True)
        self.service_error.setStyleSheet("color:#e06c75")
        self.service_error.hide()

        self.speaker = flexible_combo(ChevronCombo())
        self.speaker.addItem("系统默认", None)
        for n in audio.list_speakers():
            self.speaker.addItem(n, n)
        self._select(self.speaker, settings.loopback_device)

        self.capture_speaker = flexible_combo(ChevronCombo())
        self.capture_speaker.addItem("自动识别 CABLE-A", None)
        for n in audio.list_speakers():
            self.capture_speaker.addItem(n, n)
        self.capture_speaker.setToolTip(
            "会议软件把对方声音输出到哪个虚拟扬声器，这里就选哪个。"
            "如果你在会议软件里选了 CABLE In 16ch，这里也要选它，不能只选 CABLE Input。"
        )
        self._select(self.capture_speaker, settings.duplex_capture_device)

        self.local_speaker = flexible_combo(ChevronCombo())
        self.local_speaker.addItem("自动（优先耳机/扬声器）", None)
        for n in audio.list_speakers():
            self.local_speaker.addItem(n, n)
        self.local_speaker.setToolTip(
            "双向同传时，中文 TTS 播放到哪个设备。不要把这里选成 CABLE/Hi-Fi，否则耳机听不到；"
            "系统默认会跟随 Windows 默认输出，可能被改成 CABLE。"
        )
        self._select(self.local_speaker, settings.duplex_local_speaker)
        speakers = audio.list_speakers()
        self.speaker_test = SpeakerTestWidget(
            self.local_speaker.currentData,
            lambda _none: duplex_audio._physical_speaker(speakers),
        )
        self.local_speaker.currentIndexChanged.connect(lambda _i: self.speaker_test.refresh())
        local_box = QVBoxLayout()
        local_box.setContentsMargins(0, 0, 0, 0)
        local_box.setSpacing(6)
        local_box.addWidget(self.local_speaker)
        local_box.addWidget(self.speaker_test)

        self.mic = flexible_combo(ChevronCombo())
        self.mic.addItem("不翻译麦克风", "__off__")
        self.mic.addItem("系统默认", None)
        for n in audio.list_microphones():
            self.mic.addItem(n, n)
        self._select(self.mic, settings.mic_device if settings.mic_enabled else "__off__")
        self.mic.setToolTip(
            "01 会议实时翻译：选「不翻译麦克风」只翻对方；选设备则把你说的话也识别出来。\n"
            "03 双向同传：你说的中文从这里采集，翻成日语送给对方。\n"
            "必须选真实麦克风（如耳机麦、ジャックマイク），不要选 CABLE / Hi-Fi Cable 等虚拟声卡。\n"
            "「系统默认」跟随 Windows 默认录音设备，它可能被改成了虚拟声卡。"
        )
        self.mic_test = MicTestWidget(self.mic.currentData)
        self.mic_help = QLabel(
            "说话识别不出来时：① 点「测试麦克风」对着说话，电平条要跳动；"
            "② 不动就换一个设备再测；③ Windows「设置 → 隐私和安全性 → 麦克风」允许桌面应用访问；"
            "④ 提示音频服务未运行时，在 services.msc 启动「Windows Audio」或重启电脑。"
        )
        self.mic_help.setStyleSheet("color:#8991a4")
        self.mic_help.setWordWrap(True)
        mic_box = QVBoxLayout()
        mic_box.setContentsMargins(0, 0, 0, 0)
        mic_box.setSpacing(6)
        mic_box.addWidget(self.mic)
        mic_box.addWidget(self.mic_test)
        mic_box.addWidget(self.mic_help)

        self.direction = flexible_combo(ChevronCombo())
        for d in lang.DIRECTIONS.values():
            note = "（SenseVoice 识别" + ("）" if model_ready("sensevoice") else "，需下载约 230MB）")
            self.direction.addItem(d.label + (note if d.key == lang.ZH_JA else ""), d.key)
        self._select(self.direction, settings.direction)
        self.direction_note = QLabel("")
        self.direction_note.setStyleSheet("color:#8991a4")
        direction_row = QHBoxLayout()
        direction_row.addWidget(self.direction, 1)
        direction_row.addWidget(self.direction_note)

        self.engine = flexible_combo(ChevronCombo())
        for key, label in ENGINES:
            self.engine.addItem(label + ("" if model_ready(key) else "（需下载）"), key)
        self._select(self.engine, settings.asr_engine)

        self.provider = flexible_combo(ChevronCombo())
        self.provider.addItem("GitHub Copilot", "copilot")
        self.provider.addItem("自定义（OpenAI 兼容）", "openai")
        self._select(self.provider, settings.provider)

        self.profiles: list[dict] = [dict(p) for p in settings.profiles]
        self.profile_combo = flexible_combo(ChevronCombo(), 12)
        self._reload_profiles(settings.active_profile)
        self.btn_profile_new = QPushButton("新建…")
        self.btn_profile_edit = QPushButton("编辑…")
        self.btn_profile_del = QPushButton("删除")
        self.btn_profile_new.clicked.connect(self._new_profile)
        self.btn_profile_edit.clicked.connect(self._edit_profile)
        self.btn_profile_del.clicked.connect(self._delete_profile)
        profile_row = QHBoxLayout()
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(self.btn_profile_new)
        profile_row.addWidget(self.btn_profile_edit)
        profile_row.addWidget(self.btn_profile_del)
        self.provider_note = QLabel("")
        self.provider_note.setStyleSheet("color:#8991a4")
        self.provider_note.setWordWrap(True)
        profile_box = QVBoxLayout()
        profile_box.setContentsMargins(0, 0, 0, 0)
        profile_box.setSpacing(4)
        profile_box.addLayout(profile_row)
        profile_box.addWidget(self.provider_note)

        self.model = flexible_combo(ChevronCombo(), 16)
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
        model_box = QVBoxLayout()
        model_box.setContentsMargins(0, 0, 0, 0)
        model_box.setSpacing(4)
        model_box.addLayout(model_row)
        model_box.addWidget(self.model_info)
        self._models: list[ModelOption] = []
        self.set_models(models)

        self.effort = ChevronCombo()
        self.effort.addItems(EFFORTS)
        self.effort.setCurrentText(settings.reasoning_effort)
        self._sync_effort_choices()

        self.rotate = ChevronSpin()
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

        self.font_family = ChevronFontCombo()
        self.font_family.setCurrentFont(QFont(settings.font_family or "Microsoft YaHei UI"))
        self.font_family.setToolTip("会议实时翻译 / 语音实时转译的悬浮字幕字体（原文、译文共用）")
        # 原文（识别出的那句话）与译文各自设字号和颜色
        self.src_font = ChevronSpin()
        self.src_font.setRange(9, 48)
        self.src_font.setSuffix(" pt")
        self.src_font.setValue(settings.source_font_size)
        self.src_font.setToolTip("悬浮框里原文（识别结果）的字号；未完成的识别片段也用它")
        self.src_font_color = ColorButton(settings.source_font_color)
        self.src_font_color.setToolTip("悬浮框里原文的颜色；未完成的识别片段也用它")
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("字号"))
        src_row.addWidget(self.src_font)
        src_row.addWidget(QLabel("颜色"))
        src_row.addWidget(self.src_font_color)
        src_row.addStretch(1)
        self.font = ChevronSpin()
        self.font.setRange(9, 48)
        self.font.setSuffix(" pt")
        self.font.setValue(settings.font_size)
        self.font.setToolTip("悬浮框里译文的字号；字幕模式（只显示译文）也用它")
        self.font_color = ColorButton(settings.font_color)
        self.font_color.setToolTip("悬浮框里译文的颜色")
        zh_row = QHBoxLayout()
        zh_row.addWidget(QLabel("字号"))
        zh_row.addWidget(self.font)
        zh_row.addWidget(QLabel("颜色"))
        zh_row.addWidget(self.font_color)
        zh_row.addStretch(1)

        self.opacity = ChevronDoubleSpin()
        self.opacity.setRange(0.2, 1.0)
        self.opacity.setSingleStep(0.05)
        self.opacity.setValue(settings.opacity)
        self.show_overlay = QCheckBox("显示悬浮字幕窗（关闭后只留托盘图标和快捷键）")
        self.show_overlay.setChecked(settings.show_overlay)
        self.subtitle_mode = QCheckBox("字幕模式：只显示译文，不显示原文")
        self.subtitle_mode.setToolTip("适合看视频时当字幕用；未完成的原文片段和语气词不显示")
        self.subtitle_mode.setChecked(settings.subtitle_mode)

        self.hotkey = QLineEdit(settings.hotkey)
        self.selection_hotkey = QLineEdit(settings.selection_hotkey)

        self.selection_font_family = ChevronFontCombo()
        self.selection_font_family.setCurrentFont(QFont(settings.selection_font_family or "Microsoft YaHei UI"))
        self.selection_font_family.setToolTip("划词/句翻译弹窗的字体，与悬浮框分开设置")
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
        self._overlay_option_boxes = (self.show_remaining, self.show_used, self.show_meeting)
        for w in self._overlay_option_boxes:
            display_box.addWidget(w)
        self.show_overlay.toggled.connect(self._sync_overlay_options)

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

        form_dev.addRow("对方声音（单方向环回）", self.speaker)
        form_dev.addRow("对方声音（双向同传采集）", self.capture_speaker)
        form_dev.addRow("我的声音（麦克风）", mic_box)
        form_dev.addRow("本地耳机（中文 TTS）", local_box)
        form_tr.addRow("翻译方向", direction_row)
        form_tr.addRow("语音识别引擎（日→中）", self.engine)
        form_tr.addRow("识别纠错", self.asr_correct)
        form_tr.addRow("模型服务", self.provider)
        form_tr.addRow("端点", profile_box)
        form_tr.addRow("翻译模型", model_box)
        form_tr.addRow("推理强度（思考档位）", self.effort)
        form_tr.addRow("会话轮换间隔", self.rotate)
        form_seg.addRow("断句：静音判定", self.silence)
        form_seg.addRow("断句：未说完最多等待", self.hold)
        form_seg.addRow("断句：合并上限", self.hold_max)
        form_seg.addRow("语气词·日语（不翻译）", self.fillers)
        form_seg.addRow("语气词·中文（不翻译）", self.fillers_zh)
        form_ov.addRow("字体", self.font_family)
        form_ov.addRow("原文 字号 / 颜色", src_row)
        form_ov.addRow("译文 字号 / 颜色", zh_row)
        form_ov.addRow("背景不透明度", self.opacity)
        form_ov.addRow("悬浮框", self.show_overlay)
        form_ov.addRow("字幕模式", self.subtitle_mode)
        form_ov.addRow("显示内容", display_box)
        form_ov.addRow("开始/暂停快捷键", self.hotkey)
        form_sel.addRow("字体", self.selection_font_family)
        form_sel.addRow("划词/句翻译快捷键", self.selection_hotkey)
        form_sel.addRow("中文译文", chinese_row)
        form_sel.addRow("假名", kana_row)
        form_sel.addRow("汉字", kanji_row)
        form_data.addRow("翻译历史", history_row)
        form_data.addRow("会议记录保存位置", save_row)
        # ---- 外观：左边分区导航，右边卡片，底边固定按钮 ----
        self.setStyleSheet(SETTINGS_QSS)
        header = QWidget()
        head_lay = QVBoxLayout(header)
        head_lay.setContentsMargins(22, 18, 22, 12)
        head_lay.setSpacing(5)
        title = QLabel("设置")
        title.setObjectName("dialogTitle")
        subtitle = QLabel(
            "改动点「保存」后生效。会议中改模型、推理强度、麦克风开关会立即生效；"
            "设备、翻译方向、识别引擎、断句、识别纠错在下一次「开始」时生效。"
        )
        subtitle.setObjectName("dialogSubtitle")
        subtitle.setWordWrap(True)
        head_lay.addWidget(title)
        head_lay.addWidget(subtitle)
        head_lay.addWidget(self.service_error)

        sections = (
            ("dev", "声音与设备", "声音与设备",
             "会议软件把对方声音送到哪个虚拟声卡、中文 TTS 播到哪、麦克风要不要翻译。", form_dev),
            ("tr", "翻译与模型", "翻译与模型",
             "翻译方向、识别引擎、模型服务与推理强度。", form_tr),
            ("seg", "断句与语气词", "断句与语气词",
             "多久算一句话说完；语气词单独成句时只显示不翻译。", form_seg),
            ("ov", "悬浮框", "悬浮框",
             "会议实时翻译、语音实时转译共用的字幕窗外观。", form_ov),
            ("sel", "划词翻译", "划词翻译弹窗",
             "鼠标划词或整句翻译时弹出的小窗，字体与悬浮框分开设。", form_sel),
            ("data", "记录与历史", "记录与历史",
             "会议纪要与划词历史的保存位置。", form_data),
        )
        nav_col = QWidget()
        nav_col.setObjectName("navColumn")
        nav_col.setFixedWidth(184)
        nav_lay = QVBoxLayout(nav_col)
        nav_lay.setContentsMargins(12, 6, 10, 14)
        nav_lay.setSpacing(2)
        self.stack = QStackedWidget()
        self._nav_buttons: dict[str, QPushButton] = {}
        for key, nav_label, card_title, card_desc, sec_form in sections:
            btn = QPushButton(nav_label)
            btn.setObjectName("navItem")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(lambda _checked=False, k=key: self.show_section(k))
            self._nav_buttons[key] = btn
            nav_lay.addWidget(btn)
            self.stack.addWidget(self._page(card_title, card_desc, sec_form))
        nav_lay.addStretch(1)

        self.dirty_hint = QLabel("")
        self.dirty_hint.setObjectName("dirtyHint")
        self.btn_quit_program = QPushButton("关闭程序")
        self.btn_quit_program.setObjectName("danger")
        self.btn_quit_program.setToolTip("直接退出会议翻译（不保存未应用的修改）")
        self.btn_quit_program.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_quit_program.clicked.connect(self.quit_requested)
        self.btn_defaults = QPushButton("恢复默认")
        self.btn_defaults.setToolTip("把所有设置恢复为默认值（自定义模型端点保留），点「保存」后才生效")
        self.btn_defaults.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_defaults.clicked.connect(self._confirm_restore_defaults)
        btn_cancel = QPushButton("取消")
        btn_cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_cancel.clicked.connect(self.reject)
        self.btn_save = QPushButton("保存")
        self.btn_save.setObjectName("primary")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self.accept)
        footer = QWidget()
        footer.setObjectName("dialogFooter")
        footer_lay = QHBoxLayout(footer)
        footer_lay.setContentsMargins(18, 10, 18, 14)
        footer_lay.setSpacing(10)
        footer_lay.addWidget(self.btn_quit_program)
        footer_lay.addWidget(self.dirty_hint, 1, Qt.AlignmentFlag.AlignRight)
        footer_lay.addWidget(self.btn_defaults)
        footer_lay.addWidget(btn_cancel)
        footer_lay.addWidget(self.btn_save)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(nav_col)
        body.addWidget(self.stack, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(header)
        outer.addLayout(body, 1)
        outer.addWidget(footer)
        self.show_section("dev")
        self.setMinimumSize(780, 520)
        self.resize(920, 690)

        self._getters: dict[str, Callable[[], object]] = {
            "speaker": self.speaker.currentData,
            "capture_speaker": self.capture_speaker.currentData,
            "local_speaker": self.local_speaker.currentData,
            "mic": self.mic.currentData,
            "direction": self.direction.currentData,
            "engine": self.engine.currentData,
            "asr_correct": self.asr_correct.isChecked,
            "provider": self.provider.currentData,
            "profile": lambda: self.profile_combo.currentData() or "",
            "model": lambda: self.model.currentData() or self.model.currentText().strip() or self.s.model,
            "effort": self.effort.currentText,
            "rotate": self.rotate.value,
            "silence": self.silence.value,
            "hold": self.hold.value,
            "hold_max": self.hold_max.value,
            "fillers": self.fillers.text,
            "fillers_zh": self.fillers_zh.text,
            "font_family": lambda: self.font_family.currentFont().family(),
            "src_font": self.src_font.value,
            "src_font_color": self.src_font_color.color,
            "font": self.font.value,
            "font_color": self.font_color.color,
            "selection_font_family": lambda: self.selection_font_family.currentFont().family(),
            "opacity": self.opacity.value,
            "show_overlay": self.show_overlay.isChecked,
            "subtitle_mode": self.subtitle_mode.isChecked,
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
        for w in (self.speaker, self.capture_speaker, self.local_speaker, self.mic, self.direction, self.engine, self.model, self.effort):
            w.currentIndexChanged.connect(self._update_dirty)
        self.model.currentIndexChanged.connect(self._sync_effort_choices)
        for w in (
            self.rotate, self.silence, self.hold, self.hold_max, self.font, self.src_font, self.opacity,
            self.chinese_size, self.kana_size, self.kanji_size,
        ):
            w.valueChanged.connect(self._update_dirty)
        self.font_family.currentFontChanged.connect(self._update_dirty)
        self.selection_font_family.currentFontChanged.connect(self._update_dirty)
        for w in (self.font_color, self.src_font_color, self.chinese_color, self.kana_color, self.kanji_color):
            w.colorChanged.connect(self._update_dirty)
        for w in (self.fillers, self.fillers_zh, self.hotkey, self.selection_hotkey, self.save_dir):
            w.textChanged.connect(self._update_dirty)
        for w in (
            self.show_remaining, self.show_used, self.show_meeting,
            self.show_overlay, self.subtitle_mode, self.asr_correct, self.show_kanji,
        ):
            w.toggled.connect(self._update_dirty)
        self.direction.currentIndexChanged.connect(self._sync_engine_enabled)
        self.provider.currentIndexChanged.connect(self._sync_provider)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self._meeting_active = False
        self._sync_engine_enabled()
        self._sync_overlay_options()
        self._sync_provider()
        self._sync_kanji_enabled()

    # ---- 服务商与模型 ----

    def current_profile(self) -> dict | None:
        name = self.profile_combo.currentData()
        for p in self.profiles:
            if p.get("name") == name:
                return p
        return None

    def current_service(self) -> tuple[str, dict | None]:
        """设置窗口此刻编辑的服务：("copilot"|"openai", 端点配置或 None)。"""
        return self.provider.currentData() or "copilot", self.current_profile()

    def _desired_model(self) -> str:
        if self.provider.currentData() == "openai":
            return (self.current_profile() or {}).get("model") or ""
        return self.s.model

    def _stash_model(self) -> None:
        """把选中的模型写回端点配置：自定义端点的模型名只有一个真相来源。"""
        profile = self.current_profile()
        if self.provider.currentData() != "openai" or profile is None:
            return
        mid = self.model.currentData() or self.model.currentText().strip()
        if mid:
            profile["model"] = mid

    def _request_refresh(self) -> None:
        if self._on_refresh is not None:
            self._on_refresh()

    def _on_profile_changed(self) -> None:
        self._update_dirty()
        self._request_refresh()

    def set_models_loading(self) -> None:
        self.model_info.setText("<small>正在从该端点获取模型列表…</small>")

    def set_model_error(self, text: str) -> None:
        if text:
            self.model_info.setText(f"<small style='color:#ff6b6b'>{html.escape(text)}</small>")

    def set_service_error(self, text: str) -> None:
        self.service_error.setText(text)
        self.service_error.setVisible(bool(text))

    def fit_to_screen(self, avail: QRect) -> None:
        """初始尺寸不超过可用区域的 92%：小屏或高缩放比例下 OK/取消 也要够得着。"""
        width = max(self.minimumWidth(), min(self.width(), int(avail.width() * 0.92)))
        height = max(self.minimumHeight(), min(self.height(), int(avail.height() * 0.92)))
        self.resize(width, height)

    def is_dirty(self) -> bool:
        return any(f() != self._baseline[k] for k, f in self._getters.items())

    def _update_dirty(self) -> None:
        self.dirty_hint.setText("有未保存的改动" if self.is_dirty() else "")

    def _confirm_restore_defaults(self) -> None:
        if msg.ask(
            self,
            "恢复默认设置",
            "把所有设置恢复为默认值？",
            detail="自定义模型端点（含 API key）会保留。点「保存」后才生效。",
            yes="恢复默认",
            no="取消",
        ):
            self._restore_defaults()

    def _restore_defaults(self) -> None:
        """把对话框里的控件恢复成 Settings() 的默认值；不写盘，点 OK 才应用。"""
        d = Settings()
        self._select(self.speaker, d.loopback_device)
        self._select(self.capture_speaker, d.duplex_capture_device)
        self._select(self.local_speaker, d.duplex_local_speaker)
        self._select(self.mic, d.mic_device if d.mic_enabled else "__off__")
        if not self._meeting_active:
            self._select(self.direction, d.direction)
        self._select(self.engine, d.asr_engine)
        self._select(self.provider, d.provider)
        # 切回 Copilot 后模型列表要异步重拉；先放一个默认项占位，
        # 列表回来时 set_models() 会按 currentData 重新选中它。
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItem(d.model, d.model)
        self.model.blockSignals(False)
        if self._models:
            self.set_models(self._models)
        self.effort.setCurrentText(d.reasoning_effort)
        self._sync_effort_choices()
        self.rotate.setValue(d.session_rotate_minutes)
        self.silence.setValue(d.silence_seconds)
        self.hold.setValue(d.hold_seconds)
        self.hold_max.setValue(d.hold_max_seconds)
        self.asr_correct.setChecked(d.asr_correct)
        self.fillers.setText("、".join(d.fillers))
        self.fillers_zh.setText("、".join(d.fillers_zh))
        self.font_family.setCurrentFont(QFont(d.font_family or "Microsoft YaHei UI"))
        self.src_font.setValue(d.source_font_size)
        self.src_font_color.set_color(d.source_font_color)
        self.font.setValue(d.font_size)
        self.font_color.set_color(d.font_color)
        self.opacity.setValue(d.opacity)
        self.show_overlay.setChecked(d.show_overlay)
        self.subtitle_mode.setChecked(d.subtitle_mode)
        self.hotkey.setText(d.hotkey)
        self.selection_hotkey.setText(d.selection_hotkey)
        self.selection_font_family.setCurrentFont(QFont(d.selection_font_family or "Microsoft YaHei UI"))
        self.chinese_size.setValue(d.selection_chinese_px)
        self.chinese_color.set_color(d.selection_chinese_color)
        self.kana_size.setValue(d.selection_kana_px)
        self.kana_color.set_color(d.selection_kana_color)
        self.kanji_size.setValue(d.selection_kanji_px)
        self.kanji_color.set_color(d.selection_kanji_color)
        self.show_kanji.setChecked(d.selection_show_kanji)
        self.show_remaining.setChecked(d.show_quota_remaining)
        self.show_used.setChecked(d.show_quota_used)
        self.show_meeting.setChecked(d.show_meeting_aic)
        self.save_dir.clear()
        self._update_dirty()

    def _sync_overlay_options(self) -> None:
        """悬浮框关掉时，里面显示什么就没意义了。"""
        on = self.show_overlay.isChecked()
        for w in self._overlay_option_boxes:
            w.setEnabled(on)

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
        w = ChevronSpin()
        w.setRange(8, 72)
        w.setSuffix(" px")
        w.setValue(value)
        return w

    @staticmethod
    def _dspin(lo: float, hi: float, step: float, value: float) -> QDoubleSpinBox:
        w = ChevronDoubleSpin()
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
            msg.info(self, "翻译历史", "历史文件尚未创建，请先点击「保存划词/句翻译历史」。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            msg.warn(self, "翻译历史", f"无法打开文件：\n{path}")

    def _browse_save_dir(self) -> None:
        start = self.save_dir.text() or str(Path.home() / "Documents")
        d = QFileDialog.getExistingDirectory(self, "选择会议记录保存位置", start)
        if d:
            self.save_dir.setText(str(Path(d)))

    def _reload_profiles(self, select: str | None = None) -> None:
        # 重建期间屏蔽信号：否则 clear() 会先报一次「没有端点」，模型列表白刷一遍。
        self.profile_combo.blockSignals(True)
        try:
            self.profile_combo.clear()
            for p in self.profiles:
                self.profile_combo.addItem(p.get("name", ""), p.get("name", ""))
            target = select or (self.profiles[0].get("name", "") if self.profiles else "")
            idx = self.profile_combo.findData(target)
        finally:
            self.profile_combo.blockSignals(False)
        if idx >= 0:
            self.profile_combo.setCurrentIndex(idx)
        else:
            self._request_refresh()

    def _new_profile(self) -> None:
        dlg = ProfileDialog(None, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        p = dlg.result_profile()
        names = {x.get("name") for x in self.profiles}
        if p["name"] in names:
            msg.warn(self, "模型服务", "已存在同名端点，请换一个名字。")
            return
        self.profiles.append(p)
        self.provider.setCurrentIndex(self.provider.findData("openai"))
        self._reload_profiles(p["name"])
        self._update_dirty()

    def _edit_profile(self) -> None:
        name = self.profile_combo.currentData()
        for i, p in enumerate(self.profiles):
            if p.get("name") == name:
                dlg = ProfileDialog(p, self)
                if dlg.exec() != QDialog.DialogCode.Accepted:
                    return
                self.profiles[i] = dlg.result_profile()
                self._reload_profiles(self.profiles[i]["name"])
                self._update_dirty()
                return

    def _delete_profile(self) -> None:
        name = self.profile_combo.currentData()
        if not name:
            return
        if not msg.ask(self, "模型服务", f"删除端点「{name}」？", yes="删除", no="取消", kind="warning"):
            return
        self.profiles = [p for p in self.profiles if p.get("name") != name]
        self._reload_profiles()
        self._update_dirty()

    def _sync_provider(self) -> None:
        self.set_service_error("")
        custom = self.provider.currentData() == "openai"
        switched = getattr(self, "_synced_provider", None) not in (None, self.provider.currentData())
        self._synced_provider = self.provider.currentData()
        if switched:
            # 换服务商时先清空模型名：Copilot 的模型名带进端点会直接 400，反之亦然。
            self.set_models([])
        for w in (self.profile_combo, self.btn_profile_new, self.btn_profile_edit, self.btn_profile_del):
            w.setEnabled(custom)
        # 自定义端点也要能选模型：以前这里把它禁用了，恰恰是唯一能选模型的地方。
        self.model.setEnabled(True)
        self.model.setEditable(custom)
        self.effort.setEnabled(True)
        self.provider_note.setText(
            "自定义端点：模型从该端点的 /models 拉取，选中的名字会存进端点配置；"
            "推理强度按模型支持的档位发送（none 表示关闭思考，需端点支持）；用量按 token 记。"
            if custom
            else ""
        )
        self._sync_effort_choices()
        self._request_refresh()

    def set_models(self, models: list[ModelOption]) -> None:
        current = self.model.currentData() or self._desired_model()
        self._models = models
        ordered, best = sorted_models(models)
        self.model.blockSignals(True)
        self.model.clear()
        for o in ordered:
            self.model.addItem(model_label(o, models, best), o.id)
            self.model.setItemData(self.model.count() - 1, model_tooltip(o), Qt.ItemDataRole.ToolTipRole)
        if current and models and self.model.findData(current) < 0:
            self.model.addItem(current, current)  # 保留已保存但不在列表里的模型名；空列表不保留，免得带错服务商
        if current:
            self._select(self.model, current)
        elif self.model.count():
            # 可编辑的下拉框加完条目不会自动选中，空模型名就永远填不上。
            self.model.setCurrentIndex(0)
        self.model.blockSignals(False)
        self.model_info.setText(
            "<small>成本按本机历史会议估算（无历史时按每小时 600 句估算）；"
            "延迟：「平均」为本机实测，「约」为内置参考值。</small>"
            if models
            else "<small>正在获取模型列表…</small>"
        )
        # 端点没填模型名时，把拉到的第一项定下来，并算作初始状态而不是「已修改」。
        if self.provider.currentData() == "openai" and not (self.current_profile() or {}).get("model"):
            self._stash_model()
            if hasattr(self, "_baseline"):
                self._baseline["model"] = self._getters["model"]()
                self._update_dirty()
        if hasattr(self, "effort"):
            self._sync_effort_choices()

    def _sync_effort_choices(self, *_args) -> None:
        """端点声明了推理档位时，只展示它支持的档位（保留旧值并提示回退）。"""
        if not hasattr(self, "effort"):
            return
        current = self.effort.currentText()
        opt = None
        if self.provider.currentData() == "openai":
            mid = self.model.currentData() or self.model.currentText().strip()
            opt = next((o for o in self._models if o.id == mid), None)
        values = (
            effort_choices(opt)
            if opt is not None and (opt.efforts or opt.can_disable_thinking)
            else list(EFFORTS)
        )
        if current and current not in values:
            values.append(current)  # 旧配置值保留在列表里，交给后端 clamp，避免静默改设置。
        if [self.effort.itemText(i) for i in range(self.effort.count())] != values:
            self.effort.blockSignals(True)
            try:
                self.effort.clear()
                self.effort.addItems(values)
                self.effort.setCurrentText(current)
            finally:
                self.effort.blockSignals(False)
        tip = ""
        if opt is not None and opt.efforts:
            supported = " / ".join(effort_choices(opt))
            tip = f"该模型支持：{supported}"
            if opt.default_effort:
                tip += f"（默认 {opt.default_effort}）"
            if current and current not in effort_choices(opt):
                tip += f"\n当前选择 {current} 不在支持列表，发送时会自动回退。"
        self.effort.setToolTip(tip)

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
            self.model_info.setText(f"<small style='color:#ff6b6b'>测速失败：{html.escape(str(result))}</small>")
            return
        self.set_models(self._models)
        self.model_info.setText(f"<small>{html.escape(model)} 实测平均 {result:.2f}s（已记入统计）</small>")

    def apply(self) -> None:
        s = self.s
        s.loopback_device = self.speaker.currentData()
        s.duplex_capture_device = self.capture_speaker.currentData()
        s.duplex_local_speaker = self.local_speaker.currentData()
        mic = self.mic.currentData()
        s.mic_enabled = mic != "__off__"
        if s.mic_enabled:
            s.mic_device = mic  # 关闭时保留设备选择，悬浮窗按钮重新开启时沿用
        s.asr_engine = self.engine.currentData()
        s.asr_correct = self.asr_correct.isChecked()
        if not self._meeting_active:
            s.direction = self.direction.currentData()
        s.provider = self.provider.currentData()
        self._stash_model()
        s.profiles = [dict(p) for p in self.profiles]
        s.active_profile = self.profile_combo.currentData() or ""
        s.model = self.model.currentData() or s.model
        s.reasoning_effort = self.effort.currentText()
        s.session_rotate_minutes = self.rotate.value()
        s.silence_seconds = self.silence.value()
        s.hold_seconds = self.hold.value()
        s.hold_max_seconds = max(self.hold_max.value(), s.hold_seconds)
        s.fillers = [f.strip() for f in re.split(r"[、,，\s]+", self.fillers.text()) if f.strip()]
        s.fillers_zh = [f.strip() for f in re.split(r"[、,，\s]+", self.fillers_zh.text()) if f.strip()]
        s.font_family = self.font_family.currentFont().family()
        s.source_font_size = self.src_font.value()
        s.source_font_color = self.src_font_color.color()
        s.font_size = self.font.value()
        s.font_color = self.font_color.color()
        s.opacity = self.opacity.value()
        s.show_overlay = self.show_overlay.isChecked()
        s.subtitle_mode = self.subtitle_mode.isChecked()
        s.hotkey = self.hotkey.text().strip() or s.hotkey
        s.selection_hotkey = self.selection_hotkey.text().strip() or s.selection_hotkey
        s.selection_font_family = self.selection_font_family.currentFont().family()
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

    # ---- 分区布局 ----

    @staticmethod
    def _form() -> QFormLayout:
        """一张分区表单：窄窗口下也不换行，长设备名由下拉框自己省略。"""
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setContentsMargins(20, 10, 20, 20)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return form

    def _page(self, title: str, desc: str, form: QFormLayout) -> QScrollArea:
        """一个分区页：深色卡片包着表单，超高时自己滚。"""
        card = QFrame()
        card.setObjectName("card")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        head = QVBoxLayout()
        head.setContentsMargins(20, 16, 20, 0)
        head.setSpacing(3)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("cardTitle")
        desc_lbl = QLabel(desc)
        desc_lbl.setObjectName("cardDesc")
        desc_lbl.setWordWrap(True)
        head.addWidget(title_lbl)
        head.addWidget(desc_lbl)
        card_lay.addLayout(head)
        card_lay.addLayout(form)
        card_lay.addStretch(1)
        content = QWidget()
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(18, 8, 18, 18)
        content_lay.addWidget(card)
        content_lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def show_section(self, key: str) -> None:
        """切到某个分区；导航项跟着高亮，新页面从顶部开始看。"""
        keys = list(self._nav_buttons)
        if key not in self._nav_buttons:
            return
        for k, btn in self._nav_buttons.items():
            btn.setChecked(k == key)
        self.stack.setCurrentIndex(keys.index(key))
        page = self.stack.currentWidget()
        if isinstance(page, QScrollArea):
            page.verticalScrollBar().setValue(0)


class LoginDialog(QDialog):
    token_submitted = Signal(str)
    recheck_requested = Signal()
    use_custom_requested = Signal()

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
        lay.addWidget(
            QLabel(
                "<b>方式三：使用自己的模型服务</b><br>"
                "<small>OpenAI 兼容端点（DeepSeek / Kimi / Ollama / 公司网关…），不需要 Copilot 订阅。</small>"
            )
        )
        btn_custom = QPushButton("改用自己的 API")
        btn_custom.clicked.connect(self._choose_custom)
        lay.addWidget(btn_custom)
        self.info = QLabel("")
        lay.addWidget(self.info)

    def _choose_custom(self) -> None:
        self.use_custom_requested.emit()
        self.accept()

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
            msg.warn(parent, "下载失败", err)
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
    duplex_play = Signal(str, str, str)  # (方向, 文本, start|done|direct|clear)
    home_progress = Signal(str, str, int, int)  # (kind, 文本, 已完成, 总量)
    home_done = Signal(str, bool, str)  # (kind, 成功, 消息)
    job_done = Signal(object, object)  # (tag, result 或 Exception)；tag 可为 ("cb", 回调)


def open_path(p: Path) -> None:
    import os

    os.startfile(p)  # type: ignore[attr-defined]
