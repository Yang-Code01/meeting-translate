"""应用控制器：串起音频采集、识别、翻译、记录、界面与托盘。"""

from __future__ import annotations

import concurrent.futures
import ctypes
import itertools
import logging
import os
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QSystemTrayIcon
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtCore import QProcess

from . import duplex_audio, glossary, lang, qtcom, stats
from .asr import AsrEngine, AsrWorker, Final, ModelMissing, create_engine
from .audio import ME, THEM, Capture
from .config import (
    GLOSSARY_FILE,
    LOG_FILE,
    SELECTION_HISTORY_FILE,
    MODEL_DIR,
    TRANSCRIPT_DIR,
    Settings,
    ensure_dirs,
    use_bundled_runtime,
)
from .default_audio import DefaultOutputSwitch, find_output, set_defaults
from .duplex_audio import LivePlayer, PassthroughRouter, Player, resolve_devices
from .duplex_speech import TtsSpeaker
from .diagnostics import DiagnosticsDialog
from .home_ui import HomeWindow
from .hotkey import GlobalHotkey
from .joiner import Joiner, Pending
from .resource_details import DetailDialog, cable_html, models_html
from .selection import SelectionCapture, SelectionRecord, append_history
from .providers import make_backend
from .summarize import summarize
from .transcript import Entry, Transcript, fmt_aic
from .translator import AuthState, ModelOption, Quota, Translator
from .tts_client import TtsClient, TtsError, default_worker_command
from .tts_models import ModelManager, total_download_text
from .ui import (
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


def _icon_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / "app.ico"


def _icon() -> QIcon:
    path = _icon_path()
    if path.is_file():
        return QIcon(str(path))
    # 源码环境缺少资源时的兜底
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
        self.bridge.duplex_play.connect(self.overlay_set_duplex_play)

        self.qapp.setWindowIcon(_icon())
        self.qapp.setApplicationName("MeetingTranslate")
        self.qapp.setApplicationDisplayName("会议翻译")
        self.overlay = Overlay(self.settings)
        self.overlay.setWindowIcon(_icon())
        self.overlay.toggle_clicked.connect(self.toggle)
        self.overlay.stop_clicked.connect(self.end_meeting)
        self.overlay.home_clicked.connect(self._return_home)
        self.overlay.settings_clicked.connect(self.open_settings)
        self.overlay.mic_toggled.connect(self.set_mic_enabled)
        self.overlay.zh_ja_toggled.connect(self._on_zh_ja_toggled)
        self.overlay.ja_zh_toggled.connect(self._on_ja_zh_toggled)
        self.overlay.preview_ja_clicked.connect(self._preview_ja)
        self.overlay.set_duplex(
            self.settings.meeting_mode == "duplex",
            self.settings.duplex_zh_ja,
            self.settings.duplex_ja_zh,
        )
        self.overlay.retry_clicked.connect(self._retry_summary)
        self.overlay.geometry_changed.connect(self._save_geometry)
        self.home = HomeWindow(self.settings)
        self.home.setWindowIcon(_icon())
        self.home.mode_selected.connect(self._on_home_mode)
        self.home.selection_requested.connect(self._on_home_selection)
        self.home.settings_requested.connect(self.open_settings)
        self.home.diagnostics_requested.connect(self.open_diagnostics)
        self.home.trial_requested.connect(self.open_trial)
        self.home.model_requested.connect(self._on_model_action)
        self.home.cable_requested.connect(self._on_cable_action)
        self.home.teams_requested.connect(self._on_teams_action)
        self.home.quit_requested.connect(self.quit)
        self.bridge.home_progress.connect(self._on_home_progress)
        self.bridge.home_done.connect(self._on_home_done)
        self.model_manager = ModelManager(Path(MODEL_DIR) / "tts")
        self._model_busy = False
        self._cable_busy = False
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
            backend=make_backend(self.settings, self.bridge.status.emit),
        )
        self._watch(self.translator.start(), "start")

        self._join_timer = QTimer()
        self._join_timer.setInterval(100)
        self._join_timer.timeout.connect(lambda: self.joiner and self.joiner.tick(time.monotonic()))
        self._join_timer.start()
        self._quota_timer = QTimer()
        self._quota_timer.setInterval(QUOTA_INTERVAL_MS)
        self._quota_timer.timeout.connect(self._refresh_quota)
        self._duplex_devices = None
        self._local_player: Player | None = None
        self._remote_player: Player | None = None
        self._direct_local: LivePlayer | None = None
        self._direct_remote: LivePlayer | None = None
        self._tts_client: TtsClient | None = None
        self._speaker: TtsSpeaker | None = None
        self._passthrough: PassthroughRouter | None = None
        self._audio_switch: DefaultOutputSwitch | None = None
        self._restore_output_name: str | None = None
        threading.Thread(target=self._preload_engine, daemon=True).start()
        self.home.show()
        self.home.raise_()
        QTimer.singleShot(350, self._refresh_resources)

    # ---- 基础设施 ----

    def _watch(self, fut: concurrent.futures.Future, tag) -> None:
        def done(f: concurrent.futures.Future) -> None:
            try:
                self.bridge.job_done.emit(tag, f.result())
            except Exception as e:  # noqa: BLE001
                log.exception("任务失败: %s", tag)
                self.bridge.job_done.emit(tag, e)

        fut.add_done_callback(done)

    def _active_model(self) -> str:
        """当前后端真正要用的模型名：自定义端点由端点配置决定。"""
        if self.settings.provider == "openai":
            return (self.settings.profile() or {}).get("model", "")
        return self.settings.model

    def _set_status(self, text: str) -> None:
        self.overlay.status.setText(text)
        if hasattr(self, "home"):
            self.home.set_status(text)
        self.tray.setToolTip(f"会议翻译 - {text}")

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(_icon(), self.qapp)
        menu = QMenu()
        self.act_toggle = menu.addAction("开始", self.toggle)
        self.act_end = menu.addAction("结束会议", self.end_meeting)
        menu.addSeparator()
        menu.addAction("显示主界面", self._show_home)
        menu.addAction("显示/隐藏字幕窗", self._toggle_overlay)
        menu.addAction("设置…", self.open_settings)
        menu.addAction("环境自检…", self.open_diagnostics)
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
            act.setChecked(o.id == self._active_model())
            act.setEnabled(o.enabled)

    def set_model(self, model: str, effort: str | None = None) -> None:
        effort = effort or self.settings.reasoning_effort
        if self.settings.provider == "openai":
            # 自定义端点的模型名存在端点配置里；换模型等于换一条要连的端点。
            profile = self.settings.profile()
            if profile is None or profile.get("model") == model:
                return
            profile["model"] = model
            self.settings.save()
            self._rebind_backend()
            return
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
            tokens = self.translator.usage.tokens if self.settings.provider == "openai" else None
            self.transcript.set_usage(aic, self.translator.model, tokens)
        self._show_usage()

    def _token_cost(self, tin: int, tout: int) -> float | None:
        profile = self.settings.profile()
        if not profile:
            return None
        price_in = profile.get("input_price") or 0
        price_out = profile.get("output_price") or 0
        if not price_in and not price_out:
            return None
        return (tin / 1e6) * price_in + (tout / 1e6) * price_out

    def _show_usage(self) -> None:
        s = self.settings
        if s.provider == "openai":  # 自定义端点没有 AIC，只有 token
            tin, tout = self.translator.usage.tokens
            parts = []
            if s.show_meeting_aic and (self.state != IDLE or tin or tout):
                parts.append(f"本场 {tin:,} 输入 / {tout:,} 输出 token")
                cost = self._token_cost(tin, tout)
                if cost:
                    parts.append(f"≈ ¥{cost:.2f}")
            self.overlay.set_usage(" · ".join(parts), False)
            return
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

    def _show_overlay(self) -> None:
        self.overlay.ensure_on_screen()
        self.overlay.show()

    def _toggle_overlay(self) -> None:
        """托盘/双击托盘图标的临时开关；同步进设置，重启后保持。"""
        self.settings.show_overlay = not self.overlay.isVisible()
        self.settings.save()
        if self.settings.show_overlay:
            self._show_overlay()
        else:
            self.overlay.hide()

    def _show_home(self) -> None:
        self.home.show()
        self.home.raise_()
        QTimer.singleShot(350, self._refresh_resources)
        self.home.activateWindow()

    def _return_home(self) -> None:
        """悬浮框上的「主界面」：收起字幕窗，回到入口页。"""
        if self.state != IDLE:
            self._set_status("翻译进行中，请先结束会议再返回主界面")
            return
        self.overlay.hide()
        self._show_home()

    def _on_home_mode(self, mode: str) -> None:
        if self.state != IDLE:
            QMessageBox.information(self.home, "正在会话", "请先在悬浮窗里结束当前会话，再切换入口。")
            return
        self.settings.meeting_mode = "duplex" if mode == "duplex" else "captions"
        self.settings.show_overlay = True
        self.settings.save()
        self.overlay.set_duplex(
            self.settings.meeting_mode == "duplex",
            self.settings.duplex_zh_ja,
            self.settings.duplex_ja_zh,
        )
        self.home.set_mode(self.settings.meeting_mode)
        self.home.hide()
        self._show_overlay()
        self._sync_controls()
        if self.settings.meeting_mode == "duplex":
            self._set_status("已进入语音实时转译：请手动点「开始」")
            message = (
                "已进入「语音实时转译」。不会自动开始；"
                "请点击悬浮窗的「▶ 开始」，"
                f"或按 {self.settings.hotkey} 开始。"
            )
        else:
            self._set_status("已进入会议实时翻译：请手动点「开始」")
            message = (
                "已进入「会议实时翻译」。不会自动开始；"
                "请点击悬浮窗的「▶ 开始」，"
                f"或按 {self.settings.hotkey} 开始采集。"
            )
        self.tray.showMessage("等待开始", message, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _on_home_selection(self) -> None:
        self.home.hide()
        QTimer.singleShot(250, self._capture_selection)

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
        self._show_home()

    def _on_selection_captured(self, text: str, anchor, warning: str) -> None:
        if warning:
            self._set_status(warning.lstrip("；"))
        self._selection_request_id += 1
        request_id = self._selection_request_id
        self._latest_selection_request = request_id
        self.selection_popup.show_loading(anchor)
        future = self.translator.translate_selected(text, self._active_model(), self.settings.reasoning_effort)
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
            self._restore_default_output()
            self._flush_joiner()
            if self._speaker is not None:
                self._speaker.stop()
            self.state = PAUSED
            self._set_status("已暂停")
        elif self.state == PAUSED:
            self._switch_default_output()
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
        duplex = s.meeting_mode == "duplex"
        if duplex:
            try:
                self._duplex_devices = resolve_devices(s)
            except Exception as e:
                self._set_status(f"声卡检测失败：{e}")
                return
            if self._duplex_devices.missing:
                missing = "、".join(self._duplex_devices.missing)
                QMessageBox.warning(
                    self.overlay,
                    "缺少虚拟声卡",
                    f"双向同传需要 VB-Cable A+B，当前缺少：{missing}。\n"
                    "请先安装 VB-Cable A+B，再回到设置里重新检测。",
                )
                self._set_status(f"缺少虚拟声卡：{missing}")
                return
        else:
            self._duplex_devices = None
        self.transcript = Transcript(direction="bi" if duplex else s.direction)
        dirs = [lang.JA_ZH, lang.ZH_JA] if duplex else [s.direction]
        self.overlay.set_duplex(duplex, s.duplex_zh_ja, s.duplex_ja_zh)
        self.overlay.clear()
        if duplex:
            self.bridge.duplex_play.emit(lang.ZH_JA, "", "clear")
            self.bridge.duplex_play.emit(lang.JA_ZH, "", "clear")
        if s.show_overlay:
            self._show_overlay()
        self.joiner = Joiner(
            s.hold_seconds,
            s.hold_max_seconds,
            s.active_fillers(),
            on_open=self._on_join_open,
            on_update=self._on_join_update,
            on_commit=self._on_join_commit,
            ids=itertools.count(1),
            lang="zh" if s.direction == lang.ZH_JA else "ja",
            langs={ME: "zh", THEM: "ja"} if duplex else None,
        )
        self.usage_aic = 0.0
        opt = self.translator.option(self._active_model())
        self._meeting_price = opt.price if opt else None
        self._refresh_quota()
        self._quota_timer.start()
        self._busy = True
        self._watch(
            self.translator.start_meeting(
                self.transcript, self._active_model(), s.reasoning_effort, s.session_rotate_minutes, s.context_sentences, terms,
                s.asr_correct, dirs,
            ),
            "meeting_started",
        )
        self._set_status("正在建立翻译会话…")

    def _start_capture(self) -> None:
        assert self._engine is not None
        s = self.settings
        duplex = s.meeting_mode == "duplex"
        self.asr = AsrWorker(
            self._engine,
            [THEM, ME],  # 麦克风可在会议中开关，切句器两路都备好
            s.max_segment_seconds,
            on_partial=self.bridge.partial.emit,
            on_final=self.bridge.final.emit,
            on_speech_start=self._on_speech_start if duplex else None,
            silence_seconds=s.silence_seconds,
        )
        self.asr.start()
        if duplex:
            devices = self._duplex_devices
            loopback = devices.capture if devices else s.duplex_capture_device
            mic = (devices.mic if devices else None) or s.mic_device
            mic_on = True
            self._direct_local = LivePlayer(
                devices.local_speaker if devices else s.duplex_local_speaker,
                on_error=lambda e: self.bridge.status.emit(f"原声直通失败：{e}"),
            )
            self._direct_remote = LivePlayer(
                devices.remote_speaker if devices else s.duplex_remote_speaker,
                on_error=lambda e: self.bridge.status.emit(f"原声直通失败：{e}"),
            )
        else:
            loopback, mic, mic_on = s.loopback_device, s.mic_device, s.mic_enabled
        self.capture = Capture(self._on_capture_audio, loopback, mic, mic_on)
        self.capture.start()
        self.state = RUNNING
        direction = self.transcript.direction if self.transcript else s.direction
        label = "中日双向" if direction == "bi" else lang.get(direction).label
        self._set_status(f"● {label} 翻译中（{s.model}）")

    def _on_capture_audio(self, source: str, samples) -> None:
        """采集回调：先喂 ASR，再按开关把原始音频直通给对应输出。"""
        if self.asr is not None:
            self.asr.push(source, samples)
        s = self.settings
        if s.meeting_mode != "duplex":
            return
        if source == ME and not s.duplex_zh_ja and self._direct_remote is not None:
            self._direct_remote.write(samples)
        elif source == THEM and not s.duplex_ja_zh and self._direct_local is not None:
            self._direct_local.write(samples)

    def _on_zh_ja_toggled(self, on: bool) -> None:
        self.settings.duplex_zh_ja = bool(on)
        self.settings.save()
        self._apply_duplex_switches()
        self._set_status("中转日：" + ("开" if on else "关（麦克风原声直通）"))

    def _on_ja_zh_toggled(self, on: bool) -> None:
        self.settings.duplex_ja_zh = bool(on)
        self.settings.save()
        self._apply_duplex_switches()
        self._set_status("日转中：" + ("开" if on else "关（对方原声直通）"))

    def overlay_set_duplex_play(self, direction: str, text: str, state: str) -> None:
        self.overlay.set_duplex_play(direction, text, state)

    def _apply_duplex_switches(self) -> None:
        if self.settings.meeting_mode != "duplex":
            return
        if self._speaker is not None:
            if not self.settings.duplex_zh_ja:
                self._speaker.stop(lang.ZH_JA)
            if not self.settings.duplex_ja_zh:
                self._speaker.stop(lang.JA_ZH)
        if self.settings.duplex_zh_ja and self._direct_remote is not None:
            self._direct_remote.stop()
        if self.settings.duplex_ja_zh and self._direct_local is not None:
            self._direct_local.stop()
        self.bridge.duplex_play.emit(
            lang.ZH_JA,
            "",
            "clear" if self.settings.duplex_zh_ja else "direct",
        )
        self.bridge.duplex_play.emit(
            lang.JA_ZH,
            "",
            "clear" if self.settings.duplex_ja_zh else "direct",
        )

    def _preview_ja(self) -> None:
        if self._speaker is None or self._local_player is None:
            self._set_status("TTS 尚未就绪，无法试听日语")
            return
        audio = self._speaker.last_audio(lang.ZH_JA)
        if audio is None:
            self._set_status("还没有「中转日」结果：先说一句中文，再点试听")
            return
        self._local_player.stop()
        self._local_player.enqueue(audio.samples, audio.sample_rate)
        self._set_status("试听日语：正在本机耳机播放最近一条中转日结果")

    def _on_speech_start(self, source: str) -> None:
        """说话者开始时只停“说给他听”的那路 TTS，另一路继续播完。

        - 我开口：停本地耳机里的中文 TTS（日→中），不要把我这边正在放的
          上一句截断；对方耳机里的日语 TTS 继续，避免跳句。
        - 对方开口：停发给他耳机的日语 TTS（中→日），本地中文 TTS 继续。
        """
        if self._speaker is None:
            return
        if source == ME:
            self._speaker.stop(lang.JA_ZH)
        elif source == THEM:
            self._speaker.stop(lang.ZH_JA)
        else:
            self._speaker.stop()

    def _notify(self, title: str, message: str, warning: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if warning else QSystemTrayIcon.MessageIcon.Information
        try:
            self.tray.showMessage(title, message, icon, 6000)
        except Exception:
            log.debug("托盘通知失败", exc_info=True)

    def _switch_default_output(self) -> None:
        """双向同传开始时把 Windows 默认播放切到会议输出虚拟扬声器。"""
        if self.settings.meeting_mode != "duplex" or self._audio_switch is not None:
            return
        devices = self._duplex_devices
        if devices is None or not devices.capture:
            return
        switch = DefaultOutputSwitch()
        try:
            result = switch.switch_to(devices.capture)
        except Exception as e:
            if switch.active:
                self._audio_switch = switch
            log.warning("切换默认输出失败：%s", e)
            self._set_status(f"默认输出未切换：{e}")
            self._notify(
                "默认输出未切换",
                f"无法自动切到 {devices.capture}：{e}\n请在 Windows 声音设置里手动切换。",
                warning=True,
            )
            return
        if not result.changed:
            self._notify("默认输出无需切换", f"系统默认输出已经是：{result.target.name}")
            return
        self._audio_switch = switch
        previous = result.previous.name if result.previous else "未知"
        self._set_status(f"默认输出已切到 {result.target.name}")
        self._notify(
            "默认输出已切换",
            f"已切到：{result.target.name}\n原输出：{previous}\n结束双向同传后会自动切回。",
        )

    def _restore_default_output(self) -> None:
        switch, self._audio_switch = self._audio_switch, None
        fallback, self._restore_output_name = self._restore_output_name, None
        if switch is None and not fallback:
            return
        try:
            if switch is not None:
                restored = switch.restore()
            else:
                dev = find_output(fallback or "")
                if dev is None:
                    raise RuntimeError(f"找不到设备：{fallback}")
                set_defaults(dev.id)
                restored = dev
        except Exception as e:
            log.warning("恢复默认输出失败：%s", e)
            self._notify("默认输出未恢复", f"请手动切回耳机：{e}", warning=True)
            return
        if restored is not None:
            self._set_status(f"默认输出已恢复：{restored.name}")
            self._notify("默认输出已恢复", f"已切回：{restored.name}")

    def _start_tts(self) -> None:
        if self._speaker is not None:
            return
        s = self.settings
        devices = self._duplex_devices
        if devices is None or not devices.remote_speaker:
            raise TtsError("未检测到 CABLE-B Input，无法播放日语 TTS", "device_missing")
        root = Path(os.environ.get("MEETING_TRANSLATE_TTS_DIR") or (MODEL_DIR / "tts"))
        client: TtsClient | None = None
        try:
            if os.environ.get("MEETING_TRANSLATE_TTS_WORKER"):
                client = TtsClient()
            else:
                manager = ModelManager(root)
                missing = manager.missing("melo", verify_hash=False) + manager.missing("sbv2", verify_hash=False)
                if missing:
                    raise TtsError(
                        "TTS 模型尚未下载；请先在设置里完成语音包下载，或设置 MEETING_TRANSLATE_TTS_DIR",
                        "models_missing",
                    )
                client = TtsClient(default_worker_command(root, "auto"))
            client.start()
            local = Player(devices.local_speaker)
            remote = Player(devices.remote_speaker)
            self._local_player = local
            self._remote_player = remote
            self._tts_client = client
            self._speaker = TtsSpeaker(
                client,
                local,
                remote,
                on_error=lambda e: self.bridge.status.emit(f"TTS 失败：{e}"),
                on_event=lambda d, t, st: self.bridge.duplex_play.emit(d, t, st),
                max_queue=s.duplex_queue_max,
                max_chars=120,
                volume=s.duplex_tts_volume,
            )
            self._speaker.start()
            self._passthrough = PassthroughRouter(local, remote)
            threading.Thread(target=self._warm_tts, daemon=True).start()
        except Exception:
            if self._speaker is not None:
                self._speaker.close()
                self._speaker = None
            if client is not None:
                client.close()
            for player in (self._local_player, self._remote_player):
                if player is not None:
                    player.close()
            self._local_player = self._remote_player = None
            raise

    def _warm_tts(self) -> None:
        """后台加载两个引擎；首次开启时避免第一句等 20 秒。"""
        client = self._tts_client
        if client is None:
            return
        for engine in ("sbv2", "melo"):
            try:
                seconds = client.warm(engine, timeout=180)
                log.info("TTS 预热完成: %s %.1fs", engine, seconds)
            except Exception as e:  # noqa: BLE001
                log.warning("TTS 预热失败: %s %s", engine, e)
                self.bridge.status.emit(f"语音预热失败：{e}")
                return
        self.bridge.status.emit("语音模型已就绪")

    def _close_tts(self) -> None:
        if self._speaker is not None:
            self._speaker.close()
            self._speaker = None
        if self._tts_client is not None:
            self._tts_client.close()
            self._tts_client = None
        if self._local_player is not None:
            self._local_player.close()
            self._local_player = None
        if self._remote_player is not None:
            self._remote_player.close()
            self._remote_player = None
        self._passthrough = None

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
        if self._direct_local is not None:
            self._direct_local.close()
            self._direct_local = None
        if self._direct_remote is not None:
            self._direct_remote.close()
            self._direct_remote = None

    def end_meeting(self) -> None:
        if self.state == IDLE or self._busy:
            return
        self._stop_capture()
        self._restore_default_output()
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
            if self.settings.show_overlay:
                self._show_overlay()

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
        if self.settings.show_overlay:
            self._show_overlay()
        self.overlay.show_progress("整理中：准备…", 0, 0)
        usage = self.translator.usage if t is self.transcript else None
        self._watch(self.translator.run(summarize(t, self._active_model(), self.bridge.progress.emit, usage)), "summary")

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
        # 悬浮窗是置顶的，拿它当父窗口能让文件对话框盖在上面；但它可能被用户在设置里
        # 关掉了，隐藏的父窗口会让原生对话框一起藏起来，界面看着就像卡死了。
        owner = self._settings_dlg or (self.overlay if self.overlay.isVisible() else None)
        if self._busy or self.state != IDLE:
            QMessageBox.information(owner, "会后整理", "请先结束当前会议。")
            return
        start = self._save_dir() or TRANSCRIPT_DIR
        path, _ = QFileDialog.getOpenFileName(owner, "选择会议记录", str(start), "会议记录 (*.md)")
        if not path:
            return
        p = Path(path)
        try:
            t = Transcript.load_markdown(p)
            if not t.entries:
                raise ValueError("没有解析到任何发言")
        except (OSError, ValueError) as e:
            QMessageBox.warning(owner, "会后整理", f"无法读取会议记录：{e}")
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
                self._set_status(f"无法启动模型服务：{err}")
            else:
                self._refresh_models()
                self._refresh_quota()
        elif tag == "meeting_started":
            self._busy = False
            if err:
                self._set_status(f"建立翻译会话失败：{err}")
                self.transcript = None
                self.joiner = None
                self._restore_default_output()
                self._close_tts()
                self._quota_timer.stop()
                self._show_home()
            else:
                if self.settings.meeting_mode == "duplex":
                    self._switch_default_output()
                    try:
                        self._start_tts()
                    except Exception as e:
                        log.exception("TTS 启动失败")
                        self._set_status(f"TTS 不可用，先保留字幕：{e}")
                self._start_capture()
            self._sync_controls()
        elif tag in ("meeting_ended", "meeting_ended_empty"):
            self._busy = False
            self._restore_default_output()
            self._close_tts()
            self._finish_usage()
            if tag == "meeting_ended":
                # 结束会议后留在悬浮框；由用户点「主界面」再回去。
                self._ask_summarize()
            else:
                if self.transcript is not None:
                    self.transcript.discard()
                self._set_status("会议已结束")
                if self.settings.show_overlay:
                    self._show_overlay()
        elif tag == "summary":
            self._busy = False
            t = self._summary_target
            if err:
                self._set_status(f"整理失败：{err}")
                if self.settings.show_overlay:
                    self._show_overlay()
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
        elif tag == "rebind":
            if err:
                self._set_status(f"切换模型服务失败：{err}")
        elif tag in ("login", "recheck") and isinstance(result, AuthState):
            pass  # 结果已通过 on_auth 处理

    def _on_auth(self, state: AuthState) -> None:
        self.auth = state
        custom = self.settings.provider == "openai"
        if state.ok:
            self._set_status(f"{'已连接' if custom else '已登录'} {state.login or ''}，预热中…")
            self.translator.prepare_selection(self._active_model(), self.settings.reasoning_effort)
            if self._login:
                self._login.accept()
                self._login = None
        else:
            self._set_status(f"模型服务不可用：{state.message}" if custom else "未登录 Copilot")
            if self._login:
                self._login.info.setText(state.message)
            elif custom:
                self._show_service_error(state.message)
            else:
                self._show_login(state.message)

    def _show_service_error(self, message: str) -> None:
        """自定义端点起不来时直接进设置，而不是弹 Copilot 登录框。

        这里不能用模态 QMessageBox：悬浮字幕窗是置顶窗口，普通弹窗会被它
        盖住，看起来就像“弹窗点不了”。把错误直接写进设置窗口，用户也能
        马上改接口地址 / 模型名 / API key。
        """
        self.open_settings()
        dlg = self._settings_dlg
        if dlg is None:
            self._set_status(f"模型服务不可用：{message}")
            return
        dlg.set_service_error(f"{message}。请检查接口地址、模型名和 API key")
        dlg.raise_()
        dlg.activateWindow()

    def _switch_to_custom(self) -> None:
        self._set_status("请在「模型服务」里选择「自定义（OpenAI 兼容）」并新建一条端点")
        self.open_settings()

    def _show_login(self, message: str) -> None:
        if self._login:
            self._login.raise_()
            return
        dlg = LoginDialog(message, self.overlay)
        dlg.token_submitted.connect(lambda tok: self._watch(self.translator.login_with_token(tok), "login"))
        dlg.recheck_requested.connect(lambda: self._watch(self.translator.recheck_auth(), "recheck"))
        dlg.use_custom_requested.connect(self._switch_to_custom)
        dlg.finished.connect(lambda _: setattr(self, "_login", None))
        # 与设置窗同理：不置顶会被悬浮字幕窗盖住。
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self._login = dlg
        dlg.show()

    def _on_partial(self, source: str, text: str) -> None:
        self.overlay.partials[source] = text
        self.overlay.render()

    def _on_final(self, final) -> None:
        if self.transcript is None or self.joiner is None:
            return
        self.joiner.add(
            final.source, final.ja, final.wall_time, time.monotonic(),
            lang=getattr(final, "lang", ""), audio=getattr(final, "audio", None),
        )

    def _on_join_open(self, p: Pending) -> None:
        assert self.transcript is not None
        direction = self.transcript.direction
        entry_direction = None
        if direction == "bi":
            if p.lang == "ja":
                entry_direction = lang.JA_ZH
            elif p.lang == "zh":
                entry_direction = lang.ZH_JA
            else:
                entry_direction = lang.ZH_JA if p.source == ME else lang.JA_ZH
        e = Entry(
            p.id, p.wall_time, p.source, p.ja,
            direction=entry_direction,
            src_lang=("zh" if entry_direction == lang.ZH_JA else "ja") if entry_direction else None,
            dst_lang=("ja" if entry_direction == lang.ZH_JA else "zh") if entry_direction else None,
        )
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
        if not translate:
            self.transcript.set_translation(p.id, "")
            self.overlay.render()
            return
        direction = self.transcript.direction
        if direction == "bi":
            if p.lang == "ja":
                direction = lang.JA_ZH
            elif p.lang == "zh":
                direction = lang.ZH_JA
            else:
                direction = lang.ZH_JA if p.source == ME else lang.JA_ZH
            settings = getattr(self, "settings", None)
            if p.source == ME and not bool(getattr(settings, "duplex_zh_ja", True)):
                self.transcript.set_translation(p.id, "")
                self.overlay.render()
                return
            if p.source == THEM and not bool(getattr(settings, "duplex_ja_zh", True)):
                self.transcript.set_translation(p.id, "")
                self.overlay.render()
                return
        final = Final(
            p.id,
            p.source,
            p.ja,
            p.wall_time,
            time.monotonic(),
            lang=p.lang,
            direction=direction,
            audio=p.audio,
        )
        if (
            self.transcript.direction == "bi"
            and self._passthrough is not None
            and final.lang == ("ja" if p.source == ME else "zh")
        ):
            self._passthrough.route(final)
            self.transcript.set_translation(p.id, p.ja)
            self.overlay.render()
            return
        self.translator.submit(final)

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
            tokens = self.translator.usage.tokens if self.settings.provider == "openai" else None
            t.set_usage(aic, self.translator.model, tokens)
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
            direction = getattr(tr, "direction", None)
            if self._speaker is not None and direction:
                settings = getattr(self, "settings", None)
                if direction == lang.ZH_JA and not bool(getattr(settings, "duplex_zh_ja", True)):
                    continue
                if direction == lang.JA_ZH and not bool(getattr(settings, "duplex_ja_zh", True)):
                    continue
                self._speaker.submit(text, direction)
        self.overlay.latency.setText(f"延迟 {tr.latency:.1f}s" + (f" ×{len(tr.ids)}" if len(tr.ids) > 1 else ""))
        self.overlay.render()
        if self.settings.provider == "openai":
            self._show_usage()

    def open_diagnostics(self) -> None:
        self._show_diagnostics(0)

    def open_trial(self) -> None:
        self._show_diagnostics(1)

    def _show_diagnostics(self, tab: int) -> None:
        if self.state != IDLE:
            QMessageBox.information(self.home, "正在会话", "请先在悬浮窗里结束当前会话，再做自检或试一句。")
            return
        dlg = DiagnosticsDialog(self.settings, self.home)
        dlg.tabs.setCurrentIndex(tab)
        dlg.exec()

    # ---- 首页资源卡 ----

    def _refresh_resources(self, *, rescan: bool = False) -> None:
        """刷新三张资源卡。rescan=True 表示用户手动点了「重新检测」，结果带上时间戳
        （否则检测结果与上次相同，界面上一个像素都不会变，看上去像没生效）。"""
        stamp = f" · {time.strftime('%H:%M:%S')} 检测" if rescan else ""
        try:
            missing = self.model_manager.missing("melo", verify_hash=False) + self.model_manager.missing("sbv2", verify_hash=False)
            if missing:
                self.home.set_model_state(f"未安装 · 约 {total_download_text()}{stamp}", primary="下载")
            else:
                self.home.set_model_state(f"已安装 · 启动时加载{stamp}", primary="")
        except Exception as e:  # noqa: BLE001
            self.home.set_model_state(f"检测失败：{e}", primary="重新检测")
        try:
            devices = duplex_audio.detect_cable_devices()
            if devices.complete:
                self.home.set_cable_state(f"已安装 · CABLE-A/B 就绪{stamp}", primary="重新检测")
            else:
                self.home.set_cable_state("未安装：" + "、".join(devices.missing) + stamp, primary="下载安装")
            self.home.set_teams_state("虚拟声卡就绪，请在会议软件中确认" if devices.complete else "先安装虚拟声卡")
        except Exception as e:  # noqa: BLE001
            self.home.set_cable_state(f"检测失败：{e}", primary="重新检测")
            self.home.set_teams_state("虚拟声卡检测失败")

    def _on_model_action(self, action: str) -> None:
        if action == "详细":
            self._show_model_detail()
            return
        if self._model_busy:
            return
        if action in ("下载", "重新检测"):
            if action == "重新检测":
                self._rescan_models()
                return
            self._download_models()

    def _rescan_models(self) -> None:
        """手动「重新检测」：先亮一下「正在检测」，再给带上时间戳的结果。"""
        if self._model_busy:
            return
        self._model_busy = True
        self.home.set_model_state("正在检测语音模型…", busy=True)
        QTimer.singleShot(40, self._finish_rescan_models)

    def _finish_rescan_models(self) -> None:
        self._model_busy = False
        self._refresh_resources(rescan=True)

    def _download_models(self) -> None:
        if self._model_busy:
            return
        self._model_busy = True
        self.home.set_model_state("准备下载…", primary="下载", busy=True, done=0, total=1)
        manager = self.model_manager

        def progress(name: str, done: int, total: int) -> None:
            self.bridge.home_progress.emit("model", f"下载 {name}", done, total)

        def work() -> None:
            try:
                manager.on_progress = progress
                manager.ensure("melo")
                manager.ensure("sbv2")
                self.bridge.home_done.emit("model", True, "语音模型已安装")
            except Exception as e:  # noqa: BLE001
                log.exception("下载语音模型失败")
                self.bridge.home_done.emit("model", False, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _show_model_detail(self) -> None:
        DetailDialog(
            "语音模型",
            "日→中 MeloTTS、中→日 Style-Bert-VITS2，加上日文读音 BERT；三组一次装齐、一起校验。",
            models_html(self.model_manager.root),
            self.home,
        ).exec()

    def _show_cable_detail(self) -> None:
        downloads = Path(MODEL_DIR).parent / "downloads" / "vb-cable"
        try:
            devices = duplex_audio.detect_cable_devices()
            body = cable_html(devices, downloads)
            note = "CABLE-A/B 已就绪" if devices.complete else "缺：" + "、".join(devices.missing)
        except Exception as e:  # noqa: BLE001
            log.warning("检测虚拟声卡失败：%s", e)
            body = cable_html(None, downloads, str(e))
            note = "设备枚举失败"
        DetailDialog(
            "虚拟声卡",
            f"VB-Audio CABLE A+B 与 Hi-Fi Cable 的四个端点、会议软件（以 Teams 为例）设置与当前检测结果（{note}）。",
            body,
            self.home,
        ).exec()

    def _rescan_cable(self) -> None:
        """手动「重新检测」：先亮一下「正在检测」，再给带上时间戳的结果。

        装完 VB-Audio 驱动后用户一定会点它；若结果和上次一样而界面毫无变化，
        看上去就像按钮没生效，所以这里既给即时反馈也给时间戳。
        """
        if self._cable_busy:
            return
        self._cable_busy = True
        self.home.set_cable_state("正在检测虚拟声卡…", busy=True)
        QTimer.singleShot(40, self._finish_rescan_cable)

    def _finish_rescan_cable(self) -> None:
        self._cable_busy = False
        self._refresh_resources(rescan=True)

    def _on_cable_action(self, action: str) -> None:
        if action == "详细":
            self._show_cable_detail()
            return
        if self._cable_busy:
            return
        if action == "重新检测":
            self._rescan_cable()
            return
        if action != "下载安装":
            return
        self._cable_busy = True
        self.home.set_cable_state("准备下载安装包…", primary="下载安装", busy=True, done=0, total=1)

        def progress(name: str, done: int, total: int) -> None:
            self.bridge.home_progress.emit("cable", f"下载 {name}", done, total)

        def work() -> None:
            try:
                folder = Path(MODEL_DIR).parent / "downloads" / "vb-cable"
                folder.mkdir(parents=True, exist_ok=True)
                files = {
                    "VBCABLE_Driver_Pack45.zip": "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip",
                    "HiFiCableAsioBridgeSetup_v1007.zip": "https://download.vb-audio.com/Download_CABLE/HiFiCableAsioBridgeSetup_v1007.zip",
                }
                for name, url in files.items():
                    path = folder / name
                    if not path.exists() or path.stat().st_size < 1024:
                        self._download(url, path, progress)
                    with zipfile.ZipFile(path) as zf:
                        zf.extractall(folder / name.removesuffix(".zip"))
                setups = list(folder.glob("**/VBCABLE_Setup_x64.exe")) + list(folder.glob("**/HiFiCableAsioBridgeSetup*.exe"))
                if setups:
                    QProcess.startDetached(str(setups[0]), [], str(setups[0].parent))
                self.bridge.home_done.emit(
                    "cable",
                    True,
                    "安装包已下载。请依次运行 VBCABLE_Setup_x64.exe 和 HiFiCableAsioBridgeSetup，完成后点「重新检测」。",
                )
            except Exception as e:  # noqa: BLE001
                log.exception("下载虚拟声卡失败")
                self.bridge.home_done.emit("cable", False, str(e))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _download(url: str, dest: Path, progress) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "meeting-translate"})
        with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:
            total = int(response.headers.get("Content-Length", "0") or 0)
            done = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                progress(dest.name, done, max(total, done))

    def _on_home_progress(self, kind: str, text: str, done: int, total: int) -> None:
        if kind == "model":
            self.home.set_model_state(text, primary="下载", busy=True, done=done, total=total)
        elif kind == "cable":
            self.home.set_cable_state(text, primary="下载安装", busy=True, done=done, total=total)

    def _on_home_done(self, kind: str, ok: bool, message: str) -> None:
        if kind == "model":
            self._model_busy = False
            self._notify("语音模型", message, warning=not ok)
            self._refresh_resources()
        elif kind == "cable":
            self._cable_busy = False
            self._notify("虚拟声卡", message, warning=not ok)
            self._refresh_resources()

    def _on_teams_action(self, action: str) -> None:
        if action == "打开 Teams":
            if not QDesktopServices.openUrl(QUrl("msteams:")):
                QMessageBox.information(self.home, "打开 Teams", "没有找到 Teams。请先安装 Microsoft Teams。")
            return
        QMessageBox.information(
            self.home,
            "会议软件配置步骤（以 Teams 为例）",
            "以 Teams 为例，其他会议软件（Zoom / 腾讯会议 / 飞书 / Google Meet）同理，只是入口在各自的音频设置里：\n\n"
            "1. 先点「下载安装」装好 CABLE-A/B。 2. Teams → 设置 → 设备：扬声器 = CABLE Input (VB-Audio Virtual Cable)，麦克风 = Hi-Fi Cable Output (VB-Audio Hi-Fi Cable)。 3. 关闭 Teams 的降噪/回声消除（如果开了会切掉虚拟声卡的声音）。 4. 回到首页点「语音实时转译」，再在悬浮窗点「开始」。 5. 第一次使用先点「自检」，再点「试听日语」。",
        )

    def open_settings(self) -> None:
        """非模态：设置窗口打开时悬浮窗仍可操作，点哪个窗口就激活哪个。"""
        if self._settings_dlg is not None:
            self._settings_dlg.show()
            self._settings_dlg.raise_()
            self._settings_dlg.activateWindow()
            return
        dlg = SettingsDialog(
            self.settings,
            [] if self.settings.provider == "openai" else self.models,
            self.overlay,
            on_save_selection_history=self._save_selection_history,
            selection_history_path=SELECTION_HISTORY_FILE,
        )
        dlg.setModal(False)
        # 悬浮字幕窗是 WindowStaysOnTopHint；设置窗不置顶就会被字幕盖住。
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._settings_dlg = dlg

        def refresh() -> None:
            provider, profile = dlg.current_service()
            if provider == "openai" and not profile:
                dlg.set_models([])
                dlg.set_model_error("先新建一条端点，填好接口地址、模型名和 API key")
                return
            dlg.set_models_loading()
            fut = self.translator.list_models_for(profile) if provider == "openai" else self.translator.list_models()

            def done(result) -> None:
                if self._settings_dlg is not dlg:
                    return
                if isinstance(result, Exception):
                    dlg.set_models([])
                    dlg.set_model_error(str(result))
                else:
                    dlg.set_models(result)
                    dlg.set_model_error("")
                    dlg.set_service_error("")

            self._watch(fut, ("cb", done))

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
        dlg.quit_requested.connect(self.quit)
        dlg.finished.connect(closed)
        dlg.set_meeting_active(self.state != IDLE)
        refresh()
        self._place_settings(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _apply_settings(self, dlg: SettingsDialog) -> None:
        old_service = self._service_key()
        old_hotkey = self.settings.hotkey
        old_selection_hotkey = self.settings.selection_hotkey
        old_model = (self.settings.model, self.settings.reasoning_effort)
        old_mic = self.settings.mic_enabled
        old_engine = (self.settings.effective_engine(), self.settings.asr_language())
        new_model = (dlg.model.currentData() or self.settings.model, dlg.effort.currentText())
        custom = dlg.provider.currentData() == "openai"
        dlg.apply()
        if self.settings.show_overlay:
            self._show_overlay()
        else:
            self.overlay.hide()
        if self._service_key() != old_service:
            self._rebind_backend()
            self._refresh_models()  # 换服务商后托盘菜单里的模型名也得跟着换
        self.overlay.apply_style(self.settings)
        self.selection_popup.apply_style(self.settings)
        if hasattr(self, "home"):
            self.home.refresh(self.settings)
        self._show_usage()
        if self.settings.mic_enabled != old_mic:
            self.set_mic_enabled(self.settings.mic_enabled)
        if self.joiner is not None:
            self.joiner.hold = self.settings.hold_seconds
            self.joiner.hold_max = self.settings.hold_max_seconds
            self.joiner.fillers = self.settings.fillers_zh if self.joiner.lang == "zh" else self.settings.fillers
        if not custom and new_model != old_model:
            self.settings.model, self.settings.reasoning_effort = old_model
            self.set_model(*new_model)
        if self.settings.hotkey != old_hotkey:
            self._register_hotkey()
        if self.settings.selection_hotkey != old_selection_hotkey:
            self._register_selection_hotkey()
        if self.state == IDLE and (self.settings.effective_engine(), self.settings.asr_language()) != old_engine:
            threading.Thread(target=self._preload_engine, daemon=True).start()

    def _service_key(self) -> tuple:
        """服务商 + 端点配置的指纹：变了就要换后端。"""
        profiles = tuple(sorted((p.get("name"), p.get("base_url"), p.get("model")) for p in self.settings.profiles))
        return (self.settings.provider, self.settings.active_profile, profiles)

    def _rebind_backend(self) -> None:
        try:
            backend = make_backend(self.settings, self.bridge.status.emit)
        except Exception as e:  # 配置不完整：提示但不打断
            QMessageBox.warning(self.overlay, "模型服务", str(e))
            return
        self._watch(self.translator.use_backend(backend), "rebind")

    def _place_settings(self, dlg: SettingsDialog) -> None:
        """把设置窗口收进屏幕内；悬浮窗在屏幕内时再放到它旁边空间最大的一侧。"""
        rect = self.overlay.frameGeometry()
        screen = self.overlay.screen() if self.overlay.isVisible() else None
        if screen is not None:
            hit = rect.intersected(screen.availableGeometry())
            if hit.width() < 120 or hit.height() < 60:
                screen = None  # 悬浮窗在屏幕外，别跟着它跑
        screen = screen or self.qapp.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        dlg.fit_to_screen(avail)
        hit = rect.intersected(avail)
        if hit.width() >= 120 and hit.height() >= 60:
            point = place_beside(rect, dlg.size(), avail)
            dlg.move(point)
        else:
            dlg.move(
                avail.left() + max(0, (avail.width() - dlg.width()) // 2),
                avail.top() + max(0, (avail.height() - dlg.height()) // 2),
            )

    def quit(self) -> None:
        if self.state != IDLE:
            self._stop_capture()
            self._restore_default_output()
            if self.transcript:
                self.transcript.save()
        if self.hotkey:
            self.hotkey.stop()
        if self.selection_hotkey:
            self.selection_hotkey.stop()
        self.selection_capture.cancel()
        self.home.hide()
        self.translator.shutdown()
        self.tray.hide()
        self.qapp.quit()


def _set_app_user_model_id() -> None:
    """把任务栏从 python.exe 分组里摘出来，使用本程序图标。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YangCode.MeetingTranslate")
    except Exception:
        log.debug("设置 AppUserModelID 失败", exc_info=True)


def main() -> None:
    _set_app_user_model_id()
    ensure_dirs()
    use_bundled_runtime()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    if not qtcom.gui_thread_is_sta():
        # COM 已被别的库设成 MTA，原生文件对话框会卡死：退回 Qt 自己画的对话框。
        log.warning("COM 线程不是 STA，改用 Qt 内置对话框")
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(_icon())
    app = App(qapp)  # noqa: F841
    sys.exit(qapp.exec())
