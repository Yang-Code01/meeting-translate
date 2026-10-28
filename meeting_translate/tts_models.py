"""TTS 模型清单、下载与本地缓存。

官方 HF 的 `litagin/sbv2_koharune_ami` 只放 `.safetensors`，没有 ONNX；本程序
的快速路径用转换后的 `koharune-ami.onnx`。因此 SBV2 ONNX 需要额外的分发地址：

- `MEETING_TRANSLATE_SBV2_ONNX_URL`：直接 URL（可带 checksum）
- `MEETING_TRANSLATE_SBV2_ONNX_PATH`：本地已转换文件，复制进缓存

没有配置时 `ensure_sbv2()` 会明确报错，而不是静默退回 2.4s/句的 CPU PyTorch。
"""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import MODEL_DIR

HF_OFFICIAL = "https://huggingface.co"
HF_MIRROR = "https://hf-mirror.com"
CHUNK = 1024 * 1024
# 镜像限流（429）/ 5xx / 下到一半断线时的重试间隔（秒）；429 带 Retry-After 时以它为准
RETRY_DELAYS = (5, 15, 30, 60, 120)
RETRY_AFTER_MAX = 300
# 官方 HF 只有 .safetensors；这个 ONNX 由项目构建时转换并作为 release 资产托管。
SBV2_ONNX_DEFAULT_URL = (
    "https://github.com/Yang-Code01/meeting-translate/releases/download/"
    "tts-models-v1/koharune-ami.onnx"
)


class ModelError(RuntimeError):
    pass


class MissingFiles(ModelError):
    def __init__(self, paths: Iterable[Path]) -> None:
        self.paths = tuple(paths)
        super().__init__("缺少 TTS 模型文件：" + "、".join(str(p) for p in self.paths))


class ModelSourceError(ModelError):
    pass


class DownloadCancelled(ModelError):
    """用户点了「暂停」；.part 保留，下次从断点续传。"""

    def __init__(self) -> None:
        super().__init__("已暂停")


@dataclass(frozen=True)
class ModelFile:
    repo: str
    remote: str
    dest: str
    size: int
    sha256: str = ""


MELO_FILES = [
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "LICENSE", "LICENSE", 1053, "88a50e5a02bbc2a5c2f084dc19da751aa97b1690f5fda76cd8005c8634d1ca70"),
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "README.md", "README.md", 257, "ae6e44dead53dfdbd5b4a77106226f3118eb910222b46cb5533a3860773fc6c7"),
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "model.onnx", "model.onnx", 170429550, "bf30582eb1b012250a35b1a4a80e7dfbcf8485e7bb9de0d95efbbeef0e4ad86d"),
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "lexicon.txt", "lexicon.txt", 6837671, "7236884b02435ac5d10cf69b4be40a61b45aa676b5300f0e412f185748fee528"),
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "tokens.txt", "tokens.txt", 655, "d18664a7e12bd7ea1022ddaf951e534e136815016c5a809d6b64156bffb4369d"),
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "date.fst", "date.fst", 59154, "eb8aa079ae3cb81d8f4404992f39d61a0cb990947512b5b8d1e54d1f6980e718"),
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "new_heteronym.fst", "new_heteronym.fst", 21974, "ca14b2127e27baa571664e4bb791e143e7425f56a6bc29db08d74f97e6aa4e29"),
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "number.fst", "number.fst", 64482, "743f402181fcfebf76cc2f0546b71fa26476e626fbe4e460fb7b4c3a7a8bd5bd"),
    ModelFile("csukuangfj/vits-melo-tts-zh_en", "phone.fst", "phone.fst", 88630, "1ac2b6fa56b1442320c4de7db08353bab8963a2b57f365eebcdd3a2d3562f8d7"),
]

