<div align="center">

[![Back to Home](https://img.shields.io/badge/HOME-Back_to_Home-22A699?style=for-the-badge)](https://github.com/tuotuonuts/VoxSub)

</div>

# VoxSub

> [!WARNING]
> **VoxSub is still in early development. Its features, model compatibility, and stability are not yet mature, so it is not recommended for production or other critical use cases.** `0.7.2-beta` is developer-signed with `CN=VoxSub Dev (self-signed)`, but that certificate is not trusted by the public Windows trust chain. Windows may still show a risk warning and antivirus products may produce a false positive. Verify the SHA256 checksum before installation, and do not disable security software blindly just to run the installer.

VoxSub is a Windows 10/11 live translation app designed for general users. It turns microphone conversations, system audio from meetings or online classes, and local audio/video files into bilingual subtitles. It runs locally and offline by default; cloud STT and cloud translation can be configured independently and mixed.

Current source candidate: `0.9.0-beta`; the current public GitHub download remains `0.7.2-beta`. Its local installer has passed the build gates, but it is not presented as released before user acceptance.

> **Intel NPU support remains limited.** `0.4.1-beta` has verified Hy-MT2 1.8B Q4/Q6/Q8 on Intel AI Boost hardware: both VoxSub's automatic route and forced-NPU inference with CPU fallback disabled passed. Hy-MT2 7B Q4/Q6/Q8 are marked “NPU pending” only from public llama.cpp OpenVINO compatibility information. If the real startup translation probe fails, VoxSub automatically switches to the integrated GPU or CPU. The current sherpa-onnx ASR and OPUS runtimes do not support the NPU.

## Downloads

- Latest prerelease: [VoxSub v0.7.2-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.7.2-beta). `VoxSub-Setup-0.7.2-beta.exe` is 214,817,504 bytes (204.87 MiB); SHA256 `313714AE3C9557B88EDBCEBBFCB768A15BBDD65915A5266E0B3EB1D82CAF2211`.
- [Download the installer](https://github.com/tuotuonuts/VoxSub/releases/download/v0.7.2-beta/VoxSub-Setup-0.7.2-beta.exe) · [Download the SHA256 file](https://github.com/tuotuonuts/VoxSub/releases/download/v0.7.2-beta/VoxSub-Setup-0.7.2-beta.exe.sha256)
- Previous public build: [VoxSub v0.5.0-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.5.0-beta).

## Available Features

- **Mode A — Microphone interpreting:** choose a microphone and display segmented speech with its translation; optional simultaneous recording follows a Start → Pause/Resume → Finish and Save workflow.
- **Mode B — Application/system audio:** choose a Windows output endpoint, or capture audio only from a selected application's process tree.
- **Mode C — Audio/video subtitles:** import MP4, MKV, MOV, MP3, WAV, and other media; the bundled FFmpeg extracts audio automatically and VoxSub exports a matching SRT file.
- **OCR image and screen translation:** select a screen region or upload PNG/JPG/WebP/BMP/TIFF, inspect recognized source and translation, switch between source boxes and an in-place translated image, and export the rendered result. Live Region watches a selected area and reruns OCR only after a meaningful visual change. Source and unexported translated images use separate non-C-drive caches, keeping 15 of each by default or unlimited when set to 0.
- **Model Hub:** browse supported open-source speech recognition, translation, text-to-speech, and OCR models ordered by quality; download, switch, or uninstall them. OCR includes PP-OCRv6 Tiny/Small/Medium plus a PP-OCRv5 document/handwriting preset. Recommendations are labelled Not Recommended, Somewhat Recommended, Recommended, or Full Load based on the computer's CPU, RAM, GPU, and VRAM.
- **Global and mainland-China download sources:** automatically benchmark and fail over between sources, or manually select Hugging Face/GitHub for global access or ModelScope for mainland China. Multi-gigabyte downloads retain progress and resume automatically after a CDN disconnect.
- **Hardware routing for mainstream PCs:** discrete GPU → NPU → integrated GPU → CPU. The current source has verified automatic Intel NPU routing for Hy-MT2 1.8B Q4/Q6/Q8; other models follow their per-card Verified, Pending, or Unavailable NPU label.
- **Built-in diagnostics and live logs:** view logs without opening or locking the log file, switch DEBUG logging on inside the app, and export logs, reports, and sessions through an in-app save dialog with background writing.
- **New-device base-model repair:** a bundled Silero VAD is restored to the current user's model directory on first use, so an ASR model downloaded from Model Hub can run without a separate hidden VAD download.
- **Cloud and hybrid pipelines:** choose STT and translation independently. Cloud STT and cloud translation each have their own API key, BaseURL, and model name, supporting cloud STT plus local translation, local STT plus cloud translation, and a fully cloud-based chain. Cloud STT uploads only VAD-finalized speech segments.
- **Recognition tuning:** the existing Automatic, Low Latency, Balanced, Accuracy, and Custom behaviors remain unchanged. The new Smart Context mode can extend pauses based on sentence completeness, merge fragments within a hard wait limit, conservatively correct from custom vocabulary and repeated recent context, and optionally apply light isolated-filler cleanup. Hover over each `i` icon for a plain-language explanation; changes are saved only when explicitly confirmed.
- **0.6.0-beta candidate feature:** Smart Context now keeps one replaceable current-sentence draft that updates near word cadence and can revise earlier text. Its translation follows the changing source and accepts only the newest request result; the row enters history only when the sentence is final. When Qwen3, Fun-ASR, SenseVoice, or cloud STT is selected, the bundled Zipformer runs as a lightweight streaming draft sidecar while the selected high-quality recognizer remains authoritative for the corrected final, avoiding repeated large-model inference or audio uploads. A separate Live bilingual draft switch can disable this work while preserving Smart Context segmentation, correction, and filler cleanup.
- **0.7.0-beta candidate feature:** Model Hub can download a bilingual MeloTTS voice plus lightweight Chinese AISHELL3 and English LJSpeech voices. Settings -> Text-to-speech selects Chinese and English voices independently and switches them during a live session. The TTS toggle now applies immediately; live bilingual draft mode reads finalized translations without repeatedly speaking every draft revision. Existing `models/tts/zh` and `models/tts/en` installations remain compatible.
- **0.7.1-beta candidate fix:** all-caps interim English is now rendered in readable sentence case without changing final recognition evidence. Continuous partials no longer reset the translation debounce forever, so dynamic translation catches up at a throttled rate while finalized sentences retain priority.
- **0.7.2-beta fix:** setup no longer depends on Windows Restart Manager, which interpreted VoxSub's close-to-tray behavior as a refusal to exit. New builds receive a dedicated shutdown signal, while older builds use a fast VoxSub process-tree fallback, eliminating the roughly thirty-second freeze and close failure.
- **0.8.0-beta candidate feature:** a separate OCR workspace provides Screenshot OCR Translation and Live Region OCR. Bundled offline RapidOCR preserves line geometry, the translation overlay is excluded from later captures, and unchanged frames do not rerun the model. Screenshot pixels remain in local memory; only recognized text is sent when cloud translation is selected. The current general model targets printed text, while handwriting and decorative text can later use a replaceable OCR backend.
- **0.9.0-beta candidate feature and fix:** packaged builds explicitly include `rapidocr.main`, and upgrades remove only application-managed PyInstaller runtime directories so obsolete modules cannot shadow current OCR dependencies while `Models` and `Cache` remain intact; selection waits until Windows has fully hidden the main window to prevent a ghost image; image upload, in-place translated preview, and translated-image export are supported; source and translated images use separate non-C-drive caches with a 15-per-type default and 0 for unlimited; Model Hub adds four selectable OCR presets. Moving OCR to the top tool row also restores the A/B/C card layout.
- **Subtitle sessions:** copy text from the main window or overlay, clear the current session, or save it as TXT, SRT, or VTT. The overlay can show source only, translation only, or both, with separate controls for content padding and the gap between lines.
- **Soft Premium UI:** light, dark, and system-following themes across the main app, Settings, Model Hub, and diagnostics. The subtitle overlay supports a wider font range, free resizing, dragging, locking, and click-through mode. When locked, hovering reveals only the Unlock control.
- **Fixed-size long subtitles:** long sentences no longer enlarge the overlay or push it beyond the screen. Text wraps inside the chosen dimensions; use the mouse wheel for the current sentence and `Ctrl + wheel` for subtitle history.
- **Unified choice controls:** settings radio choices stay circular, binary settings use rounded switches, and Model Hub filters remain capsule-shaped instead of changing geometry when selected.
- **Installer language:** the setup wizard automatically follows the Windows UI language for Simplified Chinese, Traditional Chinese, or English, with English as the fallback.
- **Model storage:** fresh installs use a `Models` folder beside the installed app, organized into purpose folders such as `stt`, `translate`, `vad`, and `tts`. Upgraded installations keep their existing model root until the user changes it. Settings supports changing the location, moving an existing library, and manually importing models; updates do not remove downloaded models.
- **0.5.0-beta feature:** an independent bounded context stage lets generative/cloud STT merge incomplete fragments before translation and lets streaming Zipformer extend a pause when the sentence appears incomplete. Waiting always has a hard cap, corrections are small and auditable, and existing tuning modes bypass the stage entirely.
- **0.4.2-beta candidate fixes:** centralized config validation and migration; bounded capture, recognition, translation, and TTS queues; a working independent TTS playback worker; integrity checks and atomic writes for downloads, model commits, and subtitle exports; responsibility-based splits for Pipeline, hardware probing, and llama startup; and reliable matching up/down controls for translation font size and overlay opacity in Appearance settings.
- **0.4.1-beta fixes:** both recognition-tuning spin arrows are clickable; model moves run in the background without freezing or crashing when the page closes; after a move, the manifest is repaired and the pipeline immediately uses the new root instead of reporting missing files or reopening the old root; upgrades keep finding translation models in the previous model root; newer Teams windows are captured through their host process and child process tree; long subtitles no longer expand the overlay off-screen.
- **Update notes:** a new version shows its user-facing notes once on the first launch. The same history remains available under Settings → About.
- **Fullscreen behavior:** opening Settings or Model Hub from a fullscreen main window keeps the app fullscreen.

The Model Hub is a curated compatibility catalog, not a complete mirror of every model repository. It lists only models for which VoxSub has a working runtime integration, a clear license, and a useful quality/resource trade-off: Fun-ASR-Nano, Qwen3-ASR, SenseVoice Small, Hy-MT2 1.8B/7B in Q4/Q6/Q8 variants, and MeloTTS bilingual, AISHELL3 Chinese lightweight, and LJSpeech English lightweight voices. Built-in Zipformer and OPUS models remain only as very-low-resource fallbacks. Every model card shows an explicit NPU availability label; “NPU available” is reserved for exact model files that pass both forced-NPU inference and VoxSub's automatic application route.

## Documentation

The detailed engineering documents are currently maintained in Chinese:

- [STATUS.md](STATUS.md) — **project status and handoff guide; start here**
- [TODO.txt](TODO.txt) — timestamped change and task history
- [REQUIREMENTS.md](REQUIREMENTS.md) — requirements and scope
- [PLAN.md](PLAN.md) — technical choices and milestones
- [DESIGN.md](DESIGN.md) — architecture, modules, and interface contracts

## Development Setup

Requirements: Windows 10/11, Python 3.11+, and [uv](https://docs.astral.sh/uv/).

```powershell
uv venv --python 3.11
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

Run from source:

```powershell
.\.venv\Scripts\python.exe -m voxsub.ui.app
```

Build the Windows installer:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

The installer is written to the `Release` directory next to the project directory. In the current development workspace, that path is `D:\OneDrive\app_dve\Release`. Fresh installations store models under `<install directory>\Models`, including `stt`, `translate`, `vad`, `tts`, and `ocr`, while existing installations keep their current model root until changed in Settings. Model files are user data, are not bundled repeatedly, and are not removed by updates. The public `0.7.2-beta` installer is 214,817,504 bytes with SHA256 `313714AE3C9557B88EDBCEBBFCB768A15BBDD65915A5266E0B3EB1D82CAF2211`. The local `0.9.0-beta` candidate is 277,283,040 bytes (264.44 MiB), SHA256 `7F5DB1956C6A86485DF3AF92BC1C879CD512CF1B14D6D53B37EF2817528DF7DC`; no GitHub Release will be created before user validation.

## Project Layout

```text
voxsub/     Main Python package; see DESIGN.md for module details
tests/      Pytest test suite
scripts/    Build and utility scripts
models/     Runtime model cache; excluded by .gitignore
```
