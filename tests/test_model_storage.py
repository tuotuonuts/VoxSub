"""Storage migration tests for fresh installs and upgrades."""
from __future__ import annotations

from pathlib import Path

from voxsub.model_catalog import ModelMarketplace, get_model
from voxsub.model_storage import (
    initialize_model_storage,
    migrate_models,
    normalize_model_layout,
)
from voxsub.models import ModelManager, load_manifest, save_manifest
from voxsub.ui.config_store import ConfigStore


def test_fresh_packaged_install_uses_install_models_folder(tmp_path, monkeypatch):
    install_dir = tmp_path / "VoxSub"
    monkeypatch.setenv("VOXSUB_INSTALL_DIR", str(install_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))

    store = ConfigStore(tmp_path / "config.json")
    root = initialize_model_storage(store)

    assert root == install_dir / "Models"
    assert root.is_dir()
    assert store.get("models_root_mode") == "install"


def test_upgrade_keeps_legacy_root_and_reorganizes_known_models(tmp_path, monkeypatch):
    monkeypatch.setenv("VOXSUB_INSTALL_DIR", str(tmp_path / "new-install"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    legacy = tmp_path / "AppData" / "VoxSub" / "models"
    old_asr = legacy / "asr"
    old_asr.mkdir(parents=True)
    (old_asr / "tokens.txt").write_text("tokens", encoding="utf-8")
    (old_asr / "encoder.onnx").write_bytes(b"encoder")

    store = ConfigStore(tmp_path / "config.json")
    root = initialize_model_storage(store)

    assert root == legacy
    assert store.get("models_root_mode") == "legacy"
    assert (legacy / "stt" / "zipformer" / "tokens.txt").exists()
    assert not old_asr.exists()


def test_layout_normalization_rewrites_legacy_manifest_paths(tmp_path):
    root = tmp_path / "models"
    old_file = root / "asr" / "64" / "encoder.int8.onnx"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"encoder")
    save_manifest(root, {
        "version": 1,
        "files": {
            "asr/64/encoder.int8.onnx": {
                "size": len(b"encoder"),
                "sha256": "recorded",
                "status": "ready",
            },
        },
    })

    normalize_model_layout(root)

    files = load_manifest(root)["files"]
    assert "asr/64/encoder.int8.onnx" not in files
    assert "stt/zipformer/64/encoder.int8.onnx" in files
    assert ModelManager(root).verify_all() == []


def test_manual_migration_merges_without_overwriting_or_moving_download_cache(tmp_path):
    source = tmp_path / "old-models"
    destination = tmp_path / "new-models"
    (source / "stt" / "zipformer").mkdir(parents=True)
    (source / "stt" / "zipformer" / "new.onnx").write_bytes(b"new")
    (source / ".downloads").mkdir()
    (source / ".downloads" / "unfinished.part").write_bytes(b"partial")
    (destination / "stt" / "zipformer").mkdir(parents=True)
    (destination / "stt" / "zipformer" / "same.onnx").write_bytes(b"destination")
    (source / "stt" / "zipformer" / "same.onnx").write_bytes(b"source")

    result = migrate_models(source, destination)

    assert result.moved_paths >= 1
    assert result.kept_existing_paths >= 1
    assert (destination / "stt" / "zipformer" / "new.onnx").read_bytes() == b"new"
    assert (destination / "stt" / "zipformer" / "same.onnx").read_bytes() == b"destination"
    assert (source / ".downloads" / "unfinished.part").exists()


def test_manual_migration_merges_source_and_destination_manifests(tmp_path):
    source = tmp_path / "old-models"
    destination = tmp_path / "new-models"
    source_file = source / "nmt" / "opus_zh_en" / "encoder_model.onnx"
    target_file = destination / "vad" / "silero.onnx"
    source_file.parent.mkdir(parents=True)
    target_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"opus")
    target_file.write_bytes(b"vad")
    save_manifest(source, {"version": 1, "files": {
        "nmt/opus_zh_en/encoder_model.onnx": {
            "size": 4, "sha256": "source", "status": "ready",
        },
    }})
    save_manifest(destination, {"version": 1, "files": {
        "vad/silero.onnx": {
            "size": 3, "sha256": "target", "status": "ready",
        },
    }})

    migrate_models(source, destination)

    files = load_manifest(destination)["files"]
    assert set(files) == {
        "translate/opus/opus_zh_en/encoder_model.onnx",
        "vad/silero.onnx",
    }
    assert ModelManager(destination).verify_all() == []


def test_model_hub_finds_legacy_folder_during_interrupted_migration(tmp_path):
    model = get_model("asr-zipformer-bilingual-fast")
    assert model is not None
    legacy = tmp_path / "models" / "asr"
    legacy.mkdir(parents=True)
    (legacy / "tokens.txt").write_text("tokens", encoding="utf-8")
    (legacy / "encoder.onnx").write_bytes(b"encoder")
    (legacy / "decoder.onnx").write_bytes(b"decoder")
    (legacy / "joiner.onnx").write_bytes(b"joiner")

    found = ModelMarketplace(tmp_path / "models").available_model_dir(model)

    assert found == legacy


def test_model_catalog_uses_purpose_based_paths_for_new_downloads(tmp_path):
    asr = get_model("asr-sensevoice-small-int8")
    translate = get_model("mt-hy-mt2-1.8b-q4")
    assert asr and translate

    marketplace = ModelMarketplace(tmp_path / "Models")

    assert marketplace.model_dir(asr) == tmp_path / "Models" / "stt" / "sensevoice-small-int8"
    assert marketplace.model_dir(translate) == tmp_path / "Models" / "translate" / "hy-mt2-1.8b-q4"


def test_default_marketplace_keeps_legacy_translation_visible_after_root_switch(
    tmp_path, monkeypatch
):
    """A root key written before migration must not hide old translation files."""
    install_dir = tmp_path / "new-install"
    local = tmp_path / "AppData"
    monkeypatch.setenv("VOXSUB_INSTALL_DIR", str(install_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    legacy_model = (
        local / "VoxSub" / "models" / "marketplace" /
        "mt-hy-mt2-1.8b-q4"
    )
    legacy_model.mkdir(parents=True)
    (legacy_model / "Hy-MT2-1.8B-Q4_K_M.gguf").write_bytes(b"legacy")

    store = ConfigStore(local / "VoxSub" / "config.json")
    store.update({
        "models_root": str(install_dir / "Models"),
        "models_root_mode": "install",
        "model_storage_initialized": True,
    })

    model = get_model("mt-hy-mt2-1.8b-q4")
    assert model is not None
    marketplace = ModelMarketplace()

    assert marketplace.is_installed(model)
    assert marketplace.model_file(model) == legacy_model / "Hy-MT2-1.8B-Q4_K_M.gguf"
