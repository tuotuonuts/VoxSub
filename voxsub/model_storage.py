"""One authoritative model-storage policy for VoxSub.

Downloaded models are user data, not application payload.  This module keeps
the location stable across upgrades, provides a writable default for fresh
installations, and understands layouts produced by older VoxSub versions.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from voxsub.logging_setup import get_logger
from voxsub.models import MANIFEST_NAME, load_manifest, save_manifest

if TYPE_CHECKING:  # pragma: no cover - import only for static checking
    from voxsub.config_store import ConfigStore


logger = get_logger("model_storage")

_TRANSIENT_NAMES = {".downloads", ".installing"}
_MODEL_METADATA_NAMES = {
    ".fetch.lock",
    MANIFEST_NAME,
    f"{MANIFEST_NAME}.tmp",
    "catalog_installs.json",
}
_MIGRATION_SKIP_NAMES = _TRANSIENT_NAMES | {
    ".fetch.lock",
    MANIFEST_NAME,
    f"{MANIFEST_NAME}.tmp",
}

# Every pair moves an old, product-facing layout into the current purpose based
# layout.  The operation is repeatable and never overwrites a destination file.
_LEGACY_LAYOUT: tuple[tuple[Path, Path], ...] = (
    (Path("asr"), Path("stt") / "zipformer"),
    (Path("marketplace") / "asr-funasr-nano-2512-int8",
     Path("stt") / "funasr-nano-2512-int8"),
    (Path("marketplace") / "asr-qwen3-0.6b-int8",
     Path("stt") / "qwen3-asr-0.6b-int8"),
    (Path("marketplace") / "asr-sensevoice-small-int8",
     Path("stt") / "sensevoice-small-int8"),
    (Path("nmt"), Path("translate") / "opus"),
    (Path("marketplace") / "mt-hy-mt2-1.8b-q4",
     Path("translate") / "hy-mt2-1.8b-q4"),
    (Path("marketplace") / "mt-hy-mt2-1.8b-q6",
     Path("translate") / "hy-mt2-1.8b-q6"),
    (Path("marketplace") / "mt-hy-mt2-1.8b-q8",
     Path("translate") / "hy-mt2-1.8b-q8"),
    (Path("marketplace") / "mt-hy-mt2-7b-q4",
     Path("translate") / "hy-mt2-7b-q4"),
    (Path("marketplace") / "mt-hy-mt2-7b-q6",
     Path("translate") / "hy-mt2-7b-q6"),
    (Path("marketplace") / "mt-hy-mt2-7b-q8",
     Path("translate") / "hy-mt2-7b-q8"),
    # Pre-marketplace quality-model downloads remain usable as a clearly
    # separated legacy translation folder instead of disappearing on upgrade.
    (Path("llm"), Path("translate") / "legacy-llm"),
)


@dataclass(frozen=True)
class MigrationResult:
    """A user-readable result for model-folder reorganization or relocation."""

    source: Path
    destination: Path
    moved_paths: int = 0
    kept_existing_paths: int = 0
    normalized_paths: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.moved_paths or self.normalized_paths)


def legacy_models_root() -> Path:
    """Return the pre-0.4.1 per-user location without creating it."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "VoxSub" / "models"


def installed_app_dir() -> Path | None:
    """Locate the directory containing the installed executable.

    ``VOXSUB_INSTALL_DIR`` is intentionally supported for packaged-app smoke
    tests.  Source runs return ``None`` so development never writes model data
    into the repository checkout.
    """
    override = os.environ.get("VOXSUB_INSTALL_DIR", "").strip()
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def install_models_root() -> Path:
    """Default root for a fresh packaged installation."""
    app_dir = installed_app_dir()
    return app_dir / "Models" if app_dir is not None else legacy_models_root()


def has_model_data(root: Path | str) -> bool:
    """Whether ``root`` contains usable model data rather than only temp files."""
    path = Path(root)
    try:
        return any(
            child.name not in _TRANSIENT_NAMES and child.name not in _MODEL_METADATA_NAMES
            for child in path.iterdir()
        )
    except OSError:
        return False


def _configured_root(store: "ConfigStore | None" = None) -> Path | None:
    if store is None:
        from voxsub.config_store import ConfigStore

        store = ConfigStore()
    value = str(store.get("models_root", "") or "").strip()
    return Path(value) if value else None


