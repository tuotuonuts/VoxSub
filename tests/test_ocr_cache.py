from __future__ import annotations

from pathlib import Path

import pytest

from voxsub.ocr_cache import (
    OcrCacheLocationError,
    OcrImageCache,
    is_system_drive,
    validate_ocr_cache_root,
)


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Only cache-location tests need the non-C-drive product policy itself."""
    monkeypatch.setattr("voxsub.ocr_cache.is_system_drive", lambda _path: False)
    return tmp_path / "OCR"


def test_c_drive_is_rejected_even_when_path_does_not_exist():
    assert is_system_drive(Path("C:/VoxSub/Cache/OCR"))
    with pytest.raises(OcrCacheLocationError):
        validate_ocr_cache_root(Path("C:/VoxSub/Cache/OCR"))


def test_original_and_translated_are_physically_separate(cache_root):
    cache = OcrImageCache(cache_root, limit=15)
    assert cache.directory("original").name == "originals"
    assert cache.directory("translated").name == "translated"
    assert cache.directory("original") != cache.directory("translated")


def test_cache_prunes_each_kind_independently(cache_root):
    cache = OcrImageCache(cache_root, limit=1)
    first = cache.allocate("original")
    first.write_bytes(b"first")
    cache.finalize("original", first)
    second = cache.allocate("original")
    second.write_bytes(b"second")
    cache.finalize("original", second)
    translated = cache.allocate("translated")
    translated.write_bytes(b"translated")
    cache.finalize("translated", translated)

    assert not first.exists()
    assert second.exists()
    assert translated.exists()


def test_zero_cache_limit_is_unlimited(cache_root):
    cache = OcrImageCache(cache_root, limit=0)
    paths = []
    for index in range(3):
        path = cache.allocate("original")
        path.write_bytes(str(index).encode())
        cache.finalize("original", path)
        paths.append(path)
    assert all(path.exists() for path in paths)
