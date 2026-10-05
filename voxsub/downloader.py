"""Resumable single-file downloads with bounded retries and atomic commit."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest

from voxsub.file_io import replace_with_retry, write_text_atomically
from voxsub.logging_setup import get_logger

logger = get_logger("downloader")

CHUNK = 1 << 20
DOWNLOAD_ATTEMPTS_PER_SOURCE = 3
_PERMANENT_HTTP_ERRORS = {400, 401, 403, 404, 410}
_DOWNLOAD_ERRORS = (
    urlerror.URLError,
    OSError,
    TimeoutError,
    http.client.HTTPException,
)


class DownloadCancelled(RuntimeError):
    """Raised when a marketplace download is cancelled by the user."""


def sha256_of(path: Path | str, *, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(CHUNK):
            if cancelled and cancelled():
                raise DownloadCancelled("下载已暂停")
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class _ResponseLayout:
    status: int
    resumed: int
    response_bytes: int
    total: int


@dataclass
class _DownloadTarget:
    destination: Path
    expected_sha: str | None
    expected_size: int | None
    progress: Callable[[int, int, str], None] | None
    cancelled: Callable[[], bool] | None
    safe_resume: bool = False

    def __post_init__(self) -> None:
        self.destination = Path(self.destination)
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.part = self.destination.with_name(self.destination.name + ".part")
        self.metadata = self.part.with_name(self.part.name + ".resume.json")

    def check_cancelled(self) -> None:
        if self.cancelled and self.cancelled():
            raise DownloadCancelled("下载已取消")

    def matches_expected(self, path: Path, *, require_constraint: bool = False) -> bool:
        if not path.is_file():
            return False
        if require_constraint and self.expected_size is None and not self.expected_sha:
            return False
        if self.expected_size is not None and path.stat().st_size != self.expected_size:
            return False
        if self.expected_sha and sha256_of(path, cancelled=self.cancelled) != self.expected_sha:
            return False
        return True

    def prepare_existing(self) -> bool:
        """Return True when a valid final destination can be reused."""
        if self.destination.is_file():
            if self.matches_expected(self.destination):
                logger.info("复用已完成下载: %s", self.destination)
                return True
            if (not self.part.exists() and self.expected_size is not None and
                    self.destination.stat().st_size < self.expected_size):
                replace_with_retry(self.destination, self.part)
                logger.info("恢复旧版未完成下载为断点文件: %s (%d/%d)",
                            self.part, self.part.stat().st_size, self.expected_size)
            else:
                self.destination.unlink(missing_ok=True)
        if (self.part.is_file() and self.expected_size is not None and
                self.part.stat().st_size > self.expected_size):
            logger.warning("断点文件超过预期大小，重新下载: %s actual=%d expected=%d",
                           self.part, self.part.stat().st_size, self.expected_size)
            self.part.unlink(missing_ok=True)
        return False

    def commit(self) -> bool:
        if self.expected_size is not None and self.part.stat().st_size != self.expected_size:
            return False
        if self.expected_sha:
            actual = sha256_of(self.part, cancelled=self.cancelled)
            if actual != self.expected_sha:
                print(f"  [错误] SHA256 不匹配: 期望 {self.expected_sha}, 实际 {actual}")
                logger.error("SHA256 校验失败: %s 期望=%s 实际=%s",
                             self.destination.name, self.expected_sha, actual)
                self.part.unlink(missing_ok=True)
                return False
            print("  SHA256 校验通过")
        self.check_cancelled()
        replace_with_retry(self.part, self.destination)
        self.metadata.unlink(missing_ok=True)
        size_mb = self.destination.stat().st_size / 1e6
        print(f"  下载完成: {self.destination.name} ({size_mb:.1f} MB)")
        logger.info("下载完成: %s (%.1f MB)", self.destination.name, size_mb)
        return True

    def validate_transfer(
        self,
        layout: _ResponseLayout,
        received: int,
    ) -> None:
        actual_size = self.part.stat().st_size
        if layout.response_bytes and received < layout.response_bytes:
            raise OSError(
                f"CDN 提前断流: 本次收到 {received}/{layout.response_bytes} 字节")
        if layout.total and actual_size < layout.total:
            raise OSError(f"下载未完成: 当前 {actual_size}/{layout.total} 字节")
        if layout.total and actual_size > layout.total:
            self.part.unlink(missing_ok=True)
            raise OSError(f"下载大小超出预期: 当前 {actual_size}/{layout.total} 字节")


def _candidate_sources(
    url: str,
    mirror: str | None,
    mirrors: Iterable[str] | None,
) -> list[str]:
    sources = [url]
    if mirror:
        sources.append(mirror)
    if mirrors:
        sources.extend(item for item in mirrors if item)
    return list(dict.fromkeys(sources))


def _integer_header(headers, key: str) -> int:
    try:
        return int(headers.get(key, "0"))
    except (TypeError, ValueError):
        return 0


def _response_layout(response, existing: int, expected_size: int | None) -> _ResponseLayout:
    status = int(getattr(response, "status", response.getcode()))
    resumed = existing if status == 206 else 0
    content_range = response.headers.get("Content-Range", "")
    reported_total = 0
    if "/" in content_range:
        try:
            reported_total = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            pass
    response_bytes = _integer_header(response.headers, "Content-Length")
    total = expected_size or reported_total or (
        resumed + response_bytes if response_bytes else 0)
    if status == 206:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        if not match or int(match[1]) != existing or int(match[2]) < existing:
            raise OSError("服务器续传范围与本地断点不一致")
        if response_bytes and response_bytes != int(match[2]) - existing + 1:
            raise OSError("服务器续传长度不一致")
    elif status != 200:
        raise OSError(f"不支持的下载响应: {status}")
    if expected_size is not None and reported_total and reported_total != expected_size:
        raise OSError(f"服务器总大小异常: {reported_total}, 预期 {expected_size}")
    return _ResponseLayout(status, resumed, response_bytes, total)


def _stream_response(
    target: _DownloadTarget,
    source: str,
    response,
    layout: _ResponseLayout,
) -> int:
    received = 0
    # read1 returns available bytes instead of waiting to fill a 1 MiB block on slow links.
    read_block = getattr(response, "read1", response.read)
    mode = "ab" if layout.status == 206 else "wb"
    with target.part.open(mode) as output:
        written = layout.resumed
        if target.progress:
            target.progress(written, layout.total, source)
        while True:
            target.check_cancelled()
            block = read_block(CHUNK)
            target.check_cancelled()
            if not block:
                return received
            output.write(block)
            output.flush()
            received += len(block)
            written += len(block)
            if target.progress:
                target.progress(written, layout.total, source)


def _resume_metadata(target: _DownloadTarget, source: str) -> dict:
    if not target.safe_resume:
        return {}
    try:
        data = json.loads(target.metadata.read_text(encoding="utf-8"))
        identity = hashlib.sha256(source.encode()).hexdigest()
        if data.get("source") == identity and isinstance(data.get("validator"), str):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _resume_headers(target: _DownloadTarget, source: str) -> tuple[dict, int, dict]:
    existing = target.part.stat().st_size if target.part.exists() else 0
    metadata = _resume_metadata(target, source)
    validator = metadata.get("validator", "")
    if "\n" in validator or "\r" in validator:
        validator = ""
    if existing and target.safe_resume and not target.expected_sha and not validator:
        target.part.unlink(missing_ok=True)
        existing = 0
    headers = {"User-Agent": "VoxSub/0.9", "Accept-Encoding": "identity"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
        if validator:
            headers["If-Range"] = validator
    return headers, existing, metadata


def _remember_response(target: _DownloadTarget, source: str, response, layout: _ResponseLayout,
                       metadata: dict) -> None:
    if not target.safe_resume:
        return
    etag = response.headers.get("ETag", "")
    validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified", "")
    if layout.resumed and metadata.get("validator") and validator != metadata["validator"]:
        target.part.unlink(missing_ok=True)
        target.metadata.unlink(missing_ok=True)
        raise OSError("远端文件已变化，安全重新下载")
    if layout.status == 200:
        # Truncate BEFORE publishing the new identity: a crash cannot bind old bytes to a new ETag.
        target.part.write_bytes(b"")
    data = {"source": hashlib.sha256(source.encode()).hexdigest(), "validator": validator}
    write_text_atomically(target.metadata, json.dumps(data), encoding="utf-8")


def _download_attempt(target: _DownloadTarget, source: str) -> bool:
    target.check_cancelled()
    can_promote = not target.safe_resume or bool(target.expected_sha)
    if can_promote and target.matches_expected(target.part, require_constraint=True):
        return target.commit()
    headers, existing, metadata = _resume_headers(target, source)
    request = urlrequest.Request(source, headers=headers)
    with urlrequest.urlopen(request, timeout=15) as response:
        target.check_cancelled()
        layout = _response_layout(response, existing, target.expected_size)
        _remember_response(target, source, response, layout, metadata)
        received = _stream_response(target, source, response, layout)
    target.check_cancelled()
    target.validate_transfer(layout, received)
    return target.commit()


def _download_from_source(target: _DownloadTarget, source: str) -> bool:
    for attempt in range(1, DOWNLOAD_ATTEMPTS_PER_SOURCE + 1):
        try:
            return _download_attempt(target, source)
        except DownloadCancelled:
            logger.info("下载已取消，保留断点文件: %s", target.part)
            raise
        except _DOWNLOAD_ERRORS as exc:
            target.check_cancelled()
            print(f"  源 {source} 第 {attempt}/{DOWNLOAD_ATTEMPTS_PER_SOURCE} 次失败: {exc}")
            logger.warning(
                "下载源失败: 目标=%s 源=%s attempt=%d/%d 已有字节=%d 错误=%s",
                target.destination.name, source, attempt,
                DOWNLOAD_ATTEMPTS_PER_SOURCE,
                target.part.stat().st_size if target.part.exists() else 0,
                exc,
            )
            if isinstance(exc, urlerror.HTTPError):
                if exc.code == 416:
                    target.part.unlink(missing_ok=True)
                elif exc.code in _PERMANENT_HTTP_ERRORS:
                    return False
    return False


def fetch_file(
    url: str,
    destination: Path | str,
    expected_sha: str | None = None,
    mirror: str | None = None,
    *,
    mirrors: Iterable[str] | None = None,
    expected_size: int | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    safe_resume: bool = False,
) -> bool:
    """Download one file with resume, verification, retries, and mirrors."""
    normalized_size = expected_size if expected_size and expected_size > 0 else None
    target = _DownloadTarget(
        Path(destination), expected_sha, normalized_size, progress, cancelled, safe_resume)
    sources = _candidate_sources(url, mirror, mirrors)
    logger.info("开始下载 %s (候选源 %d 个)", target.destination.name, len(sources))
    if target.prepare_existing():
        return True
    for source in sources:
        if _download_from_source(target, source):
            return True
    print("  [错误] 所有源均失败")
    logger.error("所有下载源均失败: %s", target.destination.name)
    return False


__all__ = ["CHUNK", "DownloadCancelled", "fetch_file", "sha256_of"]
