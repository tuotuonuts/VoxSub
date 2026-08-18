"""Small model assets that must exist before any Marketplace model can run."""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from voxsub.logging_setup import get_logger

logger = get_logger("bootstrap_models")

VAD_FILENAME = "silero_vad_v5.onnx"
VAD_SHA256 = "6b99cbfd39246b6706f98ec13c7c50c6b299181f2474fa05cbc8046acc274396"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bundled_vad_candidates() -> tuple[Path, ...]:
    relative = Path("models_base") / "vad" / VAD_FILENAME
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / relative)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / relative)
    candidates.append(Path(__file__).resolve().parents[1] / "assets" / "bootstrap_models" /
                      "vad" / VAD_FILENAME)
    return tuple(candidates)


def bundled_vad_path() -> Path | None:
    """Return the verified VAD asset shipped with this application, if present."""
    for candidate in _bundled_vad_candidates():
        if not candidate.is_file():
            continue
        if _sha256(candidate) == VAD_SHA256:
            return candidate
        logger.error("内置 VAD 校验失败，忽略损坏资源: %s", candidate)
    return None


def ensure_bundled_vad(models_root: Path | str) -> Path | None:
    """Install the bundled VAD into the per-user model directory when missing.

    The Marketplace intentionally keeps large ASR models out of the installer,
    but every real-time recognizer depends on this 2.3 MB VAD helper.  Copying
    it on first use makes a newly installed app usable after the user downloads
    an ASR model, without requiring an undocumented extra download.
    """
    target = Path(models_root) / "vad" / VAD_FILENAME
    if target.is_file():
        return target

    source = bundled_vad_path()
    if source is None:
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.part-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        if _sha256(temporary) != VAD_SHA256:
            raise RuntimeError("内置 VAD 文件校验失败")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info("已修复基础 VAD 模型: %s", target)
    return target
