"""Glass configuration tests use isolated temporary files, not user preferences."""
import pytest
from voxsub.config_store import ConfigStore

@pytest.mark.parametrize("value, expected", [(0,0), (100,100), (73,73), (-1,0), (101,100), (None,50), (True,50), ("70",50), (float("nan"),50), (float("inf"),50)])
def test_glass_strength_restart(tmp_path, value, expected):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    assert store.get("overlay_glass_enabled") is False
    assert store.get("overlay_glass_strength") == 50
    store.update({"overlay_glass_enabled": True, "overlay_glass_strength": value})
    restarted = ConfigStore(path)
    assert restarted.get("overlay_glass_enabled") is True
    assert restarted.get("overlay_glass_strength") == expected
    restarted.update({"overlay_glass_enabled": False})
    assert ConfigStore(path).get("overlay_glass_strength") == expected

@pytest.mark.parametrize("value", [None, 1, "true", [], {}])
def test_glass_toggle_rejects_non_boolean(tmp_path, value):
    store = ConfigStore(tmp_path / "config.json")
    store.update({"overlay_glass_enabled": value})
    assert store.get("overlay_glass_enabled") is False
