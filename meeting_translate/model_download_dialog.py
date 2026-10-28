"""语音模型下载窗：按项列出运行时与三组模型，每行一个「下载 / 暂停 / 继续」，底部「全部下载」「全部暂停」。

各行可以同时下载；暂停后 .part 保留，「继续」从断点续传。

窗口只负责展示和发出请求；实际下载在 App 的后台线程里做，进度经
update_rows() 回灌，所以关掉窗口下载也会继续，重新打开能看到当前状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .resource_details import DETAIL_QSS

DIALOG_QSS = DETAIL_QSS + """
QLabel#dlgNote {
    color: #f0b75a; background: #2a2416; border: 1px solid #4a3d1f;
    border-radius: 9px; padding: 8px 10px; font-size: 12px;
}
QTableWidget#modelTable {
    background: #12151c; color: #c9d2e3; border: 1px solid #272d3a;
    border-radius: 12px; gridline-color: #20252f; font-size: 12px;
}
QTableWidget#modelTable::item { padding: 6px; }
QHeaderView::section {
    background: #171a21; color: #8991a4; border: none;
    border-bottom: 1px solid #272d3a; padding: 6px; font-size: 12px;
}
QPushButton#rowDownload, QPushButton#detailPrimary {
    color: white; background: #4f7cff; border: none; border-radius: 8px;
    padding: 6px 14px; font-weight: 700;
}
QPushButton#detailPrimary { padding: 9px 18px; border-radius: 9px; }
QPushButton#rowDownload:hover, QPushButton#detailPrimary:hover { background: #6a91ff; }
QPushButton#rowDownload:disabled, QPushButton#detailPrimary:disabled { color: #6f788c; background: #2a303d; }
QProgressBar#dlgProgress {
    background: #202838; border: none; border-radius: 3px;
    min-height: 6px; max-height: 6px;
}
QProgressBar#dlgProgress::chunk { background: #5b8cff; border-radius: 3px; }
QLabel#rowStatus { color: #c9d2e3; font-size: 12px; background: transparent; }
QPushButton#rowDownload[mode="pause"] {
    color: #f0b75a; background: #2a2416; border: 1px solid #4a3d1f;
}
QPushButton#rowDownload[mode="pause"]:hover { background: #3a3120; border-color: #f0b75a; }
QPushButton#rowDownload[mode="resume"] { background: #3d6be0; }
"""

COLUMNS = ("项目", "大小", "来源", "状态", "")


@dataclass(frozen=True)
class ModelRow:
    key: str       # runtime / melo / sbv2 / bert
    name: str
    role: str
    size: str      # 还需下载的量；已就绪时为总量
    source: str
    ready: bool
    status: str


class ModelDownloadDialog(QDialog):
    download_requested = Signal(str)
    pause_requested = Signal(str)
    download_all_requested = Signal()
    pause_all_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("detailDialog")
        self.setWindowTitle("语音模型")
        self.resize(940, 520)
        self.setMinimumSize(720, 420)
        self.setStyleSheet(DIALOG_QSS)
        self._keys: list[str] = []
        self._buttons: dict[str, QPushButton] = {}
        self._modes: dict[str, str] = {}
        self._status: dict[str, tuple[QLabel, QProgressBar]] = {}

        title = QLabel("语音模型")
        title.setObjectName("detailTitle")
        sub = QLabel(
            "运行时与三组模型可以逐项下载，也可以同时下载多项或一次全部下载，随时可以暂停；"
            "四项全部就绪（再加虚拟声卡）后才能进入「03 语音实时转译」。"
            "模型先走 hf-mirror，失败改走 huggingface.co，每个文件下完核对 SHA256；暂停或中断后点「继续」从断点续传。"
        )
        sub.setObjectName("detailSub")
        sub.setWordWrap(True)
        note = QLabel(
            "仅「03 语音实时转译」需要。只用「01 会议实时翻译」「02 划词翻译」的话不必下载。"
            "下载来源与校验方式见首页卡片的「详细」。"
        )
        note.setObjectName("dlgNote")
        note.setWordWrap(True)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("modelTable")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setWordWrap(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(3, 270)
        header.resizeSection(4, 104)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("detailSub")
        self.progress_label.setWordWrap(True)

        self.btn_all = QPushButton("全部下载")
        self.btn_all.setObjectName("detailPrimary")
        self.btn_all.clicked.connect(self.download_all_requested)
        self.btn_pause_all = QPushButton("全部暂停")
        self.btn_pause_all.setObjectName("detailGhost")
        self.btn_pause_all.clicked.connect(self.pause_all_requested)
        self.btn_pause_all.hide()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("detailGhost")
        close_btn.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.progress_label, 1)
        buttons.addWidget(self.btn_pause_all)
        buttons.addWidget(self.btn_all)
        buttons.addWidget(close_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(8)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addWidget(note)
        lay.addSpacing(4)
        lay.addWidget(self.table, 1)
        lay.addLayout(buttons)

    def update_rows(
        self,
        rows: list[ModelRow],
        *,
        jobs: dict[str, tuple[str, int, int]] | None = None,
        paused: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        """jobs：正在下载的行 key → (文案, 已下, 总量)；paused：暂停过、还没下完的行。"""
        jobs = jobs or {}
        if [r.key for r in rows] != self._keys:
            self._rebuild(rows)
        for i, row in enumerate(rows):
            label, bar = self._status[row.key]
            job = jobs.get(row.key)
            if job is not None:
                text, done, total = job
                pct = f"{done * 100 // total}% · " if total > 0 else ""
                label.setText("⏳ " + pct + (text or row.status))
                # total=0：运行时安装没有字节进度，用来回滚动的忙碌条
                bar.setRange(0, max(0, total))
                bar.setValue(max(0, min(done, total)) if total > 0 else 0)
                bar.show()
                mode, caption = "pause", "暂停"
            elif row.ready:
                label.setText("✅ " + row.status)
                bar.hide()
                mode, caption = "", "已就绪"
            else:
                was_paused = row.key in paused
                label.setText(("⏸ 已暂停 · " if was_paused else "") + row.status)
                bar.hide()
                resumable = was_paused or "续传" in row.status
                mode, caption = ("resume", "继续") if resumable else ("download", "下载")
            label.setToolTip(label.text())
            self._set(i, 1, row.size)
            btn = self._buttons[row.key]
            btn.setText(caption)
            btn.setEnabled(bool(mode))
            if self._modes.get(row.key) != mode:
                self._modes[row.key] = mode
                btn.setProperty("mode", mode)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
        idle = [r for r in rows if not r.ready and r.key not in jobs]
        self.btn_all.setEnabled(bool(idle))
        self.btn_all.setText("全部下载" if idle or jobs else "全部已就绪")
        self.btn_pause_all.setVisible(bool(jobs))
        self.progress_label.setText(
            f"{len(jobs)} 项下载中，关掉本窗口也会继续；首页卡片上点「查看进度」可回到这里。" if jobs else ""
        )
        self.table.resizeRowsToContents()

    def _clicked(self, key: str) -> None:
        if self._modes.get(key) == "pause":
            self.pause_requested.emit(key)
        else:
            self.download_requested.emit(key)

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        # 列宽要等布局完成才确定；之前算的行高会按过窄的列算出超高行
        self.table.resizeRowsToContents()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.table.resizeRowsToContents()

    def _rebuild(self, rows: list[ModelRow]) -> None:
        self._keys = [r.key for r in rows]
        self._buttons.clear()
        self._modes.clear()
        self._status.clear()
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self._set(i, 0, f"{row.name}\n{row.role}")
            self._set(i, 2, row.source)
            status = QWidget()
            status_box = QVBoxLayout(status)
            status_box.setContentsMargins(6, 6, 6, 6)
            status_box.setSpacing(5)
            label = QLabel(row.status)
            label.setObjectName("rowStatus")
            label.setWordWrap(True)
            bar = QProgressBar()
            bar.setObjectName("dlgProgress")
            bar.setTextVisible(False)
            bar.hide()
            status_box.addWidget(label)
            status_box.addWidget(bar)
            self._status[row.key] = (label, bar)
            self.table.setCellWidget(i, 3, status)
            btn = QPushButton("下载")
            btn.setObjectName("rowDownload")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedWidth(84)
            btn.clicked.connect(lambda _=False, k=row.key: self._clicked(k))
            self._buttons[row.key] = btn
            cell = QWidget()
            box = QHBoxLayout(cell)
            box.setContentsMargins(8, 0, 8, 0)
            box.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
            self.table.setCellWidget(i, 4, cell)

    def _set(self, row: int, col: int, text: str) -> None:
        item = self.table.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, col, item)
        item.setText(text)
        item.setToolTip(text)
