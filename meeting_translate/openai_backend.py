"""OpenAI 兼容后端：POST /chat/completions，会话历史由本地维护。

与 Copilot 后端的差别：
- 没有服务端会话，messages 自己带着走；轮换与摘要仍由 Translator 决定。
- 按设置发送 reasoning_effort；DeepSeek 或 /models 声明 can_disable_thinking 的端点用 thinking 字段关闭思考。
- 没有账户配额，quota() 恒为 None；用量按 token 记入 Usage。
- 429 / 5xx / 网络错误退避重试一次；连续 429 会拉长请求间隔并在状态栏说明。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .backend import REQUEST_TIMEOUT, AuthState, ModelOption, Quota, Session, clamp_effort
from .usage import Usage

log = logging.getLogger(__name__)

TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_INTERVAL = 8.0


def normalize_base_url(value: str) -> str:
    """补全用户少写的协议头。

    多数 OpenAI 兼容服务用 https，但本机/LAN 地址通常是 http；只写
    ``127.0.0.1:7863`` 时 httpx 会报 “URL is missing protocol”，
    在设置里却只看到“没有可用的模型名”，很难排查。
    """
    value = (value or "").strip()
    if not value:
        return value
    if "://" in value:
        scheme, rest = value.split("://", 1)
        return f"{scheme.lower()}://{rest}" if scheme.lower() in {"http", "https"} else value
    host = (urlparse("//" + value).hostname or "").lower()
    try:
        ipaddress.ip_address(host)
        scheme = "http"
    except ValueError:
        scheme = "http" if host in {"localhost"} or host.endswith(".local") else "https"
    return f"{scheme}://{value}"


def is_loopback(base_url: str) -> bool:
    """本机端点不走系统代理，否则代理连不上时会把它变成 502。"""
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"localhost", "::1"} or host.startswith("127.")


def is_deepseek(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host == "api.deepseek.com" or host.endswith(".deepseek.com")


def reasoning_payload(effort: str | None, base_url: str, can_disable_thinking: bool = False) -> dict:
    """把推理强度翻译成请求字段；不同服务商的开关方式不一样。

    DeepSeek 以及部分 OpenAI 兼容网关用 thinking.type 关思考；/models 里
    声明了 can_disable_thinking 的端点也走这个字段。其余情况照旧发
    reasoning_effort，``none`` 是否被接受由端点决定。
    """
    value = (effort or "").strip().lower()
    if not value or value == "default":
        return {}
    if value == "none":
        if is_deepseek(base_url) or can_disable_thinking:
            return {"thinking": {"type": "disabled"}}
        return {}
    body: dict = {"reasoning_effort": value}
    if is_deepseek(base_url):
        body["thinking"] = {"type": "enabled"}
    return body


@dataclass
class OpenAIProfile:
    """一条自定义端点配置。api_key 由调用方负责加密存储，这里只收明文。"""

    name: str
    base_url: str
    api_key: str = ""
    model: str = ""
    input_price: float | None = None  # 元 / 百万 token，填了才折算花费
    output_price: float | None = None
    verify_tls: bool = True

    def __post_init__(self) -> None:
        # 配置里可能存着“127.0.0.1:7863”这种没有协议头的地址。
        self.base_url = normalize_base_url(self.base_url)


class OpenAICompatSession:
    """本地维护 messages 的会话；send 追加一轮问答，失败时回滚这一轮。"""

    def __init__(
        self,
        backend: "OpenAICompatBackend",
        system: str,
        model: str,
        effort: str | None,
        usage: Usage | None,
    ) -> None:
        self._backend = backend
        self._model = model
        self._effort = effort
        self._usage = usage
        self._messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        self._system = system
        self._primed = False
        self.session_id = f"openai-{id(self)}"

    @property
    def messages(self) -> list[dict[str, str]]:
        return list(self._messages)

    async def send(self, text: str, timeout: float = REQUEST_TIMEOUT) -> str:
        # 有些 OpenAI 兼容网关会直接丢掉 system 角色（实测 wb2api 就是如此），
        # 模型只看到用户文本，就会把待翻译内容当成聊天消息来回答。
        # 首个用户消息里再带一遍 system prompt，既不依赖角色支持，也不影响
        # 正常服务端对 system 的处理。
        primed_now = False
        if not self._primed and self._system.strip():
            text = f"{self._system}\n\n{text}"
            self._primed = True
            primed_now = True
        self._messages.append({"role": "user", "content": text})
        try:
            reply, usage = await self._backend.chat(self._messages, self._model, self._effort, timeout)
        except Exception:
            self._messages.pop()  # 失败的一轮不进历史，避免下一次带着无回复的半轮请求
            if primed_now:
                self._primed = False
            raise
        self._messages.append({"role": "assistant", "content": reply})
        if self._usage is not None and usage:
            self._usage.add_tokens(
                self.session_id,
                usage.get("prompt_tokens") or 0,
                usage.get("completion_tokens") or 0,
            )
        return reply

    async def close(self) -> None:
        self._messages.clear()


class OpenAICompatBackend:
    def __init__(
        self,
        profile: OpenAIProfile,
        on_status: Callable[[str], None],
        retry_delay: float = 1.5,
    ) -> None:
        self._profile = profile
        self._on_status = on_status
        self._retry_delay = retry_delay
        self._client: httpx.AsyncClient | None = None
        self._options: dict[str, ModelOption] = {}
        self._resolved = ""
        self._models_error = ""
        self._interval = 0.0
        self._next_at = 0.0

    @property
    def models_error(self) -> str:
        """最近一次 /models 失败的原因（供设置窗口显示，避免只说“没有模型名”）。"""
        return self._models_error

    # ---- 生命周期 ----

    async def _open_client(self) -> AuthState:
        """建好 HTTP 客户端；不校验模型名，列模型的场景也要能走到这里。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        base_url = normalize_base_url(self._profile.base_url)
        if not base_url:
            return AuthState(False, message="未填写接口地址（base_url）")
        if not base_url.startswith(("http://", "https://")):
            return AuthState(False, message="接口地址要以 http:// 或 https:// 开头")
        if not self._profile.api_key:
            return AuthState(False, message="未填写 API key")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {self._profile.api_key}"},
            timeout=REQUEST_TIMEOUT,
            verify=self._profile.verify_tls,
            trust_env=not is_loopback(base_url),
        )
        if not self._profile.verify_tls:
            log.warning("已关闭 %s 的证书校验", self._profile.name)
        return AuthState(True, login=self._profile.name)

    async def start(self) -> AuthState:
        state = await self._open_client()
        if not state.ok:
            return state
        # 模型名在配置里留空时，起服务阶段就把 /models 第一项定下来。
        # 定不下来就别装作连上了——否则每条翻译都会拿空 model 撞 400。
        if not await self._ensure_model():
            await self.stop()
            detail = f"（{self._models_error}）" if self._models_error else ""
            return AuthState(
                False,
                message=f"没有可用的模型名：请在端点里填写模型名，或确认该端点的 /models 能返回列表{detail}",
            )
        # 即使配置里已经写死模型名，也拉一次 /models：推理档位和能否关闭
        # 思考只在这里返回。失败不影响启动，只是退回通用档位。
        if not self._options:
            await self.models()
        return AuthState(True, login=self._profile.name)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def login(self, token: str) -> AuthState:
        return AuthState(False, message="该后端不使用登录，请直接填写 API key")

    # ---- 模型与配额 ----

    async def models(self) -> list[ModelOption]:
        if self._client is None:
            state = await self._open_client()
            if not state.ok:
                self._models_error = state.message
                return []
        assert self._client is not None
        self._models_error = ""
        try:
            r = await self._client.get("models", timeout=10.0)
            r.raise_for_status()
            infos = [
                m for m in (r.json().get("data") or [])
                if isinstance(m, dict) and m.get("id")
            ]
        except Exception as e:
            self._models_error = self._describe_models_error(e)
            log.warning("拉取模型列表失败，改用配置里的模型名", exc_info=True)
            infos = []
        if not infos and self._profile.model:
            infos = [{"id": self._profile.model}]
        self._options = {}
        for info in infos:
            mid = str(info["id"])
            efforts = [str(x) for x in (info.get("reasoning_supported_efforts") or []) if x]
            self._options[mid] = ModelOption(
                id=mid,
                name=str(info.get("name") or mid),
                input_price=self._profile.input_price,
                output_price=self._profile.output_price,
                supports_effort=bool(efforts),
                efforts=efforts,
                enabled=True,
                can_disable_thinking=bool(info.get("can_disable_thinking")),
                default_effort=str(info.get("reasoning_default_effort") or ""),
            )
        return list(self._options.values())

    @staticmethod
    def _describe_models_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            hint = {404: "，接口地址可能少了 /v1", 401: "，API key 可能无效"}.get(code, "")
            return f"HTTP {code}{hint}"
        return str(exc).strip() or type(exc).__name__

    def option(self, model: str) -> ModelOption | None:
        return self._options.get(model)

    def default_model(self) -> str:
        return self._profile.model or next(iter(self._options), "")

    async def _ensure_model(self) -> str:
        """解析这条端点该用的模型名：配置里的优先，留空则取 /models 第一项。"""
        if self._resolved:
            return self._resolved
        if self._profile.model:
            self._resolved = self._profile.model
            return self._resolved
        if not self._options:
            await self.models()
        self._resolved = next(iter(self._options), "")
        if self._resolved:
            log.info("%s 未填模型名，自动使用 %s", self._profile.name, self._resolved)
        return self._resolved

    async def quota(self) -> Quota | None:
        return None

    # ---- 会话 ----

    async def create_session(
        self, system: str, model: str, effort: str | None = None, usage: Usage | None = None
    ) -> Session:
        if self._client is None:
            state = await self.start()
            if not state.ok:
                raise RuntimeError(state.message)
        resolved = model or await self._ensure_model()
        if not resolved:
            raise RuntimeError("没有可用的模型名：请在端点里填写模型名，或确认该端点的 /models 能返回列表")
        opt = self._options.get(resolved)
        if opt is not None and (opt.supports_effort or opt.can_disable_thinking):
            effort, note = clamp_effort(opt, effort)
            if note:
                self._on_status(note)
        return OpenAICompatSession(self, system, resolved, effort, usage)

    # ---- 请求 ----

    async def chat(
        self, messages: list[dict[str, str]], model: str, effort: str | None, timeout: float
    ) -> tuple[str, dict]:
        assert self._client is not None, "后端尚未启动"
        payload = {"model": model, "messages": messages, "stream": False}
        opt = self._options.get(model)
        payload.update(
            reasoning_payload(
                effort,
                self._profile.base_url,
                bool(opt and opt.can_disable_thinking),
            )
        )
        last_error = "请求失败"
        for attempt in (0, 1):
            await self._throttle()
            try:
                r = await self._client.post("chat/completions", json=payload, timeout=timeout)
            except httpx.HTTPError as e:
                last_error = f"网络错误：{e}"
                if attempt == 0:
                    log.warning("请求失败，重试一次：%s", e)
                    await asyncio.sleep(self._retry_delay)
                    continue
                raise RuntimeError(last_error) from e
            if r.status_code in TRANSIENT_STATUS and attempt == 0:
                self._slow_down(r.status_code)
                await asyncio.sleep(self._retry_delay)
                continue
            if r.status_code >= 400:
                raise RuntimeError(self._describe(r))
            return self._parse(r)
        raise RuntimeError(last_error)

    def _parse(self, r: httpx.Response) -> tuple[str, dict]:
        try:
            data = r.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError("端点返回的不是 JSON") from e
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("端点没有返回 choices")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("端点返回了空译文")
        return content.strip(), data.get("usage") or {}

    @staticmethod
    def _describe(r: httpx.Response) -> str:
        hint = {
            401: "API key 无效或没有权限",
            403: "没有访问该模型的权限",
            404: "base_url 或模型名不对",
            429: "请求过于频繁",
            500: "服务端内部错误",
            502: "网关错误（上游不可用，检查端点是否在运行）",
            503: "服务暂时不可用",
            504: "网关超时",
        }.get(r.status_code, "请求被拒绝")
        detail = " ".join((r.text or "").split())[:200]
        return f"{r.status_code} {hint}" + (f"：{detail}" if detail else "")

    # ---- 限流 ----

    async def _throttle(self) -> None:
        wait = self._next_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._next_at = time.monotonic() + self._interval

    def _slow_down(self, status: int) -> None:
        self._interval = min(MAX_INTERVAL, max(1.0, self._interval * 2))
        self._next_at = time.monotonic() + self._interval
        self._on_status(f"端点返回 {status}，已把请求间隔放慢到 {self._interval:.0f} 秒")
