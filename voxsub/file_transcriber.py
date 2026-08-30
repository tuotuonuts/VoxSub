"""File-audio decoding and recognition, independent of live pipeline state."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from voxsub.audio import resample_16k
from voxsub.language_guard import guard_text
from voxsub.logging_setup import get_logger
from voxsub.subtitles import SubtitleLine

logger = get_logger("file_transcriber")
SAMPLE_RATE = 16_000
ProgressCallback = Callable[[int, int, str], None]


class FileAudioDecoder:
    """Decode WAV directly or use the packaged ffmpeg for other containers."""

    @classmethod
    def decode(cls, path: Path) -> tuple[np.ndarray, Path | None]:
        temporary: Path | None = None
        if path.suffix.lower() == ".wav":
            pcm, sample_rate = cls.read_wav(path)
        else:
            ffmpeg = cls.find_ffmpeg()
            if ffmpeg is None:
                raise RuntimeError("视频/压缩音频提取组件 ffmpeg 未随程序安装")
            with tempfile.NamedTemporaryFile(
                prefix="voxsub-extract-", suffix=".wav", delete=False,
            ) as handle:
                temporary = Path(handle.name)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(path), "-vn", "-ar", "16000", "-ac", "1",
                 "-c:a", "pcm_s16le", str(temporary)],
                check=False,
                capture_output=True,
                creationflags=flags,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"无法从文件提取音频: {detail[-500:]}")
            pcm, sample_rate = cls.read_wav(temporary)
        if sample_rate != SAMPLE_RATE:
            pcm = resample_16k(pcm, sample_rate)
        return pcm, temporary

    @staticmethod
    def find_ffmpeg() -> Path | None:
        candidates: list[Path] = []
        if getattr(sys, "frozen", False):
            executable_dir = Path(sys.executable).resolve().parent
            candidates.extend([
                executable_dir / "tools" / "ffmpeg.exe",
                executable_dir / "ffmpeg.exe",
            ])
            bundle = getattr(sys, "_MEIPASS", None)
            if bundle:
                candidates.append(Path(bundle) / "tools" / "ffmpeg.exe")
        candidates.extend([
            Path(__file__).resolve().parent.parent / "tools" / "ffmpeg.exe",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) /
            "VoxSub" / "tools" / "ffmpeg.exe",
        ])
        found = shutil.which("ffmpeg")
        if found:
            candidates.append(Path(found))
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    @staticmethod
    def read_wav(path: Path) -> tuple[np.ndarray, int]:
        with wave.open(str(path), "rb") as source:
            sample_rate = source.getframerate()
            channels = source.getnchannels()
            width = source.getsampwidth()
            raw = source.readframes(source.getnframes())
        if width == 1:
            data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif width == 2:
            data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 3:
            packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            values = (packed[:, 0].astype(np.int32) |
                      (packed[:, 1].astype(np.int32) << 8) |
                      (packed[:, 2].astype(np.int32) << 16))
            values = np.where(values & 0x800000, values - 0x1000000, values)
            data = values.astype(np.float32) / 8388608.0
        elif width == 4:
            data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise RuntimeError(f"不支持的 WAV 位深: {width * 8} bit")
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        return data, sample_rate


class FileRecognizer:
    """Pure local/cloud file recognition using already-built runtime objects."""

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        completed: int,
        label: str,
        last_completed: list[int],
    ) -> None:
        """Emit at most one UI update per percentage point from worker loops."""
        value = max(0, min(100, int(completed)))
        if callback is None or value <= last_completed[0]:
            return
        last_completed[0] = value
        callback(value, 100, label)

    @staticmethod
    def local(
        pcm: np.ndarray,
        *,
        vad: Any,
        asr: Any,
        translator: Any,
        source_lang: str,
        target_lang: str,
        validate_translation: bool,
        tuning: Mapping[str, Any] | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[SubtitleLine]:
        lines: list[SubtitleLine] = []
        stream = asr.create_stream()
        segment_start: int | None = None
        silence = 0
        utterance_samples = 0
        segment_no = 0
        tuning = tuning or {}
        minimum_silence = int(
            SAMPLE_RATE * max(50.0, min(5000.0, float(
                tuning.get("silence_ms", 500)))) / 1000.0
        )
        maximum_utterance = int(
            SAMPLE_RATE * max(1000.0, min(120000.0, float(
                tuning.get("max_utterance_ms", 12000)))) / 1000.0
        )
        window = vad.window_size
        last_progress = [-1]
        FileRecognizer._progress(progress, 10, "正在识别音视频", last_progress)

        for index in range(0, pcm.size - window + 1, window):
            chunk = pcm[index:index + window]
            if vad.is_speech(chunk):
                if segment_start is None:
                    segment_start = index
                    utterance_samples = 0
                silence = 0
                asr.feed(stream, chunk)
                utterance_samples += window
            elif segment_start is not None:
                asr.feed(stream, chunk)
                silence += window
                utterance_samples += window
                if (silence >= minimum_silence or
                        utterance_samples >= maximum_utterance):
                    segment_no += 1
                    FileRecognizer._append_local_result(
                        lines, asr, stream, segment_start, source_lang,
                        segment_no=segment_no,
                        reason=("pause" if silence >= minimum_silence else "limit"),
                    )
                    asr.reset(stream)
                    segment_start = None
                    silence = 0
                    utterance_samples = 0
                    vad.reset()
            # A continuous speech run has no silence window to trigger the
            # branch above; enforce the same safety boundary while speech is
            # still active so generative decoders never receive unbounded audio.
            if segment_start is not None and utterance_samples >= maximum_utterance:
                segment_no += 1
                FileRecognizer._append_local_result(
                    lines, asr, stream, segment_start, source_lang,
                    segment_no=segment_no, reason="limit",
                )
                asr.reset(stream)
                segment_start = None
                silence = 0
                utterance_samples = 0
                vad.reset()
            FileRecognizer._progress(
                progress,
                10 + int(65 * min(index + window, pcm.size) / max(1, pcm.size)),
                "正在识别音视频",
                last_progress,
            )
        if segment_start is not None:
            segment_no += 1
            FileRecognizer._append_local_result(
                lines, asr, stream, segment_start, source_lang,
                segment_no=segment_no, reason="flush",
            )
        FileRecognizer._progress(progress, 75, "正在翻译字幕", last_progress)
        FileRecognizer._translate(
            lines, translator, source_lang, target_lang, validate_translation,
            progress=progress, start_progress=75, end_progress=95,
            last_progress=last_progress,
        )
        return lines

    @staticmethod
    def _append_local_result(
        lines: list[SubtitleLine],
        asr: Any,
        stream: Any,
        segment_start: int,
        source_lang: str,
        *,
        segment_no: int = 0,
        reason: str = "pause",
    ) -> None:
        started = time.perf_counter()
        chunks = getattr(stream, "chunks", None)
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        text = asr.decode(stream).strip()
        logger.info(
            "文件 ASR 片段完成: segment=%d reason=%s audio_ms=%.1f "
            "peak=%.4f rms=%.4f decode_ms=%.1f chars=%d",
            segment_no, reason, audio.size * 1000.0 / SAMPLE_RATE,
            peak, rms, (time.perf_counter() - started) * 1000.0, len(text),
        )
        try:
            text = guard_text(text, source_lang, kind="STT")
        except ValueError as exc:
            logger.warning("文件 STT 结果被语言约束拦截: source=%s text=%r reason=%s",
                           source_lang, text[:160], exc)
            return
        if text:
            lines.append(SubtitleLine(
                text=text,
                ts_ms=int(segment_start * 1000 / SAMPLE_RATE),
            ))

    @staticmethod
    def cloud(
        pcm: np.ndarray,
        *,
        vad: Any,
        cloud_stt: Any,
        translator: Any,
        source_lang: str,
        target_lang: str,
        tuning: Mapping[str, Any],
        validate_translation: bool,
        progress: ProgressCallback | None = None,
    ) -> list[SubtitleLine]:
        minimum_silence = int(SAMPLE_RATE * int(tuning["silence_ms"]) / 1000)
        maximum_utterance = int(
            SAMPLE_RATE * int(tuning["max_utterance_ms"]) / 1000)
        window = vad.window_size
        segments: list[tuple[int, np.ndarray]] = []
        current: list[np.ndarray] = []
        current_samples = 0
        start_sample: int | None = None
        silence = 0
        last_progress = [-1]
        FileRecognizer._progress(progress, 10, "正在识别音视频", last_progress)

        def finish() -> None:
            nonlocal current, current_samples, start_sample, silence
            if current and start_sample is not None:
                segments.append((start_sample, np.concatenate(current)))
            current = []
            current_samples = 0
            start_sample = None
            silence = 0
            vad.reset()

        for index in range(0, pcm.size - window + 1, window):
            chunk = pcm[index:index + window]
            if vad.is_speech(chunk):
                if start_sample is None:
                    start_sample = index
                current.append(chunk.copy())
                current_samples += len(chunk)
                silence = 0
            elif start_sample is not None:
                current.append(chunk.copy())
                current_samples += len(chunk)
                silence += window
                if silence >= minimum_silence or current_samples >= maximum_utterance:
                    finish()
            FileRecognizer._progress(
                progress,
                10 + int(25 * min(index + window, pcm.size) / max(1, pcm.size)),
                "正在识别音视频",
                last_progress,
            )
        if start_sample is not None:
            finish()

        lines: list[SubtitleLine] = []
        segment_total = max(1, len(segments))
        for index, (start, audio) in enumerate(segments, start=1):
            try:
                text = cloud_stt.transcribe_samples(
                    audio, source_lang=source_lang).strip()
                text = guard_text(text, source_lang, kind="cloud STT")
            except Exception as exc:
                logger.error("云 STT 文件片段失败: %s", exc, exc_info=True)
                continue
            if text:
                lines.append(SubtitleLine(
                    text=text,
                    ts_ms=int(start * 1000 / SAMPLE_RATE),
                ))
            FileRecognizer._progress(
                progress, 35 + int(40 * index / segment_total),
                "正在识别音视频", last_progress,
            )
        FileRecognizer._progress(progress, 75, "正在翻译字幕", last_progress)
        FileRecognizer._translate(
            lines, translator, source_lang, target_lang, validate_translation,
            progress=progress, start_progress=75, end_progress=95,
            last_progress=last_progress,
        )
        return lines

    @staticmethod
    def _translate(
        lines: list[SubtitleLine],
        translator: Any,
        source_lang: str,
        target_lang: str,
        validate_translation: bool,
        *,
        progress: ProgressCallback | None = None,
        start_progress: int = 0,
        end_progress: int = 100,
        last_progress: list[int] | None = None,
    ) -> None:
        emitted = last_progress if last_progress is not None else [-1]
        total = max(1, len(lines))
        for index, line in enumerate(lines, start=1):
            try:
                line.translation = translator.translate(
                    line.text, source_lang, target_lang)
                if validate_translation:
                    line.translation = guard_text(
                        line.translation, target_lang, kind="translation")
            except Exception:
                line.translation = line.text + " 〔翻译失败〕"
            FileRecognizer._progress(
                progress,
                start_progress + int((end_progress - start_progress) * index / total),
                "正在翻译字幕",
                emitted,
            )
        if not lines:
            FileRecognizer._progress(progress, end_progress, "正在翻译字幕", emitted)


__all__ = ["FileAudioDecoder", "FileRecognizer"]
