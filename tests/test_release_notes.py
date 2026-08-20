"""Regression tests for the one-time, user-facing update notes."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from voxsub.ui.config_store import ConfigStore
from voxsub.ui.i18n import language_manager
from voxsub.ui.release_notes import release_history_text, show_release_notes_once


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    language_manager.set_language("zh")
    yield app
    language_manager.set_language("zh")


def test_release_notes_are_shown_only_once_for_a_version(qapp, tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")
    parent = QWidget()

    first = show_release_notes_once(parent, store, "0.4.1-beta")
    qapp.processEvents()

    assert first is not None
    assert store.get("release_notes_seen_version") == "0.4.1-beta"
    assert show_release_notes_once(parent, store, "0.4.1-beta") is None
    assert "模型保存更安心" in release_history_text()

    first.close()
    first.deleteLater()
    parent.deleteLater()
    qapp.processEvents()


def test_release_notes_can_be_shown_for_a_newer_version(qapp, tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")
    store.set("release_notes_seen_version", "0.4.0-beta")
    parent = QWidget()

    dialog = show_release_notes_once(parent, store, "0.4.1-beta")

    assert dialog is not None
    assert store.get("release_notes_seen_version") == "0.4.1-beta"
    dialog.close()
    dialog.deleteLater()
    parent.deleteLater()
    qapp.processEvents()


def test_installer_preserves_user_model_directory():
    installer = Path(__file__).parents[1] / "scripts" / "installer.iss"
    source = installer.read_text(encoding="utf-8")

    assert 'Name: "{app}\\Models"; Permissions: users-modify' in source
    assert "OutputDir=..\\..\\Release" in source
    assert "[UninstallDelete]" not in source
    files_section = source.split("[Files]", 1)[-1].split("[Dirs]", 1)[0]
    assert "{app}\\Models" not in files_section
