"""User-level bootstrap for llama.cpp runtimes used by quality translation.

The installer bundles a validated OpenVINO runtime.  Source mode and older
installations may not have that bundle, so the application can provision the
official pinned OpenVINO archive on demand.  Files are downloaded to the
per-user VoxSub directory, verified, and installed with a staging/rename
sequence; the repository and user models are never modified.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from voxsub.logging_setup import get_logger

logger = get_logger("llama_runtime")

LLAMA_VERSION = "b10470"
OPENVINO_ASSET = "llama-b10470-bin-win-openvino-2026.2.1-x64.zip"
OPENVINO_SHA256 = (
    "671B0A0C8D5F58E20DA178732435617B182D7127E62080D2CBE270A7A0D69EBDE"
)
OPENVINO_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMA_VERSION}/{OPENVINO_ASSET}"
)


@dataclass(frozen=True)
class RuntimeStatus:
    directory: Path
    ready: bool
    source: str
    reason: str = ""


def user_runtime_root() -> Path:
    """Return the application-managed runtime root for this user."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "VoxSub" / "tools" / "llama"


def _runtime_candidates() -> tuple[Path, ...]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent / "tools" / "llama")
    roots.append(user_runtime_root())
    return tuple(dict.fromkeys(roots))


def _is_openvino_runtime(directory: Path) -> bool:
    # These files are the minimum loader set.  Checking the plugin as well as
    # ggml-openvino prevents a half-extracted archive from being reported as
    # a usable NPU candidate and then failing much later at process startup.
    required = (
        "llama-server.exe",
        "ggml-openvino.dll",
        "openvino.dll",
        "openvino_intel_npu_plugin.dll",
        "openvino_intel_npu_compiler_loader.dll",
    )
    return directory.is_dir() and all(
        (directory / name).is_file() for name in required
    )


def find_openvino_runtime() -> RuntimeStatus | None:
    """Find an existing OpenVINO runtime without changing the filesystem."""
    for root in _runtime_candidates():
        for candidate in (root, root / "openvino"):
            if _is_openvino_runtime(candidate):
                bundled_root = Path(sys.executable).resolve().parent / "tools" / "llama"
                is_bundled = getattr(sys, "frozen", False) and (
                    candidate == bundled_root or candidate.parent == bundled_root
                )
                source = "bundled" if is_bundled else "user"
                return RuntimeStatus(candidate, True, source)
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _download_verified(archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_suffix(archive.suffix + ".part")
    if archive.is_file() and _sha256(archive) == OPENVINO_SHA256:
        return
    if partial.exists():
        partial.unlink()
    logger.info("开始准备 OpenVINO llama-server 运行时: asset=%s", OPENVINO_ASSET)
    request = urllib.request.Request(
        OPENVINO_URL,
        headers={"User-Agent": "VoxSub-runtime-bootstrap/0.9"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as target:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    actual = _sha256(partial)
    if actual != OPENVINO_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"OpenVINO 运行时 SHA256 校验失败 (expected={OPENVINO_SHA256[:12]} actual={actual[:12]})"
        )
    os.replace(partial, archive)


def _acquire_bootstrap_lock(lock: Path) -> bool:
    try:
        with lock.open("x", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        return True
    except FileExistsError:
        pass
    # Another process is provisioning. Its atomic rename is enough; do not
    # delete its lock or partially written destination.
    for _ in range(120):
        if find_openvino_runtime() is not None:
            return False
        time.sleep(0.25)
    raise RuntimeError("另一个 VoxSub 进程准备 OpenVINO 运行时超时")


def _extract_verified_runtime(archive: Path, staging: Path, pending: Path) -> None:
    staging.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError("OpenVINO 压缩包包含不安全路径")
        bundle.extractall(staging)
    server = next(staging.rglob("llama-server.exe"), None)
    if server is None:
        raise RuntimeError("OpenVINO 压缩包中没有 llama-server.exe")
    pending.mkdir(parents=True, exist_ok=False)
    for item in server.parent.iterdir():
        target = pending / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    if not _is_openvino_runtime(pending):
        raise RuntimeError("OpenVINO 运行时缺少必要 DLL")


def _install_runtime(destination: Path, pending: Path) -> None:
    backup = destination.parent / (".openvino.backup-" + uuid.uuid4().hex)
    if destination.exists():
        os.replace(destination, backup)
    os.replace(pending, destination)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def ensure_openvino_runtime(*, force: bool = False) -> RuntimeStatus:
    """Ensure a usable OpenVINO llama-server exists in the user directory."""
    existing = None if force else find_openvino_runtime()
    if existing is not None:
        return existing
    destination = user_runtime_root() / "openvino"
    if os.name != "nt":
        return RuntimeStatus(destination, False, "unavailable", "仅 Windows 支持该运行时")
    cache_root = destination.parents[2] / "cache" / "llama-runtime"
    archive = cache_root / OPENVINO_ASSET
    staging = cache_root / ("extract-openvino-" + uuid.uuid4().hex)
    pending = destination.parent / (".openvino.pending-" + uuid.uuid4().hex)
    lock = destination.parent / ".openvino.bootstrap.lock"
    acquired = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        acquired = _acquire_bootstrap_lock(lock)
        if not acquired:
            existing = find_openvino_runtime()
            if existing is not None:
                return existing
            raise RuntimeError("OpenVINO 运行时锁已释放但运行时不可用")
        if not force:
            existing = find_openvino_runtime()
            if existing is not None:
                return existing
        _download_verified(archive)
        _extract_verified_runtime(archive, staging, pending)
        _install_runtime(destination, pending)
        logger.info("OpenVINO llama-server 运行时已准备: source=download")
        return RuntimeStatus(destination, True, "download")
    except Exception as exc:
        logger.warning("OpenVINO llama-server 运行时准备失败: %s", exc)
        return RuntimeStatus(destination, False, "error", str(exc))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if pending.exists():
            shutil.rmtree(pending, ignore_errors=True)
        if acquired:
            lock.unlink(missing_ok=True)


__all__ = [
    "LLAMA_VERSION",
    "OPENVINO_ASSET",
    "OPENVINO_SHA256",
    "OPENVINO_URL",
    "RuntimeStatus",
    "find_openvino_runtime",
    "ensure_openvino_runtime",
    "user_runtime_root",
]
