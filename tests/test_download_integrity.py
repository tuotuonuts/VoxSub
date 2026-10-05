"""Offline source contracts, strict error propagation and mixed-layout recovery."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import ssl
from urllib.error import HTTPError, URLError

import pytest

import voxsub.downloader as downloader
from voxsub.catalog_assets import SENSEVOICE_FILES, SENSEVOICE_CHINA_REVISION
from voxsub.download_errors import DownloadError, describe_download_error, safe_source_url
from voxsub.model_catalog import ModelMarketplace, ModelSource, RemoteFile, get_model
from voxsub.model_downloads import ModelDownloads


class Response(io.BytesIO):
    def __init__(self, body: bytes, *, size: int | None = None):
        super().__init__(body)
        self.status = 200
        self.headers = {'Content-Length': str(len(body) if size is None else size), 'ETag': '"test"'}
        self.reads = 0

    def getcode(self):
        return self.status

    def read1(self, size):
        self.reads += 1
        return super().read(size)


def fake_http(monkeypatch, status=404):
    calls = []
    def fail(request, **kwargs):
        calls.append(request)
        raise HTTPError(request.full_url, status, 'private response body', {}, None)
    monkeypatch.setattr(downloader.urlrequest, 'urlopen', fail)
    return calls


@pytest.mark.parametrize('status', [401, 403, 404, 410, 416, 500])
def test_http_error_is_actionable_and_never_echoes_credentials(tmp_path, monkeypatch, capsys, caplog, status):
    calls = fake_http(monkeypatch, status)
    url = 'https://alice:password@example.invalid/file?token=secret'
    with pytest.raises(DownloadError) as result:
        downloader.fetch_file(url, tmp_path / 'weights', raise_on_error=True)
    assert f'HTTP {status}' in str(result.value)
    assert '请' in str(result.value)
    assert calls
    text = capsys.readouterr().out + caplog.text + str(result.value)
    for private in ('password', 'token=secret', 'private response body', 'alice:'):
        assert private not in text


def test_boolean_download_api_remains_compatible(tmp_path, monkeypatch):
    calls = fake_http(monkeypatch)
    assert downloader.fetch_file('https://example.invalid/404', tmp_path / 'weights') is False
    assert len(calls) == 1


@pytest.mark.parametrize('error,code', [
    (URLError(TimeoutError('private timeout')), 'timeout'),
    (URLError(ssl.SSLError('private cert')), 'tls'),
    (OSError('private disk path'), 'connection'),
])
def test_error_classification_has_next_step_not_raw_message(error, code):
    result = describe_download_error(error)
    assert result.code == code and result.suggestion
    assert 'private' not in str(result)


def test_signed_source_url_redaction():
    assert safe_source_url('https://u:password@example.org:8443/file?token=secret#private') == 'https://example.org/file'


def test_wrong_catalog_size_is_rejected_before_reading_or_truncating_partial(tmp_path, monkeypatch):
    response = Response(b'good')
    calls = []
    def opened(*args, **kwargs):
        calls.append(args)
        return response
    monkeypatch.setattr(downloader.urlrequest, 'urlopen', opened)
    dest = tmp_path / 'weights'
    part = tmp_path / 'weights.part'
    part.write_bytes(b'old prefix')
    with pytest.raises(DownloadError, match='服务器 4 字节，预期 245 字节'):
        downloader.fetch_file('https://example.invalid/file', dest, expected_size=245,
                              expected_sha=hashlib.sha256(b'good').hexdigest(), safe_resume=True, raise_on_error=True)
    assert len(calls) == 1 and response.reads == 0
    assert part.read_bytes() == b'old prefix' and not dest.exists()


def test_corrected_size_and_sha_reuse_complete_partial_without_http(tmp_path, monkeypatch):
    payload = b'correct upstream compressed archive'
    dest = tmp_path / 'weights'
    part = tmp_path / 'weights.part'
    part.write_bytes(payload)
    monkeypatch.setattr(downloader.urlrequest, 'urlopen', lambda *a, **k: pytest.fail('complete SHA-verified partial must not redownload/416'))
    assert downloader.fetch_file('https://example.invalid/file', dest, expected_size=len(payload),
                                 expected_sha=hashlib.sha256(payload).hexdigest(), safe_resume=True, raise_on_error=True)
    assert dest.read_bytes() == payload and not part.exists()


def test_sha_failure_reaches_caller_and_never_publishes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.urlrequest, 'urlopen', lambda *a, **k: Response(b'bad!'))
    dest = tmp_path / 'weights'
    with pytest.raises(DownloadError, match='SHA-256'):
        downloader.fetch_file('https://example.invalid/file', dest, expected_size=4,
                              expected_sha=hashlib.sha256(b'good').hexdigest(), raise_on_error=True)
    assert not dest.exists()


@pytest.mark.parametrize('model_id,size,sha', [
    ('asr-sensevoice-small-int8', 163002883, '7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e'),
    ('asr-funasr-nano-2512-int8', 841730611, 'eb43d7ccc2e86b243f6a03b7df361033dda66db9523d1a92bf6aca2b50c9476b'),
    ('asr-qwen3-0.6b-int8', 878702423, '393f8a14e2f5fb96746aaab342997a40641001fbd5bf9592a080a8329178ee96'),
])
def test_archives_pin_verified_release_size_and_sha(model_id, size, sha):
    model = get_model(model_id)
    assert model.download_bytes == size and model.sha256 == sha


def test_sensevoice_china_uses_pinned_equivalent_onnx_files_not_missing_archive():
    model = get_model('asr-sensevoice-small-int8')
    source = next(s for s in model.sources if s.id == 'china')
    assert [(f.install_rel, f.size, f.sha256) for f in source.files] == list(SENSEVOICE_FILES)
    assert all('/resolve/' + SENSEVOICE_CHINA_REVISION + '/' in f.url for f in source.files)
    assert source.probe_url.endswith('/tokens.txt')
    assert not any('/asr-models/' in f.url for f in source.files)
    assert sum(f.size for f in source.files) == 239549735


def progress_model():
    return replace(get_model('asr-sensevoice-small-int8'), id='mixed-transfer', install_rel='test/model',
                   asset_name='archive.tar.bz2', download_bytes=32, sha256='',
                   sources=(ModelSource('global', '海外', 'https://example.invalid/global', ''),
                            ModelSource('china', '大陆', '', '', files=(RemoteFile('https://example.invalid/model','model.int8.onnx',64),
                                                                     RemoteFile('https://example.invalid/tokens','tokens.txt',4)))))


def mixed_partials(manager, model):
    manager.folder.mkdir(parents=True)
    (manager.folder / model.asset_name).with_name(model.asset_name + '.part').write_bytes(b'a' * 12)
    files = manager.folder / model.id
    files.mkdir()
    (files / 'model.int8.onnx.part').write_bytes(b'm' * 48)


def test_china_progress_and_restart_do_not_clamp_to_archive_or_count_both_layouts(tmp_path):
    model = progress_model()
    manager = ModelDownloads(ModelMarketplace(tmp_path))
    mixed_partials(manager, model)
    state = manager.prepare(model, 'china')
    assert state['completed'] == 48 and state['total'] == 68
    assert '_active_source' not in state
    manager.pause(model, state['token'])
    restored = ModelDownloads(ModelMarketplace(tmp_path)).snapshot(model)
    assert restored['status'] == 'paused' and restored['completed'] == 48 and restored['total'] == 68
    assert '_active_source' not in restored
    assert (manager.folder / 'archive.tar.bz2.part').stat().st_size == 12


def test_legacy_record_recomputes_actual_source_totals_without_auto_start(tmp_path):
    model = progress_model()
    manager = ModelDownloads(ModelMarketplace(tmp_path))
    mixed_partials(manager, model)
    (manager.folder / 'archive.tar.bz2.part').write_bytes(b'a' * 32)
    (manager.folder / model.id / 'model.int8.onnx.part').write_bytes(b'm' * 8)
    record = manager._record_path(model)
    record.parent.mkdir()
    record.write_text(json.dumps(dict(modelId=model.id,status='downloading',token='legacy',revision=2,source='china',error='',total=245000000)))
    state = manager.snapshot(model)
    assert state['status'] == 'paused' and state['completed'] == 8 and state['total'] == 68
    assert manager.workers == {}


def test_actual_fallback_source_survives_failure_and_restart(tmp_path, monkeypatch):
    model = progress_model()
    market = ModelMarketplace(tmp_path)
    manager = ModelDownloads(market)
    mixed_partials(manager, model)
    reports = []
    manager.emit = reports.append
    def fail(_model, **kwargs):
        kwargs['progress'](48, 68, '大陆')
        raise DownloadError('http', 'HTTP 404：地址不存在', '请更换源')
    monkeypatch.setattr(market, 'install', fail)
    prepared = manager.prepare(model, 'global')
    with pytest.raises(DownloadError):
        manager.run(model, prepared['token'], lambda: False)
    state = ModelDownloads(ModelMarketplace(tmp_path)).snapshot(model)
    assert state['completed'] == 48 and state['total'] == 68 and state['source'] == 'global'
    assert 'HTTP 404' in state['error']
    assert all('_active_source' not in r for r in reports)


def test_all_source_errors_reach_paused_state_with_partial_intact(tmp_path, monkeypatch):
    fake_http(monkeypatch)
    payload = b'good model'
    model = replace(get_model('asr-sensevoice-small-int8'), id='error-chain',install_rel='test/model',asset_name='model.bin',
                    archive=False,required_paths=('model.bin',),download_bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),
                    sources=(ModelSource('global','海外','https://example.invalid/global',''),ModelSource('china','大陆','https://example.invalid/china','')))
    market = ModelMarketplace(tmp_path)
    manager = ModelDownloads(market)
    manager.folder.mkdir()
    part = manager.folder / 'model.bin.part'
    part.write_bytes(payload[:3])
    state = manager.prepare(model, 'global')
    with pytest.raises(RuntimeError, match='HTTP 404'):
        manager.run(model, state['token'], lambda: False)
    paused = manager.snapshot(model)
    assert paused['status'] == 'paused' and '海外' in paused['error'] and '大陆' in paused['error']
    assert '请更换' in paused['error'] and part.read_bytes() == payload[:3]
    assert not market.is_installed(model)
