"""Regression coverage for the Windows/Python 3.11 Quality failure.

No packaging, GUI, model loading or audio access. CLI subprocesses deliberately
use the running interpreter, not an unrelated repository virtual environment.
"""
from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "frontend" / "tools" / "build-release.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("quality_console_regression", CLI)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("option", ["--check-only", "--help"])
def test_cli_legacy_pipe_outputs_utf8(option, tmp_path):
    env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp1252:strict",
               APPDATA=str(tmp_path / "roaming"), LOCALAPPDATA=str(tmp_path / "local"))
    result = subprocess.run([sys.executable, str(CLI), option], cwd=ROOT,
                            env=env, capture_output=True, timeout=60)
    stdout = result.stdout.decode("utf-8", errors="strict")
    stderr = result.stderr.decode("utf-8", errors="strict")
    assert result.returncode == 0, stderr
    assert any("\u4e00" <= char <= "\u9fff" for char in stdout)
    assert "UnicodeEncodeError" not in stdout + stderr


def test_cli_invalid_argument_still_fails(tmp_path):
    env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp1252:strict",
               APPDATA=str(tmp_path / "roaming"), LOCALAPPDATA=str(tmp_path / "local"))
    result = subprocess.run([sys.executable, str(CLI), "--unknown-中文"], cwd=ROOT,
                            env=env, capture_output=True, timeout=60)
    assert result.returncode == 2
    assert "--unknown-中文" in result.stderr.decode("utf-8", errors="strict")
    assert "UnicodeEncodeError" not in result.stderr.decode("utf-8")


def test_import_preserves_host_streams(monkeypatch):
    class Stream(io.StringIO):
        def reconfigure(self, **kwargs):
            pytest.fail("import must not reconfigure host streams")
    stdout, stderr = Stream(), Stream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    load_cli()
    assert sys.stdout is stdout and sys.stderr is stderr


def test_console_configures_both_streams(monkeypatch):
    module = load_cli()
    calls = []

    class Stream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", Stream())
    monkeypatch.setattr(sys, "stderr", Stream())
    module._configure_console()
    assert calls == [{"encoding": "utf-8", "errors": "backslashreplace"}] * 2


def test_console_preserves_streams_without_reconfigure(monkeypatch):
    module = load_cli()
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    module._configure_console()
    assert sys.stdout is stdout and sys.stderr is stderr


def test_report_counts_statuses_without_claiming_unchecked_passed():
    from voxsub.diagnostics import export_report
    statuses = ["ok", "ok", "warn", "fail", "not_run", "running", "resource_limited"]
    report = export_report([{"check": str(index), "status": status, "detail": "test"}
                            for index, status in enumerate(statuses)])
    assert "共 7 项: 2 ok / 1 warn / 1 fail" in report
    assert "存在 1 项失败" in report
    assert "未检查项目不作保证" in report
    assert "全部通过" not in report


def test_quality_preserves_five_jobs_and_python311():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8"))
    assert workflow["env"]["PYTHONUTF8"] == "1"
    assert workflow["env"]["PYTHONIOENCODING"] == "utf-8"
    assert set(workflow["jobs"]) == {"python-tests", "ipc-adapters", "release-gate", "integration", "frontend"}
    for name in ("python-tests", "ipc-adapters", "release-gate", "integration"):
        steps = workflow["jobs"][name]["steps"]
        assert any("uv venv --python 3.11" in step.get("run", "") for step in steps)
        assert any("requirements.lock" in step.get("run", "") for step in steps)
        assert any("pytest" in step.get("run", "") for step in steps)
        assert not workflow["jobs"][name].get("continue-on-error", False)
