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
    assert "GracefulCloseTimeoutMs = 2500" in script
    assert "MyAppShutdownEvent" in script
    assert "MyAppRunningMutex" in script
    assert "taskkill.exe" in script
    assert '/F /T /IM \"{#MyAppExeName}\"' in script


def test_release_build_requires_the_validated_no_npuw_runtime():
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    builder = (ROOT / "scripts" / "build_npu_runtime.ps1").read_text(
        encoding="utf-8")

    assert "VOXSUB_NPU_RUNTIME_DIR" in build
    assert "runtime-dependencies.txt" in build
    assert "build_npu_runtime.ps1" in build
    assert "bin-win-openvino" not in build
    assert "NPU_USE_NPUW" in builder
    assert "A private NPUW compile option remains" in builder
    assert "openvino_intel_npu_plugin.dll" in builder
