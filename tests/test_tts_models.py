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


def test_missing_and_ensure_without_download(tmp_path) -> None:
    manager = ModelManager(root=tmp_path / "models")
    assert manager.missing("melo")
    with pytest.raises(MissingFiles):
        manager.ensure("melo", download=False)


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