def resolve_models_root(store: "ConfigStore | None" = None) -> Path:
    """Resolve the active root without changing configuration or disk state."""
    configured = _configured_root(store)
    if configured is not None:
        return configured
    # A pre-0.4.1 user has no storage key.  Existing data wins over the fresh
    # install default so an update can never hide their downloaded models.
    legacy = legacy_models_root()
    if has_model_data(legacy):
        return legacy
    return install_models_root()


def model_lookup_roots(active: Path | str | None = None) -> tuple[Path, ...]:
    """Return roots that may contain models during an upgrade.

    A previous build could have persisted a new ``Models`` root before its
    old per-user library was moved. Treat that old location as a read-only
    compatibility root until the user explicitly migrates it. This prevents
    an update from hiding an already downloaded translation model and keeps
    startup free of multi-gigabyte file moves.
    """
    primary = Path(active) if active is not None else resolve_models_root()
    primary = primary.resolve()
    roots = [primary]
    for candidate in (legacy_models_root(), install_models_root()):
        resolved = candidate.resolve()
        if resolved == primary or resolved in roots:
            continue
        if has_model_data(resolved):
            roots.append(resolved)
    return tuple(roots)


def _move_merge(source: Path, destination: Path) -> tuple[int, int]:
    """Move one item without overwriting existing files.

    Returning counts makes recovery visible to callers.  Existing destination
    files are retained, leaving their source counterpart intact for manual
    review instead of risking a destructive merge.
    """
    if not source.exists():
        return 0, 0
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return 1, 0
    if source.is_file() or destination.is_file():
        return 0, 1

    moved = kept = 0
    for child in list(source.iterdir()):
        child_moved, child_kept = _move_merge(child, destination / child.name)
        moved += child_moved
        kept += child_kept
    try:
        source.rmdir()
    except OSError:
        pass
    return moved, kept


def _mapped_manifest_rel(root: Path, rel: str) -> str:
    """Return the current-layout key when its relocated file exists."""
    normalized = PurePosixPath(str(rel).replace("\\", "/"))
    parts = normalized.parts
    if normalized.is_absolute() or ".." in parts:
        return normalized.as_posix()
    for old_rel, new_rel in _LEGACY_LAYOUT:
        old_parts = PurePosixPath(old_rel.as_posix()).parts
        if parts[:len(old_parts)] != old_parts:
            continue
        mapped = PurePosixPath(new_rel.as_posix(), *parts[len(old_parts):])
        if root.joinpath(*mapped.parts).is_file():
            return mapped.as_posix()
    return normalized.as_posix()


def _normalized_manifest(root: Path, manifest: dict) -> tuple[dict, int]:
    """Rewrite legacy manifest keys while preserving their recorded metadata."""
    files = manifest.get("files")
    if not isinstance(files, dict):
        return manifest, 0

    current: dict[str, object] = {}
    relocated: list[tuple[str, object]] = []
    rewrites = 0
    for rel, entry in files.items():
        normalized_rel = PurePosixPath(str(rel).replace("\\", "/")).as_posix()
        mapped_rel = _mapped_manifest_rel(root, normalized_rel)
        if normalized_rel != str(rel):
            rewrites += 1
        if mapped_rel != normalized_rel:
            relocated.append((mapped_rel, entry))
            rewrites += 1
        else:
            current[normalized_rel] = entry

    # A destination entry contains information about the file that was kept
    # during a non-overwriting merge, so it takes priority over the old entry.
    for mapped_rel, entry in relocated:
        current.setdefault(mapped_rel, entry)

    if current == files:
        return manifest, 0
    updated = dict(manifest)
    updated["files"] = current
    return updated, rewrites


def _rewrite_manifest_layout(root: Path) -> int:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return 0
    manifest, rewrites = _normalized_manifest(root, load_manifest(root))
    if rewrites:
        save_manifest(root, manifest)
        logger.info("模型清单路径已同步整理: root=%s rewritten=%s", root, rewrites)
    return rewrites


