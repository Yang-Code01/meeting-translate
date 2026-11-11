"""用户可见文案里的入口编号与首页一致：01 会议 / 02 划词 / 03 截图 / 04 语音实时转译。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from meeting_translate import app as app_module  # noqa: E402
from meeting_translate import resource_details  # noqa: E402
from meeting_translate.app import App  # noqa: E402
from meeting_translate.model_download_dialog import ModelDownloadDialog  # noqa: E402


def _app_with_blockers(blockers: list[str]) -> App:
    app = App.__new__(App)
    app._duplex_blockers = blockers
    app._duplex_download_confirmed = False
    app.home = None
    app.overlay = None
    app._refresh_resources = lambda **_: None  # 不查真实依赖
    return app


def test_duplex_blocker_prompt_uses_card_number_04(monkeypatch, tmp_path) -> None:
    shown: list[tuple] = []
    monkeypatch.setattr(app_module.msg, "info", lambda parent, title, text, **kw: shown.append((title, text, kw.get("detail", ""))))
    assert _app_with_blockers(["虚拟声卡"])._duplex_ready(None) is False
    title, text, detail = shown[0]
    assert text == "「04 语音实时转译」需要先装好：虚拟声卡。"
    assert "「01 会议实时翻译」和「02 划词翻译」" in detail
    assert "03 语音" not in text + detail


def test_duplex_download_prompt_uses_card_number_04(monkeypatch) -> None:
    asked: list[tuple] = []

    def fake_ask(parent, title, text, **kw):
        asked.append((title, text, kw.get("detail", "")))
        return True

    monkeypatch.setattr(app_module.msg, "ask", fake_ask)
    assert _app_with_blockers([])._confirm_duplex_download("语音模型") is True
    _, text, detail = asked[0]
    assert text == "语音模型只给「04 语音实时转译」使用。"
    assert "03" not in text + detail


def test_model_download_notes_use_card_number_04() -> None:
    QApplication.instance() or QApplication([])
    dlg = ModelDownloadDialog()
    texts = [lbl.text() for lbl in dlg.findChildren(QLabel)]
    joined = "\n".join(texts)
    assert "「04 语音实时转译」" in joined
    assert "「03 语音实时转译」" not in joined
    dlg.deleteLater()


def test_resource_details_use_card_number_04(tmp_path) -> None:
    html = resource_details.models_html(tmp_path) + resource_details.cable_html(None, tmp_path)
    assert "「04 语音实时转译」" in html
    assert "「03 语音实时转译」" not in html
