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
from voxsub.npu_validation import (
    NPU_COMPATIBILITY,
    NPU_STATUS_FAILED,
    NPU_STATUS_LIMITED,
    NPU_STATUS_PENDING,
    NPU_STATUS_UNSUPPORTED,
    NPU_STATUS_VERIFIED,
    npu_compatibility,
)


def test_catalog_is_quality_sorted_and_has_no_old_qwen_translation() -> None:
    for task in ("asr", "translate", "tts"):
        scores = [model.quality_score for model in models_for_task(task)]
        assert scores == sorted(scores, reverse=True)
    ids = {model.id for model in CATALOG}
    assert "mt-hy-mt2-1.8b-q4" in ids
    assert "mt-hy-mt2-7b-q4" in ids
    assert "asr-sensevoice-small-int8" in ids
    assert {"mt-hy-mt2-1.8b-q6", "mt-hy-mt2-1.8b-q8",
            "mt-hy-mt2-7b-q6", "mt-hy-mt2-7b-q8"}.issubset(ids)
    assert len(ids) >= 11
    assert not any("qwen2.5" in model.id for model in CATALOG)


def test_tts_catalog_has_selectable_zh_en_and_bilingual_models(tmp_path: Path) -> None:
    voices = models_for_task("tts")
    assert {model.id for model in voices} == {
        "tts-melo-zh-en",
        "tts-icefall-zh-aishell3",
        "tts-icefall-en-ljspeech-low",
    }
    assert get_model("tts-melo-zh-en").tts_languages == ("zh", "en")
    assert get_model("tts-icefall-zh-aishell3").tts_languages == ("zh",)
    assert get_model("tts-icefall-en-ljspeech-low").tts_languages == ("en",)
    assert all(model.task_label == "语音朗读" for model in voices)

    # Existing installations from versions before the marketplace integration
    # remain visible, so users are not forced to download the same voice twice.
    legacy = tmp_path / "tts" / "zh"
    legacy.mkdir(parents=True)
    for relative in ("model.onnx", "tokens.txt", "lexicon.txt"):
        (legacy / relative).write_bytes(b"ready")
    marketplace = ModelMarketplace(tmp_path)
    zh_model = get_model("tts-icefall-zh-aishell3")
    assert marketplace.is_installed(zh_model)
    assert marketplace.available_model_dir(zh_model) == legacy


def test_every_catalog_model_has_explicit_npu_compatibility() -> None:
    assert set(NPU_COMPATIBILITY) == {model.id for model in CATALOG}


def test_ocr_catalog_has_builtin_speed_quality_and_document_presets() -> None:
    models = models_for_task("ocr")
    assert {model.runtime for model in models} == {
        "rapidocr-v6-small",
        "rapidocr-v6-tiny",
        "rapidocr-v6-medium",
        "rapidocr-v5-server",
    }
    assert any(model.builtin for model in models)
    assert all(model.task_label == "图片文字识别" for model in models)
    for model in CATALOG:
        evidence = npu_compatibility(model.id)
        assert evidence.status in {
            NPU_STATUS_VERIFIED, NPU_STATUS_PENDING, NPU_STATUS_UNSUPPORTED,
            NPU_STATUS_FAILED, NPU_STATUS_LIMITED,
        }
        assert bool(model.npu_supported) == (
            evidence.status in {NPU_STATUS_VERIFIED, NPU_STATUS_PENDING}
        )
        assert evidence.label_zh.startswith("NPU ")
        assert evidence.label_en.startswith("NPU ")

    assert npu_compatibility("mt-opus-fast-builtin").status == NPU_STATUS_UNSUPPORTED
    assert all(
        npu_compatibility(model.id).status == NPU_STATUS_UNSUPPORTED
        for model in CATALOG if model.task == "asr"
    )
    for model_id in (
        "mt-hy-mt2-1.8b-q4", "mt-hy-mt2-1.8b-q6", "mt-hy-mt2-1.8b-q8",
    ):
        evidence = npu_compatibility(model_id)
        assert evidence.status == NPU_STATUS_VERIFIED
        assert evidence.device == "Intel(R) AI Boost (Core Ultra 5 225H)"
        assert evidence.driver == "32.0.100.4841"
        assert evidence.runtime == "llama.cpp b10470 OpenVINO 2026.2.1 / NPU"
        assert evidence.validated_at == "2026-08-20"
    assert all(
        npu_compatibility(model_id).status == NPU_STATUS_PENDING
        for model_id in (
            "mt-hy-mt2-7b-q4", "mt-hy-mt2-7b-q6", "mt-hy-mt2-7b-q8",
        )
    )


def test_sensevoice_catalog_entry_has_downloadable_runtime_contract() -> None:
    model = get_model("asr-sensevoice-small-int8")
    assert model is not None
    assert model.runtime == "sherpa-sense-voice"
    assert model.required_paths == ("model.int8.onnx", "tokens.txt")
    assert model.archive
    assert {source.id for source in model.sources} == {"global", "china"}


