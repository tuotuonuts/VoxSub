"""Qt-owned file dialogs used by VoxSub windows.

Windows native file dialogs occasionally surface behind a frameless or tray
application, which looks exactly like a frozen export button.  Use Qt's own
dialog consistently so the prompt remains attached to the active VoxSub
window and follows its event loop.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QWidget


def choose_open_file(parent: QWidget, title: str, initial_directory: str,
                     name_filters: list[str]) -> str:
    dialog = QFileDialog(parent, title, initial_directory)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
    dialog.setNameFilters(name_filters)
    if not dialog.exec():
        return ""
    selected = dialog.selectedFiles()
    return selected[0] if selected else ""


def choose_save_file(parent: QWidget, title: str, suggested_name: str,
                     name_filters: list[str], default_suffix: str = "txt") -> tuple[str, str]:
    dialog = QFileDialog(parent, title)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    dialog.setNameFilters(name_filters)
    dialog.setDefaultSuffix(default_suffix.lstrip("."))
    dialog.selectFile(suggested_name)
    if not dialog.exec():
        return "", ""
    selected = dialog.selectedFiles()
    return (selected[0] if selected else "", dialog.selectedNameFilter())
