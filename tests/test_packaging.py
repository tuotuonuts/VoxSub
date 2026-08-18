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
    for key in ("MyAppName", "CreateDesktopShortcut", "AdditionalIcons", "LaunchApp"):
        assert f"english.{key}=" in script
        assert f"chinesesimplified.{key}=" in script
        assert f"chinesetraditional.{key}=" in script