SBV2_COMMON = [
    ModelFile("litagin/sbv2_koharune_ami", "koharune-ami/config.json", "model_assets/koharune-ami/config.json", 2333, "c997765ea15b4e668895f69adcedd40845dd21cd7ea9c4d4c1f6da53d9a0178e"),
    ModelFile("litagin/sbv2_koharune_ami", "koharune-ami/style_vectors.npy", "model_assets/koharune-ami/style_vectors.npy", 6272, "867527952ff45b6621fe18b8b51728c29abe3aeb80b74e06f235df71aa82ae5d"),
]

SBV2_SAFETENSORS = ModelFile(
    "litagin/sbv2_koharune_ami",
    "koharune-ami/koharune-ami.safetensors",
    "model_assets/koharune-ami/koharune-ami.safetensors",
    251150980,
    "980119e6660fc241b1c297016933f43240b3fc6203a9f511dd2f2dac74042991",
)

SBV2_ONNX = ModelFile(
    "", "", "model_assets/koharune-ami/koharune-ami.onnx", 249417245,
    "42e2d0f9ae1deb881c1536565f05295e64bbd8192bbb56a07cccaf98d27e77c6",
)

BERT_DIR = "bert/deberta-v2-large-japanese-char-wwm-onnx"
BERT_PLAIN_DIR = "bert/deberta-v2-large-japanese-char-wwm"
BERT_FILES = [
    ModelFile("tsukumijima/deberta-v2-large-japanese-char-wwm-onnx", "config.json", f"{BERT_DIR}/config.json", 895, "8f387ab4c6b36e47c7071327c7a42099002781279b00a7e6a7fe88f3da237a3f"),
    ModelFile("tsukumijima/deberta-v2-large-japanese-char-wwm-onnx", "special_tokens_map.json", f"{BERT_DIR}/special_tokens_map.json", 125, "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3"),
    ModelFile("tsukumijima/deberta-v2-large-japanese-char-wwm-onnx", "tokenizer.json", f"{BERT_DIR}/tokenizer.json", 432079, "9e09664b4879bced4b2d00e9d996e95dcd6ca714014bf90aeb3fa9a3f70cac4f"),
    ModelFile("tsukumijima/deberta-v2-large-japanese-char-wwm-onnx", "tokenizer_config.json", f"{BERT_DIR}/tokenizer_config.json", 520, "1cc5203f09ecac12bb7a98a05cb9c2e39a9e37a113a7d85d12542ef29190583b"),
    ModelFile("tsukumijima/deberta-v2-large-japanese-char-wwm-onnx", "vocab.txt", f"{BERT_DIR}/vocab.txt", 88151, "902cbd7e218aaf23a72955533293ceac12fcc4e010ad98c0c14757b94ce7abb6"),
    ModelFile("tsukumijima/deberta-v2-large-japanese-char-wwm-onnx", "model_fp16.onnx", f"{BERT_DIR}/model_fp16.onnx", 653075699, "23f633ae7c5900ff82b35a428b67a54e7e7911d5d6a6dcfc77967be8f1c94dc6"),
]

TOKENIZER_FILES = ("config.json", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json", "vocab.txt")


@dataclass(frozen=True)
class DownloadGroup:
    """一组要一起下载的文件：给首页卡片和「详细」弹窗共用的说明文字。"""

    role: str      # 方向：日→中 / 中→日 / 中→日依赖
    name: str      # 模型名
    source: str    # 来源（HF repo 或 release 资产）
    detail: str    # 关键文件与大小
    size: int
    key: str = ""  # ModelManager.ensure_group 用的键：melo / sbv2 / bert


def human_size(n: int) -> str:
    """字节数 → 「170.4MB」这类文案。"""
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f}GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}MB"
    if n >= 1_000:
        return f"{n / 1e3:.0f}KB"
    return f"{n}B"


