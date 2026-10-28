"""悬浮框「主界面」按钮只在没有进行中的会议时可点。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate.app import App, IDLE, RUNNING  # noqa: E402
from meeting_translate.config import Settings  # noqa: E402
from meeting_translate.transcript import Entry  # noqa: E402
from meeting_translate.ui import Overlay  # noqa: E402


def test_overlay_home_button_disabled_while_active() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    overlay = Overlay(Settings())
    overlay.set_running(False, False)
    assert overlay.btn_home.isEnabled()
    overlay.set_running(True, True)
    assert not overlay.btn_home.isEnabled()
    overlay.set_running(False, True)  # 暂停也算进行中
    assert not overlay.btn_home.isEnabled()
    overlay.set_running(False, False)
    assert overlay.btn_home.isEnabled()
    overlay.deleteLater()


def test_return_home_is_refused_while_meeting_active() -> None:
    app = App.__new__(App)
    app.state = RUNNING
    statuses: list[str] = []
    app._set_status = statuses.append  # type: ignore[method-assign]

    class FakeOverlay:
        hidden = False

        def hide(self) -> None:
            self.hidden = True

    class FakeHome:
        shown = False

        def show(self) -> None:
            self.shown = True

        def raise_(self) -> None:
            pass

        def activateWindow(self) -> None:
            pass

    app.overlay = FakeOverlay()  # type: ignore[assignment]
    app.home = FakeHome()  # type: ignore[assignment]
    app._return_home()
    assert not app.overlay.hidden
    assert not app.home.shown
    assert statuses == ["翻译进行中，请先结束会议再返回主界面"]

    app.state = IDLE
    app._return_home()
    assert app.overlay.hidden
    assert app.home.shown


def test_overlay_font_color_is_applied() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    overlay = Overlay(Settings(font_color="#ffcc00"))
    overlay.entries = [Entry(1, 1.0, "我", "你好", "こんにちは")]
    overlay.render()
    html = overlay.view.toHtml().lower()
    assert "#ffcc00" in html
    overlay.deleteLater()


def test_meeting_end_keeps_overlay_and_does_not_show_home() -> None:
    app = App.__new__(App)
    app._busy = True
    app.settings = Settings()
    app._restore_default_output = lambda: None  # type: ignore[method-assign]
    app._close_tts = lambda: None  # type: ignore[method-assign]
    app._finish_usage = lambda: None  # type: ignore[method-assign]
    app._show_home = lambda: (_ for _ in ()).throw(AssertionError("不能自动回主界面"))  # type: ignore[method-assign]
    asked: list[bool] = []
    app._ask_summarize = lambda: asked.append(True)  # type: ignore[method-assign]
    app._on_job_done("meeting_ended", None)
    assert asked == [True]


def test_overlay_scroll_follows_unless_user_scrolls_up() -> None:
    qapp = QApplication.instance() or QApplication([])  # noqa: F841
    overlay = Overlay(Settings())
    overlay.resize(500, 260)
    overlay.show()
    for i in range(60):
        overlay.entries.append(Entry(i, 0.0, "对方", f"第{i}句" * 8, f"译文{i}" * 8))
    overlay.render()
    sb = overlay.view.verticalScrollBar()
    assert sb.maximum() > 0 and sb.value() == sb.maximum()
    assert overlay.btn_bottom.isHidden()

    sb.setValue(0)  # 用户往上翻
    assert overlay.btn_bottom.isVisible()
    first = overlay.view.toPlainText().splitlines()[0]
    overlay.entries.append(Entry(60, 0.0, "对方", "新的一句", "新译文"))
    overlay.render()
    assert sb.value() == 0  # 不再被拉到底
    assert overlay.view.toPlainText().splitlines()[0] == first  # 顶部条目没被裁掉

    overlay.btn_bottom.click()
    assert sb.value() == sb.maximum() and overlay.btn_bottom.isHidden()
    sb.setValue(0)
    sb.setValue(sb.maximum())  # 手动拖回底部也恢复跟随
    assert overlay.btn_bottom.isHidden()
    overlay.entries.append(Entry(61, 0.0, "对方", "再一句", "再译文"))
    overlay.render()
    assert sb.value() == sb.maximum()
    overlay.deleteLater()
