<div align="center">

[![Back to Home](https://img.shields.io/badge/HOME-Back_to_Home-22A699?style=for-the-badge)](https://github.com/tuotuonuts/VoxSub)

</div>

# VoxSub

> [!WARNING]
> **VoxSub is still in early development. Its features, model compatibility, and stability are not yet mature, so it is not recommended for production or other critical use cases.** The published `0.4.2-beta` installer and the unreleased local `0.5.0-beta` candidate are unsigned because the current certificate is unavailable. Windows may show an “unknown publisher” warning, and antivirus products may produce a false positive. Verify the SHA256 checksum before installation, and do not disable security software blindly just to run the installer.

VoxSub is a Windows 10/11 live translation app designed for general users. It turns microphone conversations, system audio from meetings or online classes, and local audio/video files into bilingual subtitles. It runs locally and offline by default; cloud STT and cloud translation can be configured independently and mixed.

Source version: `0.5.0-beta`. Its candidate installer has been built and passed an isolated startup check, and is awaiting user validation. The public download is now `0.4.2-beta`; no `0.5.0-beta` Release has been created. This is still a development build.

> **Intel NPU support remains limited.** `0.4.1-beta` has verified Hy-MT2 1.8B Q4/Q6/Q8 on Intel AI Boost hardware: both VoxSub's automatic route and forced-NPU inference with CPU fallback disabled passed. Hy-MT2 7B Q4/Q6/Q8 are marked “NPU pending” only from public llama.cpp OpenVINO compatibility information. If the real startup translation probe fails, VoxSub automatically switches to the integrated GPU or CPU. The current sherpa-onnx ASR and OPUS runtimes do not support the NPU.

## Downloads and candidate build

- Published build: `VoxSub-Setup-0.4.2-beta.exe` on [VoxSub v0.4.2-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.4.2-beta) (unsigned); SHA256 `9B691BF2FE6B5F9F3AD7C0B53CB936547D38550CC894BDC8FAA43684770E99D6`.
- Local test candidate: `D:\OneDrive\app_dve\Release\VoxSub-Setup-0.5.0-beta.exe`, 214,795,950 bytes (204.85 MiB), unsigned; SHA256 `38CF47DE43CB39B45BAF8241464A7C06B5AEF6AF28CE15FF76E7B32063047EA8`.
- `0.5.0-beta` will not be uploaded or published as a new Release until user validation is complete.

## Available Features

- **Mode A — Microphone interpreting:** choose a microphone and display segmented speech with its translation; optional simultaneous recording follows a Start → Pause/Resume → Finish and Save workflow.
- **Mode B — Application/system audio:** choose a Windows output endpoint, or capture audio only from a selected application's process tree.
- **Mode C — Audio/video subtitles:** import MP4, MKV, MOV, MP3, WAV, and other media; the bundled FFmpeg extracts audio automatically and VoxSub exports a matching SRT file.
- **Model Hub:** browse supported open-source speech-recognition and translation models ordered by quality; download, switch, or uninstall them. Recommendations are labelled Not Recommended, Somewhat Recommended, Recommended, or Full Load based on the computer's CPU, RAM, GPU, and VRAM.
- **Global and mainland-China download sources:** automatically benchmark and fail over between sources, or manually select Hugging Face/GitHub for global access or ModelScope for mainland China. Multi-gigabyte downloads retain progress and resume automatically after a CDN disconnect.
- **Hardware routing for mainstream PCs:** discrete GPU → NPU → integrated GPU → CPU. The current source has verified automatic Intel NPU routing for Hy-MT2 1.8B Q4/Q6/Q8; other models follow their per-card Verified, Pending, or Unavailable NPU label.
- **Built-in diagnostics and live logs:** view logs without opening or locking the log file, switch DEBUG logging on inside the app, and export logs, reports, and sessions through an in-app save dialog with background writing.
- **New-device base-model repair:** a bundled Silero VAD is restored to the current user's model directory on first use, so an ASR model downloaded from Model Hub can run without a separate hidden VAD download.
- **Cloud and hybrid pipelines:** choose STT and translation independently. Cloud STT and cloud translation each have their own API key, BaseURL, and model name, supporting cloud STT plus local translation, local STT plus cloud translation, and a fully cloud-based chain. Cloud STT uploads only VAD-finalized speech segments.
- **Recognition tuning:** the existing Automatic, Low Latency, Balanced, Accuracy, and Custom behaviors remain unchanged. The new Smart Context mode can extend pauses based on sentence completeness, merge fragments within a hard wait limit, conservatively correct from custom vocabulary and repeated recent context, and optionally apply light isolated-filler cleanup. Hover over each `i` icon for a plain-language explanation; changes are saved only when explicitly confirmed.
- **Subtitle sessions:** copy text from the main window or overlay, clear the current session, or save it as TXT, SRT, or VTT. The overlay can show source only, translation only, or both, with separate controls for content padding and the gap between lines.
- **Soft Premium UI:** light, dark, and system-following themes across the main app, Settings, Model Hub, and diagnostics. The subtitle overlay supports a wider font range, free resizing, dragging, locking, and click-through mode. When locked, hovering reveals only the Unlock control.
- **Fixed-size long subtitles:** long sentences no longer enlarge the overlay or push it beyond the screen. Text wraps inside the chosen dimensions; use the mouse wheel for the current sentence and `Ctrl + wheel` for subtitle history.
- **Unified choice controls:** settings radio choices stay circular, binary settings use rounded switches, and Model Hub filters remain capsule-shaped instead of changing geometry when selected.
- **Installer language:** the setup wizard automatically follows the Windows UI language for Simplified Chinese, Traditional Chinese, or English, with English as the fallback.
- **Model storage:** fresh installs use a `Models` folder beside the installed app, organized into purpose folders such as `stt`, `translate`, `vad`, and `tts`. Upgraded installations keep their existing model root until the user changes it. Settings supports changing the location, moving an existing library, and manually importing models; updates do not remove downloaded models.
- **0.5.0-beta candidate feature:** an independent bounded context stage lets generative/cloud STT merge incomplete fragments before translation and lets streaming Zipformer extend a pause when the sentence appears incomplete. Waiting always has a hard cap, corrections are small and auditable, and existing tuning modes bypass the stage entirely.
- **0.4.2-beta candidate fixes:** centralized config validation and migration; bounded capture, recognition, translation, and TTS queues; a working independent TTS playback worker; integrity checks and atomic writes for downloads, model commits, and subtitle exports; responsibility-based splits for Pipeline, hardware probing, and llama startup; and reliable matching up/down controls for translation font size and overlay opacity in Appearance settings.
- **0.4.1-beta fixes:** both recognition-tuning spin arrows are clickable; model moves run in the background without freezing or crashing when the page closes; after a move, the manifest is repaired and the pipeline immediately uses the new root instead of reporting missing files or reopening the old root; upgrades keep finding translation models in the previous model root; newer Teams windows are captured through their host process and child process tree; long subtitles no longer expand the overlay off-screen.
- **Update notes:** a new version shows its user-facing notes once on the first launch. The same history remains available under Settings → About.
- **Fullscreen behavior:** opening Settings or Model Hub from a fullscreen main window keeps the app fullscreen.

The Model Hub is a curated compatibility catalog, not a complete mirror of every model repository. It lists only models for which VoxSub has a working runtime integration, a clear license, and a useful quality/resource trade-off: Fun-ASR-Nano, Qwen3-ASR, SenseVoice Small, and Hy-MT2 1.8B/7B in Q4/Q6/Q8 variants. Built-in Zipformer and OPUS models remain only as very-low-resource fallbacks. Every model card shows an explicit NPU availability label; “NPU available” is reserved for exact model files that pass both forced-NPU inference and VoxSub's automatic application route.

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

The installer is written to the `Release` directory next to the project directory. In the current development workspace, that path is `D:\OneDrive\app_dve\Release`. Fresh installations store models under `<install directory>\Models`, while existing installations keep their current model root until changed in Settings. Model files are user data, are not bundled repeatedly, and are not removed by updates. The current local `0.5.0-beta` candidate installer is 214,795,950 bytes.

## Project Layout

```text
voxsub/     Main Python package; see DESIGN.md for module details
tests/      Pytest test suite
scripts/    Build and utility scripts
models/     Runtime model cache; excluded by .gitignore
```
