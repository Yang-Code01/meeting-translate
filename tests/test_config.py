"""配置迁移、profile 查找与 DPAPI 存 key。"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from meeting_translate import config, providers, secrets
from meeting_translate.openai_backend import OpenAICompatBackend


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(config, "MODEL_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    return tmp_path / "config.json"


def test_v2_config_migrates_to_v3_with_copilot_default(cfg_path):
    cfg_path.write_text(json.dumps({"version": 2, "model": "gpt-5.4-mini", "direction": "ja-zh"}), encoding="utf-8")
    s = config.Settings.load()
    assert s.version == 3
    assert s.provider == "copilot"
    assert s.profiles == []
    assert s.model == "gpt-5.4-mini"  # 旧设置原样保留


def test_profile_lookup_follows_active_profile(cfg_path):
    s = config.Settings(
        provider="openai",
        active_profile="local",
        profiles=[
            {"name": "cloud", "base_url": "https://api.deepseek.com/v1"},
            {"name": "local", "base_url": "http://127.0.0.1:11434/v1"},
        ],
    )
    assert s.profile()["name"] == "local"
    assert s.profile("cloud")["base_url"] == "https://api.deepseek.com/v1"
    assert s.profile("missing") is None


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 只在 Windows 上可用")
def test_api_key_is_encrypted_at_rest():
    token = secrets.protect("sk-secret-value")
    assert token.startswith("dpapi:")
    assert "sk-secret-value" not in token
    assert secrets.unprotect(token) == "sk-secret-value"
    assert secrets.unprotect("sk-plain") == "sk-plain"  # 手改的明文仍可用


def test_make_backend_decrypts_active_profile(cfg_path):
    s = config.Settings(
        provider="openai",
        active_profile="deepseek",
        profiles=[
            {
                "name": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": secrets.protect("sk-x"),
            }
        ],
    )
    backend = providers.make_backend(s, on_status=lambda m: None)
    assert isinstance(backend, OpenAICompatBackend)
    assert backend._profile.api_key == "sk-x"
    assert backend._profile.base_url == "https://api.deepseek.com/v1"


def test_make_backend_returns_copilot_by_default(cfg_path):
    from meeting_translate.backend import CopilotBackend

    s = config.Settings(provider="copilot")
    assert isinstance(providers.make_backend(s, on_status=lambda m: None), CopilotBackend)


def test_make_backend_without_profile_reports_missing_base_url(cfg_path):
    """配置不完整时不静默回落到 Copilot，而是让 start() 报出可修的错。"""
    s = config.Settings(provider="openai", active_profile="", profiles=[])
    backend = providers.make_backend(s, on_status=lambda m: None)
    assert isinstance(backend, OpenAICompatBackend)
    state = asyncio.run(backend.start())
    assert state.ok is False
    assert "base_url" in state.message


def test_overlay_visibility_is_saved(cfg_path):
    s = config.Settings()
    assert s.show_overlay is True
    s.show_overlay = False
    s.save()
    assert config.Settings.load().show_overlay is False


def test_subtitle_mode_is_saved(cfg_path):
    s = config.Settings()
    assert s.subtitle_mode is False
    s.subtitle_mode = True
    s.save()
    assert config.Settings.load().subtitle_mode is True
