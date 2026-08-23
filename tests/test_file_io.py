"""Crash-safety tests for shared atomic file operations."""
from __future__ import annotations

from pathlib import Path

import pytest

from voxsub import file_io


def test_atomic_binary_copy_preserves_old_file_when_copy_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "new.bin"
    destination = tmp_path / "model.bin"
    source.write_bytes(b"new model bytes")
    destination.write_bytes(b"known good model")

    def fail_mid_copy(source_handle, destination_handle) -> None:
        destination_handle.write(source_handle.read(3))
        raise OSError("simulated interruption")

    monkeypatch.setattr(file_io.shutil, "copyfileobj", fail_mid_copy)

    with pytest.raises(OSError, match="simulated interruption"):
        file_io.copy_file_atomically(source, destination)

    assert destination.read_bytes() == b"known good model"
    assert not list(tmp_path.glob(".model.bin.*.part"))
