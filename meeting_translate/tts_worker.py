"""TTS sidecar 进程：stdin/stdout JSON-lines，stdout 只能输出协议消息。

启动后立刻发 ready，不加载模型；第一句 synth 才懒加载，避免 app 启动被 8s
模型加载拖慢。所有异常都转成 error 消息，进程继续服务下一句。
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from .tts_client import PROTOCOL_VERSION

log = logging.getLogger("tts_worker")
_PROTOCOL_OUT = None  # 真正的 stdout；库日志可能往 sys.stdout 打印，启动时把它改到 stderr


def _send(message: dict[str, Any]) -> None:
    out = _PROTOCOL_OUT or sys.stdout
    out.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    out.flush()


def _float32(samples: Any) -> np.ndarray:
    data = np.asarray(samples)
    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        data = data.astype(np.float32) / 2147483648.0
    else:
        data = data.astype(np.float32)
    data = np.nan_to_num(data, nan=0.0, posinf=1.0, neginf=-1.0)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak > 1.0:
        data = data / peak
    return np.ascontiguousarray(data.reshape(-1), dtype=np.float32)


class StubEngine:
    """协议测试用：不依赖任何模型，生成 0.2–1.0s 的 440Hz 音。"""

    sample_rate = 16000

    def synth(self, text: str, voice: str, style: str, speed: float) -> tuple[np.ndarray, int]:
        duration = max(0.2, min(1.0, len(text) * 0.05 / max(0.5, speed)))
        n = int(self.sample_rate * duration)
        t = np.arange(n, dtype=np.float32) / self.sample_rate
        return (np.sin(2 * np.pi * 440 * t).astype(np.float32) * 0.2, self.sample_rate)


class MeloEngine:
    sample_rate = 44100

    def __init__(self, root: Path, threads: int = 4) -> None:
        self.root = root
        self.threads = threads
        self._tts: Any = None

    def _load(self) -> Any:
        if self._tts is not None:
            return self._tts
        import sherpa_onnx

        model = self.root / "model.onnx"
        lexicon = self.root / "lexicon.txt"
        tokens = self.root / "tokens.txt"
        for path in (model, lexicon, tokens):
            if not path.exists():
                raise FileNotFoundError(f"MeloTTS 缺少文件：{path}")
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model),
                    lexicon=str(lexicon),
                    tokens=str(tokens),
                    dict_dir=str(self.root),
                ),
                num_threads=self.threads,
                provider="cpu",
            )
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self.sample_rate = int(self._tts.sample_rate)
        return self._tts

    def synth(self, text: str, voice: str, style: str, speed: float) -> tuple[np.ndarray, int]:
        tts = self._load()
        audio = tts.generate(text, sid=0, speed=max(0.5, min(2.0, speed)))
        return _float32(audio.samples), int(audio.sample_rate)


class SBV2Engine:
    sample_rate = 44100

    def __init__(self, root: Path, device: str = "cpu") -> None:
        self.root = root
        self.device = device
        self._model: Any = None
        self._languages: Any = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.root.exists():
            raise FileNotFoundError(f"Style-Bert-VITS2 目录不存在：{self.root}")
        # style_bert_vits2 默认从当前目录下的 bert/ 找 ONNX BERT；切到 runtime 根目录。
        os.chdir(self.root)
        sys.path.insert(0, str(self.root))
        from style_bert_vits2.constants import Languages
        from style_bert_vits2.tts_model import TTSModel

        model_dir = self.root / "model_assets" / "koharune-ami"
        candidates = [model_dir / "koharune-ami.onnx", model_dir / "koharune-ami.safetensors"]
        model_path = next((p for p in candidates if p.exists()), None)
        if model_path is None:
            raise FileNotFoundError(f"找不到小春音アミ模型：{model_dir}")
        config_path = model_dir / "config.json"
        style_path = model_dir / "style_vectors.npy"
        for path in (config_path, style_path):
            if not path.exists():
                raise FileNotFoundError(f"缺少模型文件：{path}")
        device = self.device
        providers: list[Any]
        if model_path.suffix == ".onnx":
            if device in ("dml", "directml", "auto"):
                providers = [("DmlExecutionProvider", {"device_id": 0}), ("CPUExecutionProvider", {})]
            elif device == "cuda":
                providers = [("CUDAExecutionProvider", {"device_id": 0}), ("CPUExecutionProvider", {})]
            else:
                providers = [("CPUExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"})]
        else:
            providers = [("CPUExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"})]
        self._languages = Languages
        self._model = TTSModel(
            model_path=model_path,
            config_path=config_path,
            style_vec_path=style_path,
            device="cuda" if device == "cuda" else "cpu",
            onnx_providers=providers,
        )
        return self._model

    def synth(self, text: str, voice: str, style: str, speed: float) -> tuple[np.ndarray, int]:
        model = self._load()
        language = getattr(self._languages, "JP", "JP")
        sr, audio = model.infer(
            text=text,
            language=language,
            style=style or "Neutral",
            speaker_id=0,
            length=1.0 / max(0.5, min(2.0, speed)),
            line_split=False,
        )
        return _float32(audio), int(sr)


class EngineRegistry:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._engines: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        if name in self._engines:
            return self._engines[name]
        if name == "stub":
            engine = StubEngine()
        elif name == "melo":
            engine = MeloEngine(_melo_dir(self.args), threads=self.args.threads)
        elif name == "sbv2":
            engine = SBV2Engine(_sbv2_dir(self.args), device=self.args.device)
        else:
            raise ValueError(f"未知 TTS 引擎：{name}")
        self._engines[name] = engine
        return engine


def _sbv2_dir(args: argparse.Namespace) -> Path:
    if args.sbv2_dir:
        return Path(args.sbv2_dir).resolve()
    if args.models_dir:
        return (Path(args.models_dir) / "sbv2").resolve()
    env = os.environ.get("MEETING_TRANSLATE_SBV2_DIR")
    return Path(env).resolve() if env else Path("sbv2").resolve()


def _melo_dir(args: argparse.Namespace) -> Path:
    if args.melo_dir:
        return Path(args.melo_dir).resolve()
    if args.models_dir:
        return (Path(args.models_dir) / "melo" / "vits-melo-tts-zh_en").resolve()
    env = os.environ.get("MEETING_TRANSLATE_MELO_DIR")
    return Path(env).resolve() if env else (Path("melo") / "vits-melo-tts-zh_en").resolve()


def handle(request: dict[str, Any], registry: EngineRegistry) -> dict[str, Any] | None:
    kind = request.get("type")
    if kind == "shutdown":
        return None
    if kind == "ping":
        return {"type": "pong", "id": request.get("id")}
    if kind not in ("synth", "warm"):
        return {"type": "error", "id": request.get("id"), "code": "bad_request", "message": f"未知请求：{kind}"}
    request_id = request.get("id")
    engine_name = str(request.get("engine", "sbv2"))
    if kind == "warm":
        started = time.perf_counter()
        try:
            engine = registry.get(engine_name)
            engine.synth("こんにちは。" if engine_name == "sbv2" else "你好。", voice="koharune_ami", style="Neutral", speed=1.0)
            return {"type": "result", "id": request_id, "ms": round((time.perf_counter() - started) * 1000, 1)}
        except Exception as e:
            log.exception("TTS 预热失败")
            return {"type": "error", "id": request_id, "code": "warm_failed", "message": str(e)}
    text = str(request.get("text", ""))
    if not text.strip():
        return {"type": "error", "id": request_id, "code": "empty_text", "message": "文本为空"}
    started = time.perf_counter()
    try:
        engine = registry.get(engine_name)
        samples, sample_rate = engine.synth(
            text,
            voice=str(request.get("voice", "koharune_ami")),
            style=str(request.get("style", "Neutral")),
            speed=float(request.get("speed", 1.0)),
        )
        return {
            "type": "result",
            "id": request_id,
            "sample_rate": int(sample_rate),
            "samples": base64.b64encode(samples.tobytes()).decode("ascii"),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as e:
        log.exception("TTS 合成失败")
        return {"type": "error", "id": request_id, "code": "synth_failed", "message": str(e)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="meeting-translate TTS sidecar")
    parser.add_argument("--stub", action="store_true", help="使用不依赖模型的测试引擎")
    parser.add_argument("--models-dir", default="", help="TTS 模型根目录")
    parser.add_argument("--sbv2-dir", default="", help="Style-Bert-VITS2 runtime 目录")
    parser.add_argument("--melo-dir", default="", help="MeloTTS 模型目录")
    parser.add_argument("--device", default=os.environ.get("MEETING_TRANSLATE_TTS_DEVICE", "auto"))
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    global _PROTOCOL_OUT
    _PROTOCOL_OUT = sys.stdout
    # Style-Bert-VITS2 / rich 默认往 stdout 打日志，会污染 JSONL 协议；统一改到 stderr。
    sys.stdout = sys.stderr
    registry = EngineRegistry(args)
    engines = ["stub"] if args.stub else ["sbv2", "melo"]
    _send({"type": "ready", "version": PROTOCOL_VERSION, "engines": engines, "device": args.device})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("请求必须是对象")
            response = handle(request, registry)
        except Exception as e:
            log.exception("处理请求失败")
            response = {"type": "error", "id": None, "code": "bad_request", "message": str(e)}
        if response is None:
            break
        try:
            _send(response)
        except (BrokenPipeError, OSError):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
