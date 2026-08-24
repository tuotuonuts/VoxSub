"""Non-system-drive storage policy for OCR source and rendered images."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from voxsub.file_io import copy_file_atomically
from voxsub.model_storage import installed_app_dir, resolve_models_root


class OcrCacheLocationError(ValueError):
    """Raised when OCR image pixels would be persisted on the C drive."""


_KINDS = {"original": "originals", "translated": "translated"}
_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def is_system_drive(path: Path | str) -> bool:
    """Return whether a Windows path resolves to the forbidden C drive."""
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        candidate = candidate.absolute()
    return candidate.drive.casefold() == "c:"


def validate_ocr_cache_root(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise OcrCacheLocationError("OCR 图片缓存目录必须是绝对路径")
    candidate = candidate.resolve(strict=False)
    if is_system_drive(candidate):
        raise OcrCacheLocationError("OCR 图片缓存禁止使用 C 盘，请选择其他磁盘")
    return candidate


def resolve_ocr_cache_root(store=None) -> Path:
    """Resolve the configured root, preferring the app's own Cache folder."""
    if store is not None:
        configured = str(store.get("ocr_cache_root", "") or "").strip()
        if configured:
            return validate_ocr_cache_root(configured)

    app_dir = installed_app_dir()
    if app_dir is None:
        # Source runs are intentionally kept inside the checkout instead of a
        # user profile directory, matching packaged-app behavior.
        app_dir = Path(__file__).resolve().parents[1]
    candidate = app_dir / "Cache" / "OCR"
    if not is_system_drive(candidate):
        return validate_ocr_cache_root(candidate)

    models = resolve_models_root(store)
    if not is_system_drive(models):
        # <install>/Models -> <install>/Cache/OCR. A custom model directory
        # similarly keeps cache on that chosen non-system disk.
        return validate_ocr_cache_root(models.parent / "Cache" / "OCR")
    raise OcrCacheLocationError(
        "应用和模型目录都在 C 盘，请先在设置中选择非 C 盘 OCR 缓存目录")


class OcrImageCache:
    """Allocate and prune separate, bounded original/translated image stores."""

    def __init__(self, root: Path | str, *, limit: int = 15) -> None:
        self.root = validate_ocr_cache_root(root)
        self.limit = max(0, int(limit))

    def directory(self, kind: str) -> Path:
        try:
            directory = self.root / _KINDS[kind]
        except KeyError as exc:
            raise ValueError(f"未知 OCR 缓存类型: {kind}") from exc
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def allocate(self, kind: str, suffix: str = ".png") -> Path:
        normalized = suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
        if normalized not in _IMAGE_SUFFIXES:
            normalized = ".png"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self.directory(kind) / f"{stamp}-{uuid4().hex[:10]}{normalized}"

    def cache_file(self, source: Path | str, *, kind: str = "original") -> Path:
        source_path = Path(source)
        destination = self.allocate(kind, source_path.suffix)
        copy_file_atomically(source_path, destination)
        self.finalize(kind, destination)
        return destination

    def finalize(self, kind: str, path: Path | str) -> Path:
        candidate = Path(path).resolve(strict=False)
        directory = self.directory(kind).resolve(strict=False)
        if candidate.parent != directory:
            raise ValueError("OCR 缓存文件不在受管目录中")
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        self.prune(kind)
        return candidate

    def prune(self, kind: str) -> tuple[Path, ...]:
        if self.limit == 0:
            return ()
        directory = self.directory(kind).resolve(strict=False)
        files = sorted(
            (
                item for item in directory.iterdir()
                if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES
            ),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
        removed: list[Path] = []
        for stale in files[self.limit:]:
            resolved = stale.resolve(strict=False)
            if resolved.parent != directory:
                continue
            resolved.unlink(missing_ok=True)
            removed.append(resolved)
        return tuple(removed)

    def discard(self, kind: str, path: Path | str) -> bool:
        """Remove one managed cache file after an explicit successful export."""
        candidate = Path(path).resolve(strict=False)
        directory = self.directory(kind).resolve(strict=False)
        if candidate.parent != directory:
            return False
        existed = candidate.is_file()
        candidate.unlink(missing_ok=True)
        return existed


def cache_from_store(store) -> OcrImageCache:
    return OcrImageCache(
        resolve_ocr_cache_root(store),
        limit=int(store.get("ocr_cache_limit", 15)),
    )


__all__ = [
    "OcrCacheLocationError",
    "OcrImageCache",
    "cache_from_store",
    "is_system_drive",
    "resolve_ocr_cache_root",
    "validate_ocr_cache_root",
]
