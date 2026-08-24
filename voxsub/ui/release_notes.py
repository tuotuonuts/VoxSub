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
        "0.9.0-beta",
        "OCR 图片翻译、缓存导出与可切换模型",
        "OCR image translation, cache/export, and selectable models",
        (
            "修复安装包内 RapidOCR 延迟导入缺失，截图与实时区域 OCR 现在使用同一条已验证的离线识别链路。",
            "主窗口可上传 PNG、JPG、WebP、BMP 或 TIFF；结果可在原图标框和原位覆盖译文之间切换，并导出译后图片。",
            "原图与未导出的译后图分别缓存在应用目录的 originals / translated；禁止写入 C 盘，默认每类保留 15 张，0 表示无限。",
            "模型广场新增 OCR 分类，可切换 PP-OCRv6 Tiny、内置 Small、Medium，以及更适合复杂中英文手写/竖排的 PP-OCRv5 Server。",
            "框选器会等待主窗口从 Windows 桌面合成帧中完全消失，避免截图带有应用虚影。",
            "OCR 入口移到顶部工具区，A/B/C 模式卡恢复独立布局；引擎不可用时实时模式会暂停，不再持续重试刷日志。",
        ),
        (
            "Fixes the missing RapidOCR lazy import in packaged builds, so screenshot and live-region OCR share one verified offline path.",
            "The main window can upload PNG, JPG, WebP, BMP, or TIFF images, switch between source boxes and in-place translations, and export the rendered result.",
            "Source and unexported translated images use separate originals/translated app cache folders. Drive C is rejected; each type keeps 15 images by default, while 0 is unlimited.",
            "Model Hub adds OCR presets: PP-OCRv6 Tiny, bundled Small, Medium, and PP-OCRv5 Server for more challenging Chinese/English handwriting and vertical text.",
            "The selector waits for the main window to leave the Windows compositor frame, preventing a VoxSub afterimage in the frozen desktop.",
            "OCR moves to the top tool row so A/B/C mode cards keep their layout. A missing engine pauses live OCR instead of retrying and flooding logs.",
        ),
    ),
    ReleaseNote(
        "0.8.0-beta",
        "新增截图与实时区域 OCR 翻译",
        "Screenshot and live-region OCR translation",
        (
            "主窗口新增 OCR 翻译工作区，可框选一次截图，离线识别文字并使用当前翻译设置生成译文。",
            "实时区域模式只在画面发生明显变化时重新识别，适合游戏、视频、网页和无法复制文字的应用。",
            "译文会按 OCR 返回的文字坐标覆盖原文；覆盖层不接收鼠标输入，也不会被下一轮截图再次识别。",
            "独立控制条支持暂停、临时显示原文、重选区域和结束，不需要反复切回主窗口。",
            "截图像素只在本机内存中处理；只有选择云翻译时，识别出的文字才会发送到对应服务。",
            "当前内置模型优先覆盖中英文常见印刷体；手写体、艺术体与更多语种将通过可替换 OCR 后端继续增强。",
        ),
        (
            "The main window adds an OCR workspace for selecting one screenshot, recognizing it offline, and translating it with the current translation settings.",
            "Live Region reruns OCR only after a meaningful visual change, fitting games, video, webpages, and apps whose text cannot be copied.",
            "Translations cover the source at OCR-provided coordinates; the click-through overlay is excluded from later captures to prevent self-recognition.",
            "A separate control bar can pause, reveal the source temporarily, reselect the area, or stop without returning to the main window.",
            "Screenshot pixels stay in local memory. Only recognized text is sent out when cloud translation is selected.",
            "The bundled model currently prioritizes common printed Chinese and English; handwriting, stylized text, and more languages can be added through the replaceable OCR backend.",
        ),
    ),
    ReleaseNote(
        "0.7.2-beta",
        "安装更新不再长时间假死",
        "Installer updates no longer appear frozen",
        (
            "修复安装器把“关闭主窗口”误当成“退出应用”，等待约半分钟后才报无法关闭的问题。",
            "新安装器会向语幕发送专用退出信号，让当前任务先做安全收尾，而不是把窗口缩到托盘。",
            "旧版没有退出信号时，安装器会立即关闭 VoxSub.exe 及它启动的推理子进程，不再进入 Windows 默认的长等待。",
            "后台处理线程退出改为共享总时限，不会再对每条线程逐一叠加等待。",
        ),
        (
            "Fixes the installer treating a closed main window as an exited app, then waiting roughly thirty seconds before reporting failure.",
            "The installer now sends VoxSub a dedicated shutdown signal so active work can clean up instead of merely hiding the window in the tray.",
            "Older builds without the handshake are closed immediately together with their inference child processes, bypassing the long Windows default wait.",
            "Background workers now share one total shutdown deadline instead of accumulating one timeout per thread.",
        ),
    ),
    ReleaseNote(
        "0.7.1-beta",
        "实时双语草稿更易读也更跟手",
        "More readable and responsive live bilingual drafts",
        (
            "英文草稿不再长时间保持全大写；显示层会使用更易读的句子式大小写，不改变最终识别原文。",
            "修复连续说话时新识别词反复重置计时，译文长期停在“生成中”的问题。",
            "草稿翻译现在会定期合并最新原文并追赶识别；完整句的最终翻译仍然始终优先。",
            "原文继续向后增长时，已生成的译文会保留到新译文接替，减少长时间只显示占位提示。",
        ),
        (
            "All-caps interim English is rendered in readable sentence case without changing the finalized source transcript.",
            "Continuous recognition updates no longer reset the timer forever and leave translation stuck on its generating placeholder.",
            "Draft translation periodically coalesces the newest source and catches up, while finalized sentence translation always retains priority.",
            "As the source grows, the last completed translation remains visible until a newer revision replaces it, reducing placeholder-only time.",
        ),
    ),
    ReleaseNote(
        "0.7.0-beta",
        "可下载和切换中英朗读模型",
        "Downloadable and selectable Chinese and English voices",
        (
            "模型广场新增语音朗读分类，可下载中文轻量、英文轻量和 MeloTTS 中英双语自然音色。",
            "设置 → 语音朗读可分别选择中文与英文模型，运行中切换会立即重载后台朗读引擎。",
            "修复朗读开关只保存设置、当前会话不生效的问题；安装完模型后下一个终句可自动开始朗读。",
            "实时双语草稿与智能上下文可使用 TTS；为避免每次草稿修订都重复抢读，只朗读稳定后的最终译文。",
            "旧版 models/tts/zh 和 models/tts/en 已有模型会自动识别，不需要重复下载。",
        ),
        (
            "Model Hub adds a Text-to-speech category with lightweight Chinese and English voices plus a more natural bilingual MeloTTS voice.",
            "Settings -> Text-to-speech can select Chinese and English models independently, with live sessions hot-reloading the speech worker.",
            "The TTS switch now applies to the current session instead of only persisting; a newly installed model is discovered for the next finalized sentence.",
            "Smart Context and live bilingual drafts support TTS, but only finalized translations are read to avoid repeated, overlapping draft speech.",
            "Existing models under models/tts/zh and models/tts/en are detected automatically and do not need to be downloaded again.",
        ),
    ),
    ReleaseNote(
        "0.6.0-beta",
        "当前句会随识别与翻译实时更新",
        "Live recognition and translation drafts",
        (
            "智能上下文模式会在同一条草稿中持续追加和纠正当前句，整句稳定后才写入字幕历史。",
            "流式 Zipformer 更接近逐词刷新；识别假设改变时，主窗口与浮窗会原位替换，不重复堆积临时行。",
            "选择 Qwen3、Fun-ASR、SenseVoice 或云 STT 时，内置 Zipformer 会作为轻量草稿旁路连续出字，当前模型仍负责最终纠偏定稿。",
            "“实时双语草稿”可以独立关闭；关闭后仍保留智能断句、上下文纠偏和语气词清理。",
            "译文会跟随当前原文更新；只处理最新草稿，较慢返回的旧译文不会覆盖新内容。",
            "终句翻译始终优先，下一句话即使已经开始识别，也不会与上一句发生错位。",
            "生成式与云端终句接口仍只处理完整声学片段，不会为每个草稿重复运行大模型或上传音频。",
        ),
        (
            "Smart Context continuously extends and corrects one draft row, committing it to history only after the sentence stabilizes.",
            "Streaming Zipformer refreshes closer to word cadence, replacing the main-window and overlay draft in place.",
            "With Qwen3, Fun-ASR, SenseVoice, or cloud STT selected, bundled Zipformer supplies lightweight streaming drafts while the selected recognizer remains authoritative for the corrected final.",
            "Live bilingual drafts can be switched off independently without disabling Smart Context segmentation, correction, or filler cleanup.",
            "Translation follows the changing source; only the newest draft is processed and stale completions cannot overwrite it.",
            "Final sentence translation has priority, so the following sentence cannot be paired with the wrong result.",
            "Generative and cloud final recognizers still process complete acoustic segments only, avoiding repeated large-model inference or uploads for each draft.",
        ),
    ),
    ReleaseNote(
        "0.5.0-beta",
        "新增智能上下文断句与保守纠偏",
        "Smart context segmentation and conservative correction",
        (
            "识别调优新增“智能上下文”模式；原有自动、响应优先、均衡、准确优先和自定义模式保持不变。",
            "句子可能没说完时会结合上下文短暂等待，到达可调上限后一定提交，不会无限拖延。",
            "翻译前可根据常用词和最近重复出现的词做小范围纠偏，不会自由改写或补写内容。",
            "可选轻度清理独立的嗯、啊、呃等语气词；关闭后完整保留原话。",
            "外观与识别调优的数值控件继续使用上下均可点击的统一箭头。",
        ),
        (
            "Recognition tuning adds Smart Context while every existing preset and custom mode keeps its previous behavior.",
            "Possibly incomplete sentences wait briefly for context, with a configurable hard deadline that always commits.",
            "Before translation, small corrections can use custom vocabulary and repeatedly established recent terms without free rewriting.",
            "Optional light cleanup removes isolated fillers such as um or uh; turning it off preserves the original wording.",
            "Appearance and recognition tuning continue to use matching, reliable up/down controls.",
        ),
    ),
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
