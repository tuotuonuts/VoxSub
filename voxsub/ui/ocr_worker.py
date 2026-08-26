"""Bounded single-owner worker for OCR and OCR-line translation."""
from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict
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
    prepared = Signal(bool, str)


def _untranslated_frame(frame: OcrFrame) -> TranslatedOcrFrame:
    return TranslatedOcrFrame(
        width=frame.width,
        height=frame.height,
        lines=tuple(
            TranslatedOcrLine(line.box, line.text, "", line.confidence)
            for line in frame.lines
        ),
        ocr_elapsed_ms=frame.elapsed_ms,
        translate_elapsed_ms=0,
        failed_lines=len(frame.lines),
        ocr_backend=frame.backend,
        ocr_model_id=frame.model_id,
        translation_requests=0,
    )


class OcrWorker:
    """Serialize native models and keep at most one pending captured frame."""

    def __init__(self, bridge: OcrWorkerBridge) -> None:
        self._bridge = bridge
        self._requests: queue.Queue[OcrJob | None] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._latest_revision = -1
        self._thread = threading.Thread(
            target=self._run, name="ocr-translation", daemon=True
        )
        self._thread.start()

    def submit(self, job: OcrJob, *, replace_pending: bool = False) -> bool:
        if self._stop.is_set() or (self._busy.is_set() and not replace_pending):
            return False
        if job.revision >= 0:
            self._latest_revision = job.revision
        self._busy.set()
        try:
            self._requests.put_nowait(job)
            return True
        except queue.Full:
            if not replace_pending:
                self._busy.clear()
                return False
            # A live capture is only useful if it is the newest screen.  Drop
            # the one pending frame, keeping the currently running native
            # inference untouched and avoiding an ever-growing backlog.
            try:
                self._requests.get_nowait()
            except queue.Empty:
                self._busy.clear()
                return False
            try:
                self._requests.put_nowait(job)
                return True
            except queue.Full:
                self._busy.clear()
                return False

    def prepare(
        self, config: Mapping[str, Any], source_lang: str, target_lang: str
    ) -> bool:
        """Warm the fast OCR and translation runtimes on the owner thread."""
        try:
            import cv2

            image = np.full((360, 960, 3), 248, dtype=np.uint8)
            for index, text in enumerate((
                "VoxSub live OCR preview",
                "Context aware screen translation",
                "Fast first frame and accurate refinement",
            )):
                cv2.putText(
                    image, text, (32, 88 + index * 92),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2,
                    cv2.LINE_AA,
                )
        except Exception:  # noqa: BLE001 - OpenCV is optional at import time
            image = np.full((360, 960, 3), 248, dtype=np.uint8)
            image[72:78, 32:720] = 20
            image[164:170, 32:820] = 20
            image[256:262, 32:900] = 20
        return self.submit(OcrJob(
            -1, "prepare", image, source_lang, target_lang, dict(config)))

    def is_busy(self) -> bool:
        return self._busy.is_set()

    def _is_stale(self, job: OcrJob) -> bool:
        return (
            self._stop.is_set()
            or job.revision >= 0
            and job.revision != self._latest_revision
        )

    def _run(self) -> None:
        engines: OrderedDict[tuple[str, str, bool, bool, float], RapidOcrEngine] = (
            OrderedDict())
        translator = OcrTranslationService()
        while not self._stop.is_set():
            job = self._requests.get()
            if job is None:
                break
            if self._is_stale(job):
                self._busy.clear()
                continue
            requested_key = (
                str(job.config.get("ocr_model_id", "")),
                str(job.config.get("models_root", "")),
                bool(job.config.get("ocr_live_mode", False)),
                bool(job.config.get("ocr_refinement_mode", False)),
                float(job.config.get("ocr_minimum_confidence", 0.52)),
            )
            engine = engines.get(requested_key)
            if engine is None:
                try:
                    engine = RapidOcrEngine(
                        config=job.config,
                        minimum_confidence=requested_key[-1],
                    )
                    engines[requested_key] = engine
                    engines.move_to_end(requested_key)
                    while len(engines) > 3:
                        engines.popitem(last=False)
                except Exception as exc:  # noqa: BLE001 - model boundary
                    logger.exception("OCR 模型配置失败")
                    if job.purpose == "prepare":
                        self._bridge.prepared.emit(False, str(exc))
                    else:
                        self._bridge.failed.emit(
                            job.revision, job.purpose, str(exc))
                    self._busy.clear()
                    continue
            else:
                engines.move_to_end(requested_key)
            self._process_job(engine, translator, job)
            self._busy.clear()
        translator.close()

    def _process_job(
        self, engine: RapidOcrEngine, translator: OcrTranslationService, job: OcrJob
    ) -> None:
        started = time.perf_counter()
        try:
            recognized = engine.recognize(job.image)
            if job.purpose == "prepare":
                translator.warmup(job.config)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    "OCR 模式预热完成: model=%s backend=%s elapsed_ms=%d",
                    recognized.model_id, recognized.backend, elapsed_ms,
                )
                self._bridge.prepared.emit(
                    True, f"{recognized.model_id} / {recognized.backend}")
                return
            if self._is_stale(job):
                logger.debug(
                    "丢弃过期 OCR 任务: purpose=%s revision=%d latest=%d",
                    job.purpose, job.revision, self._latest_revision)
                return
            result, warning = self._translate_or_retain(
                recognized, translator, job
            )
            if self._is_stale(job):
                logger.debug(
                    "丢弃过期 OCR 结果: purpose=%s revision=%d latest=%d",
                    job.purpose, job.revision, self._latest_revision)
                return
            logger.info(
                "OCR 帧完成: purpose=%s revision=%d lines=%d model=%s "
                "backend=%s ocr_ms=%d translate_ms=%d requests=%d total_ms=%d",
                job.purpose,
                job.revision,
                len(result.lines),
                result.ocr_model_id,
                result.ocr_backend,
                result.ocr_elapsed_ms,
                result.translate_elapsed_ms,
                result.translation_requests,
                int((time.perf_counter() - started) * 1000),
            )
            self._bridge.result_ready.emit(job.revision, job.purpose, result, warning)
        except Exception as exc:  # noqa: BLE001 - worker boundary
            logger.exception("OCR 后台任务失败")
            if job.purpose == "prepare":
                self._bridge.prepared.emit(False, str(exc))
            else:
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
                maximum_lines=int(job.config.get("ocr_maximum_lines", 48)),
                maximum_characters=int(
                    job.config.get("ocr_maximum_characters", 3000)),
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
