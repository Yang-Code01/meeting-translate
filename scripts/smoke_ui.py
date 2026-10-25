"""离屏冒烟：构造设置/端点/登录对话框并 apply，确认没有运行时错误。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["APPDATA"] = tempfile.mkdtemp()
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_translate.config import Settings  # noqa: E402
from meeting_translate.ui import LoginDialog, ProfileDialog, SettingsDialog  # noqa: E402

app = QApplication([])
s = Settings(
    provider="openai",
    active_profile="ds",
    profiles=[{"name": "ds", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key": "dpapi:xxx"}],
)
dlg = SettingsDialog(s, [])
dlg.apply()
assert s.provider == "openai", s.provider
assert s.profiles and s.profiles[0]["name"] == "ds"
dlg2 = ProfileDialog(s.profiles[0])
assert dlg2.result_profile()["name"] == "ds"
dlg3 = LoginDialog("未登录")

# 默认（Copilot）配置也要能构造：端点行应是禁用状态
plain = Settings()
dlg4 = SettingsDialog(plain, [])
assert not dlg4.profile_combo.isEnabled(), "Copilot 模式下端点选择应禁用"
dlg4.apply()
assert plain.provider == "copilot"
print("UI smoke ok:", s.provider, len(s.profiles), "| copilot ok")
