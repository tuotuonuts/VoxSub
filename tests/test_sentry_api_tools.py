"""Static contracts for local-only Sentry API diagnostics tools."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INITIALIZER = ROOT / "scripts" / "initialize_sentry_diagnostics.ps1"
ISSUES = ROOT / "scripts" / "get_sentry_issues.ps1"


def test_initializer_prompts_for_token_without_storing_it_in_repo() -> None:
    text = INITIALIZER.read_text(encoding="utf-8-sig")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "Read-Host \"Sentry API Token\" -AsSecureString" in text
    assert "sentry_auth_token.txt" in text
    assert "sentry_api.json" in text
    assert "ConvertTo-Json" in text
    assert "org:read" in text
    assert "project:read" in text
    assert "event:read" in text
    assert "sentry_auth_token.txt" in ignore
    assert "sentry_api.json" in ignore


def test_powershell_tools_use_utf8_bom_for_windows_powershell_compatibility() -> None:
    assert INITIALIZER.read_bytes().startswith(b"\xef\xbb\xbf")
    assert ISSUES.read_bytes().startswith(b"\xef\xbb\xbf")


def test_initializer_auto_discovers_the_project_with_read_only_api_calls() -> None:
    text = INITIALIZER.read_text(encoding="utf-8-sig")

    assert '"https://de.sentry.io"' in text
    assert '"voxsub"' in text
    assert '"/api/0/organizations/"' in text
    assert '"/api/0/organizations/$orgSlug/projects/"' in text
    assert "-Method Get" in text
    assert "Authorization = \"Bearer $token\"" in text


def test_issue_reader_uses_only_local_api_configuration() -> None:
    text = ISSUES.read_text(encoding="utf-8-sig")

    assert "sentry_auth_token.txt" in text
    assert "sentry_api.json" in text
    assert '"is:unresolved"' in text
    assert '"$base/api/0/projects/$org/$project/issues/' in text
    assert "-Method Get" in text
