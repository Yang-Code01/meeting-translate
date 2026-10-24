"""应用控制器：串起音频采集、识别、翻译、记录、界面与托盘。"""

from __future__ import annotations

import concurrent.futures
import itertools
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QSystemTrayIcon
from PySide6.QtCore import Qt, QTimer

from . import glossary, lang, stats
from .asr import AsrEngine, AsrWorker, Final, ModelMissing, create_engine
from .audio import ME, THEM, Capture
from .config import (
    GLOSSARY_FILE,
    LOG_FILE,
    SELECTION_HISTORY_FILE,
    TRANSCRIPT_DIR,
    Settings,
    ensure_dirs,
    use_bundled_runtime,
)
from .hotkey import GlobalHotkey
from .joiner import Joiner, Pending
from .selection import SelectionCapture, SelectionRecord, append_history
from .summarize import summarize
from .transcript import Entry, Transcript, fmt_aic
from .translator import AuthState, ModelOption, Quota, Translator
from .ui import (
    FRAME_ALLOWANCE,
    MODEL_LABELS,
    Bridge,
    LoginDialog,
    Overlay,
    SelectionResultPopup,
    SettingsDialog,
    download_model,
    model_label,
    open_path,
    place_beside,
    sorted_models,
)

log = logging.getLogger(__name__)

IDLE, RUNNING, PAUSED = "idle", "running", "paused"
QUOTA_INTERVAL_MS = 5 * 60 * 1000
LOW_QUOTA_PERCENT = 10.0


