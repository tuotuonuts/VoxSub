"""Read-only, silent CPU check using a sherpa Qwen3 model's bundled public WAVs.

Never records/plays audio, launches UI, downloads models, or loads user config.
Run with isolated APPDATA/LOCALAPPDATA and cleared PYTHONPATH/PYTHONHOME.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import wave

import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import sherpa_onnx
    from voxsub.asr import OfflineGenerativeASR, create_asr_stream
    from voxsub.language_guard import text_matches_language
    from voxsub.audio import resample_16k

    model = OfflineGenerativeASR(args.model_dir, "sherpa-qwen3-asr", source_lang="zh", num_threads=1)
    native_recognizer = model._recognizer
    observed: list[str | None] = []

    class ObserveNativeOption:
        def create_stream(self):
            return native_recognizer.create_stream()

        def decode_stream(self, stream):
            # Real native stream readback at the actual inference boundary.
            observed.append(stream.get_option("language") if stream.has_option("language") else None)
            native_recognizer.decode_stream(stream)

    model._recognizer = ObserveNativeOption()
    cases = [("f1_noise.wav", "auto", 1.0), ("f1_noise.wav", "en", 1.0),
             ("f1_noise.wav", "en", .15), ("noise1-en.wav", "en", 1.0),
             ("codeswitch.wav", "en", 1.0)]
    rows = []
    for filename, language, gain in cases:
        sample = args.model_dir / "test_wavs" / filename
        with wave.open(str(sample), "rb") as wav:
            rate = wav.getframerate()
            assert wav.getsampwidth() == 2
            samples = np.frombuffer(wav.readframes(rate * 6), dtype="<i2").astype(np.float32) / 32768
            samples = samples.reshape(-1, wav.getnchannels()).mean(axis=1)
        samples = resample_16k(samples, rate)
        samples *= gain
        stream = create_asr_stream(model, language)
        model.feed(stream, samples)
        started = time.perf_counter()
        text = model.decode(stream)
        elapsed = (time.perf_counter() - started) * 1000
        expected = None if language == "auto" else "English"
        assert observed[-1] == expected
        assert text.strip(), f"Empty output for {filename}/{language}/{gain}"
        count = len(observed)
        assert model.decode(stream) == text and len(observed) == count  # No retry/double inference.
        rows.append({"sample": filename, "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
                     "requested_source": language, "native_language_option": observed[-1], "gain": gain,
                     "audio_ms": round(samples.size / 16, 1), "decode_ms": round(elapsed, 2),
                     "output_chars": len(text), "latin_script_plausible": text_matches_language(text, "en"),
                     "inferences": 1})
        model.reset(stream)
    report = {"status": "PASS", "sherpa_onnx": sherpa_onnx.__version__, "runtime": model.runtime,
              "provider": model.provider, "threads": 1, "cases": rows,
              "boundaries": ["Public bundled WAV excerpts, not the user's original video", "No playback, capture, UI, cloud or config writes",
                             "Native hint readback and non-empty result verified, not word accuracy or perfect language exclusivity",
                             "Translation integration is separately unit-tested; no production translator/server used"]}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