def _merge_source_manifest(source_manifest: dict, destination: Path) -> int:
    """Merge entries for files that really arrived, keeping target entries."""
    normalized_source, _ = _normalized_manifest(destination, source_manifest)
    source_files = normalized_source.get("files", {})
    if not isinstance(source_files, dict) or not source_files:
        return 0
    destination_manifest = load_manifest(destination)
    destination_files = destination_manifest.setdefault("files", {})
    if not isinstance(destination_files, dict):
        destination_files = {}
        destination_manifest["files"] = destination_files

    merged = 0
    for rel, entry in source_files.items():
        rel_path = PurePosixPath(str(rel).replace("\\", "/"))
        if (rel_path.is_absolute() or ".." in rel_path.parts or
                not destination.joinpath(*rel_path.parts).is_file()):
            continue
        if rel_path.as_posix() not in destination_files:
            destination_files[rel_path.as_posix()] = entry
            merged += 1
    if merged:
        save_manifest(destination, destination_manifest)
        logger.info("模型清单已合并: root=%s entries=%s", destination, merged)
    return merged


def normalize_model_layout(root: Path | str) -> MigrationResult:
    """Classify known legacy folders under ``root`` by model purpose.

    The root itself is intentionally unchanged.  This is what lets upgrade
    users keep their established storage location while receiving the clearer
    ``stt`` / ``translate`` / ``vad`` / ``tts`` organization.
    """
    base = Path(root)
    if not base.exists():
        return MigrationResult(base, base)
    moved = kept = 0
    for old_rel, new_rel in _LEGACY_LAYOUT:
        source = base / old_rel
        destination = base / new_rel
        if source == destination or not source.exists():
            continue
        changed, retained = _move_merge(source, destination)
        moved += changed
        kept += retained
    rewritten = _rewrite_manifest_layout(base)
    if moved or rewritten:
        logger.info(
            "模型目录已按用途整理: root=%s moved=%s retained=%s manifest=%s",
            base, moved, kept, rewritten,
        )
    return MigrationResult(base, base, moved, kept, moved + rewritten)


def migrate_models(source: Path | str, destination: Path | str) -> MigrationResult:
    """Move an existing model library to another root without data loss.

    Downloads in progress are deliberately left at the old location.  Completed
    models and their catalog selection state are moved, then normalized at the
    destination.  Configuration is not changed here; callers switch it only
    after this function returns successfully.
    """
    source_root = Path(source).resolve()
    destination_root = Path(destination).resolve()
    if source_root == destination_root:
        return normalize_model_layout(source_root)
    if destination_root.is_relative_to(source_root) or source_root.is_relative_to(destination_root):
        raise ValueError("新的模型目录不能是当前模型目录的上级或子目录")
    if not source_root.exists():
        destination_root.mkdir(parents=True, exist_ok=True)
        return normalize_model_layout(destination_root)

    source_manifest = load_manifest(source_root)
    destination_root.mkdir(parents=True, exist_ok=True)
    moved = kept = 0
    for child in list(source_root.iterdir()):
        if child.name in _MIGRATION_SKIP_NAMES:
            continue
        changed, retained = _move_merge(child, destination_root / child.name)
        moved += changed
        kept += retained
    normalized = normalize_model_layout(destination_root)
    _merge_source_manifest(source_manifest, destination_root)
    manifest_rewrites = _rewrite_manifest_layout(destination_root)
    result = MigrationResult(
        source_root,
        destination_root,
        moved + normalized.moved_paths,
        kept + normalized.kept_existing_paths,
        normalized.normalized_paths + manifest_rewrites,
    )
    logger.info("模型迁移完成: source=%s destination=%s moved=%s retained=%s",
                source_root, destination_root, result.moved_paths,
                result.kept_existing_paths)
    return result


def initialize_model_storage(store: "ConfigStore") -> Path:
    """Persist the one-time root decision and normalize an upgrade in place."""
    root = _configured_root(store)
    if root is None:
        legacy = legacy_models_root()
        if has_model_data(legacy):
            root, mode = legacy, "legacy"
        else:
            root, mode = install_models_root(), "install"
        store.update({
            "models_root": str(root),
            "models_root_mode": mode,
            "model_storage_initialized": True,
        })
        logger.info("模型存储已初始化: mode=%s root=%s", mode, root)
    root.mkdir(parents=True, exist_ok=True)
    normalize_model_layout(root)
    return root


__all__ = [
    "MigrationResult",
    "has_model_data",
    "initialize_model_storage",
    "install_models_root",
    "installed_app_dir",
    "legacy_models_root",
    "model_lookup_roots",
    "migrate_models",
    "normalize_model_layout",
    "resolve_models_root",
]
