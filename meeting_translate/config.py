"""路径、用户设置、内置资源定位。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "meeting-translate"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
TRANSCRIPT_DIR = APP_DIR / "transcripts"
MODEL_DIR = APP_DIR / "models"
GLOSSARY_FILE = APP_DIR / "glossary.txt"
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "app.log"
STATS_FILE = APP_DIR / "model_stats.json"
SELECTION_HISTORY_FILE = APP_DIR / "selection_translation_history.md"

HF_BASE = os.environ.get("MT_HF_BASE", "https://hf-mirror.com")

REAZON_DIR_NAME = "reazonspeech-k2-v2"
SILERO_VAD_NAME = "silero_vad.onnx"
WHISPER_REPO = "csukuangfj/sherpa-onnx-whisper-{size}"
SENSEVOICE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"


def resource_dir() -> Path:
    """内置资源根目录：PyInstaller 解包目录或源码仓库根目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent.parent


def use_bundled_runtime() -> None:
    """打包版内置了 Copilot 运行时，指给 SDK 以免首启动从 GitHub 下载。"""
    exe = resource_dir() / "copilot_runtime" / "copilot-runtime.exe"
    if exe.is_file() and not os.environ.get("COPILOT_CLI_PATH"):
        os.environ["COPILOT_CLI_PATH"] = str(exe)


def find_model(name: str) -> Path | None:
    """先找内置模型，再找用户下载目录。"""
    for root in (resource_dir() / "models", MODEL_DIR):
        p = root / name
        if p.exists():
            return p
    return None


DEFAULT_FILLERS = ["え", "あ", "えー", "えーと", "あの", "うん", "ん"]
DEFAULT_FILLERS_ZH = ["嗯", "啊", "呃", "哦", "额", "那个", "就是"]
SETTINGS_VERSION = 5


