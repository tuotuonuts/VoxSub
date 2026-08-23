"""Embedded screenshot and live-region OCR translation workspace."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from voxsub.config_store import ConfigStore
from voxsub.logging_setup import get_logger
from voxsub.ocr import (
    TranslatedOcrFrame,
    frame_fingerprint,
    materially_changed,
)
from voxsub.ui.i18n import tr
from voxsub.ui.ocr_overlay import OcrLiveControlBar, OcrTranslationOverlay
from voxsub.ui.ocr_worker import OcrJob, OcrWorker, OcrWorkerBridge
from voxsub.ui.screen_capture import (
    CapturedRegion,
    ScreenRegionPicker,
    capture_screen_region,
    qimage_to_bgr,
)

logger = get_logger("ui.ocr_workspace")


class OcrPreview(QWidget):
    """Scale one screenshot and its detected line boxes without copying pixels."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._frame: TranslatedOcrFrame | None = None
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_result(self, image: QImage, frame: TranslatedOcrFrame) -> None:
        self._image = image.copy()
        self._frame = frame
        self.update()

    def clear(self) -> None:
        self._image = QImage()
        self._frame = None
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#101010"))
        if self._image.isNull() or self._frame is None:
            painter.setPen(QColor("#9CA3AF"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("截图预览"))
            return
        target = self._fit_rect(self._image.width(), self._image.height())
        painter.drawImage(target, self._image)
        painter.setPen(QPen(QColor("#14B8A6"), 2))
        scale_x = target.width() / max(1, self._frame.width)
        scale_y = target.height() / max(1, self._frame.height)
        for line in self._frame.lines:
            box = line.box
            painter.drawRoundedRect(QRect(
                target.left() + round(box.left * scale_x),
                target.top() + round(box.top * scale_y),
                max(1, round(box.width * scale_x)),
                max(1, round(box.height * scale_y)),
            ), 3, 3)

    def _fit_rect(self, width: int, height: int) -> QRect:
        scale = min(self.width() / max(1, width), self.height() / max(1, height))
        target_width = max(1, round(width * scale))
        target_height = max(1, round(height * scale))
        return QRect(
            (self.width() - target_width) // 2,
            (self.height() - target_height) // 2,
            target_width,
            target_height,
        )


def _mode_button(text: str, parent: QWidget) -> QPushButton:
    button = QPushButton(text, parent)
    button.setCheckable(True)
    button.setObjectName("filterPill")
    button.setMinimumHeight(40)
    return button


class OcrWorkspace(QWidget):
    """Two OCR modes sharing one bounded recognizer/translation worker."""

    LIVE_INTERVAL_MS = 700
    LIVE_CHANGE_THRESHOLD = 0.035

    def __init__(self, store: ConfigStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ocrWorkspace")
        self._store = store or ConfigStore()
        self._picker = ScreenRegionPicker(self)
        self._picker.selected.connect(self._on_region_selected)
        self._picker.cancelled.connect(self._on_pick_cancelled)
        self._bridge = OcrWorkerBridge(self)
        self._worker = OcrWorker(self._bridge)
        self._bridge.result_ready.connect(self._on_result)
        self._bridge.failed.connect(self._on_failure)
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(self.LIVE_INTERVAL_MS)
        self._live_timer.timeout.connect(self._capture_live_tick)
        self._overlay = OcrTranslationOverlay()
        self._control = OcrLiveControlBar()
        self._control.paused_changed.connect(self._on_live_paused)
        self._control.original_changed.connect(self._on_original_changed)
        self._control.reselect_requested.connect(self._reselect_live)
        self._control.stop_requested.connect(self.stop_live)
        self._pick_purpose = ""
        self._live_rect: QRect | None = None
        self._live_paused = False
        self._showing_original = False
        self._previous_fingerprint: bytes | None = None
        self._revision = 0
        self._captures: dict[int, QImage] = {}
        self._host_was_visible = False
        self._empty_live_frames = 0
        self._build_ui()
        self.set_mode("screenshot")

    def set_embedded(self, _embedded: bool = True) -> None:
        return

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 24)
        root.setSpacing(16)
        title = QLabel(tr("OCR 屏幕翻译"), self)
        title.setObjectName("sectionTitle")
        intro = QLabel(tr(
            "一次截图适合图片与文档；实时区域只在画面变化时重新识别，并把译文原位覆盖。"
        ), self)
        intro.setObjectName("secondaryLabel")
        intro.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(intro)

        mode_row = QHBoxLayout()
        self.screenshot_mode_button = _mode_button(tr("截图 OCR 翻译"), self)
        self.live_mode_button = _mode_button(tr("实时区域 OCR"), self)
        self.screenshot_mode_button.clicked.connect(lambda: self.set_mode("screenshot"))
        self.live_mode_button.clicked.connect(lambda: self.set_mode("live"))
        mode_row.addWidget(self.screenshot_mode_button)
        mode_row.addWidget(self.live_mode_button)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        self.pages = QStackedWidget(self)
        self.screenshot_page = self._build_screenshot_page()
        self.live_page = self._build_live_page()
        self.pages.addWidget(self.screenshot_page)
        self.pages.addWidget(self.live_page)
        root.addWidget(self.pages, 1)

    def _build_screenshot_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        action_row = QHBoxLayout()
        self.screenshot_button = QPushButton(tr("框选屏幕并翻译"), page)
        self.screenshot_button.setObjectName("primaryButton")
        self.screenshot_button.setMinimumHeight(44)
        self.screenshot_button.clicked.connect(self.start_screenshot)
        self.screenshot_status = QLabel(tr("等待框选"), page)
        self.screenshot_status.setObjectName("secondaryLabel")
        action_row.addWidget(self.screenshot_button)
        action_row.addWidget(self.screenshot_status, 1)
        layout.addLayout(action_row)
        self.preview = OcrPreview(page)
        layout.addWidget(self.preview, 2)
        texts = QHBoxLayout()
        source_box, self.source_text = self._text_result_box(
            tr("识别原文"), tr("复制原文"), self._copy_source, page
        )
        target_box, self.translation_text = self._text_result_box(
            tr("译文"), tr("复制译文"), self._copy_translation, page
        )
        texts.addWidget(source_box, 1)
        texts.addWidget(target_box, 1)
        layout.addLayout(texts, 1)
        return page

    def _text_result_box(self, title, button_text, callback, parent):
        frame = QFrame(parent)
        frame.setObjectName("settingsCard")
        layout = QVBoxLayout(frame)
        header = QHBoxLayout()
        label = QLabel(title, frame)
        label.setObjectName("sectionTitle")
        button = QPushButton(button_text, frame)
        button.setObjectName("ghostButton")
        button.clicked.connect(callback)
        header.addWidget(label)
        header.addStretch(1)
        header.addWidget(button)
        edit = QPlainTextEdit(frame)
        edit.setObjectName("ocrTextResult")
        edit.setReadOnly(True)
        edit.setPlaceholderText(tr("框选后显示结果"))
        header.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(edit)
        return frame, edit

    def _build_live_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        explanation = QFrame(page)
        explanation.setObjectName("settingsCard")
        card = QVBoxLayout(explanation)
        heading = QLabel(tr("原位覆盖，不重复识别静止画面"), explanation)
        heading.setObjectName("sectionTitle")
        details = QLabel(tr(
            "选择一块游戏、视频、网页或应用区域。VoxSub 会保留文字坐标，"
            "用取自原图的背景色遮住原文，再在同一位置绘制译文。"
        ), explanation)
        details.setObjectName("secondaryLabel")
        details.setWordWrap(True)
        card.addWidget(heading)
        card.addWidget(details)
        layout.addWidget(explanation)
        action_row = QHBoxLayout()
        self.live_start_button = QPushButton(tr("选择区域并开始"), page)
        self.live_start_button.setObjectName("primaryButton")
        self.live_start_button.setMinimumHeight(44)
        self.live_start_button.clicked.connect(self.start_live)
        self.live_stop_button = QPushButton(tr("结束实时 OCR"), page)
        self.live_stop_button.setObjectName("secondaryButton")
        self.live_stop_button.setMinimumHeight(44)
        self.live_stop_button.clicked.connect(self.stop_live)
        self.live_stop_button.setEnabled(False)
        action_row.addWidget(self.live_start_button)
        action_row.addWidget(self.live_stop_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        self.live_status = QLabel(tr("未选择区域"), page)
        self.live_status.setObjectName("secondaryLabel")
        self.live_status.setWordWrap(True)
        layout.addWidget(self.live_status)
        privacy = QLabel(tr(
            "隐私：截图像素只在本机内存中送入 OCR；只有当你在设置中选择云翻译时，"
            "识别出的文字才会发送给对应翻译服务。"
        ), page)
        privacy.setObjectName("secondaryLabel")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        layout.addStretch(1)
        return page

    def set_mode(self, mode: str) -> None:
        live = mode == "live"
        self.pages.setCurrentWidget(self.live_page if live else self.screenshot_page)
        self.live_mode_button.setChecked(live)
        self.screenshot_mode_button.setChecked(not live)

    def _begin_pick(self, purpose: str) -> None:
        self._pick_purpose = purpose
        host = self.window()
        host_is_visible = bool(host.isVisible())
        self._host_was_visible = self._host_was_visible or host_is_visible
        if host_is_visible:
            host.hide()
        QTimer.singleShot(100, self._picker.begin)

    def start_screenshot(self) -> None:
        if self._worker.is_busy() or self._captures:
            self.screenshot_status.setText(tr("上一张截图仍在处理中"))
            return
        if self._live_rect is not None:
            self.stop_live(restore_host=False)
        self.set_mode("screenshot")
        self.screenshot_status.setText(tr("拖动选择区域，Esc 取消"))
        self._begin_pick("screenshot")

    def start_live(self) -> None:
        if self._worker.is_busy() or self._captures:
            self.live_status.setText(tr("OCR 正在处理中，请稍候"))
            return
        self.stop_live(restore_host=False)
        self.set_mode("live")
        self.live_status.setText(tr("拖动选择要持续翻译的区域，Esc 取消"))
        self._begin_pick("live")

    def _on_pick_cancelled(self) -> None:
        purpose, self._pick_purpose = self._pick_purpose, ""
        self._restore_host()
        label = self.live_status if purpose == "live" else self.screenshot_status
        label.setText(tr("已取消框选"))

    def _on_region_selected(self, rect: QRect) -> None:
        purpose, self._pick_purpose = self._pick_purpose, ""
        if purpose == "screenshot":
            self._capture_screenshot(rect)
            return
        if purpose == "live":
            self._start_live_region(rect)

    def _capture_screenshot(self, rect: QRect) -> None:
        try:
            captured = capture_screen_region(rect)
        except Exception as exc:  # noqa: BLE001 - OS capture boundary
            self.screenshot_status.setText(f"{tr('截图失败')}：{exc}")
            self._restore_host()
            return
        self._restore_host()
        self.preview.clear()
        self.source_text.clear()
        self.translation_text.clear()
        self.screenshot_status.setText(tr("正在识别并翻译…"))
        self._submit(captured, "screenshot")

    def _start_live_region(self, rect: QRect) -> None:
        try:
            captured = capture_screen_region(rect)
        except Exception as exc:  # noqa: BLE001 - OS capture boundary
            self.live_status.setText(f"{tr('截图失败')}：{exc}")
            self._restore_host()
            return
        self._live_rect = QRect(captured.global_rect)
        self._previous_fingerprint = None
        self._empty_live_frames = 0
        self._live_paused = False
        self._showing_original = False
        self._overlay.set_capture_region(self._live_rect)
        self._overlay.show()
        self._overlay.raise_()
        self._control.place_near(self._live_rect)
        self._control.show()
        self._control.raise_()
        self.live_stop_button.setEnabled(True)
        self.live_start_button.setText(tr("重选区域"))
        self.live_status.setText(tr("正在识别所选区域…"))
        self._control.set_status(tr("正在识别…"))
        self._live_timer.start()
        self._submit_live_capture(captured)

    def _capture_live_tick(self) -> None:
        if (
            self._live_rect is None
            or self._live_paused
            or self._worker.is_busy()
            or self._captures
        ):
            return
        overlay_needs_hide = (
            self._overlay.isVisible() and not self._overlay.capture_excluded
        )
        control_needs_hide = (
            self._control.isVisible()
            and not self._control.capture_excluded
            and self._control.geometry().intersects(self._live_rect)
        )
        if overlay_needs_hide or control_needs_hide:
            self._overlay.hide()
            if control_needs_hide:
                self._control.hide()
            QTimer.singleShot(45, self._capture_live_after_overlay_hide)
            return
        self._capture_live_after_overlay_hide()

    def _capture_live_after_overlay_hide(self) -> None:
        if (
            self._live_rect is None
            or self._live_paused
            or self._worker.is_busy()
            or self._captures
        ):
            return
        try:
            captured = capture_screen_region(self._live_rect)
        except Exception as exc:  # noqa: BLE001 - OS capture boundary
            self.live_status.setText(f"{tr('实时截图失败')}：{exc}")
            return
        if not self._showing_original:
            self._overlay.show()
        if not self._control.isVisible():
            self._control.show()
            self._control.raise_()
        self._submit_live_capture(captured)

    def _submit_live_capture(self, captured: CapturedRegion) -> None:
        image = qimage_to_bgr(captured.image)
        fingerprint = frame_fingerprint(image)
        if not materially_changed(
            self._previous_fingerprint, fingerprint, self.LIVE_CHANGE_THRESHOLD
        ):
            self._control.set_status(tr("画面稳定"))
            return
        if self._submit(captured, "live", image=image):
            self._previous_fingerprint = fingerprint
            self._control.set_status(tr("识别翻译中…"))

    def _submit(
        self, captured: CapturedRegion, purpose: str, *, image: np.ndarray | None = None
    ) -> bool:
        config = self._store.load()
        pair = str(config.get("lang_pair", "zh-en"))
        source_lang, target_lang = pair.split("-", 1)
        self._revision += 1
        revision = self._revision
        job = OcrJob(
            revision,
            purpose,
            image if image is not None else qimage_to_bgr(captured.image),
            source_lang,
            target_lang,
            dict(config),
        )
        if not self._worker.submit(job):
            return False
        self._captures[revision] = captured.image.copy()
        return True

    def _on_result(
        self, revision: int, purpose: str, frame: TranslatedOcrFrame, warning: str
    ) -> None:
        capture = self._captures.pop(revision, QImage())
        if revision != self._revision:
            return
        if purpose == "screenshot":
            self._show_screenshot_result(capture, frame, warning)
            return
        if purpose == "live" and self._live_rect is not None:
            self._show_live_result(capture, frame, warning)

    def _show_screenshot_result(
        self, capture: QImage, frame: TranslatedOcrFrame, warning: str
    ) -> None:
        self.preview.set_result(capture, frame)
        self.source_text.setPlainText(frame.source_text)
        self.translation_text.setPlainText(frame.translation_text)
        if warning:
            self.screenshot_status.setText(warning)
        elif not frame.lines:
            self.screenshot_status.setText(tr("没有检测到文字，请放大区域或提高对比度"))
        else:
            self.screenshot_status.setText(tr(
                f"完成 · {len(frame.lines)} 行 · OCR {frame.ocr_elapsed_ms}ms · "
                f"翻译 {frame.translate_elapsed_ms}ms",
                f"Done · {len(frame.lines)} lines · OCR {frame.ocr_elapsed_ms}ms · "
                f"translation {frame.translate_elapsed_ms}ms",
            ))

    def _show_live_result(
        self, capture: QImage, frame: TranslatedOcrFrame, warning: str
    ) -> None:
        if not frame.lines:
            self._empty_live_frames += 1
            if self._empty_live_frames < 2:
                self._control.set_status(tr("未检测到文字，等待下一帧"))
                return
        else:
            self._empty_live_frames = 0
        self._overlay.set_frame(frame, capture)
        if not self._showing_original:
            self._overlay.show()
        status = warning or tr(
            f"{len(frame.lines)} 行 · OCR {frame.ocr_elapsed_ms}ms · "
            f"翻译 {frame.translate_elapsed_ms}ms",
            f"{len(frame.lines)} lines · OCR {frame.ocr_elapsed_ms}ms · "
            f"translation {frame.translate_elapsed_ms}ms",
        )
        self.live_status.setText(status)
        self._control.set_status(status)

    def _on_failure(self, revision: int, purpose: str, error: str) -> None:
        self._captures.pop(revision, None)
        if revision != self._revision:
            return
        self._previous_fingerprint = None
        label = self.live_status if purpose == "live" else self.screenshot_status
        label.setText(f"{tr('OCR 失败')}：{error}")
        if purpose == "live":
            self._control.set_status(tr("OCR 失败，画面变化后重试"))

    def _on_live_paused(self, paused: bool) -> None:
        self._live_paused = bool(paused)
        status = tr("已暂停，覆盖译文保持显示") if paused else tr("已继续，等待画面变化")
        self.live_status.setText(status)
        self._control.set_status(status)

    def _on_original_changed(self, showing_original: bool) -> None:
        self._showing_original = bool(showing_original)
        self._overlay.setVisible(not self._showing_original and self._live_rect is not None)

    def _reselect_live(self) -> None:
        self.stop_live(restore_host=False)
        self.set_mode("live")
        self._begin_pick("live")

    def stop_live(self, restore_host: bool = True) -> None:
        self._live_timer.stop()
        self._live_rect = None
        self._live_paused = False
        self._previous_fingerprint = None
        self._revision += 1
        self._captures.clear()
        self._overlay.hide()
        self._control.hide()
        self.live_stop_button.setEnabled(False)
        self.live_start_button.setText(tr("选择区域并开始"))
        self.live_status.setText(tr("实时 OCR 已结束"))
        if restore_host:
            self._restore_host()

    def _restore_host(self) -> None:
        host = self.window()
        if self._host_was_visible and not host.isVisible():
            host.show()
            host.raise_()
            host.activateWindow()
        self._host_was_visible = False

    def _copy_source(self) -> None:
        QApplication.clipboard().setText(self.source_text.toPlainText())

    def _copy_translation(self) -> None:
        QApplication.clipboard().setText(self.translation_text.toPlainText())

    def shutdown(self) -> None:
        self._picker.cancel(emit_signal=False)
        self.stop_live(restore_host=False)
        self._worker.shutdown()
        self._overlay.close()
        self._control.close()


__all__ = ["OcrPreview", "OcrWorkspace"]
