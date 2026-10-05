"""Silent read-only adapter checks and prerecorded partial-cadence comparison.

No device enumeration, recording, playback, UI, downloads, user configuration,
cloud calls, or user audio. Only model-package public fixtures, at CPU/one thread.
"""
from __future__ import annotations
import argparse
import gc
import json
import logging
from pathlib import Path
import sys
import time
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voxsub.asr import OfflineGenerativeASR, StreamingASR, WindowVAD, UtteranceSegmenter, create_asr_stream
from voxsub.audio import resample_16k
from voxsub.file_io import write_text_atomically
from voxsub.language_guard import language_name, text_matches_language


def samples(path: Path, seconds: int = 6):
    with wave.open(str(path), "rb") as wav:
        assert wav.getsampwidth() == 2
        rate, channels = wav.getframerate(), wav.getnchannels()
        audio = np.frombuffer(wav.readframes(rate * seconds), dtype="<i2").astype(np.float32) / 32768
    return resample_16k(audio.reshape(-1, channels).mean(axis=1), rate)


def check_offline(root: Path, runtime: str, cases: list[tuple[str, str]]) -> list[dict]:
    model = OfflineGenerativeASR(root, runtime, source_lang="zh", num_threads=1)
    native = model._recognizer
    calls = []
    original_decode = native.decode_stream
    def observe(stream):
        delivered = (stream.get_option("language") if runtime == "sherpa-qwen3-asr"
                     else native.config.model_config.sense_voice.language)
        calls.append(delivered)
        original_decode(stream)
    native.decode_stream = observe
    rows = []
    for filename, source in cases:
        audio = samples(root / "test_wavs" / filename)
        stream = create_asr_stream(model, source)
        model.feed(stream, audio)
        before = len(calls); started = time.perf_counter()
        text = model.decode(stream)
        elapsed = (time.perf_counter() - started) * 1000
        assert len(calls) == before + 1
        assert calls[-1] == (language_name(source) if runtime == "sherpa-qwen3-asr" else source)
        assert text.strip()
        assert model.decode(stream) == text and len(calls) == before + 1
        rows.append({"fixture": filename, "language": source, "hint": calls[-1],
                     "audio_ms": round(len(audio)/16, 2), "decode_ms": round(elapsed, 2),
                     "output_chars": len(text), "script_plausible": text_matches_language(text, source),
                     "decoder_calls": 1})
    return rows


def check_streaming(root: Path, vad_path: Path):
    model = StreamingASR(root, num_threads=1, max_active_paths=2, source_lang="en")
    audio = samples(root / "test_wavs" / "3.wav", 12)
    rows = []
    for interval in (360, 140):
        delivered = [0]; partials = []; finals = []
        segmenter = UtteranceSegmenter(model, WindowVAD(str(vad_path)),
            lambda text: finals.append(len(text)), min_silence_ms=350, max_utterance_ms=4500,
            partial_interval_ms=interval, on_partial=lambda text: partials.append((round(delivered[0]/16, 2), len(text))))
        started = time.perf_counter()
        for start in range(0, len(audio), 320):
            chunk = audio[start:start+320]; delivered[0] += len(chunk); segmenter.feed(chunk)
        segmenter.flush()
        assert partials and finals
        rows.append({"partial_interval_ms": interval, "audio_ms": round(len(audio)/16, 2),
                     "first_partial_audio_ms": partials[0][0], "partial_events": len(partials),
                     "final_count": len(finals), "compute_ms": round((time.perf_counter()-started)*1000, 2),
                     "partials": partials})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    logging.disable(logging.INFO)
    qwen = check_offline(args.models_root / "stt/qwen3-asr-0.6b-int8", "sherpa-qwen3-asr",
        [("fr1.wav", "fr"), ("de.wav", "de"), ("ar1.wav", "ar"), ("ru1.wav", "ru"), ("cantonese.wav", "yue")])
    gc.collect()
    sense = check_offline(args.models_root / "stt/sensevoice-small-int8", "sherpa-sense-voice",
        [("en.wav", "en"), ("ja.wav", "ja"), ("ko.wav", "ko"), ("yue.wav", "yue"), ("zh.wav", "zh")])
    gc.collect()
    cadence = check_streaming(args.models_root / "stt/zipformer", args.models_root / "vad/silero_vad_v5.onnx")
    report = {"qwen": qwen, "sensevoice": sense, "streaming": cadence,
              "boundary": "Public-fixture, accelerated replay, CPU only. SenseVoice verifies real SetConfig delivery, not a native readback API. No WER, translation model, wall-clock end-to-end latency, GUI, microphone or desktop capture verification."}
    write_text_atomically(args.report, json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
