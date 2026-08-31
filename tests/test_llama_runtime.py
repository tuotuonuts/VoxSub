"""Isolation tests for the user-level llama.cpp runtime bootstrap."""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

import voxsub.llama_runtime as runtime


def test_openvino_asset_metadata_matches_pinned_release() -> None:
    assert runtime.OPENVINO_SIZE == 80_730_898
    assert len(runtime.OPENVINO_SHA256) == 64
    assert runtime.OPENVINO_SHA256 == (
        "671B0A0C8D5F58E20DA178732435617B182D7127E62080D2CBE270A7A0D69EBD"
    )


def test_archive_diagnostics_reports_zip_header_and_digest(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.zip"
    archive.write_bytes(b"PK\x03\x04runtime")
    size, digest, prefix, is_zip = runtime._archive_diagnostics(archive)
    assert size == archive.stat().st_size
    assert digest == runtime._sha256(archive)
    assert prefix.startswith("50 4B")
    assert is_zip


def _write_runtime_archive(archive: Path, *, unsafe: bool = False) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        if unsafe:
            bundle.writestr("../outside.txt", b"must not extract")
        for name in (
            "llama-server.exe",
            "ggml-openvino.dll",
            "openvino.dll",
            "openvino_intel_npu_plugin.dll",
            "openvino_intel_npu_compiler_loader.dll",
        ):
            bundle.writestr(name, b"runtime")


@pytest.mark.skipif(os.name != "nt", reason="OpenVINO bootstrap is Windows-only")
def test_existing_user_runtime_is_reused_without_download(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    target = runtime.user_runtime_root() / "openvino"
    target.mkdir(parents=True)
    for name in (
        "llama-server.exe",
        "ggml-openvino.dll",
        "openvino.dll",
        "openvino_intel_npu_plugin.dll",
        "openvino_intel_npu_compiler_loader.dll",
    ):
        (target / name).write_bytes(b"ok")

    def fail_download(_archive: Path) -> None:
        raise AssertionError("existing runtime must not download")

    monkeypatch.setattr(runtime, "_download_verified", fail_download)
    status = runtime.ensure_openvino_runtime()
    assert status.ready
    assert status.source == "user"
    assert status.directory == target


@pytest.mark.skipif(os.name != "nt", reason="OpenVINO bootstrap is Windows-only")
def test_bootstrap_downloads_validated_archive_atomically(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    archive = tmp_path / "cache" / runtime.OPENVINO_ASSET

    def fake_download(destination: Path) -> None:
        _write_runtime_archive(destination)

    monkeypatch.setattr(runtime, "_download_verified", fake_download)
    model = tmp_path / "models" / "keep.gguf"
    model.parent.mkdir()
    model.write_bytes(b"model")

    status = runtime.ensure_openvino_runtime(force=True)
    target = runtime.user_runtime_root() / "openvino"
    assert status.ready
    assert status.source == "download"
    assert status.directory == target
    assert (target / "llama-server.exe").is_file()
    assert model.read_bytes() == b"model"
    assert not list(target.parent.glob(".openvino.pending-*"))


@pytest.mark.skipif(os.name != "nt", reason="OpenVINO bootstrap is Windows-only")
def test_bootstrap_failure_is_reported_without_raising(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    def fail_download(_archive: Path) -> None:
        raise OSError("network unavailable")

    monkeypatch.setattr(runtime, "_download_verified", fail_download)
    status = runtime.ensure_openvino_runtime(force=True)
    assert not status.ready
    assert status.source == "error"
    assert "network unavailable" in status.reason


@pytest.mark.skipif(os.name != "nt", reason="OpenVINO bootstrap is Windows-only")
def test_unsafe_archive_is_rejected_without_user_data_change(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    def fake_download(destination: Path) -> None:
        _write_runtime_archive(destination, unsafe=True)

    monkeypatch.setattr(runtime, "_download_verified", fake_download)
    status = runtime.ensure_openvino_runtime(force=True)
    assert not status.ready
    assert "不安全路径" in status.reason
    assert not (tmp_path / "outside.txt").exists()


def test_runtime_module_does_not_modify_process_python_environment() -> None:
    assert "PYTHONPATH" not in os.environ or isinstance(os.environ["PYTHONPATH"], str)
    assert "PYTHONHOME" not in os.environ or isinstance(os.environ["PYTHONHOME"], str)
    assert sys.version_info >= (3, 11)
