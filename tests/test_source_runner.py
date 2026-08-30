"""Static contract tests for the one-click Windows source runner."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / "更新并启动测试版.bat"
PS1 = ROOT / "scripts" / "run_source_test.ps1"


def test_double_click_launcher_delegates_to_powershell() -> None:
    text = BAT.read_text(encoding="utf-8")
    assert "powershell.exe" in text
    assert "-ExecutionPolicy Bypass" in text
    assert "scripts\\run_source_test.ps1" in text
    assert "pause" in text


def test_source_runner_updates_and_prepares_local_environment() -> None:
    text = PS1.read_text(encoding="utf-8")
    required = (
        "git -C $repoRoot pull --ff-only",
        "Python 3.11",
        "3.11.0",
        "-m", "venv",
        "requirements.lock",
        "uv pip sync",
        "pip install -r $lockFile",
        "run_app.py",
        "import PySide6, onnxruntime, sherpa_onnx, sentry_sdk",
    )
    for marker in required:
        assert marker in text, marker


def test_source_runner_cleans_python_environment_and_sets_testing() -> None:
    text = PS1.read_text(encoding="utf-8")
    assert "Remove-Item Env:PYTHONPATH" in text
    assert "Remove-Item Env:PYTHONHOME" in text
    assert '$env:VOXSUB_ENVIRONMENT = "testing"' in text
    assert "sentry_dsn.txt" in text
    assert "LOCALAPPDATA" in text


def test_runner_keeps_runtime_data_outside_git_and_logs_failures() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    script = PS1.read_text(encoding="utf-8")
    assert ".venv/" in ignore
    assert "sentry_dsn.txt" in ignore
    assert "VoxSub\\diagnostics\\source-run" in script
    assert "Tee-Object -FilePath $logPath" in script
    assert "exit 1" in script