def download_groups() -> list[DownloadGroup]:
    """首次安装要下载的东西，按用途分组。文案由清单算出，避免手写的「约 xGB」漂移。"""
    melo = {f.dest: f.size for f in MELO_FILES}
    bert = {f.dest: f.size for f in BERT_FILES}
    bert_model = bert[f"{BERT_DIR}/model_fp16.onnx"]
    return [
        DownloadGroup(
            role="日→中",
            name="MeloTTS（VITS 中文）",
            source="HuggingFace csukuangfj/vits-melo-tts-zh_en",
            detail=(
                f"model.onnx {human_size(melo['model.onnx'])} + lexicon.txt {human_size(melo['lexicon.txt'])}"
                " + tokens.txt 与 date / number / phone / heteronym 四个 fst"
            ),
            size=sum(melo.values()),
            key="melo",
        ),
        DownloadGroup(
            role="中→日",
            name="Style-Bert-VITS2 koharune-ami",
            source="HuggingFace litagin/sbv2_koharune_ami；ONNX 由本项目 release tts-models-v1 托管",
            detail=f"koharune-ami.onnx {human_size(SBV2_ONNX.size)} + config.json、style_vectors.npy",
            size=sum(f.size for f in SBV2_COMMON) + SBV2_ONNX.size,
            key="sbv2",
        ),
        DownloadGroup(
            role="中→日依赖",
            name="DeBERTa-v2-large 日文 BERT（ONNX fp16）",
            source="HuggingFace tsukumijima/deberta-v2-large-japanese-char-wwm-onnx",
            detail=(
                f"model_fp16.onnx {human_size(bert_model)} + tokenizer.json、vocab.txt、config"
                f" 共 {human_size(sum(bert.values()) - bert_model)}"
            ),
            size=sum(bert.values()),
            key="bert",
        ),
    ]


def total_download_bytes() -> int:
    return sum(g.size for g in download_groups())


def total_download_text() -> str:
    """人类可读的总量，如「1.1GB」。"""
    return human_size(total_download_bytes())


def download_summary_lines() -> str:
    """每行一组：方向 模型：大小；末行合计。"""
    lines = [f"{g.role} {g.name}：{human_size(g.size)}" for g in download_groups()]
    lines.append(f"合计约 {total_download_text()}")
    return "\n".join(lines)


def hf_url(repo: str, remote: str, base: str = HF_MIRROR) -> str:
    return f"{base.rstrip('/')}/{repo}/resolve/main/{urllib.parse.quote(remote)}"


def sbv2_onnx_url() -> str | None:
    return os.environ.get("MEETING_TRANSLATE_SBV2_ONNX_URL") or SBV2_ONNX_DEFAULT_URL or None


