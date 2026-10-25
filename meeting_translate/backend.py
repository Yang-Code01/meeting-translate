"""翻译后端：会话与客户端抽象，以及 Copilot 实现。

Translator 只依赖 Session / Backend 两个协议：

- Session：send(文本) -> 文本，close() 负责结算用量；
- Backend：启动、登录、模型目录、配额、建会话。

换后端（例如 OpenAI 兼容端点）只需再实现一个 Backend，
批次合并、解析、轮换、摘要这些业务逻辑不必改动。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from . import stats
from .usage import Usage

log = logging.getLogger(__name__)

GITHUB_HOST = "https://github.com"
REQUEST_TIMEOUT = 60.0


@dataclass
class AuthState:
    ok: bool
    login: str | None = None
    message: str = ""


@dataclass
class ModelOption:
    id: str
    name: str
    input_price: float | None  # AIC / 百万 token
    output_price: float | None
    supports_effort: bool
    efforts: list[str]
    enabled: bool
    # 端点 /models 可以声明是否允许彻底关闭思考；没声明时保持未知。
    can_disable_thinking: bool = False
    default_effort: str = ""

    @property
    def price(self) -> float | None:
        if self.input_price is None or self.output_price is None:
            return None
        return stats.blended_price(self.input_price, self.output_price)

    @classmethod
    def from_info(cls, m) -> "ModelOption":
        d = m.to_dict() if hasattr(m, "to_dict") else {}
        tp = (d.get("billing") or {}).get("tokenPrices") or {}
        supports = bool(((d.get("capabilities") or {}).get("supports") or {}).get("reasoningEffort"))
        state = (d.get("policy") or {}).get("state")
        return cls(
            id=d.get("id") or m.id,
            name=d.get("name") or d.get("id") or m.id,
            input_price=tp.get("inputPrice"),
            output_price=tp.get("outputPrice"),
            supports_effort=supports,
            efforts=list(d.get("supportedReasoningEfforts") or []),
            enabled=state in (None, "enabled"),
        )


@dataclass
class Quota:
    remaining_percent: float | None
    unlimited: bool
    used: int | None = None  # 本计费周期账户已用
    entitlement: int | None = None  # 本周期额度；-1 / None = 不限或未知


def clamp_effort(opt: ModelOption | None, effort: str | None) -> tuple[str | None, str]:
    """返回 (实际 effort, 回退说明)。effort=None 表示用模型默认值。"""
    if not effort or effort == "default" or opt is None:
        return (effort if effort != "default" else None), ""
    if effort == "none" and (opt.can_disable_thinking or "none" in opt.efforts):
        return "none", ""
    if not opt.supports_effort:
        return None, f"{opt.name} 不支持推理强度设置，已用默认值"
    if not opt.efforts or effort in opt.efforts:
        return effort, ""
    fallback = opt.efforts[0]
    return fallback, f"{opt.name} 不支持 effort={effort}，已改用 {fallback}"


def reply_text(event) -> str:
    """从 Copilot 的会话事件里取出文本。"""
    return (getattr(getattr(event, "data", None), "content", None) or "").strip() if event else ""


class Session(Protocol):
    """一次可对话的会话。实现方负责自己的历史维护方式。"""

    async def send(self, text: str, timeout: float = REQUEST_TIMEOUT) -> str: ...

    async def close(self) -> None: ...


class Backend(Protocol):
    async def start(self) -> AuthState: ...

    async def stop(self) -> None: ...

    async def login(self, token: str) -> AuthState: ...

    async def models(self) -> list[ModelOption]: ...

    async def quota(self) -> Quota | None: ...

    async def create_session(
        self, system: str, model: str, effort: str | None, usage: Usage | None = None
    ) -> Session: ...

    def option(self, model: str) -> ModelOption | None: ...


class CopilotSession:
    """把 Copilot SDK 的会话包装成 Session：send 只回文本，close 时结算用量。"""

    def __init__(self, session, backend: "CopilotBackend", usage: Usage | None) -> None:
        self._session = session
        self._backend = backend
        self._usage = usage
        self.session_id: str = getattr(session, "session_id", "")

    async def send(self, text: str, timeout: float = REQUEST_TIMEOUT) -> str:
        return reply_text(await self._session.send_and_wait(text, timeout=timeout))

    async def close(self) -> None:
        await self._backend.close_session(self._session, self._usage)


class CopilotBackend:
    """GitHub Copilot 后端：会话在服务端保存历史，用量按 nanoAIC 结算。"""

    def __init__(self, on_status: Callable[[str], None]) -> None:
        self._on_status = on_status
        self._client = None
        self._options: dict[str, ModelOption] = {}

    # ---- 客户端与登录 ----

    async def start(self) -> AuthState:
        from copilot import CopilotClient

        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:
                log.exception("停止客户端失败")
        self._on_status("正在连接 Copilot…")
        self._client = CopilotClient(log_level="error")
        await self._client.start()
        return await self._auth_state()

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:
                log.exception("关闭 Copilot 客户端失败")

    async def _auth_state(self) -> AuthState:
        assert self._client is not None
        try:
            st = await self._client.get_auth_status()
            if st.isAuthenticated:
                return AuthState(True, getattr(st, "login", None))
            return AuthState(False, message=getattr(st, "statusMessage", "") or "未登录")
        except Exception as e:
            return AuthState(False, message=str(e))

    async def login(self, token: str) -> AuthState:
        from copilot.generated.rpc import AccountLoginRequest

        assert self._client is not None
        try:
            await self._client.rpc.account.login(AccountLoginRequest(host=GITHUB_HOST, token=token.strip()))
        except Exception as e:
            return AuthState(False, message=f"Token 无效或缺少 Copilot Requests 权限：{e}")
        return await self.start()

    # ---- 模型与配额 ----

    async def models(self) -> list[ModelOption]:
        assert self._client is not None
        opts = [ModelOption.from_info(m) for m in await self._client.list_models()]
        self._options = {o.id: o for o in opts}
        return opts

    def option(self, model: str) -> ModelOption | None:
        return self._options.get(model)

    def default_model(self) -> str:
        return "claude-haiku-5.5"

    async def quota(self) -> Quota | None:
        from copilot.generated.rpc import AccountGetQuotaRequest

        assert self._client is not None
        try:
            r = await self._client.rpc.account.get_quota(AccountGetQuotaRequest())
        except Exception:
            log.exception("查询配额失败")
            return None
        snap = (r.quota_snapshots or {}).get("premium_interactions")
        if snap is None:
            return None
        return Quota(
            snap.remaining_percentage,
            bool(snap.is_unlimited_entitlement),
            getattr(snap, "used_requests", None),
            getattr(snap, "entitlement_requests", None),
        )

    # ---- 会话 ----

    async def create_session(
        self, system: str, model: str, effort: str | None, usage: Usage | None = None
    ) -> Session:
        from copilot.generated.session_events import SessionUsageCheckpointData
        from copilot.session import PermissionHandler

        assert self._client is not None
        if not self._options:
            try:
                await self.models()
            except Exception:
                log.exception("获取模型列表失败")
        effort, note = clamp_effort(self._options.get(model), effort)
        if note:
            self._on_status(note)
        s = await self._client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            model=model,
            reasoning_effort=effort,
            system_message={"mode": "replace", "content": system},
            available_tools=[],
            skip_custom_instructions=True,
            enable_skills=False,
            streaming=False,
        )
        if usage is not None:
            sid = s.session_id

            def on_event(e) -> None:
                d = getattr(e, "data", None)
                if isinstance(d, SessionUsageCheckpointData) and d.total_nano_aiu:
                    usage.update(sid, int(d.total_nano_aiu))

            s.on(on_event)
        return CopilotSession(s, self, usage)

    async def close_session(self, session, usage: Usage | None) -> None:
        """结算会话用量后断开并删除会话。"""
        sid = getattr(session, "session_id", None)
        if usage is not None and sid:
            try:
                m = (await session.rpc.usage.get_metrics()).to_dict()
                nano = sum(int(v.get("totalNanoAiu") or 0) for v in (m.get("modelMetrics") or {}).values())
                usage.update(sid, nano)
            except Exception:
                log.debug("获取会话用量失败 %s", sid, exc_info=True)
        try:
            await session.disconnect()
            if sid and self._client is not None:
                await self._client.delete_session(sid)
        except Exception:
            log.debug("删除会话失败 %s", sid, exc_info=True)
