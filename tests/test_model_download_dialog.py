"""语音模型下载窗：多项同时下载时每行各自显示进度，按钮在「暂停 / 继续」之间切换。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate.model_download_dialog import ModelDownloadDialog, ModelRow  # noqa: E402


def _row(key: str, *, ready: bool = False, status: str = "未下载") -> ModelRow:
    return ModelRow(key=key, name=key, role="", size="1MB", source="", ready=ready, status=status)


def test_rows_download_concurrently_and_can_be_paused() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    dlg = ModelDownloadDialog()
    paused: list[str] = []
    started: list[str] = []
    dlg.pause_requested.connect(paused.append)
    dlg.download_requested.connect(started.append)
    rows = [_row("runtime", ready=True, status="已就绪"), _row("melo"), _row("sbv2"), _row("bert")]

    dlg.update_rows(rows, jobs={"melo": ("1MB / 2MB", 1, 2), "sbv2": ("准备下载…", 0, 0)})
    assert [dlg._buttons[k].text() for k in ("runtime", "melo", "sbv2", "bert")] == ["已就绪", "暂停", "暂停", "下载"]
    assert dlg.btn_all.isEnabled()  # bert 还能一起开始
    assert not dlg.btn_pause_all.isHidden()

    dlg._buttons["melo"].click()
    dlg._buttons["bert"].click()
    assert paused == ["melo"] and started == ["bert"]

    dlg.update_rows(rows, jobs={"sbv2": ("准备下载…", 0, 0)}, paused={"melo"})
    assert dlg._buttons["melo"].text() == "继续"
    assert "已暂停" in dlg._status["melo"][0].text()
    dlg._buttons["melo"].click()
    assert started == ["bert", "melo"]