def sbv2_onnx_path() -> Path | None:
    value = os.environ.get("MEETING_TRANSLATE_SBV2_ONNX_PATH")
    return Path(value) if value else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelManager:
    def __init__(
        self,
        root: Path | None = None,
        mirror: str = HF_MIRROR,
        on_progress: Callable[[str, int, int], None] | None = None,
        cancel: threading.Event | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.root = Path(root) if root else Path(MODEL_DIR) / "tts"
        self.mirror = mirror
        self.on_progress = on_progress
        self.cancel = cancel
        self.on_status = on_status

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise DownloadCancelled()

    def _wait(self, seconds: float) -> None:
        """重试前等待；等待中点暂停立刻生效。"""
        if self.cancel is not None:
            self.cancel.wait(seconds)
        else:
            time.sleep(seconds)
        self._check_cancel()

    @staticmethod
    def _retry_delay(error: Exception, attempt: int, progressed: bool) -> float | None:
        """可重试的错误返回等待秒数，否则 None（换下一个源）。"""
        if attempt >= len(RETRY_DELAYS):
            return None
        if isinstance(error, urllib.error.HTTPError):
            if error.code != 429 and error.code < 500:
                return None
            after = (error.headers or {}).get("Retry-After", "")
            if str(after).strip().isdigit():
                return min(RETRY_AFTER_MAX, max(1, int(after)))
            return RETRY_DELAYS[attempt]
        # 连不上的源（如被墙的 huggingface.co）别反复等超时；下到一半断的才续传重试
        if progressed and isinstance(error, (OSError, http.client.HTTPException)):
            return RETRY_DELAYS[attempt]
        return None

    def engine_root(self, engine: str) -> Path:
        if engine == "melo":
            return self.root / "melo" / "vits-melo-tts-zh_en"
        if engine == "sbv2":
            return self.root / "sbv2"
        raise ValueError(f"未知 TTS 引擎：{engine}")

    def files(self, engine: str) -> list[ModelFile]:
        if engine == "melo":
            return MELO_FILES
        if engine == "sbv2":
            return SBV2_COMMON + BERT_FILES
        raise ValueError(f"未知 TTS 引擎：{engine}")

    def dest(self, engine: str, file: ModelFile) -> Path:
        return self.engine_root(engine) / file.dest

    def missing(self, engine: str, verify_hash: bool = False) -> list[Path]:
        missing = []
        for file in self.files(engine):
            path = self.dest(engine, file)
            if not path.exists() or path.stat().st_size != file.size:
                missing.append(path)
            elif verify_hash and file.sha256 and sha256_file(path) != file.sha256:
                missing.append(path)
        if engine == "sbv2":
            onnx = self.dest("sbv2", SBV2_ONNX)
            if not onnx.exists() or onnx.stat().st_size != SBV2_ONNX.size:
                missing.append(onnx)
        return missing

    def missing_bytes(self, engine: str) -> int:
        """还要下载多少字节（按清单大小算，已下好的文件不计）。"""
        files = self.files(engine) + ([SBV2_ONNX] if engine == "sbv2" else [])
        total = 0
        for file in files:
            path = self.dest(engine, file)
            if not path.exists() or path.stat().st_size != file.size:
                total += file.size
        return total

    # ---- 按下载分组（与 download_groups() 的 key 对应）----

    GROUPS = {
        "melo": ("melo", MELO_FILES),
        "sbv2": ("sbv2", [*SBV2_COMMON, SBV2_ONNX]),
        "bert": ("sbv2", BERT_FILES),
    }

    def group_missing_bytes(self, key: str) -> int:
        engine, files = self.GROUPS[key]
        total = 0
        for file in files:
            path = self.dest(engine, file)
            if not path.exists() or path.stat().st_size != file.size:
                total += file.size
        return total

    def ensure_group(self, key: str) -> None:
        """只下载一组（列表里单行的「下载」）。已下好且校验通过的文件跳过。"""
        engine, files = self.GROUPS[key]
        for file in files:
            self._check_cancel()
            if file is SBV2_ONNX:
                self._ensure_onnx()
                continue
            path = self.dest(engine, file)
            if path.exists() and path.stat().st_size == file.size and (not file.sha256 or sha256_file(path) == file.sha256):
                continue
            self.download_file(engine, file)
        if key == "bert":
            self._materialize_plain_bert()

    def ensure(self, engine: str, download: bool = True) -> Path:
        if engine not in ("melo", "sbv2"):
            raise ValueError(f"未知 TTS 引擎：{engine}")
        if engine == "sbv2" and not sbv2_onnx_url() and not (sbv2_onnx_path() and sbv2_onnx_path().exists()):
            # 先检查是否已经缓存，避免已装好的环境被误报
            onnx = self.dest("sbv2", SBV2_ONNX)
            if not onnx.exists() or onnx.stat().st_size != SBV2_ONNX.size:
                raise ModelSourceError(
                    "缺少转换后的 SBV2 ONNX。默认应从项目 release 的 tts-models-v1 下载；"
                    "也可设置 MEETING_TRANSLATE_SBV2_ONNX_URL 或 "
                    "MEETING_TRANSLATE_SBV2_ONNX_PATH（转换后的 ONNX）。"
                )
        missing = self.missing(engine, verify_hash=True)
        if not missing:
            self._materialize_plain_bert()
            return self.engine_root(engine)
        if not download:
            raise MissingFiles(missing)
        for file in self.files(engine):
            path = self.dest(engine, file)
            if path.exists() and path.stat().st_size == file.size and (not file.sha256 or sha256_file(path) == file.sha256):
                continue
            self.download_file(engine, file)
        if engine == "sbv2":
            self._ensure_onnx()
            self._materialize_plain_bert()
        return self.engine_root(engine)

    def _ensure_onnx(self) -> None:
        dest = self.dest("sbv2", SBV2_ONNX)
        if dest.exists() and dest.stat().st_size == SBV2_ONNX.size:
            return
        local = sbv2_onnx_path()
        if local and local.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, dest)
            if dest.stat().st_size != SBV2_ONNX.size:
                raise ModelError(f"ONNX 文件大小不符：{dest}")
            return
        url = sbv2_onnx_url()
        if not url:
            raise ModelSourceError("没有可用的 SBV2 ONNX 分发地址")
        self.download_file("sbv2", SBV2_ONNX, sources=[url])

    def _materialize_plain_bert(self) -> None:
        """g2p 的 tokenizer 从非 -onnx 目录读；复用 ONNX 目录的 tokenizer 文件。"""
        plain = self.root / "sbv2" / BERT_PLAIN_DIR
        source = self.root / "sbv2" / BERT_DIR
        plain.mkdir(parents=True, exist_ok=True)
        for name in TOKENIZER_FILES:
            src, dst = source / name, plain / name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)

    def download_file(self, engine: str, file: ModelFile, sources: list[str] | None = None) -> Path:
        dest = self.dest(engine, file)
        dest.parent.mkdir(parents=True, exist_ok=True)
        urls = sources or ([hf_url(file.repo, file.remote, self.mirror), hf_url(file.repo, file.remote, HF_OFFICIAL)] if file.repo else [])
        if not urls:
            raise ModelSourceError(f"没有下载地址：{dest.name}")
        errors: list[str] = []
        part = dest.with_suffix(dest.suffix + ".part")
        for url in urls:
            host = urllib.parse.urlsplit(url).netloc or url
            attempt = 0
            while True:
                before = part.stat().st_size if part.exists() else 0
                try:
                    self._download_one(url, dest, file)
                    return dest
                except DownloadCancelled:
                    raise
                except Exception as e:
                    after = part.stat().st_size if part.exists() else 0
                    wait = self._retry_delay(e, attempt, after > before)
                    if wait is None:
                        errors.append(f"{host}：{e}")
                        break
                    attempt += 1
                    if self.on_status:
                        reason = "服务器限流（429）" if getattr(e, "code", None) == 429 else f"{e}"
                        self.on_status(
                            f"{dest.name}：{host} {reason}，{int(wait)} 秒后第 {attempt}/{len(RETRY_DELAYS)} 次重试"
                        )
                    self._wait(wait)
        raise ModelError(f"{dest.name} 下载失败（" + "；".join(errors) + "）")

    def _download_one(self, url: str, dest: Path, file: ModelFile) -> None:
        part = dest.with_suffix(dest.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if offset > file.size:
            part.unlink()
            offset = 0
        if offset and offset == file.size:
            # 上次已下完但没通过校验（或在 replace 前中断）：再发 Range 只会 416，直接校验
            self._verify_part(part, dest, file)
            return
        # hf-mirror 对 Python 默认的 Python-urllib UA 直接回 403
        headers = {"User-Agent": "meeting-translate"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            status = getattr(response, "status", 200)
            if offset and status != 206:
                offset = 0
                part.unlink(missing_ok=True)
            total = int(response.headers.get("Content-Length", "0") or 0) + offset
            mode = "ab" if offset else "wb"
            done = offset
            with part.open(mode) as out:
                while True:
                    self._check_cancel()
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if self.on_progress:
                        self.on_progress(dest.name, done, max(total, done))
        self._verify_part(part, dest, file)

    def _verify_part(self, part: Path, dest: Path, file: ModelFile) -> None:
        size = part.stat().st_size
        if size != file.size:
            if size > file.size:
                part.unlink(missing_ok=True)  # 超长的续传不了，下次从头下
            raise ModelError(f"文件大小不符：{part}（{size} != {file.size}）")
        if file.sha256 and sha256_file(part) != file.sha256:
            part.unlink(missing_ok=True)
            raise ModelError(f"SHA256 校验失败：{part.name}")
        part.replace(dest)
