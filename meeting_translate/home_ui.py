"""三入口主界面：会议字幕、划词翻译、双向语音同传。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
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
"""


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
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(4)
        lay.addLayout(head)
        lay.addWidget(desc)
        lay.addWidget(self.progress)
        lay.addLayout(row)
        lay.addStretch(1)

    def set_state(
        self,
        status: str,
        *,
        primary: str | None = None,
        secondary: str | None = None,
        busy: bool = False,
        done: int = 0,
        total: int = 0,
    ) -> None:
        self.status.setText(status)
        self.status.setToolTip(status)
        if primary is not None:
            self._primary_key = primary
            self.btn_primary.setText(primary)
            self.btn_primary.setVisible(bool(primary))
            self.btn_primary.setEnabled(bool(primary) and not busy)
        if secondary is not None:
            self._secondary_key = secondary
            self.btn_secondary.setText(secondary)
            self.btn_secondary.setVisible(bool(secondary))
            self.btn_secondary.setEnabled(not busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(max(0, done))
        self.btn_primary.setEnabled(not busy and bool(self.btn_primary.text()))
        self.btn_secondary.setEnabled(not busy and bool(self.btn_secondary.text()))


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

        question = QLabel("今天要做什么？")
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

        cards = QHBoxLayout()
        cards.setSpacing(16)
        for c in (self.card_meeting, self.card_selection, self.card_duplex):
            cards.addWidget(c, 1)

        self.model_card = ResourceCard(
            "语音模型",
            f"中文 MeloTTS + 日文 SBV2，约 {total_download_text()}；首次还要装一个 Python 运行时。",
            primary="下载",
            secondary="详细",
        )
        self.model_card.setToolTip(download_summary_lines())
        self.cable_card = ResourceCard(
            "虚拟声卡",
            "CABLE-A 收对方声音，CABLE-B 送日语给对方。",
            primary="下载安装",
            secondary="详细",
        )
        self.cable_card.setToolTip(
            "下载 VB-Audio CABLE A+B（VBCABLE_Driver_Pack45）与 Hi-Fi Cable（HiFiCableAsioBridgeSetup）\n"
            "CABLE-A Input：会议软件把对方声音放这里，本程序环回采集\n"
            "CABLE-B Output：会议软件的麦克风选它，本程序往 CABLE-B 送日语 TTS"
        )
        self.teams_card = ResourceCard(
            "会议软件配置",
            "以 Teams 为例：扬声器选 CABLE Input、麦克风选 Hi-Fi Cable Output。Zoom、腾讯会议、Google Meet 同理，只是入口在各自音频设置里。",
            primary="打开 Teams",
            secondary="配置步骤",
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

    def set_model_state(self, status: str, *, primary: str | None = None, busy: bool = False, done: int = 0, total: int = 0) -> None:
        self.model_card.set_state(status, primary=primary, busy=busy, done=done, total=total)

    def set_cable_state(self, status: str, *, primary: str | None = None, busy: bool = False, done: int = 0, total: int = 0) -> None:
        self.cable_card.set_state(status, primary=primary, busy=busy, done=done, total=total)

    def set_teams_state(self, status: str) -> None:
        self.teams_card.set_state(status)

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
