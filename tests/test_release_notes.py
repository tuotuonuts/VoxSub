"""Regression tests for the one-time, user-facing update notes."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from voxsub import __version__
from voxsub.ui.config_store import ConfigStore
from voxsub.ui.i18n import language_manager
from voxsub.ui.release_notes import (
    RELEASE_HISTORY,
    latest_release_note,
    older_release_count,
    release_history_text,
    show_release_notes_once,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    language_manager.set_language("zh")
    yield app
    language_manager.set_language("zh")


@pytest.fixture(autouse=True)
def _restore_language():
    """Every case starts and ends in Chinese so text assertions are stable."""
    language_manager.set_language("zh")
    yield
    language_manager.set_language("zh")


def test_release_notes_are_shown_only_once_for_a_version(qapp, tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")
    parent = QWidget()

    first = show_release_notes_once(parent, store, __version__)
    qapp.processEvents()

    assert first is not None
    assert store.get("release_notes_seen_version") == __version__
    assert show_release_notes_once(parent, store, __version__) is None

    first.close()
    first.deleteLater()
    parent.deleteLater()
    qapp.processEvents()


def test_release_notes_can_be_shown_for_a_newer_version(qapp, tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")
    store.set("release_notes_seen_version", "0.4.0-beta")
    parent = QWidget()

    dialog = show_release_notes_once(parent, store, __version__)

    assert dialog is not None
    assert store.get("release_notes_seen_version") == __version__
    dialog.close()
    dialog.deleteLater()
    parent.deleteLater()
    qapp.processEvents()


# ---------------------------------------------------------------------------
# 更新日志：默认只展示最新一则，历史需显式展开
# ---------------------------------------------------------------------------
class TestReleaseHistoryDefaults:
    def test_history_is_ordered_newest_first(self):
        versions = [note.version for note in RELEASE_HISTORY]
        assert versions == sorted(versions, key=versions.index)
        assert latest_release_note() is RELEASE_HISTORY[0]

    def test_default_text_shows_only_the_newest_entry(self):
        """默认必须是"最后一则"——这是本轮需求的核心契约。"""
        text = release_history_text()
        newest = RELEASE_HISTORY[0]
        assert newest.version in text
        assert newest.title_zh in text
        for older in RELEASE_HISTORY[1:]:
            assert older.title_zh not in text, (
                f"默认视图不应包含 {older.version}")

    def test_expanded_text_contains_every_entry(self):
        text = release_history_text(include_history=True)
        for note in RELEASE_HISTORY:
            assert note.version in text
            assert note.title_zh in text

    def test_expanded_text_is_a_superset_of_the_default(self):
        assert release_history_text() in release_history_text(
            include_history=True)

    def test_older_release_count_matches_the_remaining_entries(self):
        assert older_release_count() == len(RELEASE_HISTORY) - 1

    def test_english_selection_returns_english_items(self):
        language_manager.set_language("en")
        try:
            newest = RELEASE_HISTORY[0]
            assert newest.title_en in release_history_text()
            assert newest.title_zh not in release_history_text()
        finally:
            language_manager.set_language("zh")

    def test_newest_note_covers_the_interface_rework(self):
        """本版更新日志必须记录本轮的界面层次调整，而不是只留 OCR 条目。"""
        newest = RELEASE_HISTORY[0]
        joined = " ".join(newest.items_zh)
        assert "界面层次" in joined
        assert "识别语言" in joined


class TestReleaseHistoryUi:
    def _settings(self, tmp_path: Path):
        from voxsub.ui.settings_window import SettingsWindow

        return SettingsWindow(store=ConfigStore(tmp_path / "config.json"))

    def test_history_starts_collapsed_and_expands_on_click(self, qapp, tmp_path):
        sw = self._settings(tmp_path)
        try:
            assert sw._release_history_expanded is False  # noqa: SLF001
            collapsed = sw.release_history_label.text()
            assert RELEASE_HISTORY[0].version in collapsed
            assert RELEASE_HISTORY[1].title_zh not in collapsed

            sw.release_history_toggle.click()
            qapp.processEvents()

            assert sw._release_history_expanded is True  # noqa: SLF001
            expanded = sw.release_history_label.text()
            assert RELEASE_HISTORY[1].title_zh in expanded

            sw.release_history_toggle.click()
            qapp.processEvents()
            assert sw._release_history_expanded is False  # noqa: SLF001
            assert sw.release_history_label.text() == collapsed
        finally:
            sw.close()
            sw.deleteLater()

    def test_toggle_label_names_the_remaining_count(self, qapp, tmp_path):
        sw = self._settings(tmp_path)
        try:
            assert str(older_release_count()) in sw.release_history_toggle.text()
            sw.release_history_toggle.click()
            qapp.processEvents()
            assert "收起" in sw.release_history_toggle.text()
        finally:
            sw.close()
            sw.deleteLater()

    def test_expansion_survives_a_language_switch(self, qapp, tmp_path):
        """切语言应重绘文案但不能把用户展开的历史收回去。"""
        sw = self._settings(tmp_path)
        try:
            sw.release_history_toggle.click()
            qapp.processEvents()
            language_manager.set_language("en")
            qapp.processEvents()
            assert sw._release_history_expanded is True  # noqa: SLF001
            assert RELEASE_HISTORY[1].title_en in sw.release_history_label.text()
        finally:
            language_manager.set_language("zh")
            sw.close()
            sw.deleteLater()


def test_installer_preserves_user_model_directory():
    installer = Path(__file__).parents[1] / "scripts" / "installer.iss"
    source = installer.read_text(encoding="utf-8")

    assert 'Name: "{app}\\Models"; Permissions: users-modify' in source
    assert "OutputDir=..\\..\\Release" in source
    assert "[UninstallDelete]" not in source
    files_section = source.split("[Files]", 1)[-1].split("[Dirs]", 1)[0]
    assert "{app}\\Models" not in files_section


def test_packaged_app_includes_diagnostics_module():
    build_script = Path(__file__).parents[1] / "scripts" / "build.ps1"
    source = build_script.read_text(encoding="utf-8")

    assert '"--hidden-import", "voxsub.diagnostics"' in source
