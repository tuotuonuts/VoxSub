"""Regression checks for the default non-intrusive test policy."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _hardware_audio_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Attribute)
            and decorator.attr == "hardware_audio"
            for decorator in node.decorator_list
        )
    }


def test_default_pytest_run_excludes_physical_audio_tests() -> None:
    config = (ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert 'addopts = -m "not hardware_audio"' in config
    assert "hardware_audio:" in config


def test_tests_that_access_physical_audio_devices_are_marked() -> None:
    assert _hardware_audio_tests(ROOT / "tests" / "test_audio.py") == {
        "test_list_microphones_real_nonempty",
        "test_list_loopbacks_real_paired_with_speakers",
        "test_loopback_closure_sine",
        "test_loopback_chunk_format",
        "test_mic_source_smoke",
    }
    assert _hardware_audio_tests(ROOT / "tests" / "test_process_audio.py") == {
        "test_process_loopback_captures_target_tone",
        "test_process_loopback_excludes_other_process_tone",
    }
