<div align="center">

[![简体中文](https://img.shields.io/badge/LANG-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-EA4C4C?style=for-the-badge)](README.md)
[![English](https://img.shields.io/badge/LANG-English-22A699?style=for-the-badge)](README_EN.md)

</div>

# VoxSub

> [!WARNING]
> **VoxSub is still in early development. Its features, model compatibility, and stability are not yet mature, so it is not recommended for production or other critical use cases.** The current Windows installer is signed with a developer self-signed certificate. Microsoft SmartScreen or antivirus products may show an “unknown publisher” warning, flag the file as risky, or produce a false positive. Download VoxSub only from this repository's Releases page and verify the SHA256 checksum before installation. Do not disable security software blindly just to run the installer.

VoxSub is a Windows 10/11 live translation app designed for general users. It turns microphone conversations, system audio from meetings or online classes, and local audio/video files into bilingual subtitles. It runs locally and offline by default, with optional cloud translation for higher quality.

Current version: `0.3.3-beta`. Testing and feedback are welcome, but please expect possible issues with recognition quality, audio-device compatibility, performance, crashes, and UI interactions.

## Download

- [GitHub Release v0.3.3-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.3.3-beta)
- Installer: `VoxSub-Setup-0.3.3-beta.exe`
- SHA256: `A4C527FCF71D2A916E05F61DC32A5F763ED91328CEF110C0885EDCB5BC14309B`

## Available Features

- **Mode A — Microphone interpreting:** choose a microphone and display segmented speech with its translation; optional simultaneous recording follows a Start → Pause/Resume → Finish and Save workflow.
- **Mode B — Application/system audio:** choose a Windows output endpoint, or capture audio only from a selected application's process tree.
- **Mode C — Audio/video subtitles:** import MP4, MKV, MOV, MP3, WAV, and other media; the bundled FFmpeg extracts audio automatically and VoxSub exports a matching SRT file.
- **Model Hub:** browse supported open-source speech-recognition and translation models ordered by quality; download, switch, or uninstall them. Recommendations are labelled Not Recommended, Somewhat Recommended, Recommended, or Full Load based on the computer's CPU, RAM, GPU, and VRAM.
- **Global and mainland-China download sources:** automatically benchmark and fail over between sources, or manually select Hugging Face/GitHub for global access or ModelScope for mainland China.
- **Hardware routing for mainstream PCs:** discrete GPU → NPU → integrated GPU → CPU. An accelerator is used only when both the model and runtime genuinely support it; otherwise VoxSub falls back and records the reason in the in-app log.
- **Built-in diagnostics and live logs:** view logs without opening or locking the log file, and switch DEBUG logging on inside the app.
- **Recognition tuning:** use Automatic, Low Latency, Balanced, or Accuracy presets, or adjust sensitivity, pause-based segmentation, maximum utterance length, decoding candidates, maximum text length, and custom vocabulary over broad ranges. Hover over each `i` icon for a plain-language explanation; changes are saved only when explicitly confirmed.
- **Subtitle sessions:** copy text from the main window or overlay, clear the current session, or save it as TXT, SRT, or VTT. When the overlay is locked, hover over it to adjust the font size or unlock it in place.
- **Soft Premium UI:** light, dark, and system-following themes. The subtitle overlay supports font-size controls, dragging, locking, and click-through mode, and can also be unlocked from the main window.

The Model Hub is a curated compatibility catalog, not a complete mirror of every model repository. It lists only models for which VoxSub has a working runtime integration, a clear license, and a useful quality/resource trade-off. Built-in Zipformer and OPUS models remain available as very-low-resource fallbacks, while higher-quality options include Fun-ASR-Nano, Qwen3-ASR, and Hy-MT2.

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
