"""翻译业务层：批次合并、校正解析、会话轮换与摘要、划词会话、用量账本。

Copilot 的具体调用在 backend.py；本模块不 import copilot，也不知道会话历史
是服务端保存还是本地维护。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import glossary, lang, stats
from .backend import REQUEST_TIMEOUT, AuthState, Backend, CopilotBackend, ModelOption, Quota, Session
from .transcript import Transcript
from .usage import Usage

if TYPE_CHECKING:  # 仅用于类型标注：运行时导入会让本模块依赖 ASR 栈
    from .asr import Final

log = logging.getLogger(__name__)


PRIME_PROMPT = "（连接测试，无需翻译，请只回复：好）"
SELECTION_SYSTEM_PROMPT = """\
你是日语翻译助手。对用户提供的日文，生成中文翻译和带假名注音的原文。
每条用户消息都是一段独立的新文本，与之前的消息无关，不要参考之前的内容；原文放在 <<< 与 >>> 之间，可能有多行。
只输出一个 JSON 对象，且只包含字符串字段 "chinese" 与 "hiragana"；不要使用 Markdown 或添加解释。
"hiragana" 必须完整保留日文原文（含假名、标点），只在每个汉字词后面紧跟全角括号标注该词的平假名读音，
例如「会議の資料を送ります」→「会議（かいぎ）の資料（しりょう）を送（おく）ります」；没有汉字的部分原样保留，不要把中文翻译转写成假名。"""
SELECTION_SESSION_MAX_REQUESTS = 20
BENCH_SENTENCES = {
    "ja-zh": [
        "【对方】来週の打ち合わせは火曜日の午後でよろしいでしょうか。",
        "【对方】この件については社内で検討してから改めてご連絡いたします。",
        "【我】承知しました。資料は明日までにお送りします。",
    ],
    "zh-ja": [
        "【对方】下周的会议安排在星期二下午可以吗？",
        "【对方】这个问题我们内部讨论之后再联系您。",
        "【我】明白了，资料我明天之前发给您。",
    ],
}

SYSTEM_PROMPT = """\
你是会议同声传译，把{src}会议发言翻译成{dst}。
规则：
- 【对方】/【我】表示说话方，只用于理解上下文，译文中不要输出。
- 只输出译文，不解释、不加引号、不加前缀。
- 输入来自语音识别，可能有错字、漏字或缺少标点，请结合上下文推断原意后翻译。
- 单句输入：直接输出译文。带编号的多句输入：每行输出「编号. 译文」，编号与输入一一对应。
- 无法理解或只是语气词时，照常给出最贴近的简短译文，不要拒绝。"""

SYSTEM_PROMPT_CORRECT = """\
你是会议同声传译，把{src}会议发言翻译成{dst}。
输入来自语音识别，常有同音/近音错字、漏字、错误断句或缺少标点。每句先校正原文，再翻译。
规则：
- 【对方】/【我】表示说话方，只用于理解上下文，不要输出。
- 校正：结合会议上下文、前情摘要和术语表，把识别错误改成说话人最可能说的话，补上标点；
  只修正识别错误，不润色、不改语气、不增删内容。没有错误就原样输出。
