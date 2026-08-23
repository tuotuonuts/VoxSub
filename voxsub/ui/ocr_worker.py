"""Bounded single-owner worker for OCR and OCR-line translation."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
from PySide6.QtCore import QObject, Signal

from voxsub.logging_setup import get_logger
from voxsub.ocr import (
    OcrFrame,
    OcrTranslationService,
    RapidOcrEngine,
    TranslatedOcrFrame,
    TranslatedOcrLine,
)

logger = get_logger("ui.ocr_worker")


@dataclass(frozen=True)
class OcrJob:
    revision: int
    purpose: str
    image: np.ndarray
    source_lang: str
    target_lang: str
    config: Mapping[str, Any]


class OcrWorkerBridge(QObject):
    result_ready = Signal(int, str, object, str)
    failed = Signal(int, str, str)


def _untranslated_frame(frame: OcrFrame) -> TranslatedOcrFrame:
    return TranslatedOcrFrame(
        frame.width,
        frame.height,
        tuple(
            TranslatedOcrLine(line.box, line.text, "", line.confidence)
            for line in frame.lines
        ),
        frame.elapsed_ms,
        0,
        len(frame.lines),
    )


class OcrWorker:
    """Serialize native models and keep at most one pending captured frame."""

    def __init__(self, bridge: OcrWorkerBridge) -> None:
        self._bridge = bridge
        self._requests: queue.Queue[OcrJob | None] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="ocr-translation", daemon=True
        )
        self._thread.start()

    def submit(self, job: OcrJob) -> bool:
        if self._stop.is_set() or self._busy.is_set():
            return False
        try:
            self._requests.put_nowait(job)
            self._busy.set()
            return True
        except queue.Full:
            return False

    def is_busy(self) -> bool:
        return self._busy.is_set()

    def _run(self) -> None:
        engine = RapidOcrEngine()
        translator = OcrTranslationService()
        while not self._stop.is_set():
            job = self._requests.get()
            if job is None:
                break
            self._process_job(engine, translator, job)
            self._busy.clear()
        translator.close()

    def _process_job(
        self, engine: RapidOcrEngine, translator: OcrTranslationService, job: OcrJob
    ) -> None:
        try:
            recognized = engine.recognize(job.image)
            result, warning = self._translate_or_retain(
                recognized, translator, job
            )
            self._bridge.result_ready.emit(job.revision, job.purpose, result, warning)
        except Exception as exc:  # noqa: BLE001 - worker boundary
            logger.exception("OCR 后台任务失败")
            self._bridge.failed.emit(job.revision, job.purpose, str(exc))

    @staticmethod
    def _translate_or_retain(
        recognized: OcrFrame, translator: OcrTranslationService, job: OcrJob
    ) -> tuple[TranslatedOcrFrame, str]:
        if not recognized.lines:
            return _untranslated_frame(recognized), ""
        try:
            return translator.translate_frame(
                recognized,
                job.source_lang,
                job.target_lang,
                job.config,
            ), ""
        except Exception as exc:  # noqa: BLE001 - retain OCR-only result
            logger.exception("OCR 文本翻译失败，保留识别结果")
            return _untranslated_frame(recognized), f"OCR 已完成，但翻译失败：{exc}"

    def shutdown(self) -> None:
        self._stop.set()
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            logger.warning("OCR 工作线程未在 2 秒内退出，将随进程结束")


__all__ = ["OcrJob", "OcrWorker", "OcrWorkerBridge"]
