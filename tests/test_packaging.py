"""Static guards for release-facing installer behavior."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_auto_detects_windows_ui_language_with_english_fallback():
    script = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")
    assert "LanguageDetectionMethod=uilanguage" in script
    assert "ShowLanguageDialog=no" in script
    assert 'Name: "english"; MessagesFile: "compiler:Default.isl"' in script
    assert (
        'Name: "chinesesimplified"; '
        'MessagesFile: "compiler:Languages\\ChineseSimplified.isl"'
    ) in script
    assert (
        'Name: "chinesetraditional"; '
        'MessagesFile: "compiler:Languages\\ChineseTraditional.isl"'
    ) in script


def test_installer_custom_messages_cover_visible_actions():
    script = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")
    for key in (
        "MyAppName", "CreateDesktopShortcut", "AdditionalIcons", "LaunchApp",
        "ClosingApp", "AppCloseFailed",
    ):
        assert f"english.{key}=" in script
        assert f"chinesesimplified.{key}=" in script
        assert f"chinesetraditional.{key}=" in script


def test_installer_uses_bounded_shutdown_instead_of_restart_manager_wait():
    script = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")

    assert "CloseApplications=no" in script
    assert "RestartApplications=no" in script
    assert "function PrepareToInstall" in script
    assert "GracefulCloseTimeoutMs = 5000" in script
    assert "ForcedCloseVerifyTimeoutMs = 2000" in script
    assert "function WaitForNewVoxSubToExit" in script
    assert "if not WaitForNewVoxSubToExit(GracefulCloseTimeoutMs)" in script
    assert "if not WaitForNewVoxSubToExit(ForcedCloseVerifyTimeoutMs)" in script
    assert "MyAppShutdownEvent" in script
    assert "MyAppRunningMutex" in script
    assert "taskkill.exe" in script
    assert '/F /T /IM \"{#MyAppExeName}\"' in script

    app = (ROOT / "voxsub" / "ui" / "app.py").read_text(encoding="utf-8")
    assert "app.aboutToQuit.connect(installer_shutdown.close)" not in app

    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    assert 'Run-Checked "packaged installer shutdown smoke"' in build
    assert '"scripts\\smoke_installer_shutdown.py" --exe $Exe' in build


def test_upgrade_removes_stale_runtime_without_touching_user_data():
    script = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")
    install_delete = script.split("[InstallDelete]", 1)[1].split("[Dirs]", 1)[0]

    assert 'Name: "{app}\\_internal"' in install_delete
    assert 'Name: "{app}\\tools"' in install_delete
    assert 'Name: "{app}\\models_base"' in install_delete
    assert "{app}\\Models" not in install_delete
    assert "{app}\\Cache" not in install_delete


def test_release_build_requires_the_validated_no_npuw_runtime():
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    builder = (ROOT / "scripts" / "build_npu_runtime.ps1").read_text(
        encoding="utf-8")

    assert "VOXSUB_NPU_RUNTIME_DIR" in build
    assert "runtime-dependencies.txt" in build
    assert "build_npu_runtime.ps1" in build
    assert "bin-win-openvino" not in build
    assert '".pytest-build-"' in build
    assert "$env:TEMP" not in build.split('Run-Checked "pytest"')[0]
    assert "NPU_USE_NPUW" in builder
    assert "A private NPUW compile option remains" in builder
    assert "openvino_intel_npu_plugin.dll" in builder


def test_release_build_collects_the_offline_ocr_runtime():
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    entrypoint = (ROOT / "run_app.py").read_text(encoding="utf-8")

    assert "rapidocr==3.9.2" in requirements
    assert '"--collect-all", "rapidocr"' in build
    assert '"--hidden-import", "rapidocr.main"' in build
    assert '"--collect-all", "cv2"' in build
    assert '"--hidden-import", "voxsub.ui.ocr_workspace"' in build
    assert '"--ocr-smoke"' in entrypoint
    assert '"--qt-smoke"' in entrypoint
    assert 'ArgumentList "--qt-smoke"' in build
    assert 'Start-Process -FilePath $Exe -ArgumentList "--ocr-smoke"' in build
    assert "$OcrSmoke.ExitCode -ne 0" in build
def test_pyinstaller_runs_with_isolated_native_search_path():
    runner = (ROOT / "scripts" / "run_pyinstaller.py").read_text(
        encoding="utf-8")
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert "os.environ.pop(\"PYTHONPATH\", None)" in runner
    assert "os.environ.pop(\"PYTHONHOME\", None)" in runner
    assert "isolated_windows_path" in runner
    assert "run_pyinstaller.py" in build
    assert "Unexpected ICU DLLs in frozen bundle" in build
