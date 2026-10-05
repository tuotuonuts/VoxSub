"""Silent real-model, real-time-paced PCM acceptance (no audio devices/UI).

Uses installed models read-only. Replays public model-package samples, not a
full video/WASAPI test. Writes counts/timings only, never recognition bodies.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def read_sample(path):
    import numpy as np
    from voxsub.audio import resample_16k
    with wave.open(str(path), "rb") as stream:
        if stream.getsampwidth() != 2:
            raise ValueError("Acceptance requires PCM16 sample")
        pcm = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        pcm = pcm.reshape(-1, stream.getnchannels()).mean(axis=1)
        return resample_16k(pcm, stream.getframerate())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=900)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(args.output / "local")
    os.environ["APPDATA"] = str(args.output / "roaming")
    import numpy as np
    import psutil
    from voxsub.asr import StreamingASR, WindowVAD, UtteranceSegmenter
    from voxsub.pipeline import Pipeline, PipelineState
    from voxsub.translate.opus import OpusFastTranslator
    from voxsub.contextual_text import ContextualTextProcessor
    from voxsub.diagnostic_trace import snapshot
    from voxsub.file_io import write_text_atomically

    paths = [args.models / "stt/sensevoice-small-int8/test_wavs/en.wav",
             args.models / "stt/qwen3-asr-0.6b-int8/test_wavs/noise1-en.wav"]
    samples = [read_sample(p) for p in paths]
    pipe = Pipeline(provider="cpu", models=args.models)
    pipe.set_langs("en", "zh")
    pipe.set_asr_tuning({"profile": "context"})
    pipe._mode = "b"
    pipe._diagnostic_session_id = "silent-acceptance"
    pipe._translator = OpusFastTranslator(args.models / "translate/opus", threads=2, providers=["CPUExecutionProvider"])
    pipe._trans_kind = "opus-fast"
    pipe._trans_pair = "en-zh"
    started = time.monotonic()
    pipe._translator.translate("Hello, how are you?", "en", "zh")
    warmup_ms = (time.monotonic() - started) * 1000
    asr = StreamingASR(args.models / "stt/zipformer", provider="cpu", num_threads=2, decoding_method="greedy_search")
    vad = WindowVAD(str(next((args.models / "vad").glob("*.onnx"))), threshold=0.32)
    pipe._asr, pipe._vad = asr, vad
    pipe._context_processor = ContextualTextProcessor(source_lang="en", hold_ms=1800, defer_incomplete=False)
    pipe._seg = UtteranceSegmenter(asr, vad, pipe._on_sentence, min_silence_ms=500,
                                  max_utterance_ms=18000, on_partial=pipe._on_asr_partial,
                                  partial_interval_ms=140,
                                  boundary_decider=pipe._context_processor.should_defer_endpoint,
                                  semantic_hold_ms=1800)
    counts = {"partials": 0, "draft_updates": 0, "translated_drafts": 0, "finals": 0, "empty_finals": 0, "language_rejections": 0}
    first_partials = {}
    first_drafts = []
    draft_sentences = set()
    def partial(text):
        counts["partials"] += 1
        first_partials.setdefault(pipe._live_draft.sentence_id, time.monotonic())
    def draft(source, target):
        counts["draft_updates"] += 1
        counts["translated_drafts"] += bool(target)
        sid = pipe._live_draft.sentence_id
        if target and sid in first_partials and sid not in draft_sentences:
            first_drafts.append((time.monotonic() - first_partials[sid]) * 1000)
            draft_sentences.add(sid)
    def final(source, target):
        counts["finals"] += 1
        counts["empty_finals"] += not bool(target)
    pipe.on_status(lambda message: counts.__setitem__("language_rejections", counts["language_rejections"] + (message == "识别到其他语言，已忽略当前片段")))
    pipe.on_partial(partial)
    pipe.on_draft(draft)
    pipe.on_utterance(final)
    pipe._set_state(PipelineState.RUNNING)
    threads = [threading.Thread(target=fn, name=name, daemon=True) for name, fn in
               (("accept-process", pipe._process_loop), ("accept-context", pipe._context_loop), ("accept-translate", pipe._translation_loop))]
    pipe._threads = threads
    for thread in threads:
        thread.start()
    sequence = np.concatenate([np.concatenate([pcm, np.zeros(16000, dtype=np.float32)]) for pcm in samples])
    duration_frames = int(args.duration * 16000)
    wall_start = time.monotonic()
    captured = 0
    peaks = {"capture": 0, "context": 0, "translation": 0}
    windows = []
    next_report = 60.0
    try:
        while captured < duration_frames and not pipe._stop_evt.is_set():
            size = min(480, duration_frames - captured)
            offset = captured % len(sequence)
            indices = (np.arange(size) + offset) % len(sequence)
            pipe._accept_capture_chunk(sequence[indices])
            captured += size
            for name, queue in (("capture", pipe._queue), ("context", pipe._context_queue), ("translation", pipe._translation_queue)):
                peaks[name] = max(peaks[name], queue.qsize())
            remaining = wall_start + captured / 16000 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            elapsed = time.monotonic() - wall_start
            if elapsed >= next_report:
                windows.append({"elapsed_s": round(elapsed, 2), "counts": dict(counts), "rss_mb": round(psutil.Process().memory_info().rss / 2**20, 2),
                                "latency": getattr(pipe, "_latency", None).snapshot() if hasattr(pipe, "_latency") else None})
                write_text_atomically(args.output / "progress.json", json.dumps(windows[-1], indent=2))
                next_report += 60
    finally:
        feed_wall_s = time.monotonic() - wall_start
        pipe._stop_evt.set()
        for thread in threads:
            thread.join(timeout=30)
        alive = [thread.name for thread in threads if thread.is_alive()]
        trace = snapshot()
        report = {"boundary": "Actual CPU Zipformer/VAD/OPUS; public English samples repeated at 1x PCM cadence. No WASAPI, UI, speakers, mic, cloud or user config. No WER/reference-quality claim.",
                  "requested_s": args.duration, "audio_s": captured / 16000, "feed_wall_s": round(feed_wall_s, 3), "drain_wall_s": round(time.monotonic() - wall_start - feed_wall_s, 3),
                  "warmup_ms": round(warmup_ms, 2), "counts": counts,
                  "first_draft_from_first_partial_ms": {"samples": len(first_drafts), "p50": float(np.percentile(first_drafts, 50)) if first_drafts else None, "p95": float(np.percentile(first_drafts, 95)) if first_drafts else None},
                  "quality_status": "NOT_RUN: no reference alignment/WER/translation review; language guard rejection counts are reported, not hidden", "queue_peaks": peaks, "workers_alive": alive,
                  "state": pipe.state.value, "windows": windows, "latency": pipe._latency.snapshot() if hasattr(pipe, "_latency") else None,
                  "recent_trace": trace, "rss_mb": round(psutil.Process().memory_info().rss / 2**20, 2)}
        passed = captured == duration_frames and counts["finals"] > 0 and counts["empty_finals"] == 0 and not alive and pipe.state != PipelineState.FAILED
        report["status"] = "PASS" if passed else "FAIL"
        write_text_atomically(args.output / "result.json", json.dumps(report, indent=2))
        print(json.dumps({key: value for key, value in report.items() if key not in {"windows", "recent_trace", "latency"}}))
        if not alive:
            pipe.close()
    return 0 if passed else 1

if __name__ == "__main__":
    raise SystemExit(main())
