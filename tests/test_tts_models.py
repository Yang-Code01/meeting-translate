from __future__ import annotations

import hashlib

import pytest

from meeting_translate import tts_models
from meeting_translate.tts_models import (
    MissingFiles,
    ModelFile,
    ModelManager,
    ModelSourceError,
    hf_url,
)


def test_hf_url_keeps_path_components() -> None:
    url = hf_url("litagin/sbv2_koharune_ami", "koharune-ami/config.json", "https://hf-mirror.com")
    assert url == "https://hf-mirror.com/litagin/sbv2_koharune_ami/resolve/main/koharune-ami/config.json"


def test_download_file_from_file_url(tmp_path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    digest = hashlib.sha256(b"hello").hexdigest()
    manager = ModelManager(root=tmp_path / "models")
    file = ModelFile("", "", "x.bin", 5, digest)
    seen: list[tuple[str, int, int]] = []
    manager.on_progress = lambda name, done, total: seen.append((name, done, total))
    dest = manager.download_file("melo", file, sources=[source.as_uri()])
    assert dest.read_bytes() == b"hello"
    assert seen and seen[-1][0] == "x.bin"
    assert seen[-1][1] == 5


def test_download_sends_user_agent(tmp_path, monkeypatch) -> None:
    """hf-mirror 对默认的 Python-urllib UA 回 403。"""
    seen: list[str] = []

    def fake_urlopen(request, timeout=None):
        seen.append(request.get_header("User-agent"))
        raise OSError("stop")

    monkeypatch.setattr(tts_models.urllib.request, "urlopen", fake_urlopen)
    manager = ModelManager(root=tmp_path / "models")
    with pytest.raises(tts_models.ModelError, match="hf-mirror.com"):
        manager.download_file("melo", tts_models.MELO_FILES[0])
    assert seen and all(ua and "Python-urllib" not in ua for ua in seen)


def test_cancel_keeps_part_file_and_skips_other_sources(tmp_path, monkeypatch) -> None:
    import io
    import threading

    file = tts_models.MELO_FILES[0]
    calls: list[str] = []

    class _Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(file.size)}

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        return _Response(b"x" * file.size)

    monkeypatch.setattr(tts_models.urllib.request, "urlopen", fake_urlopen)
    cancel = threading.Event()
    manager = ModelManager(root=tmp_path / "models", on_progress=lambda *_a: cancel.set(), cancel=cancel)
    with pytest.raises(tts_models.DownloadCancelled):
        manager.download_file("melo", file)
    assert len(calls) == 1  # 暂停不是失败，不该换下一个源重试
    dest = manager.dest("melo", file)
    part = dest.parent / (dest.name + ".part")
    assert part.stat().st_size == file.size  # 已下的部分留着，继续时续传


def test_complete_part_is_verified_without_range_request(tmp_path, monkeypatch) -> None:
    """.part 已经是完整大小（上次只差校验）：不能再发 Range（服务器回 416），直接校验后落盘。"""
    monkeypatch.setattr(tts_models.urllib.request, "urlopen", lambda *a, **k: pytest.fail("不该再请求"))
    manager = ModelManager(root=tmp_path / "models")
    file = ModelFile("r", "a.txt", "a.txt", 5, hashlib.sha256(b"hello").hexdigest())
    dest = manager.dest("melo", file)
    dest.parent.mkdir(parents=True)
    (dest.parent / "a.txt.part").write_bytes(b"hello")
    assert manager.download_file("melo", file).read_bytes() == b"hello"


def test_oversized_part_restarts_download(tmp_path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    manager = ModelManager(root=tmp_path / "models")
    file = ModelFile("", "", "x.bin", 5, hashlib.sha256(b"hello").hexdigest())
    dest = manager.dest("melo", file)
    dest.parent.mkdir(parents=True)
    (dest.parent / "x.bin.part").write_bytes(b"too long data")
    assert manager.download_file("melo", file, sources=[source.as_uri()]).read_bytes() == b"hello"


def test_missing_and_ensure_without_download(tmp_path) -> None:
    manager = ModelManager(root=tmp_path / "models")
    assert manager.missing("melo")
    with pytest.raises(MissingFiles):
        manager.ensure("melo", download=False)


def test_missing_bytes_skips_files_already_downloaded(tmp_path) -> None:
    manager = ModelManager(root=tmp_path / "models")
    full = manager.missing_bytes("melo")
    assert full == sum(f.size for f in tts_models.MELO_FILES)
    done = tts_models.MELO_FILES[0]
    path = manager.dest("melo", done)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * done.size)
    assert manager.missing_bytes("melo") == full - done.size


