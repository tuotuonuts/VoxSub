<div align="center">

[![Back to Home](https://img.shields.io/badge/HOME-Back_to_Home-22A699?style=for-the-badge)](https://github.com/tuotuonuts/VoxSub)

</div>

# VoxSub

> [!WARNING]
> **VoxSub is still in early development. Its features, model compatibility, and stability are not yet mature, so it is not recommended for production or other critical use cases.** The `0.4.0-beta` installer uses a developer self-signed certificate. Windows may show an “unknown publisher” warning, and antivirus products may produce a false positive. Download VoxSub only from this repository's Releases page and verify the SHA256 checksum before installation. Do not disable security software blindly just to run the installer.

VoxSub is a Windows 10/11 live translation app designed for general users. It turns microphone conversations, system audio from meetings or online classes, and local audio/video files into bilingual subtitles. It runs locally and offline by default; cloud STT and cloud translation can be configured independently and mixed.

Source version: `0.4.0-beta`. The complete installer has been generated, and this remains a development build. Testing and feedback are welcome, but please expect possible issues with recognition quality, audio-device compatibility, performance, crashes, and UI interactions.

> **Intel NPU support remains limited.** `0.4.0-beta` has verified Hy-MT2 1.8B Q4/Q6/Q8 on Intel AI Boost hardware: both VoxSub's automatic route and forced-NPU inference with CPU fallback disabled passed. Hy-MT2 7B Q4/Q6/Q8 are marked “NPU pending” only from public llama.cpp OpenVINO compatibility information. If the real startup translation probe fails, VoxSub automatically switches to the integrated GPU or CPU. The current sherpa-onnx ASR and OPUS runtimes do not support the NPU.

## Download

- [GitHub Releases](https://github.com/tuotuonuts/VoxSub/releases)
- Installer: `VoxSub-Setup-0.4.0-beta.exe` (204.73 MiB, developer self-signed)
- SHA256: `408AE75789EDDD880BF1A50976363CA27564C80D27988382EA6B5AE887BDDFCA` (the matching `.sha256` file has been generated)
- Local build path: `D:\OneDrive\app_dve\Release\VoxSub-Setup-0.4.0-beta.exe`

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
- **Recognition tuning:** use Automatic, Low Latency, Balanced, or Accuracy presets, or adjust sensitivity, pause-based segmentation, maximum utterance length, decoding candidates, maximum text length, and custom vocabulary over broad ranges. Hover over each `i` icon for a plain-language explanation; changes are saved only when explicitly confirmed.
- **Subtitle sessions:** copy text from the main window or overlay, clear the current session, or save it as TXT, SRT, or VTT. When the overlay is locked, hover over it to adjust the font size or unlock it in place.
- **Soft Premium UI:** light, dark, and system-following themes across the main app, Settings, Model Hub, and diagnostics. The subtitle overlay supports font-size controls, dragging, locking, and click-through mode, and can be unlocked from its hover control island or Settings.
- **Unified choice controls:** settings radio choices stay circular, binary settings use rounded switches, and Model Hub filters remain capsule-shaped instead of changing geometry when selected.
- **Installer language:** the setup wizard automatically follows the Windows UI language for Simplified Chinese, Traditional Chinese, or English, with English as the fallback.

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

The installer is written to the `Release` directory next to the project directory. In the current development workspace, that path is `D:\OneDrive\app_dve\Release`. Downloaded models remain under `%LOCALAPPDATA%\VoxSub\models` and are not bundled repeatedly into the installer.

## Project Layout

```text
voxsub/     Main Python package; see DESIGN.md for module details
tests/      Pytest test suite
scripts/    Build and utility scripts
models/     Runtime model cache; excluded by .gitignore
```
