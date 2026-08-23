"""Friendly, in-app update notes shown once for each installed version."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from voxsub.config_store import ConfigStore
from voxsub.ui.i18n import LANGUAGE_EN, language_manager, tr


@dataclass(frozen=True)
class ReleaseNote:
    version: str
    title_zh: str
    title_en: str
    items_zh: tuple[str, ...]
    items_en: tuple[str, ...]


RELEASE_HISTORY: tuple[ReleaseNote, ...] = (
    ReleaseNote(
        "0.4.2-beta",
        "更稳定的长时间同传与模型管理",
        "More reliable long-running translation and model management",
        (
            "识别、翻译和语音播放积压时会受到明确限制，不再无限占用内存。",
            "语音朗读已接入独立后台播放；失败时字幕仍可继续工作。",
            "下载和模型写入增加完整性检查与中断保护，旧文件不容易被半成品覆盖。",
            "配置升级与异常值处理集中完成，旧设置会继续兼容。",
            "外观设置中的译文字号和浮窗不透明度现在使用可靠的上下箭头，两边都可正常点击。",
        ),
        (
            "Recognition, translation, and speech backlogs are bounded instead of growing memory indefinitely.",
            "Speech playback now runs independently in the background; subtitles continue if it fails.",
            "Downloads and model writes have stronger integrity checks and interruption protection.",
            "Config upgrades and invalid values are handled centrally while preserving old settings.",
            "Translation font size and overlay opacity now use reliable matching arrow controls in Appearance settings.",
        ),
    ),
    ReleaseNote(
        "0.4.1-beta",
        "模型保存和字幕浮窗更顺手",
        "Better model storage and subtitle overlay controls",
        (
            "可以在设置中选择模型保存的位置，也能把已有模型迁移到其他磁盘。",
            "识别、翻译、语音模型会按用途整理，方便查看和管理。",
            "更新软件不会清空已经下载好的模型。",
            "迁移完成后会立即使用新位置，并自动修正旧清单，不再误报模型缺失。",
            "全屏打开设置或模型广场时，应用会继续保持全屏。",
            "浮窗可以只显示原文、只显示译文或对照翻译，并能调节两种间距。",
            "长句会在固定大小的浮窗内换行和滚动，不再自动跑到屏幕外。",
        ),
        (
            "Choose where models are stored in Settings, and move existing models to another drive.",
            "Speech, translation, and voice models are organized by purpose for easier management.",
            "Updating VoxSub no longer clears models you have already downloaded.",
            "After a move, VoxSub uses the new location and repairs old catalog paths instead of reporting missing models.",
            "Opening Settings or Model Hub now keeps VoxSub in fullscreen mode.",
            "The overlay can show source, translation, or both, with adjustable content and line spacing.",
            "Long sentences now wrap and scroll inside the chosen overlay size instead of expanding off-screen.",
        ),
    ),
    ReleaseNote(
        "0.4.0-beta",
        "更好地使用 Intel NPU",
        "Better support for Intel NPUs",
        (
            "部分翻译模型可以更好地利用 Intel NPU；不适合时会自动选择其他可用设备。",
            "大模型下载更稳定，网络短暂中断后可以继续下载。",
        ),
        (
            "Some translation models can make better use of Intel NPUs, with automatic fallback when needed.",
            "Large model downloads are more resilient and can continue after a brief network interruption.",
        ),
    ),
    ReleaseNote(
        "0.3.9-beta",
        "更顺手的设置与模型广场",
        "A smoother Settings and Model Hub experience",
        (
            "设置和模型广场会直接在主窗口中打开，不再打断你的工作。",
            "缺少的内置识别和翻译文件可以在应用内尝试修复。",
        ),
        (
            "Settings and Model Hub now open inside the main window without interrupting your work.",
            "Missing built-in recognition and translation files can be repaired from inside the app.",
        ),
    ),
)


def _note(version: str) -> ReleaseNote | None:
    return next((item for item in RELEASE_HISTORY if item.version == version), None)


def release_history_text() -> str:
    """Return a compact, user-facing history for Settings > About."""
    english = language_manager.language == LANGUAGE_EN
    blocks: list[str] = []
    for note in RELEASE_HISTORY:
        title = note.title_en if english else note.title_zh
        items = note.items_en if english else note.items_zh
        blocks.append(f"{note.version}  {title}\n" + "\n".join(f"• {item}" for item in items))
    return "\n\n".join(blocks)


class ReleaseNotesDialog(QDialog):
    """Soft, non-native first-launch update message."""

    def __init__(self, version: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._version = version
        self._note = _note(version)
        self.setObjectName("releaseNotesDialog")
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle(tr("这一版更新了什么", "What is new"))
        self.setMinimumWidth(520)
        self.setMaximumWidth(620)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 22)
        root.setSpacing(14)
        shell = QFrame(self)
        shell.setObjectName("releaseNotesSurface")
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)
        self.eyebrow = QLabel(shell)
        self.eyebrow.setObjectName("eyebrowLabel")
        self.title_label = QLabel(shell)
        self.title_label.setObjectName("releaseNotesTitle")
        self.summary = QLabel(shell)
        self.summary.setObjectName("secondaryLabel")
        self.summary.setWordWrap(True)
        self.items_label = QLabel(shell)
        self.items_label.setObjectName("releaseNotesItems")
        self.items_label.setWordWrap(True)
        layout.addWidget(self.eyebrow)
        layout.addWidget(self.title_label)
        layout.addWidget(self.summary)
        layout.addSpacing(4)
        layout.addWidget(self.items_label)
        root.addWidget(shell)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.close_button = QPushButton(self)
        self.close_button.setObjectName("primaryButton")
        self.close_button.setMinimumHeight(40)
        self.close_button.clicked.connect(self.accept)
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        language_manager.language_changed.connect(self._render)
        self._render()

    def _render(self, *_args) -> None:
        note = self._note
        english = language_manager.language == LANGUAGE_EN
        self.setWindowTitle(tr("这一版更新了什么", "What is new"))
        self.eyebrow.setText(f"VOXSUB  /  {self._version.upper()}")
        if note is None:
            title = tr("欢迎使用新版 VoxSub", "Welcome to the new VoxSub")
            items = (tr("这次更新带来了体验改进。", "This update includes experience improvements."),)
        else:
            title = note.title_en if english else note.title_zh
            items = note.items_en if english else note.items_zh
        self.title_label.setText(title)
        self.summary.setText(tr("更新内容已准备好，之后也可以在“设置 > 关于”中查看。",
                                 "You can revisit these notes later in Settings > About."))
        self.items_label.setText("\n".join(f"• {item}" for item in items))
        self.close_button.setText(tr("开始使用", "Start using VoxSub"))


def show_release_notes_once(parent: QWidget, store: ConfigStore, version: str) -> ReleaseNotesDialog | None:
    """Show the installed version's notes once, then persist that decision."""
    if str(store.get("release_notes_seen_version", "")) == version:
        return None
    # Mark before display so a forced close/crash cannot make every next start
    # feel like another update prompt.
    store.set("release_notes_seen_version", version)
    dialog = ReleaseNotesDialog(version, parent)
    parent._voxsub_release_notes = dialog  # type: ignore[attr-defined]
    dialog.open()
    return dialog


__all__ = [
    "RELEASE_HISTORY",
    "ReleaseNotesDialog",
    "release_history_text",
    "show_release_notes_once",
]
