"""Hardware-aware curated model marketplace tests."""
from __future__ import annotations

import io
import os
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from voxsub.model_catalog import (
    CATALOG,
    HardwareProfile,
    ModelMarketplace,
    ModelSource,
    RemoteFile,
    assess_model,
    get_model,
    models_for_task,
)


def test_catalog_is_quality_sorted_and_has_no_old_qwen_translation() -> None:
    for task in ("asr", "translate"):
        scores = [model.quality_score for model in models_for_task(task)]
        assert scores == sorted(scores, reverse=True)
    ids = {model.id for model in CATALOG}
    assert "mt-hy-mt2-1.8b-q4" in ids
    assert "mt-hy-mt2-7b-q4" in ids
    assert not any("qwen2.5" in model.id for model in CATALOG)


def test_recommendation_levels_cover_requested_semantics() -> None:
    hy_small = get_model("mt-hy-mt2-1.8b-q4")
    hy_large = get_model("mt-hy-mt2-7b-q4")
    assert hy_small and hy_large

    low = HardwareProfile("old cpu", 2, 4, 4.0)
    assert assess_model(hy_large, low).level == "不推荐"

    mainstream = HardwareProfile("8 core", 8, 16, 32.0, "RTX 4060", 8.0, "CUDA")
    assert assess_model(hy_large, mainstream).level == "满载"
    assert assess_model(hy_small, mainstream).level == "推荐"

    workstation = HardwareProfile("24 core", 24, 32, 64.0, "RTX 4090", 24.0, "CUDA")
    # Small model is easy to run, but this configuration can run a better one.
    assert assess_model(hy_small, workstation).level == "较为推荐"
    opus = get_model("mt-opus-fast-builtin")
    assert opus is not None
    assert assess_model(opus, workstation).level == "不推荐"

    npu_laptop = HardwareProfile(
        "Core Ultra", 4, 8, 16.0,
        npu_name="Intel AI Boost", npu_provider="OpenVINO",
        integrated_gpu_name="Intel Arc Graphics", integrated_gpu_provider="DirectML",
    )
    npu_assessment = assess_model(hy_small, npu_laptop)
    assert npu_assessment.level in {"推荐", "较为推荐"}
    assert npu_assessment.reason.startswith("NPU ")


def _make_archive(path: Path, root_name: str, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:bz2") as tf:
        for rel, payload in files.items():
            info = tarfile.TarInfo(f"{root_name}/{rel}")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))


def test_install_archive_verify_and_uninstall(tmp_path: Path, monkeypatch) -> None:
    base = get_model("asr-qwen3-0.6b-int8")
    assert base is not None
    source = ModelSource("global", "test", "https://example.invalid/model.tar.bz2",
                         "https://example.invalid")
    model = replace(base, sources=(source,), required_paths=("a.onnx", "tokenizer/vocab.json"),
                    install_rel="marketplace/test-model", asset_name="test.tar.bz2",
                    sha256="")

    def fake_fetch(_url, dest, **kwargs):
        _make_archive(Path(dest), "upstream-name", {
            "a.onnx": b"model", "tokenizer/vocab.json": b"{}"})
        callback = kwargs.get("progress")
        if callback:
            callback(10, 10, source.url)
        return True

    import voxsub.model_catalog as catalog

    monkeypatch.setattr(catalog, "fetch_file", fake_fetch)
    market = ModelMarketplace(tmp_path / "models")
    target = market.install(model, "global")
    assert target == tmp_path / "models" / "marketplace" / "test-model"
    assert market.is_installed(model)
    assert (target / "a.onnx").read_bytes() == b"model"

    market.uninstall(model)
    assert not target.exists()


def test_uninstall_refuses_builtin_and_selected(tmp_path: Path) -> None:
    market = ModelMarketplace(tmp_path)
    builtin = get_model("mt-opus-fast-builtin")
    regular = get_model("mt-hy-mt2-1.8b-q4")
    assert builtin and regular
    with pytest.raises(RuntimeError, match="内置"):
        market.uninstall(builtin)
    with pytest.raises(RuntimeError, match="正在使用"):
        market.uninstall(regular, in_use=True)


def test_install_multi_file_source_and_remove_partial_staging(
        tmp_path: Path, monkeypatch) -> None:
    base = get_model("asr-qwen3-0.6b-int8")
    assert base is not None
    files = (
        RemoteFile("https://example.invalid/encoder", "encoder.int8.onnx", 7),
        RemoteFile("https://example.invalid/vocab", "tokenizer/vocab.json", 2),
    )
    source = ModelSource("china", "test multi-file", "https://example.invalid",
                         "https://example.invalid", files=files)
    model = replace(base, sources=(source,),
                    required_paths=("encoder.int8.onnx", "tokenizer/vocab.json"),
                    install_rel="marketplace/test-multi", archive=False)

    def fake_fetch(url, dest, **kwargs):
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = b"encoder" if url.endswith("encoder") else b"{}"
        destination.write_bytes(payload)
        callback = kwargs.get("progress")
        if callback:
            callback(len(payload), len(payload), url)
        return True

    import voxsub.model_catalog as catalog

    monkeypatch.setattr(catalog, "fetch_file", fake_fetch)
    market = ModelMarketplace(tmp_path / "models")
    target = market.install(model, "china")
    assert (target / "encoder.int8.onnx").read_bytes() == b"encoder"
    assert (target / "tokenizer/vocab.json").read_bytes() == b"{}"

    staged = market._downloads / model.id
    staged.mkdir(parents=True)
    (staged / "leftover.part").write_bytes(b"partial")
    market.uninstall(model)
    assert not target.exists()
    assert not staged.exists()


def test_source_preference_is_respected_without_probe(tmp_path: Path) -> None:
    market = ModelMarketplace(tmp_path)
    model = get_model("asr-funasr-nano-2512-int8")
    assert model is not None
    assert market.ordered_sources(model, "china")[0].id == "china"
    assert market.ordered_sources(model, "global")[0].id == "global"
