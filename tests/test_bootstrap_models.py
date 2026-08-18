"""Regression coverage for small model assets shipped with the application."""
from __future__ import annotations

import hashlib
from pathlib import Path

from voxsub.bootstrap_models import VAD_SHA256, bundled_vad_path, ensure_bundled_vad


def test_bundled_vad_is_present_and_copies_to_a_fresh_models_directory(tmp_path: Path) -> None:
    source = bundled_vad_path()
    assert source is not None

    installed = ensure_bundled_vad(tmp_path / "models")
    assert installed == tmp_path / "models" / "vad" / "silero_vad_v5.onnx"
    assert installed is not None and installed.is_file()
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == VAD_SHA256


def test_existing_vad_is_not_overwritten(tmp_path: Path) -> None:
    existing = tmp_path / "models" / "vad" / "silero_vad_v5.onnx"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing compatible model")

    assert ensure_bundled_vad(tmp_path / "models") == existing
    assert existing.read_bytes() == b"existing compatible model"
