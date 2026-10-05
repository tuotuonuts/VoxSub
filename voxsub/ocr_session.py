"""Serialized Electron OCR runtime. One live engine, one bounded exact-frame cache.

No screen capture here; caller supplies RGB arrays. Pixels and bodies stay in
memory. Static requests release their engine; live engines close on replacement,
explicit release or backend shutdown. Translation service owns only its runtimes.
"""
from __future__ import annotations
import hashlib
import threading
import time
from typing import Any
import numpy as np
from voxsub import ocr
from voxsub.logging_setup import get_logger
from voxsub.diagnostic_trace import record, traced_stage

logger = get_logger("ocr.session")


class OcrSession:
    def __init__(self, translator_provider=None, *, clock=time.monotonic):
        self._lock = threading.RLock()
        self._clock = clock
        self._engine = None
        self._engine_key = None
        self._cache_key = None
        self._cached = None
        self._cached_at = 0.0
        self._translations = ocr.OcrTranslationService(translator_provider=translator_provider)
        self._closed = False

    def _release_engine(self):
        engine, self._engine = self._engine, None
        self._engine_key = None
        self._close_engine(engine)

    @staticmethod
    def _close_engine(engine):
        if engine is None:
            return
        try:
            engine.close()
        except Exception as error:
            logger.warning("OCR engine release failed: error_type=%s", type(error).__name__)

    @traced_stage("ocr_recognition")
    def _recognize(self, pixels, config, live):
        key = (tuple(str(config.get(name, "")) for name in ("ocr_model_id", "models_root", "models_root_mode")),
               config.get("ocr_minimum_confidence", .52))
        if not live:
            engine = ocr.RapidOcrEngine(config=config, minimum_confidence=float(key[1]))
            try:
                return engine.recognize(pixels)
            finally:
                self._close_engine(engine)
        if self._engine is None or key != self._engine_key:
            self._release_engine()
            self._engine = ocr.RapidOcrEngine(config=config, minimum_confidence=float(key[1]))
            self._engine_key = key
        try:
            return self._engine.recognize(pixels)
        except Exception:
            self._release_engine()
            self._cache_key = None
            raise

    def recognize(self, pixels: np.ndarray, config: dict, *, source="auto", target="zh", translate=True, live=False) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ocr.OcrRuntimeClosedError("OCR session is closed")
            prepared = {**config, "ocr_live_mode": live, "ocr_group_paragraphs": False}
            key = (pixels.shape, hashlib.sha256(pixels.tobytes()).digest(),
                   ocr.ocr_result_signature(prepared, source_lang=source, target_lang=target), bool(translate))
            if live and key == self._cache_key and self._clock() - self._cached_at < 2.0:
                return {**self._cached, "unchanged": True}
            started = self._clock()
            result = self._recognize(pixels, prepared, live)
            frame = ocr.OcrFrame(pixels.shape[1], pixels.shape[0], tuple(result.lines),
                                 int((self._clock() - started) * 1000),
                                 getattr(result, "backend", "CPU"), getattr(result, "model_id", ""))
            record("ocr_recognition", "ok", model=frame.model_id, provider=frame.backend,
                   output_chars=sum(len(line.text) for line in frame.lines), duration_ms=frame.elapsed_ms)
            output = self._translate(frame, prepared, source, target, translate)
            self._cache_key = key if live and not output["failedLines"] else None
            self._cached = output if self._cache_key else None
            self._cached_at = self._clock()
            return output

    @traced_stage("ocr_translation")
    def _translate(self, frame, config, source, target, enabled):
        lines = [{"text": line.text, "translation": "", "box": [line.box.left, line.box.top, line.box.right, line.box.bottom],
                  "confidence": line.confidence} for line in frame.lines]
        elapsed, requests, failures = 0, 0, 0
        if enabled and lines:
            try:
                translated = self._translations.translate_frame(frame, source, target, config,
                    maximum_lines=int(config.get("ocr_maximum_lines", 24 if config.get("ocr_live_mode") else 48)),
                    maximum_characters=int(config.get("ocr_maximum_characters", 2400 if config.get("ocr_live_mode") else 6000)))
                targets = {line.box: line.translation for line in translated.lines}
                for line, raw in zip(lines, frame.lines):
                    line["translation"] = targets.get(raw.box, "")
                elapsed, requests = translated.translate_elapsed_ms, translated.translation_requests
                failures = translated.failed_lines
            except Exception as error:
                # Do not include exception messages: adapters may embed bodies/keys.
                logger.warning("OCR translation unavailable: error_type=%s lines=%d", type(error).__name__, len(lines))
                failures = len(lines)
        record("ocr_translation", "failed" if failures else "ok", source=source, target=target,
               input_chars=sum(len(line["text"]) for line in lines),
               output_chars=sum(len(line["translation"]) for line in lines), duration_ms=elapsed,
               error_code="partial_translation" if failures else "")
        return {"text": "\n".join(line["text"] for line in lines),
                "translation": "\n".join(line["translation"] for line in lines if line["translation"]),
                "lines": lines, "width": frame.width, "height": frame.height,
                "ocrElapsedMs": frame.elapsed_ms, "translateElapsedMs": elapsed,
                "failedLines": failures, "untranslatedLines": sum(not line["translation"] for line in lines) if enabled else 0,
                "translationRequests": requests,
                "ocrBackend": frame.backend, "ocrModelId": frame.model_id, "unchanged": False}

    def release_live(self):
        with self._lock:
            self._release_engine()
            self._cache_key = self._cached = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.release_live()
            self._translations.close()
