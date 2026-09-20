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


# ---------------------------------------------------------------- 缓存配置有效性
#
# 缺陷 #8（缓存配置失效）：条目名只由时间戳+随机串组成，**不含任何配置维度**，
# 于是"改了设置以后旧缓存仍然命中"。下面这些用例把"缓存键必须带配置指纹"这条
# 规则钉住：改配置 → 旧条目不再命中 → 由既有的 prune 机制清掉。


def _write(cache, kind: str, payload: bytes):
    path = cache.allocate(kind)
    path.write_bytes(payload)
    return cache.finalize(kind, path)


def test_entry_name_carries_the_configuration_signature(cache_root):
    cache = OcrImageCache(cache_root, signature="deadbeefcafe0001")
    path = _write(cache, "original", b"pixels")

    assert path.name.split("-")[1] == "deadbeefcafe0001", path.name
    assert cache.entry_signature(path) == "deadbeefcafe0001"
    assert cache.matches(path)


def test_changed_configuration_makes_old_entries_miss(cache_root):
    """**核心回归**：改配置后旧条目不能再命中。"""
    old = OcrImageCache(cache_root, signature="aaaa1111aaaa1111")
    stale = _write(old, "translated", b"old-config-output")

    # 用户改了设置（这里用同一个 root 上的新签名代表"另一套配置"）。
    new = OcrImageCache(cache_root, signature="bbbb2222bbbb2222")

    assert stale.exists(), "前置条件：旧条目确实在盘上"
    assert not new.matches(stale), "旧配置的条目不该被判定为当前配置"
    assert new.latest("translated") is None, "改配置后必须未命中"

    fresh = _write(new, "translated", b"new-config-output")
    assert new.latest("translated") == fresh.resolve()
    assert new.matches(fresh)


def test_invalidate_stale_drops_only_other_configurations(cache_root):
    old = OcrImageCache(cache_root, signature="aaaa1111aaaa1111")
    stale_original = _write(old, "original", b"old-original")
    stale_translated = _write(old, "translated", b"old-translated")

    new = OcrImageCache(cache_root, signature="bbbb2222bbbb2222")
    kept_original = _write(new, "original", b"new-original")
    kept_translated = _write(new, "translated", b"new-translated")

    removed = new.invalidate_stale()

    assert not stale_original.exists()
    assert not stale_translated.exists()
    assert kept_original.exists() and kept_translated.exists()
    assert {path.name for path in removed} == {
        stale_original.name, stale_translated.name}


def test_signature_is_computed_by_the_ocr_module_not_duplicated_here(cache_root):
    """维度表只有一份：改一个影响结果的配置键，缓存签名必须跟着变。"""
    from voxsub.ocr import ocr_result_signature

    baseline = {"translate_tier": "fast", "lang_pair": "zh-en"}
    changed = dict(baseline, lang_pair="ja-zh")
    cache = OcrImageCache(
        cache_root, signature=ocr_result_signature(baseline))
    entry = _write(cache, "original", b"pixels")

    other = OcrImageCache(cache_root, signature=ocr_result_signature(changed))
    assert not other.matches(entry)
    assert other.latest("original") is None


def test_unconfigured_cache_keeps_legacy_behaviour(cache_root):
    """不传签名时旧缓存目录照常可用：命名/命中/淘汰都不变。"""
    cache = OcrImageCache(cache_root, limit=15)
    path = _write(cache, "original", b"legacy")

    assert path.name.count("-") == 1, path.name
    assert cache.entry_signature(path) == ""
    assert cache.matches(path), "未配置签名时任何条目都算命中"
    assert cache.latest("original") == path
    assert cache.invalidate_stale() == (), "没配置配置就不该删用户已有缓存"
    assert path.exists()


def test_prune_still_bounds_each_kind_with_a_signature(cache_root):
    cache = OcrImageCache(cache_root, limit=1, signature="deadbeefcafe0001")
    first = _write(cache, "original", b"first")
    second = _write(cache, "original", b"second")
    translated = _write(cache, "translated", b"translated")

    assert not first.exists()
    assert second.exists()
    assert translated.exists()


def test_cache_file_tags_the_copy_with_a_signature(cache_root, tmp_path):
    source = tmp_path / "shot.png"
    source.write_bytes(b"pixels")
    cache = OcrImageCache(cache_root, signature="aaaa1111aaaa1111")

    copied = cache.cache_file(source, kind="original", signature="cccc3333cccc3333")

    assert cache.entry_signature(copied) == "cccc3333cccc3333"
    assert cache.latest("original") is None, "当前配置是 aaaa…，不该命中 cccc…"
