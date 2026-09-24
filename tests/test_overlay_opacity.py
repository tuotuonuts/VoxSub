"""Opacity uses the existing unified schema; never touch the user's config."""
import pytest

from voxsub.config_store import ConfigStore


@pytest.mark.parametrize("value, expected", [
    (0.2, 0.2), (1, 1.0), (0.92, 0.92), (0.47, 0.47),
    (0, 0.2), (-1, 0.2), (2, 1.0),
    (None, 0.92), (True, 0.92), ("0.5", 0.92),
    (float("nan"), 0.92), (float("inf"), 0.92),
])
def test_opacity_validation_and_restart(tmp_path, value, expected):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    assert store.get("overlay_opacity") == 0.92
    store.update({"overlay_opacity": value})
    assert ConfigStore(path).get("overlay_opacity") == expected