@dataclass
class Settings:
    version: int = SETTINGS_VERSION
    model: str = "claude-haiku-5.5"
    reasoning_effort: str = "none"
    direction: str = "ja-zh"  # ja-zh | zh-ja；中→日固定使用 SenseVoice 识别中文
    asr_engine: str = "sensevoice"  # 日→中的识别引擎：reazon | sensevoice | whisper-small | whisper-medium
    asr_threads: int = 4
    loopback_device: str | None = None  # None = 默认扬声器
    mic_device: str | None = None  # None = 默认麦克风
    mic_enabled: bool = False  # 新安装默认关闭，避免旁人说话被记为「我」
    hotkey: str = "Ctrl+Alt+J"
    selection_hotkey: str = "Alt+M"
    selection_font_family: str = "Microsoft YaHei UI"  # 划词/句翻译弹窗字体
    selection_chinese_px: int = 10  # 划词/句翻译弹窗：中文译文字号（像素）
    selection_kana_px: int = 10  # 划词/句翻译弹窗：假名字号（像素）
    selection_kanji_px: int = 10  # 划词/句翻译弹窗：汉字字号（像素）
    selection_chinese_color: str = "#eeeeee"
    selection_kana_color: str = "#ffe082"
    selection_kanji_color: str = "#dddddd"
    selection_show_kanji: bool = True  # False = 只显示假名读音
    selection_popup_size: list[int] = field(default_factory=list)  # 用户手动调整后的弹窗 [宽, 高]；空 = 按内容自动
    session_rotate_minutes: int = 30
    max_segment_seconds: float = 10.0
    silence_seconds: float = 0.6  # VAD 判定一句结束的静音时长
    hold_seconds: float = 3.0  # 未说完的句子最多等后续多久
    hold_max_seconds: float = 20.0  # 合并后累计超过此时长就送翻译
    fillers: list[str] = field(default_factory=lambda: list(DEFAULT_FILLERS))
    fillers_zh: list[str] = field(default_factory=lambda: list(DEFAULT_FILLERS_ZH))
    context_sentences: int = 5
    asr_correct: bool = True  # 翻译时让 LLM 一并校正识别原文
    font_family: str = "Microsoft YaHei UI"
    # 悬浮框原文（识别出的那句话）与译文分开设置字号/字色，单位 pt
    source_font_size: int = 12
    source_font_color: str = "#ffffff"  # 原文颜色；未完成的识别片段也用它
    font_color: str = "#ffffff"  # 悬浮框字幕（译文）文字颜色
    font_size: int = 16  # 悬浮框字幕（译文）字号
    opacity: float = 0.85
    show_overlay: bool = True  # 是否显示置顶悬浮字幕窗（关闭后只留托盘和快捷键）
    subtitle_mode: bool = False  # 字幕模式：悬浮窗只显示译文，不显示原文和未翻译片段
    window_geometry: list[int] = field(default_factory=lambda: [200, 600, 900, 260])
    show_quota_remaining: bool = False  # 悬浮窗显示「账户剩余 %」（低于阈值时仍强制显示）
    show_quota_used: bool = True  # 悬浮窗显示「账户已用 AIC」
    show_meeting_aic: bool = True  # 悬浮窗显示「本场 AIC」
    save_dir: str | None = None  # 会议记录保存目录；None = 尚未设定，结束会议时询问
    provider: str = "copilot"  # copilot | openai（自定义端点）
    active_profile: str = ""  # 自定义端点里当前选中的那条；空 = 仍用 Copilot
    # 每条自定义端点：name / base_url / model / api_key（dpapi 密文）/
    # input_price / output_price（元每百万 token）/ verify_tls
    profiles: list[dict] = field(default_factory=list)
    # 双向同传（P0/P1 先只落配置，UI 与音频路由在后续阶段接入）
    meeting_mode: str = "captions"  # captions | duplex
    duplex_zh_ja: bool = True  # 中转日：我的中文 -> 日语 TTS 给对方；关 = 麦克风原声直通
    duplex_ja_zh: bool = True  # 日转中：对方日语 -> 中文 TTS 给我；关 = 对方原声直通
    duplex_mic_device: str | None = None
    duplex_capture_device: str | None = None  # CABLE-A Input（扬声器名，App 环回采集对方）
    duplex_remote_speaker: str | None = None  # CABLE-B Input（对方听到的日语 TTS）
    duplex_local_speaker: str | None = None  # 耳机（我听到的中文 TTS）
    duplex_tts_enabled: bool = True
    duplex_tts_voice: str = "koharune_ami"
    duplex_tts_style: str = "Neutral"
    duplex_tts_speed: float = 1.0
    duplex_tts_volume: float = 0.8
    duplex_queue_max: int = 2
    duplex_context_turns: int = 3
    duplex_latency_target: float = 3.0

    def profile(self, name: str | None = None) -> dict | None:
        target = name or self.active_profile
        if not target:
            return None
        for p in self.profiles:
            if p.get("name") == target:
                return p
        return None

    def effective_engine(self) -> str:
        if self.meeting_mode == "duplex":
            return "sensevoice"
        return "sensevoice" if self.direction == "zh-ja" else self.asr_engine

    def asr_language(self) -> str:
        if self.meeting_mode == "duplex":
            return "auto"
        return "zh" if self.direction == "zh-ja" else "ja"

    def active_fillers(self) -> list[str]:
        if self.meeting_mode == "duplex":
            return self.fillers + self.fillers_zh
        return self.fillers_zh if self.direction == "zh-ja" else self.fillers

    @classmethod
    def load(cls) -> "Settings":
        ensure_dirs()
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                # 每步只把版本推进到自己那一步（写成 SETTINGS_VERSION 会让后面的步骤整段跳过，
                # 比如 v2 配置升到 v5 时，v4 的 direction="bi" 升级和 v5 的字号拆分都不会执行）。
                if data.get("version", 1) < 2:
                    data.pop("max_segment_seconds", None)  # v1 的 6 秒强制切分改为 10 秒
                    data["version"] = 2
                if data.get("version", 1) < 3:
                    data.setdefault("provider", "copilot")  # v2 之前只有 Copilot
                    data.setdefault("active_profile", "")
                    data.setdefault("profiles", [])
                    data["version"] = 3
                if data.get("version", 1) < 4:
                    # v4 新增双向同传字段；旧的 direction="bi" 自动升级为 duplex。
                    if data.get("direction") == "bi":
                        data.setdefault("meeting_mode", "duplex")
                    data["version"] = 4
                if data.get("version", 1) < 5:
                    # v5 起悬浮框原文/译文分开设置字号字色：旧的字号字色视为译文，
                    # 原文沿用原来的 0.75 倍字号与同色，视觉上不变。
                    size = data.get("font_size", 16)
                    color = data.get("font_color", "#ffffff")
                    data.setdefault("source_font_size", max(9, round(size * 0.75)))
                    data.setdefault("source_font_color", color)
                    data["version"] = 5
                data["version"] = SETTINGS_VERSION
                s = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
                if s.meeting_mode not in ("captions", "duplex"):
                    s.meeting_mode = "captions"
                if s.asr_engine == "funasr-nano":  # 已移除的引擎，回落到默认
                    s.asr_engine = "sensevoice"
                s.save()
                return s
            except (json.JSONDecodeError, TypeError):
                pass
        s = cls()
        s.save()
        return s

    def save(self) -> None:
        ensure_dirs()
        CONFIG_FILE.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_dirs() -> None:
    for d in (APP_DIR, TRANSCRIPT_DIR, MODEL_DIR):
        d.mkdir(parents=True, exist_ok=True)