def test_ensure_group_downloads_only_that_group(tmp_path, monkeypatch) -> None:
    manager = ModelManager(root=tmp_path / "models")
    fetched: list[str] = []

    def fake_download(engine, file, sources=None):
        fetched.append(file.dest)
        path = manager.dest(engine, file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    monkeypatch.setattr(manager, "download_file", fake_download)
    manager.ensure_group("bert")
    assert fetched == [f.dest for f in tts_models.BERT_FILES]
    assert manager.group_missing_bytes("melo") == sum(f.size for f in tts_models.MELO_FILES)
    # tokenizer 文件要同步到非 -onnx 目录，g2p 从那里读
    assert (manager.root / "sbv2" / tts_models.BERT_PLAIN_DIR / "vocab.txt").exists()


def test_sbv2_requires_onnx_source(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MEETING_TRANSLATE_SBV2_ONNX_URL", raising=False)
    monkeypatch.delenv("MEETING_TRANSLATE_SBV2_ONNX_PATH", raising=False)
    monkeypatch.setattr(tts_models, "SBV2_ONNX_DEFAULT_URL", "")
    manager = ModelManager(root=tmp_path / "models")
    with pytest.raises(ModelSourceError, match="MEETING_TRANSLATE_SBV2_ONNX"):
        manager.ensure("sbv2", download=False)


def test_checksum_failure_removes_part(tmp_path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"hello")
    manager = ModelManager(root=tmp_path / "models")
    file = ModelFile("", "", "x.bin", 5, "0" * 64)
    with pytest.raises(Exception, match="SHA256"):
        manager.download_file("melo", file, sources=[source.as_uri()])


def _http_error(code: int, retry_after: str = ""):
    import email.message
    import urllib.error

    headers = email.message.Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://hf-mirror.com/x", code, "err", headers, None)


def test_rate_limited_download_waits_and_retries(tmp_path, monkeypatch) -> None:
    import io

    file = ModelFile("r", "a.txt", "a.txt", 5, hashlib.sha256(b"hello").hexdigest())
    calls: list[str] = []

    class _Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": "5"}

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise _http_error(429, "7")
        return _Response(b"hello")

    monkeypatch.setattr(tts_models.urllib.request, "urlopen", fake_urlopen)
    waits: list[float] = []
    status: list[str] = []
    manager = ModelManager(root=tmp_path / "models", on_status=status.append)
    monkeypatch.setattr(manager, "_wait", waits.append)
    assert manager.download_file("melo", file).read_bytes() == b"hello"
    assert waits == [7]  # 按 Retry-After 等
    assert len(calls) == 2 and "hf-mirror.com" in calls[1]  # 同一个镜像重试，没跳去 huggingface.co
    assert "429" in status[0]


def test_unreachable_source_is_not_retried(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        raise TimeoutError("timed out")

    monkeypatch.setattr(tts_models.urllib.request, "urlopen", fake_urlopen)
    manager = ModelManager(root=tmp_path / "models")
    monkeypatch.setattr(manager, "_wait", lambda s: pytest.fail("连不上不该等待重试"))
    with pytest.raises(tts_models.ModelError):
        manager.download_file("melo", tts_models.MELO_FILES[0])
    assert len(calls) == 2  # 镜像、官方各一次


def test_pause_during_retry_wait(tmp_path, monkeypatch) -> None:
    import threading

    def fake_urlopen(request, timeout=None):
        raise _http_error(429)

    monkeypatch.setattr(tts_models.urllib.request, "urlopen", fake_urlopen)
    cancel = threading.Event()
    manager = ModelManager(root=tmp_path / "models", cancel=cancel, on_status=lambda _t: cancel.set())
    with pytest.raises(tts_models.DownloadCancelled):
        manager.download_file("melo", tts_models.MELO_FILES[0])
