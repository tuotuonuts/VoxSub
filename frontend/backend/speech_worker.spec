# -*- mode: python ; coding: utf-8 -*-
"""Optional CPU speech runtime, separate from the ONNX IPC sidecar."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, copy_metadata
ROOT = Path(SPECPATH).resolve().parents[1]
datas, binaries, hidden = [(str(ROOT / 'docs' / 'licenses'), 'licenses')], [], ['voxsub.speech_worker', 'voxsub.speech_index']
for package in ('transformers', 'torch', 'torchaudio', 'librosa', 'soundfile', 'safetensors'):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hidden += h
for distribution in ('transformers', 'torch', 'accelerate', 'librosa'):
    datas += copy_metadata(distribution, recursive=True)
a = Analysis([str(ROOT / 'scripts' / 'speech-worker.py')], pathex=[str(ROOT)],
    binaries=binaries, datas=datas, hiddenimports=hidden,
    excludes=['PySide6', 'pytest', 'tensorflow', 'jax', 'torchvision', 'matplotlib'],
    noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='VoxSubSpeechWorker', console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='VoxSubSpeechWorker')
