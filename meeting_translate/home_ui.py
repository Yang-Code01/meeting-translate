"""三入口主界面：会议字幕、划词翻译、双向语音同传。"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .config import Settings
from .tts_models import download_summary_lines, total_download_text

HOME_QSS = """
QWidget#home { background: #0f1115; }
QLabel#brandMark {
    background: #5b8cff; color: white; border-radius: 12px;
    font-size: 24px; font-weight: 800;
}
QLabel#brandTitle { color: #f4f6fb; font-size: 22px; font-weight: 700; }
QLabel#brandSub { color: #8991a4; font-size: 12px; }
QLabel#question { color: #f4f6fb; font-size: 34px; font-weight: 800; }
QLabel#questionSub { color: #8991a4; font-size: 13px; }
QLabel#statusChip {
    color: #b9c2d6; background: #191d26; border: 1px solid #2a3040;
    border-radius: 14px; padding: 5px 12px; font-size: 12px;
}
QFrame#modeCard {
    background: #171a21; border: 1px solid #272d3a; border-radius: 18px;
}
QFrame#modeCard:hover { background: #1b1f29; border-color: #4b6dff; }
QFrame#modeCard[accent="true"] { background: #171c2a; border-color: #3b5fe0; }
QFrame#modeCard[active="true"] { border-color: #5b8cff; }
QLabel#modeNumber { color: #667089; font-size: 11px; font-weight: 700; }
QLabel#modeTitle { color: #f4f6fb; font-size: 20px; font-weight: 700; }
QLabel#modeDesc { color: #aeb7c9; font-size: 13px; }
QLabel#modeBullets { color: #7f8aa1; font-size: 12px; }
QLabel#badge {
    color: #9db7ff; background: #223158; border-radius: 9px;
    padding: 2px 8px; font-size: 10px; font-weight: 700;
}
QLabel#hotkey { color: #7f8aa1; font-size: 12px; }
QLabel#hotkeyChip {
    color: #9fb2d9; background: #121722; border: 1px dashed #3a4a6b;
    border-radius: 9px; padding: 9px 12px; font-size: 12px; font-weight: 500;
}
QPushButton#primary {
    color: white; background: #4f7cff; border: none; border-radius: 10px;
    padding: 10px 16px; font-size: 13px; font-weight: 700;
}
QPushButton#primary:hover { background: #6a91ff; }
QPushButton#primary:disabled { color: #6f788c; background: #2a303d; }
QLabel#modeLock {
    color: #f0b75a; background: #2a2416; border: 1px solid #4a3d1f;
    border-radius: 9px; padding: 7px 10px; font-size: 12px;
}
QPushButton#ghost {
    color: #dce3f2; background: #202635; border: 1px solid #313a4f;
    border-radius: 10px; padding: 10px 16px; font-size: 13px;
}
QPushButton#ghost:hover { background: #283149; border-color: #4b6dff; }
QPushButton#topGhost {
    color: #aeb7c9; background: transparent; border: 1px solid #2a3040;
    border-radius: 8px; padding: 6px 12px; font-size: 12px;
}
QPushButton#topGhost:hover { color: #f4f6fb; border-color: #4b6dff; }
QPushButton:focus { outline: none; }
QFrame#resourceCard {
    background: #141821; border: 1px solid #242b38; border-radius: 14px;
}
QFrame#resourceCard:hover { border-color: #3556b8; background: #171d29; }
QLabel#resourceTitle { color: #e9eefb; font-size: 13px; font-weight: 700; }
QLabel#resourceStatus { color: #8991a4; font-size: 11px; }
QProgressBar#cardProgress {
    background: #202838; border: none; border-radius: 3px; max-height: 5px;
}
QProgressBar#cardProgress::chunk { background: #5b8cff; border-radius: 3px; }
QLabel#scopeTag {
    color: #f0b75a; background: #2a2416; border: 1px solid #4a3d1f;
    border-radius: 9px; padding: 2px 8px; font-size: 10px; font-weight: 700;
}
QLabel#scopeTag:hover { border-color: #f0b75a; }
QToolTip {
    color: #dce3f2; background: #1b2030; border: 1px solid #313a4f;
    padding: 6px 8px; font-size: 12px;
}
"""

DUPLEX_ONLY_TAG = "语音转换 Only"
DUPLEX_ONLY_TIP = "仅「03 语音实时转译」需要。\n只用「01 会议实时翻译」「02 划词翻译」不必下载、安装或配置。"

SLOGANS = (
    "言通中日，语达八方。",
    "一言既出，两国皆闻。",
    "耳聪目明，天堑咫尺。",
    "天涯若比邻，异语亦同声。",
    "听其言，知其意，传其声。",
    "言语无隔，心意相通。",
    "信达雅，弹指间。",
    "千里之会，如在一室。",
    "书同文，语同声。",
    "不费吹灰之力，而通异邦之语。",
)


class SloganLabel(QLabel):
    """主页大标题：几条标语定时淡出淡入轮换；窗口隐藏时停表。"""

    def __init__(self, slogans: tuple[str, ...] = SLOGANS, interval_ms: int = 4500, parent: QWidget | None = None) -> None:
        super().__init__(slogans[0], parent)
        self._slogans = slogans
        self._index = 0
        self._fading_out = False
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(350)
        self._fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade.finished.connect(self._on_fade_finished)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._fade_out)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()
        self._fade.stop()
        self._fading_out = False
        self._effect.setOpacity(1.0)

    def advance(self) -> None:
        self._index = (self._index + 1) % len(self._slogans)
        self.setText(self._slogans[self._index])

    def _fade_out(self) -> None:
        if self._fade.state() == QPropertyAnimation.State.Running:
            return
        self._fading_out = True
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_finished(self) -> None:
        if self._fading_out:
            self._fading_out = False
            self.advance()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()


class ModeCard(QFrame):
    activated = Signal()

    def __init__(
        self,
        number: str,
        title: str,
        description: str,
        bullets: list[str],
        action: str | None = None,
        *,
        accent: bool = False,
        badge: str = "",
        hotkey: str = "",
    ) -> None:
        super().__init__()
        self.setObjectName("modeCard")
        self.setProperty("accent", "true" if accent else "false")
        self.setProperty("active", "false")
        self.setMinimumHeight(292)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if action else Qt.CursorShape.ArrowCursor
        )
        self._button: QPushButton | None = None
        if action:
            self._button = QPushButton(action)
            # 两张可进入的卡都用蓝色主按钮（划词卡没有按钮，只展示快捷键）。
            self._button.setObjectName("primary")
            self._button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._button.clicked.connect(self.activated)
        self._hotkey = QLabel(f"快捷键  {hotkey}") if hotkey else None
        if self._hotkey is not None:
            self._hotkey.setObjectName("hotkeyChip")
            self._hotkey.setAlignment(Qt.AlignmentFlag.AlignCenter)

        top = QHBoxLayout()
        num = QLabel(number)
        num.setObjectName("modeNumber")
        top.addWidget(num)
        top.addStretch(1)
        if badge:
            b = QLabel(badge)
            b.setObjectName("badge")
            top.addWidget(b)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("modeTitle")
        desc = QLabel(description)
        desc.setObjectName("modeDesc")
        desc.setWordWrap(True)
        bullet_lbl = QLabel("\n".join(f"· {b}" for b in bullets))
        bullet_lbl.setObjectName("modeBullets")
        bullet_lbl.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(10)
        lay.addLayout(top)
        lay.addSpacing(4)
        lay.addWidget(title_lbl)
        lay.addWidget(desc)
        lay.addSpacing(2)
        lay.addWidget(bullet_lbl)
        lay.addStretch(1)
        # 前置依赖没装好时，在按钮上方说明缺什么；按钮置灰
        self._lock = QLabel("")
        self._lock.setObjectName("modeLock")
        self._lock.setWordWrap(True)
        self._lock.hide()
        lay.addWidget(self._lock)
        if self._button is not None:
            lay.addWidget(self._button)
        elif self._hotkey is not None:
            lay.addWidget(self._hotkey)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 120))
        self.setGraphicsEffect(shadow)

    def set_hotkey(self, hotkey: str) -> None:
        if self._hotkey is not None:
            self._hotkey.setText(f"快捷键  {hotkey}")

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_locked(self, reason: str) -> None:
        """reason 非空 = 不可进入：按钮置灰并显示原因。点卡片仍会发 activated，由上层弹提示。"""
        self._lock.setText(reason)
        self._lock.setVisible(bool(reason))
        if self._button is not None:
            self._button.setEnabled(not reason)
            self._button.setToolTip(reason)

    @property
    def locked(self) -> bool:
        return bool(self._lock.text())

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mouseReleaseEvent(e)


class ResourceCard(QFrame):
    """首页底部的一张资源卡：模型、虚拟声卡、会议软件配置。"""

    action_requested = Signal(str)

    def __init__(
        self,
        title: str,
        description: str,
        *,
        primary: str = "",
        secondary: str = "",
        duplex_only: bool = False,
    ) -> None:
        super().__init__()
        self.setObjectName("resourceCard")
        self.setMinimumHeight(118)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._primary_key = primary
        self._secondary_key = secondary
        title_lbl = QLabel(title)
        title_lbl.setObjectName("resourceTitle")
        self.status = QLabel("检测中…")
        self.status.setObjectName("resourceStatus")
        self.status.setWordWrap(True)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(title_lbl)
        head.addStretch(1)
        head.addWidget(self.status)
        desc = QLabel(description)
        desc.setObjectName("resourceStatus")
        desc.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setObjectName("cardProgress")
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.btn_primary = QPushButton(primary)
        self.btn_secondary = QPushButton(secondary)
        for b in (self.btn_primary, self.btn_secondary):
            b.setObjectName("topGhost")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_primary.clicked.connect(lambda: self.action_requested.emit(self._primary_key))
        self.btn_secondary.clicked.connect(lambda: self.action_requested.emit(self._secondary_key))
        # 没有 secondary 的卡只留主按钮；有的话一开始就让看得见（set_state 不会代劳）
        self.btn_secondary.setVisible(bool(secondary))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.btn_primary)
        row.addWidget(self.btn_secondary)
        row.addStretch(1)
        # 右下角角标：只有「03 语音实时转译」才用得到，悬停看说明
        self.scope_tag: QLabel | None = None
        if duplex_only:
            self.scope_tag = QLabel(DUPLEX_ONLY_TAG)
            self.scope_tag.setObjectName("scopeTag")
            self.scope_tag.setToolTip(DUPLEX_ONLY_TIP)
            row.addWidget(self.scope_tag, 0, Qt.AlignmentFlag.AlignBottom)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(desc)
        lay.addWidget(self.progress)
        lay.addStretch(1)
        lay.addLayout(row)

    def set_state(
        self,
        status: str,
        *,
        primary: str | None = None,
        secondary: str | None = None,
        busy: bool = False,
        done: int = 0,
        total: int = 0,
        keep_buttons: bool = False,
    ) -> None:
        """keep_buttons=True：显示进度的同时按钮仍可点（如「查看进度」）。"""
        self.status.setText(status)
        self.status.setToolTip(status)
        locked = busy and not keep_buttons
        if primary is not None:
            self._primary_key = primary
            self.btn_primary.setText(primary)
            self.btn_primary.setVisible(bool(primary))
            self.btn_primary.setEnabled(bool(primary) and not locked)
        if secondary is not None:
            self._secondary_key = secondary
            self.btn_secondary.setText(secondary)
            self.btn_secondary.setVisible(bool(secondary))
            self.btn_secondary.setEnabled(not locked)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(max(0, done))
        self.btn_primary.setEnabled(not locked and bool(self.btn_primary.text()))
        self.btn_secondary.setEnabled(not locked and bool(self.btn_secondary.text()))


class HomeWindow(QWidget):
    """三入口主界面。"""

    mode_selected = Signal(str)
    selection_requested = Signal()
    settings_requested = Signal()
    diagnostics_requested = Signal()
    trial_requested = Signal()
    model_requested = Signal(str)
    cable_requested = Signal(str)
    teams_requested = Signal(str)
    quit_requested = Signal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.setObjectName("home")
        self.setWindowTitle("会议翻译")
        self.resize(1080, 780)
        self.setMinimumSize(900, 640)
        self.setStyleSheet(HOME_QSS)
        self._placed = False

        mark = QLabel("译")
        mark.setObjectName("brandMark")
        mark.setFixedSize(46, 46)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("会议翻译")
        title.setObjectName("brandTitle")
        sub = QLabel("中日实时沟通工作台")
        sub.setObjectName("brandSub")
        brand = QVBoxLayout()
        brand.setSpacing(1)
        brand.addWidget(title)
        brand.addWidget(sub)
        header_left = QHBoxLayout()
        header_left.setSpacing(12)
        header_left.addWidget(mark)
        header_left.addLayout(brand)
        header_left.addStretch(1)

        self.status_chip = QLabel("正在连接模型服务…")
        self.status_chip.setObjectName("statusChip")
        self.status_chip.setMaximumWidth(380)
        btn_trial = QPushButton("试一句")
        btn_trial.setObjectName("topGhost")
        btn_trial.setToolTip("录一句中文，听翻译后的日文")
        btn_trial.clicked.connect(self.trial_requested)
        btn_diag = QPushButton("自检")
        btn_diag.setObjectName("topGhost")
        btn_diag.setToolTip("检查 CABLE、麦克风和 TTS 是否就绪")
        btn_diag.clicked.connect(self.diagnostics_requested)
        btn_settings = QPushButton("⚙")
        btn_settings.setObjectName("topGhost")
        btn_settings.setToolTip("设置")
        btn_settings.setFixedWidth(38)
        btn_settings.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_settings.clicked.connect(self.settings_requested)
        btn_quit = QPushButton("退出")
        btn_quit.setObjectName("topGhost")
        btn_quit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_quit.clicked.connect(self.quit_requested)
        header = QHBoxLayout()
        header.addLayout(header_left, 1)
        header.addWidget(self.status_chip)
        header.addWidget(btn_trial)
        header.addWidget(btn_diag)
        header.addWidget(btn_settings)
        header.addWidget(btn_quit)

        question = SloganLabel()
        question.setObjectName("question")
        question_sub = QLabel("三种入口共用同一套模型、术语表与记录目录；进入后可在悬浮窗里开始或结束会话。")
        question_sub.setObjectName("questionSub")
        question_sub.setWordWrap(True)

        self.card_meeting = ModeCard(
            "01",
            "会议实时翻译",
            "把对方的话变成中文字幕，适合听会、访谈和单向会议。",
            ["环回采集会议声音", "中日字幕 + 会后纪要", "不改变会议里的声音"],
            "进入会议字幕",
        )
        self.card_selection = ModeCard(
            "02",
            "划词翻译",
            "鼠标选中词语或者句子，按下快捷键，进入翻译。支持英语、日语转换为中文。",
            ["全局快捷键直接触发", "中文 + 汉字注音", "自动保存划词历史"],
            hotkey=settings.selection_hotkey,
        )
        self.card_duplex = ModeCard(
            "03",
            "语音实时转译",
            "你说中文，对方听到日语；对方说日语，你听到中文。会议软件以 Teams 为例：扬声器选 CABLE Input、麦克风选 Hi-Fi Cable Output；Zoom、腾讯会议等改法相同。",
            ["双向语音同传", "CABLE + Hi-Fi 路由", "字幕与纪要保留"],
            "进入双向同传",
            accent=True,
            badge="Beta",
        )
        self.card_meeting.activated.connect(lambda: self.mode_selected.emit("captions"))
        # 划词翻译常驻全局快捷键，不设“开始划词”按钮；卡片只展示快捷键。
        self.card_duplex.activated.connect(lambda: self.mode_selected.emit("duplex"))
        self.card_duplex.set_locked("正在检测语音模型与虚拟声卡…")

        cards = QHBoxLayout()
        cards.setSpacing(16)
        for c in (self.card_meeting, self.card_selection, self.card_duplex):
            cards.addWidget(c, 1)

        self.model_card = ResourceCard(
            "语音模型",
            f"中文 MeloTTS + 日文 SBV2，约 {total_download_text()}，另需约 400MB 运行时。",
            primary="下载",
            secondary="详细",
            duplex_only=True,
        )
        self.model_card.setToolTip(download_summary_lines())
        self.cable_card = ResourceCard(
            "虚拟声卡",
            "CABLE-A 收对方声音，CABLE-B 送日语给对方。",
            primary="下载安装",
            secondary="详细",
            duplex_only=True,
        )
        self.cable_card.setToolTip(
            "下载 VB-Audio CABLE A+B（VBCABLE_Driver_Pack45）与 Hi-Fi Cable（HiFiCableAsioBridgeSetup）\n"
            "CABLE-A Input：会议软件把对方声音放这里，本程序环回采集\n"
            "CABLE-B Output：会议软件的麦克风选它，本程序往 CABLE-B 送日语 TTS"
        )
        self.teams_card = ResourceCard(
            "会议软件配置",
            "以 Teams 为例：扬声器选 CABLE Input、麦克风选 Hi-Fi Cable Output，Zoom、腾讯会议、Google Meet 同理。",
            primary="打开 Teams",
            secondary="配置步骤",
            duplex_only=True,
        )
        self.model_card.action_requested.connect(self.model_requested)
        self.cable_card.action_requested.connect(self.cable_requested)
        self.teams_card.action_requested.connect(self.teams_requested)
        resources = QHBoxLayout()
        resources.setSpacing(12)
        for c in (self.model_card, self.cable_card, self.teams_card):
            resources.addWidget(c, 1)

        self.hotkey_label = QLabel("")
        self.hotkey_label.setObjectName("hotkey")
        version = QLabel("v0.3.0 · 本地优先")
        version.setObjectName("hotkey")
        footer = QHBoxLayout()
        footer.addWidget(self.hotkey_label, 1)
        footer.addWidget(version)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(34, 28, 34, 24)
        lay.setSpacing(18)
        lay.addLayout(header)
        lay.addSpacing(8)
        lay.addWidget(question)
        lay.addWidget(question_sub)
        lay.addSpacing(6)
        lay.addLayout(cards, 1)
        lay.addLayout(resources)
        lay.addLayout(footer)
        self.refresh(settings)

    def set_model_state(
        self, status: str, *, primary: str | None = None, busy: bool = False, done: int = 0, total: int = 0, keep_buttons: bool = False
    ) -> None:
        self.model_card.set_state(status, primary=primary, busy=busy, done=done, total=total, keep_buttons=keep_buttons)

    def set_cable_state(self, status: str, *, primary: str | None = None, busy: bool = False, done: int = 0, total: int = 0) -> None:
        self.cable_card.set_state(status, primary=primary, busy=busy, done=done, total=total)

    def set_teams_state(self, status: str) -> None:
        self.teams_card.set_state(status)

    def set_duplex_blockers(self, blockers: list[str]) -> None:
        """「03 语音实时转译」的前置依赖没齐时锁住入口。"""
        reason = ("需先装好：" + "、".join(blockers) + "（见下方卡片）") if blockers else ""
        self.card_duplex.set_locked(reason)

    def refresh(self, settings: Settings) -> None:
        self.hotkey_label.setText(
            f"会议快捷键  {settings.hotkey}    ·    全局划词  {settings.selection_hotkey}"
        )
        self.card_selection.set_hotkey(settings.selection_hotkey)
        self.set_mode(settings.meeting_mode)

    def set_mode(self, mode: str) -> None:
        self.card_meeting.set_active(mode != "duplex")
        self.card_duplex.set_active(mode == "duplex")

    def set_status(self, text: str) -> None:
        self.status_chip.setText(text)
        self.status_chip.setToolTip(text)

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        if not self._placed:
            screen = self.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                self.move(
                    area.left() + max(0, (area.width() - self.width()) // 2),
                    area.top() + max(0, (area.height() - self.height()) // 2),
                )
            self._placed = True

    def closeEvent(self, e) -> None:  # noqa: N802
        # 关窗口 = 收进托盘，不退出程序
        self.hide()
        e.ignore()
