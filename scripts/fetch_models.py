"""下载内置模型到 models/（构建 exe 前执行一次）。

用法: uv run python scripts/fetch_models.py
可用环境变量 MT_HF_BASE 覆盖 HF 镜像（默认 https://hf-mirror.com）。
"""

import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HF = os.environ.get("MT_HF_BASE", "https://hf-mirror.com")
REAZON = f"{HF}/reazon-research/reazonspeech-k2-v2/resolve/main"
SENSEVOICE = f"{HF}/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
FILES = {
    "reazonspeech-k2-v2/encoder-epoch-99-avg-1.int8.onnx": f"{REAZON}/encoder-epoch-99-avg-1.int8.onnx",
    "reazonspeech-k2-v2/decoder-epoch-99-avg-1.int8.onnx": f"{REAZON}/decoder-epoch-99-avg-1.int8.onnx",
    "reazonspeech-k2-v2/joiner-epoch-99-avg-1.int8.onnx": f"{REAZON}/joiner-epoch-99-avg-1.int8.onnx",
    "reazonspeech-k2-v2/tokens.txt": f"{REAZON}/tokens.txt",
    "sensevoice/model.int8.onnx": f"{SENSEVOICE}/model.int8.onnx",
    "sensevoice/tokens.txt": f"{SENSEVOICE}/tokens.txt",
    "sensevoice/LICENSE": f"{SENSEVOICE}/LICENSE",
    "silero_vad.onnx": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
}


def main() -> None:
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "meeting-translate")]  # hf-mirror 对默认 UA 的重定向返回 403
    urllib.request.install_opener(opener)
    for rel, url in FILES.items():
        dst = ROOT / "models" / rel
        if dst.exists() and dst.stat().st_size > 0:
            print("已存在", rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        print("下载", rel, "…", flush=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dst)
    print("完成")


if __name__ == "__main__":
    main()
