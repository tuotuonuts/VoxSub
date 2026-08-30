"""Static contract tests for the one-click Windows source runner."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / "更新并启动测试版.bat"
PS1 = ROOT / "scripts" / "run_source_test.ps1"


def test_double_click_launcher_delegates_to_powershell() -> None:
    raw = BAT.read_bytes()
    text = raw.decode("ascii")
    assert "powershell.exe" in text
    assert "-ExecutionPolicy Bypass" in text
    assert "scripts\\run_source_test.ps1" in text
    assert "chcp 65001" in text
    assert "pause" in text


def test_windows_powershell_script_has_utf8_bom() -> None:
    # Windows PowerShell 5.1 otherwise decodes a UTF-8 script as the system ANSI code page.
    assert PS1.read_bytes().startswith(b"\xef\xbb\xbf")
    assert '$OutputEncoding = [System.Text.UTF8Encoding]::new($false)' in PS1.read_text(encoding="utf-8")


def test_source_runner_updates_and_prepares_local_environment() -> None:
    text = PS1.read_text(encoding="utf-8")
    required = (
        "git -C $repoRoot pull --ff-only",
        "Python 3.11",
        "3.11.0",
        "-m", "venv",
        "requirements.lock",
        "uv pip sync",
        "--without-pip",
        "Move-VenvAside",
        "重建后同步锁定依赖",
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
    assert '$ErrorActionPreference = "Continue"' in script
    assert "System.Management.Automation.ErrorRecord" in script
    assert "exit 1" in script


def test_runner_checks_processes_before_pull_and_waits_after_exit() -> None:
    text = PS1.read_text(encoding="utf-8")
    assert "Get-CimInstance -ClassName Win32_Process" in text
    assert "Get-VoxSubProcesses" in text
    assert "git -C $repoRoot pull --ff-only" in text
    assert "退出应用" in text
    assert "Wait-VoxSubProcessesExit" in text
    assert "Start-Sleep -Milliseconds 500" in text
    assert "TimeoutSeconds = 30" in text
    assert "未执行 git pull" in text
    assert "Stop-Process" not in text
    assert "taskkill" not in text


def test_runner_stops_before_pull_for_tracked_local_source_changes() -> None:
    text = PS1.read_text(encoding="utf-8")

    assert "function Get-TrackedWorkingTreeChanges" in text
    assert "git -C $repoRoot status --porcelain --untracked-files=no" in text
    assert "$localChanges = @(Get-TrackedWorkingTreeChanges)" in text
    assert "检测到未提交的 VoxSub 源码修改，已安全停止更新。" in text
    assert "模型、配置、日志和 .venv 不会触发此检查。" in text
    assert text.index("$localChanges = @(Get-TrackedWorkingTreeChanges)") < text.index(
        "git -C $repoRoot pull --ff-only")


def test_tray_exit_action_is_explicit_and_localized() -> None:
    tray = (ROOT / "voxsub" / "ui" / "tray.py").read_text(encoding="utf-8")
    i18n = (ROOT / "voxsub" / "ui" / "i18n.py").read_text(encoding="utf-8")
    assert 'QAction(tr("退出应用")' in tray
    assert 'self._quit_action.setText(tr("退出应用"))' in tray
    assert '"退出应用": "Exit application"' in i18n