- 严格按以下格式输出，编号与输入一一对应（单句输入编号为 1），不解释、不加引号：
1原：校正后的{src}原文
1译：{dst}译文
- 无法理解或只是语气词时，照常给出最贴近的简短校正与译文，不要拒绝。"""

SEGMENT_SUMMARY_PROMPT = """\
下面是一场{src}会议的一个片段（附{dst}译文）。请用中文写出截至目前的会议前情摘要（300 字以内），
包含：正在讨论的议题、已达成的结论、出现的人名/系统名/专有术语（保留原词）。只输出摘要正文。"""

def system_prompt(direction: str, correct: bool = False) -> str:
    d = lang.get(direction)
    return (SYSTEM_PROMPT_CORRECT if correct else SYSTEM_PROMPT).format(src=d.src, dst=d.dst)


def segment_summary_prompt(direction: str) -> str:
    d = lang.get(direction)
    return SEGMENT_SUMMARY_PROMPT.format(src=d.src, dst=d.dst)

@dataclass
class Translation:
    ids: list[int]
    texts: list[str]
    latency: float  # 从最早一句定稿到译文返回（秒）
    sources: list[str] | None = None  # LLM 校正后的原文；None/空串 = 未校正
    direction: str = lang.JA_ZH  # 这一批属于哪个方向（双向会议时区分）


@dataclass
class SelectionTranslation:
    chinese: str
    hiragana: str

def _format_batch(items: list[Final], correct: bool = False) -> str:
    if correct:
        lines = [f"{i}. 【{f.source}】{f.ja}" for i, f in enumerate(items, 1)]
        return "\n".join(lines)
    if len(items) == 1:
        return f"【{items[0].source}】{items[0].ja}"
    lines = [f"{i}. 【{f.source}】{f.ja}" for i, f in enumerate(items, 1)]
    return "以下多句请按编号逐句翻译：\n" + "\n".join(lines)


_NUM_LINE = re.compile(r"^\s*(\d+)\s*[.．、:：)）]\s*(.*)$")
_PAIR_LINE = re.compile(r"^\s*(\d+)\s*[.．、)）]?\s*(原|译|譯|訳)\s*[:：]\s*(.*)$")
_SPEAKER_TAG = re.compile(r"^【[^】]{1,4}】")


def _parse_corrected(reply: str, n: int) -> tuple[list[str], list[str]] | None:
    """解析「N原：…/N译：…」格式；没有任何译文行时返回 None（交给旧格式解析）。"""
    srcs, texts = [""] * n, [""] * n
    for line in reply.splitlines():
        m = _PAIR_LINE.match(line)
        if m and 1 <= int(m.group(1)) <= n:
            body = _SPEAKER_TAG.sub("", m.group(3).strip()).strip()
            (srcs if m.group(2) == "原" else texts)[int(m.group(1)) - 1] = body
    if not any(texts):
        return None
    return srcs, texts


def _parse_batch(reply: str, n: int) -> list[str]:
    if n == 1:
        return [reply.strip()]
    out = [""] * n
    for line in reply.splitlines():
        m = _NUM_LINE.match(line)
        if m and 1 <= int(m.group(1)) <= n:
            out[int(m.group(1)) - 1] = m.group(2).strip()
    if not any(out):
        out[-1] = reply.strip()
    return out



def _parse_selection_translation(reply: str) -> SelectionTranslation:
    match = re.search(r"\{[\s\S]*\}", reply)
    if not match:
        raise ValueError("模型未按要求返回日文翻译和假名")
    data = json.loads(match.group())
    if not isinstance(data, dict):
        raise ValueError("模型返回的翻译格式无效")
    chinese = data.get("chinese")
    hiragana = data.get("hiragana")
    if not isinstance(chinese, str) or not chinese.strip() or not isinstance(hiragana, str) or not hiragana.strip():
        raise ValueError("模型返回的中文翻译或假名读音为空")
    return SelectionTranslation(chinese.strip(), hiragana.strip())


_KANA_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")


def _selection_request(text: str) -> str:
    return (
        "请处理下面 <<< 与 >>> 之间的日文原文（可能有多行）。"
        "\"hiragana\" 字段必须是日文原文本身加读音注释，绝不能是中文翻译。\n<<<\n"
        f"{text}\n>>>"
    )


def _reading_keeps_source(source: str, reading: str) -> bool:
    """读音字段去掉括号注音后，应保留原文中的大部分假名；否则多半是模型把中文译文填了进去。"""
    src_kana = len(_KANA_RE.findall(source))
    if src_kana == 0:
        return True
    kept = len(_KANA_RE.findall(re.sub(r"（[^）]*）|\([^)]*\)", "", reading)))
    return kept >= src_kana * 0.6

class Translator:
    """翻译业务层：队列合并、批次解析、轮换与摘要、划词会话、用量账本。"""

    def __init__(
        self,
        on_translation: Callable[[Translation], None],
        on_status: Callable[[str], None],
        on_auth: Callable[[AuthState], None],
        on_usage: Callable[[float], None] | None = None,
        backend: Backend | None = None,
    ) -> None:
        self._on_translation = on_translation
        self._on_status = on_status
        self._on_auth = on_auth
        self._on_usage = on_usage
        self._backend: Backend = backend or CopilotBackend(on_status=on_status)
        self.usage = Usage(on_usage)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="translator", daemon=True)
        self._session: Session | None = None
        self._sessions: dict[str, Session] = {}
        self._session_started = 0.0
        self._session_started_by: dict[str, float] = {}
        self._segment_start_wall = 0.0
        self._segment_start_wall_by: dict[str, float] = {}
        self._summary = ""
        self._summaries: dict[str, str] = {}
        self._directions: list[str] = []
        self._send_lock: asyncio.Lock | None = None
        self._selection_lock: asyncio.Lock | None = None
        self._selection_session: Session | None = None
        self._selection_key: tuple[str, str] | None = None
        self._selection_count = 0
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._pending = 0
        self._rotating = False
        self._transcript: Transcript | None = None
        self._model = ""
        self._effort = "none"
        self._rotate_seconds = 1800.0
        self._context_n = 5
        self._correct = False
        self._terms: list[glossary.Term] = []

    # ---- 线程桥接 ----

    def _call(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def start(self) -> concurrent.futures.Future:
        self._thread.start()
        return self._call(self._start())

    def shutdown(self) -> None:
        try:
            self._call(self._shutdown()).result(timeout=15)
        except Exception:
            log.exception("关闭翻译后端失败")
        self._loop.call_soon_threadsafe(self._loop.stop)

    def submit(self, final: Final) -> None:
        if self._queue is not None:
            self._loop.call_soon_threadsafe(self._enqueue, final)

    def _enqueue(self, final: Final) -> None:
        if self._queue is None:
            return
        self._queue.put_nowait(final)

    def start_meeting(
        self,
        transcript: Transcript,
        model: str,
        effort: str,
        rotate_minutes: int,
        context_n: int,
        terms: list[glossary.Term],
        correct: bool = False,
        directions: list[str] | None = None,
    ) -> concurrent.futures.Future:
        return self._call(
            self._start_meeting(transcript, model, effort, rotate_minutes, context_n, terms, correct, directions)
        )

    def end_meeting(self) -> concurrent.futures.Future:
        return self._call(self._end_meeting())

    def login_with_token(self, token: str) -> concurrent.futures.Future:
        return self._call(self._login_with_token(token))

    def recheck_auth(self) -> concurrent.futures.Future:
        return self._call(self._restart_client())

    def use_backend(self, backend: Backend) -> concurrent.futures.Future:
        """换后端（设置里切换服务商/端点后调用）：关旧会话、启新后端并重新预热。"""
        return self._call(self._switch_backend(backend))

    async def _close_all_sessions(self) -> None:
        sessions = list({id(s): s for s in [*self._sessions.values(), self._session] if s is not None}.values())
        self._sessions.clear()
        self._session = None
        for s in sessions:
            await s.close()

    async def _switch_backend(self, backend: Backend) -> AuthState:
        await self._close_all_sessions()
        if self._selection_session is not None:
            await self._selection_session.close()
            self._selection_session = None
        await self._backend.stop()
        self._backend = backend
        return await self._restart_client()

    def list_models(self) -> concurrent.futures.Future:
        """Future[list[ModelOption]]，实时从后端拉取。"""
        return self._call(self._list_models())

    def list_models_for(self, profile: dict) -> concurrent.futures.Future:
        """Future[list[ModelOption]]：拉取某条自定义端点自己的模型列表。"""
        return self._call(self._list_models_for(profile))

    def switch_model(self, model: str, effort: str) -> concurrent.futures.Future:
        return self._call(self._switch_model(model, effort))

    def benchmark(self, model: str, effort: str, direction: str = lang.JA_ZH) -> concurrent.futures.Future:
        """Future[float]：预热后测 3 句的平均延迟（秒）。"""
        return self._call(self._benchmark(model, effort, direction))

    def translate_selected(self, text: str, model: str, effort: str) -> concurrent.futures.Future:
        """Future[SelectionTranslation]：在隔离的一次性会话中翻译选中的日文。"""
        return self._call(self._translate_selected(text, model, effort))

    def prepare_selection(self, model: str, effort: str) -> concurrent.futures.Future:
        """提前建立并预热划词翻译会话，避免第一次按快捷键时等待冷启动。"""
        return self._call(self._prepare_selection(model, effort))

    def quota(self) -> concurrent.futures.Future:
        """Future[Quota | None]"""
        return self._call(self._quota())

    @property
    def model(self) -> str:
        return self._model

    def option(self, model: str) -> ModelOption | None:
        return self._backend.option(model)

    def run(self, coro_factory: Callable[["Translator"], object]) -> concurrent.futures.Future:
        """在翻译线程执行任意协程（供会后整理复用同一后端）。"""
        return self._call(coro_factory(self))  # type: ignore[arg-type]

    # ---- 启动与登录 ----

    async def _start(self) -> None:
        self._send_lock = asyncio.Lock()
        self._selection_lock = asyncio.Lock()
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work())
        await self._restart_client()

    async def _restart_client(self) -> AuthState:
        self._selection_session, self._selection_key = None, None
        state = await self._backend.start()
        self._on_auth(state)
        if state.ok:
            asyncio.create_task(self._warmup())
        return state

    async def _login_with_token(self, token: str) -> AuthState:
        state = await self._backend.login(token)
        self._on_auth(state)
        if state.ok:
            asyncio.create_task(self._warmup())
        return state

    async def _list_models(self) -> list[ModelOption]:
        return await self._backend.models()

    async def _list_models_for(self, profile: dict) -> list[ModelOption]:
        """为设置窗口里正在编辑的端点临时拉一次模型列表。

        不能问正在运行的后端：那条端点还没保存，运行中的后端指向的是上一条，
        问它永远问不到新填的地址。
        """
        from . import secrets
        from .openai_backend import OpenAICompatBackend, OpenAIProfile

        base_url = (profile.get("base_url") or "").strip()
        if not base_url:
            raise RuntimeError("先填写接口地址")
        if not (profile.get("api_key") or profile.get("model")):
            raise RuntimeError("先填写 API key")
        backend = OpenAICompatBackend(
            OpenAIProfile(
                name=profile.get("name", ""),
                base_url=base_url,
                api_key=secrets.unprotect(profile.get("api_key", "")),
                model=(profile.get("model") or "").strip(),
                input_price=profile.get("input_price"),
                output_price=profile.get("output_price"),
                verify_tls=bool(profile.get("verify_tls", True)),
            ),
            on_status=self._on_status,
        )
        try:
            opts = await backend.models()
        finally:
            await backend.stop()
        if not opts:
            detail = backend.models_error
            hint = f"（{detail}）" if detail else ""
            raise RuntimeError(f"该端点没有返回模型列表{hint}，请检查接口地址或手动填写模型名")
        return opts

    async def _quota(self) -> Quota | None:
        return await self._backend.quota()

    async def _prime(self, session: Session) -> None:
        """新会话的第一个请求有数秒冷启动，先用一条短请求吸收掉。

        失败不再静默：吞掉异常只会让人在界面上看到「用不起来」，看不到原因。
        """
        try:
            await session.send(PRIME_PROMPT, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            log.warning("会话预热失败", exc_info=True)
            self._on_status(f"翻译服务返回错误：{e}")
            raise  # 让开场就失败：否则界面只显示「会话已建立」，用户不知道根本翻不了

    async def _warmup(self) -> None:
        """启动时用临时会话预热运行时（不计入会议用量）。"""
        try:
            try:
                await self._list_models()
            except Exception:
                log.exception("获取模型列表失败")
            s = await self.new_session("只回复：OK", self._model or self._backend.default_model(), "none")
            await s.send("ping", timeout=REQUEST_TIMEOUT)
            await s.close()
            self._on_status("模型服务已就绪")
        except Exception as e:
            log.exception("预热失败")
            self._on_status(f"模型服务预热失败：{e}")

    async def _shutdown(self) -> None:
        if self._worker:
            self._worker.cancel()
        await self._close_all_sessions()
        if self._selection_session is not None:
            await self._selection_session.close()
            self._selection_session = None
        await self._backend.stop()

    # ---- 会话 ----

    async def new_session(self, system: str, model: str, effort: str | None, usage: Usage | None = None) -> Session:
        return await self._backend.create_session(system, model, effort, usage)

    def _direction(self) -> str:
        return self._transcript.direction if self._transcript is not None else lang.JA_ZH

    def _final_direction(self, final) -> str:
        direction = getattr(final, "direction", None)
        if direction in lang.DIRECTIONS:
            return direction
        transcript_dir = self._transcript.direction if self._transcript is not None else None
        if transcript_dir in lang.DIRECTIONS:
            return transcript_dir
        # 双向会议：旧调用方没填 direction 时按来源判断。
        return lang.ZH_JA if getattr(final, "source", "") == "我" else lang.JA_ZH

    def _session_for(self, direction: str) -> Session | None:
        session = self._sessions.get(direction)
        if session is not None:
            return session
        if self._session is not None and direction == self._direction():
            return self._session
        return None

    def _summary_for(self, direction: str) -> str:
        return self._summaries.get(direction, self._summary)

    def _session_started_for(self, direction: str) -> float:
        return self._session_started_by.get(direction, self._session_started)

    def _segment_start_for(self, direction: str) -> float:
        return self._segment_start_wall_by.get(direction, self._segment_start_wall)

    def _system_with_context(self, direction: str | None = None) -> str:
        direction = direction or self._direction()
        if direction == "bi":
            direction = lang.JA_ZH
        parts = [system_prompt(direction, self._correct), glossary.to_prompt(self._terms, direction)]
        summary = self._summary_for(direction)
        if summary:
            parts.append("会议前情摘要：\n" + summary)
        if self._transcript is not None:
            recent = self._transcript.last(self._context_n, direction)
            if recent:
                parts.append(
                    "最近的发言与译文：\n" + "\n".join(f"【{e.source}】{e.ja}\n→ {e.zh}" for e in recent)
                )
        return "\n\n".join(p for p in parts if p)

    async def _start_meeting(
        self, transcript, model, effort, rotate_minutes, context_n, terms, correct=False, directions: list[str] | None = None
    ) -> None:
        self._transcript = transcript
        self._model, self._effort = model, effort
        self._rotate_seconds = rotate_minutes * 60
        self._context_n = context_n
        self._terms = terms
        self._correct = correct
        self._summary = ""
        self._summaries = {}
        if directions:
            dirs = list(dict.fromkeys(directions))
        elif transcript.direction == "bi":
            dirs = [lang.JA_ZH, lang.ZH_JA]
        else:
            dirs = [transcript.direction]
        self._directions = dirs
        self._session_started_by = {}
        self._segment_start_wall_by = {}
        self.usage = Usage(self._on_usage)
        assert self._send_lock is not None

        new_sessions: dict[str, Session] = {}
        try:
            for direction in dirs:
                new = await self.new_session(self._system_with_context(direction), model, effort, self.usage)
                try:
                    await self._prime(new)
                except Exception:
                    await new.close()
                    raise
                new_sessions[direction] = new
        except Exception:
            for s in new_sessions.values():
                await s.close()
            raise

        async with self._send_lock:
            old_sessions = list(
                {id(s): s for s in [*self._sessions.values(), self._session] if s is not None}.values()
            )
            self._sessions = new_sessions
            self._session = new_sessions[dirs[0]]
            now = time.monotonic()
            wall = time.time()
            self._session_started = now
            self._segment_start_wall = wall
            for direction in dirs:
                self._session_started_by[direction] = now
                self._segment_start_wall_by[direction] = wall
        for old in old_sessions:
            await old.close()
        labels = "、".join(lang.get(d).label for d in dirs)
        self._on_status(f"会话已建立（{model}，{labels}）")

    async def _end_meeting(self) -> None:
        assert self._queue is not None
        # 等进行中的轮换/切换模型完成，避免新会话在结束后才挂上而泄漏
        for _ in range(int(REQUEST_TIMEOUT * 4)):
            if not self._rotating:
                break
            await asyncio.sleep(0.5)
        # 等待队列里剩余的句子翻完
        for _ in range(int(REQUEST_TIMEOUT * 2)):
            if self._queue.empty() and self._pending == 0 and not (self._send_lock and self._send_lock.locked()):
                break
            await asyncio.sleep(0.5)
        await self._close_all_sessions()
        self._directions = []
        self._transcript = None

    async def _switch_model(self, model: str, effort: str) -> None:
        if (not self._sessions and self._session is None) or self._transcript is None:
            self._model, self._effort = model, effort
            return
        while self._rotating:
            await asyncio.sleep(0.5)
        self._rotating = True
        dirs = self._directions or [self._direction()]
        for direction in dirs:
            await self._rotate(model, effort, direction)

    async def _benchmark(self, model: str, effort: str, direction: str) -> float:
        s = await self.new_session(system_prompt(direction), model, effort, self.usage if self._transcript else None)
        times: list[float] = []
        try:
            await self._prime(s)
            for text in BENCH_SENTENCES.get(direction, BENCH_SENTENCES[lang.JA_ZH]):
                t0 = time.monotonic()
                await s.send(text, timeout=REQUEST_TIMEOUT)
                times.append(time.monotonic() - t0)
        finally:
            await s.close()
        st = stats.get()
        for t in times:
            st.record_latency(model, t)
        return sum(times) / len(times)

    async def _translate_selected(self, text: str, model: str, effort: str) -> SelectionTranslation:
        if self._selection_lock is None:
            raise RuntimeError("翻译客户端尚未启动")
        async with self._selection_lock:
            result = None
            for _ in range(2):
                session = await self._selection_session_for(model, effort)
                try:
                    reply = await session.send(_selection_request(text), timeout=REQUEST_TIMEOUT)
                except Exception:
                    await self._reset_selection_session()
                    raise
                self._selection_count += 1
                result = _parse_selection_translation(reply)
                if _reading_keeps_source(text, result.hiragana):
                    return result
                log.warning("划词读音未保留日文原文，换新会话重试")
                await self._reset_selection_session()
            # 重试仍不对：宁可显示无注音的原文，也不显示错误内容
            return SelectionTranslation(result.chinese, text.strip())

    async def _prepare_selection(self, model: str, effort: str) -> None:
        if self._selection_lock is None:
            return
        try:
            async with self._selection_lock:
                await self._selection_session_for(model, effort)
        except Exception:
            log.warning("预热划词翻译会话失败", exc_info=True)

    async def _selection_session_for(self, model: str, effort: str) -> Session:
        """复用划词会话；模型/推理强度变化或请求次数达到上限时换新会话，避免上下文无限增长。"""
        key = (model, effort)
        if (
            self._selection_session is not None
            and self._selection_key == key
            and self._selection_count < SELECTION_SESSION_MAX_REQUESTS
        ):
            return self._selection_session
        await self._reset_selection_session()
        session = await self.new_session(SELECTION_SYSTEM_PROMPT, model, effort)
        try:
            await self._prime(session)
        except Exception:
            await session.close()
            raise
        self._selection_session, self._selection_key, self._selection_count = session, key, 0
        return session

    async def _reset_selection_session(self) -> None:
        old, self._selection_session, self._selection_key = self._selection_session, None, None
        if old is not None:
            await old.close()

    async def _rotate(self, model: str | None = None, effort: str | None = None, direction: str | None = None) -> None:
        """后台生成摘要并新建会话（可换模型）；期间旧会话继续翻译。"""
        direction = direction or self._direction()
        if direction == "bi":
            direction = lang.JA_ZH
        target = model or self._model
        target_effort = effort if model else self._effort
        try:
            transcript = self._transcript
            if transcript is None:
                return
            segment = transcript.since(self._segment_start_for(direction), direction)
            summary = self._summary_for(direction)
            if segment:
                text = "\n".join(f"【{e.source}】{e.ja}\n→ {e.zh or ''}" for e in segment)
                prompt = (f"已有前情摘要：\n{summary}\n\n" if summary else "") + "新片段：\n" + text
                s = await self.new_session(segment_summary_prompt(direction), self._model, "low", self.usage)
                reply = await s.send(prompt, timeout=180)
                await s.close()
                summary = reply or summary
                self._summaries[direction] = summary
                self._summary = summary
            new = await self.new_session(self._system_with_context(direction), target, target_effort, self.usage)
            try:
                await self._prime(new)
            except Exception:
                await new.close()
                raise
            assert self._send_lock is not None
            async with self._send_lock:
                old = self._sessions.get(direction)
                self._sessions[direction] = new
                if direction == (self._directions[0] if self._directions else direction):
                    self._session = new
                self._model, self._effort = target, target_effort
                now = time.monotonic()
                self._session_started = now
                self._session_started_by[direction] = now
                self._segment_start_wall = time.time()
                self._segment_start_wall_by[direction] = self._segment_start_wall
            if old is not None:
                await old.close()
            self._on_status(f"已切换到 {target}" if model else "已轮换翻译会话（携带前情摘要）")
        except Exception as e:
            log.exception("轮换会话失败")
            what = f"切换到 {target} 失败" if model else "轮换会话失败"
            self._on_status(f"{what}，继续使用旧会话：{e}")
            self._session_started = time.monotonic()
        finally:
            self._rotating = False

    # ---- 翻译队列 ----

    async def _work(self) -> None:
        assert self._queue is not None and self._send_lock is not None
        while True:
            first = await self._queue.get()
            self._pending += 1
            batch = [first]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
                self._pending += 1

            try:
                groups: dict[str, list] = {}
                for final in batch:
                    groups.setdefault(self._final_direction(final), []).append(final)

                for direction, items in groups.items():
                    session = self._session_for(direction)
                    if session is None:
                        continue
                    if (
                        not self._rotating
                        and self._rotate_seconds > 0
                        and time.monotonic() - self._session_started_for(direction) >= self._rotate_seconds
                    ):
                        self._rotating = True
                        asyncio.create_task(self._rotate(direction=direction))
                    sources: list[str] | None = None
                    try:
                        async with self._send_lock:
                            model = self._model
                            correct = self._correct
                            reply = await session.send(_format_batch(items, correct), timeout=REQUEST_TIMEOUT)
                        parsed = _parse_corrected(reply, len(items)) if correct else None
                        if parsed is not None:
                            sources, texts = parsed
                        else:
                            texts = _parse_batch(reply, len(items))
                        ok = True
                    except Exception as e:
                        log.exception("翻译失败")
                        self._on_status(f"翻译失败：{e}")
                        texts = ["（翻译失败）"] * len(items)
                        ok = False
                    latency = time.monotonic() - min(f.mono_time for f in items)
                    if ok and len(items) == 1:
                        stats.get().record_latency(model, latency)
                    self._on_translation(Translation([f.id for f in items], texts, latency, sources, direction))
            finally:
                self._pending -= len(batch)

