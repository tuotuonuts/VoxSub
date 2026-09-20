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

#: 缓存条目名字里的配置指纹段长度上限。签名由调用方提供
#: （通常是 ``voxsub.ocr.ocr_result_signature`` 的结果）——本模块不认识配置
#: 语义，只把它当不透明字符串，这样文件缓存与译文缓存用的是同一条有效性规则，
#: 而不是各写一份维度表。
_SIGNATURE_LENGTH = 16
_SIGNATURE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def is_system_drive(path: Path | str) -> bool:
    """Return whether a Windows path resolves to the forbidden C drive."""
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        candidate = candidate.absolute()
    return candidate.drive.casefold() == "c:"


def _normalize_signature(signature: str) -> str:
    """把配置指纹收敛成可安全放进文件名的短串（非字母数字一律丢掉）。"""
    cleaned = "".join(
        character for character in str(signature or "")
        if character in _SIGNATURE_CHARS)
    return cleaned[:_SIGNATURE_LENGTH]


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
    """Allocate and prune separate, bounded original/translated image stores.

    ``signature`` 是"产生这些图片的那套配置"的指纹（不透明字符串）。给了它，
    条目名里就带上这一段，于是一份缓存只对**同一套配置**有效：

    · 改配置后旧条目**不再命中**（``latest``/``matches`` 找不到）；
    · 旧条目由 ``invalidate_stale()`` 清掉，而它复用的就是本类既有的
      有界淘汰机制（同一个目录扫描 + ``prune``），不另建一套平行缓存。

    不传签名时命名与行为与以前完全一致（旧缓存目录照常可用）。
    """

    def __init__(
        self, root: Path | str, *, limit: int = 15, signature: str = "",
    ) -> None:
        self.root = validate_ocr_cache_root(root)
        self.limit = max(0, int(limit))
        self.signature = _normalize_signature(signature)

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
        middle = f"{self.signature}-" if self.signature else ""
        return self.directory(kind) / f"{stamp}-{middle}{uuid4().hex[:10]}{normalized}"

    def entry_signature(self, path: Path | str) -> str:
        """条目的配置指纹；没有指纹段（旧条目）时返回空串。"""
        parts = Path(path).stem.split("-")
        if len(parts) != 3:
            return ""
        return parts[1]

    def _accepts(self, path: Path | str, expected: str) -> bool:
        """未配置签名时任何条目都算命中（旧缓存目录照常可用）；配置了则必须相等。"""
        return not expected or self.entry_signature(path) == expected

    def matches(self, path: Path | str, *, signature: str | None = None) -> bool:
        """该条目是否由 ``signature``（默认当前配置）产生。"""
        expected = self.signature if signature is None else _normalize_signature(signature)
        return self._accepts(path, expected)

    def cache_file(
        self, source: Path | str, *, kind: str = "original",
        signature: str | None = None,
    ) -> Path:
        source_path = Path(source)
        destination = self._allocate_tagged(kind, source_path.suffix, signature)
        copy_file_atomically(source_path, destination)
        self.finalize(kind, destination)
        return destination

    def _allocate_tagged(self, kind: str, suffix: str, signature: str | None) -> Path:
        """按指定签名分配（不改本实例的当前签名）。"""
        if signature is None:
            return self.allocate(kind, suffix)
        tagged = OcrImageCache(self.root, limit=self.limit, signature=signature)
        return tagged.allocate(kind, suffix)

    def finalize(self, kind: str, path: Path | str) -> Path:
        candidate = Path(path).resolve(strict=False)
        directory = self.directory(kind).resolve(strict=False)
        if candidate.parent != directory:
            raise ValueError("OCR 缓存文件不在受管目录中")
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        self.prune(kind)
        return candidate

    def _managed_files(self, kind: str) -> list[Path]:
        """受管目录里的图片，按 (mtime, name) 从新到旧。"""
        directory = self.directory(kind).resolve(strict=False)
        return sorted(
            (
                item for item in directory.iterdir()
                if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES
            ),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )

    def latest(self, kind: str = "original", *, signature: str | None = None) -> Path | None:
        """命中最新的、配置匹配的条目；没有就返回 None（= 缓存未命中）。"""
        expected = self.signature if signature is None else _normalize_signature(signature)
        for item in self._managed_files(kind):
            if self._accepts(item, expected):
                return item.resolve(strict=False)
        return None

    def prune(self, kind: str) -> tuple[Path, ...]:
        if self.limit == 0:
            return ()
        directory = self.directory(kind).resolve(strict=False)
        removed: list[Path] = []
        for stale in self._managed_files(kind)[self.limit:]:
            resolved = stale.resolve(strict=False)
            if resolved.parent != directory:
                continue
            resolved.unlink(missing_ok=True)
            removed.append(resolved)
        return tuple(removed)

    def invalidate_stale(self, kind: str | None = None) -> tuple[Path, ...]:
        """清掉**不属于当前配置**的条目（复用上面的扫描与 prune）。

        没配置签名（``signature=""``）时是空操作：那时没有"当前配置"可比，
        删掉旧条目只会白丢用户已有缓存。
        """
        if not self.signature:
            return ()
        kinds = tuple(_KINDS) if kind is None else (kind,)
        directory = None
        removed: list[Path] = []
        for name in kinds:
            directory = self.directory(name).resolve(strict=False)
            for item in self._managed_files(name):
                if self.entry_signature(item) == self.signature:
                    continue
                resolved = item.resolve(strict=False)
                if resolved.parent != directory:
                    continue
                resolved.unlink(missing_ok=True)
                removed.append(resolved)
            self.prune(name)
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


def cache_from_store(store, *, signature: str = "") -> OcrImageCache:
    """从配置建缓存。

    ``signature`` 应由调用方用 ``voxsub.ocr.ocr_result_signature(config)`` 算好
    再传进来：维度表只有一份，不在本模块里再抄一遍。
    """
    return OcrImageCache(
        resolve_ocr_cache_root(store),
        limit=int(store.get("ocr_cache_limit", 15)),
        signature=signature,
    )


__all__ = [
    "OcrCacheLocationError",
    "OcrImageCache",
    "cache_from_store",
    "is_system_drive",
    "resolve_ocr_cache_root",
    "validate_ocr_cache_root",
]
