"""按设置构造后端：Copilot 或某条自定义端点。"""

from __future__ import annotations

from collections.abc import Callable

from . import secrets
from .backend import Backend, CopilotBackend
from .config import Settings
from .openai_backend import OpenAICompatBackend, OpenAIProfile


def make_backend(settings: Settings, on_status: Callable[[str], None]) -> Backend:
    if settings.provider != "openai":
        return CopilotBackend(on_status=on_status)
    profile = settings.profile() or {}  # 缺失时交给 start() 报「未填写 base_url」
    return OpenAICompatBackend(
        OpenAIProfile(
            name=profile.get("name", ""),
            base_url=profile.get("base_url", ""),
            api_key=secrets.unprotect(profile.get("api_key", "")),
            model=profile.get("model", ""),
            input_price=profile.get("input_price"),
            output_price=profile.get("output_price"),
            verify_tls=bool(profile.get("verify_tls", True)),
        ),
        on_status=on_status,
    )
