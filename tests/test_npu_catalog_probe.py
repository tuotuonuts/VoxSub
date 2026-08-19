from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from scripts.npu_catalog_probe import _find_reusable_asset, _valid_asset
from voxsub.model_catalog import get_model


def _test_model(payload: bytes):
    model = get_model("mt-hy-mt2-1.8b-q4")
    assert model is not None
    import hashlib

    return replace(
        model,
        download_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_find_reusable_asset_uses_installed_marketplace_model(tmp_path: Path) -> None:
    payload = b"verified NPU model"
    model = _test_model(payload)
    installed = tmp_path / model.install_rel / model.asset_name
    installed.parent.mkdir(parents=True)
    installed.write_bytes(payload)

    assert _find_reusable_asset(model, [tmp_path]) == installed


def test_find_reusable_asset_rejects_corrupt_installed_model(tmp_path: Path) -> None:
    payload = b"verified NPU model"
    model = _test_model(payload)
    installed = tmp_path / model.install_rel / model.asset_name
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"corrupt NPU model!")

    assert not _valid_asset(installed, model)
    assert _find_reusable_asset(model, [tmp_path]) is None