def test_hy_mt2_7b_q8_uses_exact_upstream_asset_metadata() -> None:
    model = get_model("mt-hy-mt2-7b-q8")
    assert model is not None
    assert model.asset_name == "HY-MT2-7B-Q8_0.gguf"
    assert model.required_paths == ("HY-MT2-7B-Q8_0.gguf",)
    assert model.download_bytes == 7_981_928_896
    assert model.sha256 == "58b3ad55dd6f6fa08c695cddc34fb5f8f708a844f78ae10508071914b0ed67c0"
    assert all("HY-MT2-7B-Q8_0.gguf" in source.url for source in model.sources)


def test_sensevoice_runtime_uses_catalog_file_layout(tmp_path: Path, monkeypatch) -> None:
    import voxsub.asr as asr

    model_dir = tmp_path / "sensevoice"
    model_dir.mkdir()
    (model_dir / "model.int8.onnx").write_bytes(b"model")
    (model_dir / "tokens.txt").write_text("<blk> 0\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class _FakeOfflineRecognizer:
        @staticmethod
        def from_sense_voice(**kwargs):
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(asr.sherpa_onnx, "OfflineRecognizer", _FakeOfflineRecognizer)
    recognizer = asr.OfflineGenerativeASR(
        model_dir, "sherpa-sense-voice", provider="cpu", num_threads=3,
        source_lang="zh",
    )

    assert recognizer.runtime == "sherpa-sense-voice"
    assert captured["model"] == str(model_dir / "model.int8.onnx")
    assert captured["tokens"] == str(model_dir / "tokens.txt")
    assert captured["language"] == "zh"
    assert captured["use_itn"] is True


def test_funasr_runtime_receives_selected_language_and_prompt(tmp_path: Path, monkeypatch) -> None:
    import voxsub.asr as asr

    model_dir = tmp_path / "funasr"
    (model_dir / "Qwen3-0.6B").mkdir(parents=True)
    for filename in ("encoder_adaptor.int8.onnx", "llm.int8.onnx", "embedding.int8.onnx"):
        (model_dir / filename).write_bytes(b"model")
    captured: dict[str, object] = {}

    class _FakeOfflineRecognizer:
        @staticmethod
        def from_funasr_nano(**kwargs):
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(asr.sherpa_onnx, "OfflineRecognizer", _FakeOfflineRecognizer)
    asr.OfflineGenerativeASR(
        model_dir, "sherpa-funasr-nano", provider="cpu", num_threads=3,
        source_lang="en",
    )

    assert captured["language"] == "en"
    assert "English" in str(captured["system_prompt"])
    assert "English" in str(captured["user_prompt"])


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
        npu_driver_version="32.0.100.4841",
    )
    npu_assessment = assess_model(hy_small, npu_laptop)
    assert npu_assessment.level in {"推荐", "较为推荐"}
    assert npu_assessment.reason.startswith("NPU ")


def test_npu_assessment_accepts_bundled_openvino_runtime(monkeypatch) -> None:
    npu_laptop = HardwareProfile(
        "Core Ultra", 8, 16, 32.0,
        npu_name="Intel AI Boost", integrated_gpu_name="Intel Arc Graphics",
        npu_driver_version="32.0.100.4841")
    monkeypatch.setattr(
        "voxsub.model_catalog.discover_llama_runtimes",
        lambda: [type("Runtime", (), {"backend": "openvino"})()],
    )
    hy_small = get_model("mt-hy-mt2-1.8b-q4")
    assert hy_small is not None
    assessment = assess_model(hy_small, npu_laptop)
    assert assessment.reason.startswith("NPU ")


def test_npu_assessment_rejects_outdated_intel_driver(monkeypatch) -> None:
    npu_laptop = HardwareProfile(
        "Core Ultra", 8, 16, 32.0,
        npu_name="Intel AI Boost", integrated_gpu_name="Intel Arc Graphics",
        npu_driver_version="32.0.100.3159")
    monkeypatch.setattr(
        "voxsub.model_catalog.discover_llama_runtimes",
        lambda: [type("Runtime", (), {"backend": "openvino"})()],
    )
    hy_small = get_model("mt-hy-mt2-1.8b-q4")
    assert hy_small is not None

    assessment = assess_model(hy_small, npu_laptop)

    assert not assessment.reason.startswith("NPU ")
    assert assessment.reason.startswith("核显 ")


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


def test_builtin_repair_downloads_only_missing_files(tmp_path: Path, monkeypatch) -> None:
    model = get_model("mt-opus-fast-builtin")
    assert model is not None
    market = ModelMarketplace(tmp_path / "models")
    target = market.model_dir(model)
    existing = target / "opus_zh_en" / "config.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("{}", encoding="utf-8")
    calls: list[str] = []

    def fake_fetch(url, dest, **kwargs):
        calls.append(str(dest))
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"model asset")
        return True

    import voxsub.model_catalog as catalog

    monkeypatch.setattr(catalog, "fetch_file", fake_fetch)
    market.install(model, "global")

    assert market.is_installed(model)
    assert len(calls) == len(model.required_paths) - 1
    assert existing.read_text(encoding="utf-8") == "{}"
    assert market.missing_paths(model) == ()


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