def _icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2b7de9"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, 64, 64, 14, 14)
    p.setPen(QColor("white"))
    p.setFont(QFont("Microsoft YaHei UI", 30, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "译")
    p.end()
    return QIcon(pm)


class App:
    def __init__(self, qapp: QApplication) -> None:
        self.qapp = qapp
        self.settings = Settings.load()
        glossary.load()
        self.state = IDLE
        self.auth = AuthState(False, message="连接中")
        self.models: list[ModelOption] = []
        self.joiner: Joiner | None = None
        self.quota: Quota | None = None
        self.usage_aic = 0.0
        self._meeting_price: float | None = None
        self._settings_dlg: SettingsDialog | None = None
        self.transcript: Transcript | None = None
        self.capture: Capture | None = None
        self.asr: AsrWorker | None = None
        self._engine: AsrEngine | None = None
        self._engine_key: tuple | None = None
        self._engine_lock = threading.Lock()
        self._login: LoginDialog | None = None
        self._busy = False
        self._summary_target: Transcript | None = None
        self._last_output: Path | None = None
        self._pending_selection_history: list[SelectionRecord] = []
        self._selection_request_id = 0
        self._latest_selection_request = 0

        self.bridge = Bridge()
        self.bridge.partial.connect(self._on_partial)
        self.bridge.final.connect(self._on_final)
        self.bridge.translated.connect(self._on_translated)
        self.bridge.status.connect(self._set_status)
        self.bridge.auth.connect(self._on_auth)
        self.bridge.hotkey.connect(self.toggle)
        self.bridge.selection_hotkey.connect(self._capture_selection)
        self.bridge.job_done.connect(self._on_job_done)
        self.bridge.usage.connect(self._on_usage)
        self.bridge.progress.connect(self._on_summary_progress)

        self.overlay = Overlay(self.settings)
        self.overlay.setWindowIcon(_icon())
        self.overlay.toggle_clicked.connect(self.toggle)
        self.overlay.stop_clicked.connect(self.end_meeting)
        self.overlay.settings_clicked.connect(self.open_settings)
        self.overlay.mic_toggled.connect(self.set_mic_enabled)
        self.overlay.retry_clicked.connect(self._retry_summary)
        self.overlay.geometry_changed.connect(self._save_geometry)
        self.overlay.show()
        self.selection_popup = SelectionResultPopup()
        self.selection_popup.apply_style(self.settings)
        self.selection_popup.user_resized.connect(self._save_selection_popup_size)
        self.selection_capture = SelectionCapture(self.qapp)
        self.selection_capture.captured.connect(self._on_selection_captured)
        self.selection_capture.failed.connect(self._on_selection_capture_failed)

        self._build_tray()
        self.hotkey: GlobalHotkey | None = None
        self._register_hotkey()
        self.selection_hotkey: GlobalHotkey | None = None
        self._register_selection_hotkey()

        self.translator = Translator(
            on_translation=self.bridge.translated.emit,
            on_status=self.bridge.status.emit,
            on_auth=self.bridge.auth.emit,
            on_usage=self.bridge.usage.emit,
        )
        self._watch(self.translator.start(), "start")

        self._join_timer = QTimer()
        self._join_timer.setInterval(100)
        self._join_timer.timeout.connect(lambda: self.joiner and self.joiner.tick(time.monotonic()))
        self._join_timer.start()
        self._quota_timer = QTimer()
        self._quota_timer.setInterval(QUOTA_INTERVAL_MS)
        self._quota_timer.timeout.connect(self._refresh_quota)
        threading.Thread(target=self._preload_engine, daemon=True).start()

    # ---- 基础设施 ----

    def _watch(self, fut: concurrent.futures.Future, tag) -> None:
        def done(f: concurrent.futures.Future) -> None:
            try:
                self.bridge.job_done.emit(tag, f.result())
            except Exception as e:  # noqa: BLE001
                log.exception("任务失败: %s", tag)
                self.bridge.job_done.emit(tag, e)

        fut.add_done_callback(done)

    def _set_status(self, text: str) -> None:
        self.overlay.status.setText(text)
        self.tray.setToolTip(f"会议翻译 - {text}")

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(_icon(), self.qapp)
        menu = QMenu()
        self.act_toggle = menu.addAction("开始", self.toggle)
        self.act_end = menu.addAction("结束会议", self.end_meeting)
        menu.addSeparator()
        menu.addAction("显示/隐藏字幕窗", self._toggle_overlay)
        menu.addAction("设置…", self.open_settings)
        self._model_menu = menu.addMenu("模型")
        self._model_menu.aboutToShow.connect(self._fill_model_menu)
        menu.addAction("编辑术语表", lambda: open_path(GLOSSARY_FILE))
        menu.addAction("打开记录文件夹", lambda: open_path(self._save_dir() or TRANSCRIPT_DIR))
        menu.addAction("整理历史会议…", self.summarize_history)
        menu.addAction("登录 Copilot…", lambda: self._show_login(self.auth.message))
        menu.addSeparator()
        menu.addAction("退出", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._toggle_overlay() if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.messageClicked.connect(lambda: self._last_output and open_path(self._last_output))
        self.tray.show()
        self._tray_menu = menu
        self._sync_controls()

    def _fill_model_menu(self) -> None:
        m = self._model_menu
        m.clear()
        if not self.models:
            m.addAction("（正在获取模型列表…）").setEnabled(False)
            self._refresh_models()
            return
        ordered, best = sorted_models(self.models)
        for o in ordered:
            act = m.addAction(model_label(o, self.models, best), lambda mid=o.id: self.set_model(mid))
            act.setCheckable(True)
            act.setChecked(o.id == self.settings.model)
            act.setEnabled(o.enabled)

    def set_model(self, model: str, effort: str | None = None) -> None:
        effort = effort or self.settings.reasoning_effort
        changed = (model, effort) != (self.translator.model, self.translator._effort)
        self.settings.model, self.settings.reasoning_effort = model, effort
        self.settings.save()
        if self.state != IDLE and changed:
            self._set_status(f"正在切换到 {model}（生成前情摘要并预热新会话）…")
            self._watch(self.translator.switch_model(model, effort), ("cb", lambda _r: None))
        if self.auth.ok:
            self.translator.prepare_selection(model, effort)

    def _refresh_models(self, then=None) -> None:
        def done(result) -> None:
            if not isinstance(result, Exception):
                self.models = result
            if then:
                then(result)

        self._watch(self.translator.list_models(), ("cb", done))

    def _refresh_quota(self) -> None:
        def done(result) -> None:
            if isinstance(result, Quota):
                self.quota = result
                self._show_usage()

        self._watch(self.translator.quota(), ("cb", done))

    def _on_usage(self, aic: float) -> None:
        self.usage_aic = aic
        if self.transcript is not None:
            self.transcript.set_usage(aic, self.translator.model)
        self._show_usage()

    def _show_usage(self) -> None:
        s = self.settings
        parts = []
        if s.show_meeting_aic and (self.state != IDLE or self.usage_aic):
            parts.append(f"本场 {fmt_aic(self.usage_aic)} AIC")
        warn = False
        q = self.quota
        if q is not None:
            if s.show_quota_used and q.used is not None:
                limit = f" / {q.entitlement}" if q.entitlement and q.entitlement > 0 and not q.unlimited else ""
                parts.append(f"账户已用 {q.used}{limit} AIC")
            if q.unlimited:
                if s.show_quota_remaining:
                    parts.append("账户不限量")
            elif q.remaining_percent is not None:
                warn = q.remaining_percent < LOW_QUOTA_PERCENT
                if s.show_quota_remaining or warn:  # 额度告急时无论开关都提醒
                    parts.append(f"账户剩余 {q.remaining_percent:.1f}%")
        self.overlay.set_usage(" · ".join(parts), warn)

    def _toggle_overlay(self) -> None:
        self.overlay.setVisible(not self.overlay.isVisible())

    def _register_hotkey(self) -> None:
        if self.hotkey:
            self.hotkey.stop()
        try:
            self.hotkey = GlobalHotkey(self.settings.hotkey, self.bridge.hotkey.emit)
            self.hotkey.start()
            if self.hotkey.error:
                self._set_status(self.hotkey.error)
        except ValueError as e:
            self._set_status(f"快捷键无效：{e}")

    def _register_selection_hotkey(self) -> None:
        if self.selection_hotkey:
            self.selection_hotkey.stop()
        try:
            self.selection_hotkey = GlobalHotkey(
                self.settings.selection_hotkey, self.bridge.selection_hotkey.emit
            )
            self.selection_hotkey.start()
            if self.selection_hotkey.error:
                self._set_status(self.selection_hotkey.error)
        except ValueError as e:
            self._set_status(f"划词翻译快捷键无效：{e}")

    def _capture_selection(self) -> None:
        if not self.selection_capture.start():
            self._set_status("正在读取选区，请稍候…")

    def _on_selection_capture_failed(self, message: str) -> None:
        self._set_status(message)
        self.tray.showMessage("划词翻译", message, QSystemTrayIcon.MessageIcon.Warning, 5000)

    def _on_selection_captured(self, text: str, anchor, warning: str) -> None:
        if warning:
            self._set_status(warning.lstrip("；"))
        self._selection_request_id += 1
        request_id = self._selection_request_id
        self._latest_selection_request = request_id
        self.selection_popup.show_loading(anchor)
        future = self.translator.translate_selected(text, self.settings.model, self.settings.reasoning_effort)
        self._watch(
            future,
            (
                "cb",
                lambda result: self._on_selection_translated(request_id, text, anchor, result),
            ),
        )

    def _on_selection_translated(self, request_id: int, source: str, anchor, result) -> None:
        if isinstance(result, Exception):
            message = f"翻译失败：{result}"
            if request_id == self._latest_selection_request:
                self.selection_popup.show_error(message, anchor)
            self.tray.showMessage("划词翻译失败", str(result), QSystemTrayIcon.MessageIcon.Warning, 5000)
            return
        record = SelectionRecord(datetime.now(), source, result.chinese, result.hiragana)
        self._pending_selection_history.append(record)
        if request_id == self._latest_selection_request:
            self.selection_popup.show_result(result.chinese, result.hiragana, anchor)

    def _save_selection_popup_size(self, width: int, height: int) -> None:
        self.settings.selection_popup_size = [width, height]
        self.settings.save()

    def _save_selection_history(self) -> None:
        parent = self._settings_dlg or self.overlay
        try:
            result = append_history(SELECTION_HISTORY_FILE, self._pending_selection_history)
        except (OSError, UnicodeError) as e:
            log.exception("保存划词翻译历史失败")
            QMessageBox.warning(parent, "保存失败", f"无法写入历史文件：{e}")
            return
        self._pending_selection_history.clear()
        if not (result.added or result.skipped or result.cleaned):
            QMessageBox.information(parent, "翻译历史", "当前没有待保存的划词翻译记录，历史文件中也没有重复。")
            return
        details = [f"新增 {result.added} 条记录"]
        if result.skipped:
            details.append(f"跳过重复 {result.skipped} 条")
        if result.cleaned:
            details.append(f"清理文件中已有重复 {result.cleaned} 条")
        QMessageBox.information(
            parent,
            "保存成功",
            "，".join(details) + f"。\n{SELECTION_HISTORY_FILE}",
        )

    def _save_geometry(self, g: list) -> None:
        self.settings.window_geometry = g
        self.settings.save()

    def _sync_controls(self) -> None:
        active = self.state != IDLE
        self.overlay.set_running(self.state == RUNNING, active)
        self.act_toggle.setText("暂停" if self.state == RUNNING else ("继续" if active else "开始"))
        self.act_end.setEnabled(active)
        if self._settings_dlg is not None:
            self._settings_dlg.set_meeting_active(active)

    # ---- 识别引擎 ----

    def _engine_for(self, terms: list[glossary.Term]) -> AsrEngine:
        name = self.settings.effective_engine()
        hotwords = tuple(t.ja for t in terms) if name == "reazon" else ()
        key = (name, hotwords, self.settings.asr_threads, self.settings.asr_language())
        with self._engine_lock:
            if self._engine is None or self._engine_key != key:
                self._engine = create_engine(key[0], list(key[1]), key[2], key[3])
                self._engine_key = key
            return self._engine

    def _preload_engine(self) -> None:
        try:
            self._engine_for(glossary.load())
            self.bridge.status.emit("识别模型已加载")
        except ModelMissing:
            pass
        except Exception:
            log.exception("预加载识别模型失败")

    # ---- 会议流程 ----

    def toggle(self) -> None:
        if self._busy:
            return
        if self.state == RUNNING:
            self._stop_capture()
            self._flush_joiner()
            self.state = PAUSED
            self._set_status("已暂停")
        elif self.state == PAUSED:
            self._start_capture()
        else:
            self.start_meeting()
        self._sync_controls()

    def start_meeting(self) -> None:
        if not self.auth.ok:
            self._show_login(self.auth.message)
            return
        terms = glossary.load()
        try:
            self._set_status("加载识别模型…")
            self.qapp.processEvents()
            self._engine_for(terms)
        except ModelMissing as e:
            label = MODEL_LABELS.get(e.name)
            if label and QMessageBox.question(
                self.overlay, "需要下载模型", f"{label} 模型尚未下载，现在从 hf-mirror 下载吗？"
            ) == QMessageBox.StandardButton.Yes:
                download_model(e.name, self.overlay, lambda ok: ok and self.start_meeting())
            else:
                self._set_status(f"缺少模型：{e.name}")
            return
        s = self.settings
        self.transcript = Transcript(direction=s.direction)
        self.overlay.clear()
        self.overlay.show()
        self.joiner = Joiner(
            s.hold_seconds,
            s.hold_max_seconds,
            s.active_fillers(),
            on_open=self._on_join_open,
            on_update=self._on_join_update,
            on_commit=self._on_join_commit,
            ids=itertools.count(1),
            lang="zh" if s.direction == lang.ZH_JA else "ja",
        )
        self.usage_aic = 0.0
        opt = self.translator.option(s.model)
        self._meeting_price = opt.price if opt else None
        self._refresh_quota()
        self._quota_timer.start()
        self._busy = True
        self._watch(
            self.translator.start_meeting(
                self.transcript, s.model, s.reasoning_effort, s.session_rotate_minutes, s.context_sentences, terms,
                s.asr_correct,
            ),
            "meeting_started",
        )
        self._set_status("正在建立翻译会话…")

    def _start_capture(self) -> None:
        assert self._engine is not None
        s = self.settings
        self.asr = AsrWorker(
            self._engine,
            [THEM, ME],  # 麦克风可在会议中开关，切句器两路都备好
            s.max_segment_seconds,
            on_partial=self.bridge.partial.emit,
            on_final=self.bridge.final.emit,
            silence_seconds=s.silence_seconds,
        )
        self.asr.start()
        self.capture = Capture(self.asr.push, s.loopback_device, s.mic_device, s.mic_enabled)
        self.capture.start()
        self.state = RUNNING
        label = lang.get(self.transcript.direction if self.transcript else s.direction).label
        self._set_status(f"● {label} 翻译中（{s.model}）")

    def set_mic_enabled(self, on: bool) -> None:
        """悬浮窗麦克风按钮 / 设置对话框：持久化开关，会议采集中立即生效。"""
        self.settings.mic_enabled = on
        self.settings.save()
        self.overlay.set_mic(on)
        if self._settings_dlg is not None:
            self._settings_dlg.sync_mic(on)
        if self.capture is None or self.asr is None:
            return
        self.capture.set_enabled(ME, on)
        if not on:
            self.asr.flush_source(ME)  # 采集线程已退出，把已说的半句定稿后再停

    def _stop_capture(self) -> None:
        if self.capture:
            self.capture.stop()
            self.capture = None
        if self.asr:
            self.asr.stop()
            self.asr = None

    def end_meeting(self) -> None:
        if self.state == IDLE or self._busy:
            return
        self._stop_capture()
        self._flush_joiner()
        self.joiner = None
        self._quota_timer.stop()
        self.state = IDLE
        self._sync_controls()
        transcript = self.transcript
        self._set_status("结束中，等待剩余译文…")
        self._busy = True
        fut = self.translator.end_meeting()
        ask = bool(transcript and transcript.entries)
        self._watch(fut, "meeting_ended" if ask else "meeting_ended_empty")

    def _ask_summarize(self) -> None:
        t = self.transcript
        if t is None:
            return
        # 悬浮窗置顶会盖住确认框，会议结束后先收起
        self.overlay.hide()
        box = QMessageBox()
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.setWindowIcon(_icon())
        box.setWindowTitle("会后整理")
        box.setText(f"会议已结束（{len(t.entries)} 句）。现在整理纪要、待办和清洗后的双语全文吗？")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        want = box.exec() == QMessageBox.StandardButton.Yes
        self._store_transcript(t)
        if want:
            self._run_summary(t)
        else:
            t.discard_json()
            self._set_status(f"记录已保存：{t.md_path}")

    def _save_dir(self) -> Path | None:
        d = self.settings.save_dir
        return Path(d) if d and Path(d).is_dir() else None

    def _store_transcript(self, t: Transcript) -> None:
        """把 md 移到保存目录；尚未设定时询问一次，取消则本次留在默认目录、下次再问。"""
        folder = self._save_dir()
        if folder is None:
            dlg = QFileDialog(None, "选择会议记录保存位置（之后默认保存到这里，可在设置中修改）")
            dlg.setFileMode(QFileDialog.FileMode.Directory)
            dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
            dlg.setDirectory(str(Path.home() / "Documents"))
            dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            if dlg.exec() and dlg.selectedFiles():
                folder = Path(dlg.selectedFiles()[0])
                self.settings.save_dir = str(folder)
                self.settings.save()
            else:
                folder = TRANSCRIPT_DIR
        try:
            t.move_markdown(folder)
        except OSError as e:
            log.exception("保存会议记录失败")
            QMessageBox.warning(None, "保存失败", f"无法写入 {folder}：{e}\n记录已保留在 {t.md_path.parent}")

    def _run_summary(self, t: Transcript) -> None:
        self._busy = True
        self._summary_target = t
        self.overlay.show()
        self.overlay.show_progress("整理中：准备…", 0, 0)
        usage = self.translator.usage if t is self.transcript else None
        self._watch(self.translator.run(summarize(t, self.settings.model, self.bridge.progress.emit, usage)), "summary")

    def _retry_summary(self) -> None:
        if self._summary_target is None:
            return
        if self._busy or self.state != IDLE:
            self._set_status("请先结束当前会议再重试整理")
            return
        self._run_summary(self._summary_target)

    def _on_summary_progress(self, text: str, value: int, maximum: int) -> None:
        self._set_status(text)
        self.overlay.show_progress(text, value, maximum)

    def summarize_history(self) -> None:
        if self._busy or self.state != IDLE:
            QMessageBox.information(self.overlay, "会后整理", "请先结束当前会议。")
            return
        start = self._save_dir() or TRANSCRIPT_DIR
        path, _ = QFileDialog.getOpenFileName(self.overlay, "选择会议记录", str(start), "会议记录 (*.md)")
        if not path:
            return
        p = Path(path)
        try:
            t = Transcript.load_markdown(p)
            if not t.entries:
                raise ValueError("没有解析到任何发言")
        except (OSError, ValueError) as e:
            QMessageBox.warning(self.overlay, "会后整理", f"无法读取会议记录：{e}")
            return
        self._run_summary(t)

    # ---- 回调 ----

    def _on_job_done(self, tag, result) -> None:
        err = result if isinstance(result, Exception) else None
        if isinstance(tag, tuple) and tag[0] == "cb":
            try:
                tag[1](result)
            except RuntimeError:
                log.debug("回调目标已销毁", exc_info=True)
            return
        if tag == "start":
            if err:
                self._set_status(f"无法启动 Copilot：{err}")
            else:
                self._refresh_models()
                self._refresh_quota()
        elif tag == "meeting_started":
            self._busy = False
            if err:
                self._set_status(f"建立翻译会话失败：{err}")
                self.transcript = None
                self.joiner = None
                self._quota_timer.stop()
            else:
                self._start_capture()
            self._sync_controls()
        elif tag in ("meeting_ended", "meeting_ended_empty"):
            self._busy = False
            self._finish_usage()
            if tag == "meeting_ended":
                self._ask_summarize()
            else:
                if self.transcript is not None:
                    self.transcript.discard()
                self.overlay.hide()
                self._set_status("会议已结束")
        elif tag == "summary":
            self._busy = False
            t = self._summary_target
            if err:
                self._set_status(f"整理失败：{err}")
                self.overlay.show()
                self.overlay.show_progress_error(f"整理失败：{err}")
                self.tray.showMessage("会后整理失败", str(err), QSystemTrayIcon.MessageIcon.Warning, 8000)
            else:
                if t is not None:
                    t.discard_json()
                self._summary_target = None
                self._last_output = result
                self.overlay.show_progress_done(f"整理完成：{result.name}（{result.parent}）")
                self.tray.showMessage("会后整理完成", f"{result.name}\n点击打开", _icon(), 8000)
                open_path(result)
        elif tag in ("login", "recheck") and isinstance(result, AuthState):
            pass  # 结果已通过 on_auth 处理

    def _on_auth(self, state: AuthState) -> None:
        self.auth = state
        if state.ok:
            self._set_status(f"已登录 {state.login or ''}，Copilot 预热中…")
            self.translator.prepare_selection(self.settings.model, self.settings.reasoning_effort)
            if self._login:
                self._login.accept()
                self._login = None
        else:
            self._set_status("未登录 Copilot")
            if self._login:
                self._login.info.setText(state.message)
            else:
                self._show_login(state.message)

    def _show_login(self, message: str) -> None:
        if self._login:
            self._login.raise_()
            return
        dlg = LoginDialog(message, self.overlay)
        dlg.token_submitted.connect(lambda tok: self._watch(self.translator.login_with_token(tok), "login"))
        dlg.recheck_requested.connect(lambda: self._watch(self.translator.recheck_auth(), "recheck"))
        dlg.finished.connect(lambda _: setattr(self, "_login", None))
        self._login = dlg
        dlg.show()

    def _on_partial(self, source: str, text: str) -> None:
        self.overlay.partials[source] = text
        self.overlay.render()

    def _on_final(self, final) -> None:
        if self.transcript is None or self.joiner is None:
            return
        self.joiner.add(final.source, final.ja, final.wall_time, time.monotonic())

    def _on_join_open(self, p: Pending) -> None:
        assert self.transcript is not None
        e = Entry(p.id, p.wall_time, p.source, p.ja)
        self.transcript.add(e)
        self.overlay.entries.append(e)  # 与记录共享同一对象，后续追加自动同步
        self.overlay.render()

    def _on_join_update(self, p: Pending) -> None:
        assert self.transcript is not None
        self.transcript.set_ja(p.id, p.ja)
        self.overlay.render()

    def _on_join_commit(self, p: Pending, translate: bool) -> None:
        if self.transcript is None:
            return
        if translate:
            self.translator.submit(Final(p.id, p.source, p.ja, p.wall_time, time.monotonic()))
        else:
            self.transcript.set_translation(p.id, "")
            self.overlay.render()

    def _flush_joiner(self) -> None:
        if self.joiner is None:
            return
        self.qapp.processEvents()  # 先收下识别线程停止时定稿的最后几句
        self.joiner.flush()

    def _finish_usage(self) -> None:
        t = self.transcript
        aic = self.translator.usage.aic
        self.usage_aic = aic
        if t is not None:
            t.set_usage(aic, self.translator.model)
            t.save()
            if self._meeting_price:
                hours = (time.time() - t.started.timestamp()) / 3600
                stats.get().record_meeting(aic, hours, self._meeting_price)
        self._refresh_quota()
        self._show_usage()

    def _on_translated(self, tr) -> None:
        if self.transcript is None:
            return
        for idx, (i, text) in enumerate(zip(tr.ids, tr.texts)):
            fixed = tr.sources[idx] if tr.sources else None
            self.transcript.set_translation(i, text, fixed)
        self.overlay.latency.setText(f"延迟 {tr.latency:.1f}s" + (f" ×{len(tr.ids)}" if len(tr.ids) > 1 else ""))
        self.overlay.render()

    def open_settings(self) -> None:
        """非模态：设置窗口打开时悬浮窗仍可操作，点哪个窗口就激活哪个。"""
        if self._settings_dlg is not None:
            self._settings_dlg.show()
            self._settings_dlg.raise_()
            self._settings_dlg.activateWindow()
            return
        dlg = SettingsDialog(
            self.settings,
            self.models,
            self.overlay,
            on_save_selection_history=self._save_selection_history,
            selection_history_path=SELECTION_HISTORY_FILE,
        )
        dlg.setModal(False)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._settings_dlg = dlg

        def refresh() -> None:
            self._refresh_models(lambda r: dlg.set_models(self.models) if self._settings_dlg is dlg else None)

        def bench(model: str, effort: str) -> None:
            self._watch(
                self.translator.benchmark(model, effort, dlg.direction.currentData()),
                ("cb", lambda r: dlg.bench_done(model, r) if self._settings_dlg is dlg else None),
            )

        def closed(_result: int) -> None:
            if self._settings_dlg is dlg:
                self._settings_dlg = None

        dlg._on_refresh = refresh
        dlg._on_bench = bench
        dlg.accepted.connect(lambda: self._apply_settings(dlg))
        dlg.finished.connect(closed)
        dlg.set_meeting_active(self.state != IDLE)
        refresh()
        self._place_settings(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _apply_settings(self, dlg: SettingsDialog) -> None:
        old_hotkey = self.settings.hotkey
        old_selection_hotkey = self.settings.selection_hotkey
        old_model = (self.settings.model, self.settings.reasoning_effort)
        old_mic = self.settings.mic_enabled
        old_engine = (self.settings.effective_engine(), self.settings.asr_language())
        new_model = (dlg.model.currentData() or self.settings.model, dlg.effort.currentText())
        dlg.apply()
        self.overlay.apply_style(self.settings)
        self.selection_popup.apply_style(self.settings)
        self._show_usage()
        if self.settings.mic_enabled != old_mic:
            self.set_mic_enabled(self.settings.mic_enabled)
        if self.joiner is not None:
            self.joiner.hold = self.settings.hold_seconds
            self.joiner.hold_max = self.settings.hold_max_seconds
            self.joiner.fillers = self.settings.fillers_zh if self.joiner.lang == "zh" else self.settings.fillers
        if new_model != old_model:
            self.settings.model, self.settings.reasoning_effort = old_model
            self.set_model(*new_model)
        if self.settings.hotkey != old_hotkey:
            self._register_hotkey()
        if self.settings.selection_hotkey != old_selection_hotkey:
            self._register_selection_hotkey()
        if self.state == IDLE and (self.settings.effective_engine(), self.settings.asr_language()) != old_engine:
            threading.Thread(target=self._preload_engine, daemon=True).start()

    def _place_settings(self, dlg: SettingsDialog) -> None:
        """悬浮窗可见时把设置窗口放到它旁边空间最大的一侧，避免互相遮挡。"""
        if not self.overlay.isVisible():
            return
        screen = self.overlay.screen() or self.qapp.primaryScreen()
        if screen is None:
            return
        dlg.adjustSize()
        size = dlg.sizeHint().expandedTo(dlg.minimumSizeHint()) + FRAME_ALLOWANCE
        dlg.move(place_beside(self.overlay.frameGeometry(), size, screen.availableGeometry()))

    def quit(self) -> None:
        if self.state != IDLE:
            self._stop_capture()
            if self.transcript:
                self.transcript.save()
        if self.hotkey:
            self.hotkey.stop()
        if self.selection_hotkey:
            self.selection_hotkey.stop()
        self.selection_capture.cancel()
        self.translator.shutdown()
        self.tray.hide()
        self.qapp.quit()


def main() -> None:
    ensure_dirs()
    use_bundled_runtime()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    app = App(qapp)  # noqa: F841
    sys.exit(qapp.exec())
