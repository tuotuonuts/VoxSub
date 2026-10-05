"""Contracts for the three executable, dual-source marketplace additions."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib
import numpy as np
import pytest

from voxsub.model_catalog import CATALOG, ModelMarketplace, ModelSource, RemoteFile, get_model
from voxsub.asr import OfflineGenerativeASR
from voxsub.tts import TTSEngine
from voxsub.models import DownloadCancelled

IDS = ("asr-moonshine-tiny-en-v2", "asr-parakeet-tdt-0.6b-v3-int8", "tts-kokoro-v1.1-int8-zh-en")

@pytest.mark.parametrize("mid", IDS)
def test_new_models_have_honest_dual_sources_and_integrity(mid):
    m = get_model(mid)
    assert m is not None and m.archive and len(m.sha256) == 64
    assert [s.id for s in m.sources] == ["global", "china"]
    assert not any((m.npu_supported, m.gpu_supported, m.igpu_supported))
    china = m.sources[1]
    if china.files:
        assert "第三方" in china.label
        assert {f.install_rel for f in china.files} == set(m.required_paths)
        assert sum(f.size for f in china.files) == m.installed_bytes
        assert all(f.size > 0 and len(f.sha256) == 64 and "/main/" not in f.url for f in china.files)
    else:
        assert "modelscope.cn" in china.url
    assert len({m.id for m in CATALOG}) == len(CATALOG)

@pytest.mark.parametrize("mid", IDS)
def test_missing_runtime_file_is_never_installed(mid, tmp_path):
    m = get_model(mid); market = ModelMarketplace(tmp_path)
    target = market.model_dir(m)
    for name in m.required_paths:
        p = target / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"stub")
    assert market.is_installed(m)
    missing = m.required_paths[-1]
    (target / missing).rename(target / (missing + ".absent"))
    assert not market.is_installed(m) and missing in market.missing_paths(m)

@pytest.mark.parametrize("mid,factory", [(IDS[0], "from_moonshine_v2"), (IDS[1], "from_transducer")])
def test_offline_factory_contract_and_cached_sentence(mid, factory, tmp_path, monkeypatch):
    import voxsub.asr as asr
    m = get_model(mid)
    for name in m.required_paths: (tmp_path / name).write_bytes(b"stub")
    native = SimpleNamespace(result=SimpleNamespace(text="Hello world"), accept_waveform=Mock())
    recognizer = SimpleNamespace(create_stream=Mock(return_value=native), decode_stream=Mock())
    constructor = Mock(return_value=recognizer)
    monkeypatch.setattr(asr.sherpa_onnx.OfflineRecognizer, factory, constructor)
    engine = OfflineGenerativeASR(tmp_path, m.runtime, num_threads=2, source_lang="en")
    kw = constructor.call_args.kwargs
    assert kw["provider"] == "cpu" and kw["num_threads"] == 2
    if factory == "from_transducer": assert kw["model_type"] == "nemo_transducer" and kw["joiner"].endswith("joiner.int8.onnx")
    else: assert kw["encoder"].endswith("encoder_model.ort")
    stream = engine.create_stream(); engine.feed(stream, np.ones(1600, dtype=np.float32))
    assert engine.get_result(stream) == ""
    assert engine.decode(stream) == engine.decode(stream) == "Hello world"
    recognizer.decode_stream.assert_called_once()
    (tmp_path / m.required_paths[0]).rename(tmp_path / "missing-weight")
    with pytest.raises(FileNotFoundError): OfflineGenerativeASR(tmp_path, m.runtime)
    assert constructor.call_count == 1

@pytest.mark.parametrize("lang,sid", [("zh", 3), ("en", 0)])
def test_kokoro_config_and_synthesis_keep_language_voice(lang, sid, tmp_path, monkeypatch):
    import sherpa_onnx
    m = get_model(IDS[2]); market = ModelMarketplace(tmp_path); d = market.model_dir(m)
    for name in m.required_paths:
        p = d / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"stub")
    engine = TTSEngine(tmp_path / "tts", model_ids={lang: m.id})
    capture = Mock(return_value=SimpleNamespace(generate=Mock(return_value=SimpleNamespace(samples=np.ones(2400), sample_rate=24000))))
    monkeypatch.setattr(sherpa_onnx, "OfflineTts", capture)
    audio = engine.synthesize("test", lang)
    assert audio is not None and audio.dtype == np.float32 and audio.shape == (1600,)
    cfg = capture.call_args.args[0]
    assert cfg.model.kokoro.voices.endswith("voices.bin")
    assert "lexicon-zh.txt" in cfg.model.kokoro.lexicon and "lexicon-us-en.txt" in cfg.model.kokoro.lexicon
    assert cfg.model.kokoro.data_dir.endswith("espeak-ng-data")
    assert cfg.model.kokoro.dict_dir.endswith("dict")
    assert cfg.model.kokoro.lang == ("zh" if lang == "zh" else "en-us")
    assert "number-zh.fst" in cfg.rule_fsts
    capture.return_value.generate.assert_called_once_with("test", sid=sid, speed=1.0)


def test_incomplete_selected_kokoro_does_not_load_as_vits(tmp_path):
    m = get_model(IDS[2]); d = ModelMarketplace(tmp_path).model_dir(m); d.mkdir(parents=True)
    for name in ("model.int8.onnx", "voices.bin", "tokens.txt"): (d/name).write_bytes(b"stub")
    engine = TTSEngine(tmp_path / "tts", model_ids={"zh": m.id})
    assert engine.synthesize("你好", "zh") is None


def test_legacy_vits_fallback_preserves_speaker_zero(tmp_path):
    import sherpa_onnx
    d = tmp_path/"tts"/"zh"; d.mkdir(parents=True)
    for name in ("model.onnx", "tokens.txt", "lexicon.txt"): (d/name).write_bytes(b"stub")
    engine = TTSEngine(tmp_path/"tts", model_ids={"zh": IDS[2]})
    cfg = engine._tts_model_config(sherpa_onnx, d, "zh", d/"model.onnx", d/"tokens.txt")
    assert cfg.vits.model.endswith("model.onnx") and not cfg.kokoro.model
    assert engine._speaker_ids["zh"] == 0

@pytest.mark.parametrize("mid", IDS[1:])
def test_china_file_integrity_arguments_and_source_fallback(mid, tmp_path, monkeypatch):
    import voxsub.model_catalog as catalog
    payload=b"verified"; sha=hashlib.sha256(payload).hexdigest(); calls=[]
    f=RemoteFile("https://example.invalid/file", "tokens.txt",len(payload),sha)
    china=ModelSource("china","China",f.url,f.url,(f,)); global_source=ModelSource("global","Global","https://example.invalid/archive","https://example.invalid")
    model=replace(get_model(mid),sources=(global_source,china),required_paths=("tokens.txt",))
    def fetch(url,dest,**kw):
        calls.append(url)
        if url==global_source.url: return False
        assert kw["expected_sha"]==sha and kw["expected_size"]==len(payload)
        dest.parent.mkdir(parents=True,exist_ok=True); dest.write_bytes(payload); return True
    monkeypatch.setattr(catalog,"fetch_file",fetch)
    market=ModelMarketplace(tmp_path)
    assert market.install(model,"global").joinpath("tokens.txt").read_bytes()==payload
    assert calls==[global_source.url,f.url]

@pytest.mark.parametrize("mid", IDS[1:])
def test_china_cancellation_never_falls_back_to_overseas(mid, tmp_path, monkeypatch):
    import voxsub.model_catalog as catalog
    calls=[]
    def fetch(url,*args,**kw): calls.append(url); raise DownloadCancelled("cancelled")
    monkeypatch.setattr(catalog,"fetch_file",fetch)
    m=get_model(mid)
    with pytest.raises(DownloadCancelled): ModelMarketplace(tmp_path).install(m,"china")
    assert calls == [m.sources[1].files[0].url]

def test_mirror_urls_encode_spaces_and_special_paths():
    from urllib.parse import unquote, urlsplit
    source = get_model(IDS[2]).sources[1]
    entry = next(f for f in source.files if f.install_rel.endswith("Mr serious"))
    assert "%20" in entry.url and " " not in entry.url
    assert unquote(urlsplit(entry.url).path).endswith(entry.install_rel)


def test_download_cache_lock_does_not_misreport_committed_model(tmp_path, monkeypatch):
    import voxsub.model_catalog as catalog
    content = b"ready"
    item = RemoteFile("https://example.invalid", "tokens.txt", len(content), hashlib.sha256(content).hexdigest())
    source = ModelSource("china", "test", item.url, item.url, (item,))
    m = replace(get_model(IDS[1]), required_paths=("tokens.txt",), sources=(source,))
    market = ModelMarketplace(tmp_path)
    staged = market._downloads / m.id / item.install_rel
    staged.parent.mkdir(parents=True); staged.write_bytes(content)
    def locked(*args, **kwargs): raise PermissionError("OneDrive temporary lock")
    monkeypatch.setattr(catalog.shutil, "rmtree", locked)
    assert market.install(m, "china").joinpath("tokens.txt").read_bytes() == content
    assert market.is_installed(m)
    assert market._state_path.is_file()

@pytest.mark.parametrize("preference", ["auto", "global", "china"])
def test_marketplace_ipc_passes_source_and_real_progress_parameter(preference, monkeypatch, tmp_path):
    import threading
    backend = Path(__file__).resolve().parents[1] / "frontend" / "backend"
    monkeypatch.syspath_prepend(str(backend))
    from handlers.models import ModelsHandlers
    import handlers.models as handlers
    install = Mock(); m = get_model(IDS[0])
    handler = ModelsHandlers()
    handler._lock = threading.RLock()
    handler._model_downloads = {}
    handler._marketplace = lambda args: SimpleNamespace(install=install, models_dir=tmp_path, is_installed=lambda model:False)
    handler._spec = lambda mid: m
    event = Mock(); monkeypatch.setattr(handlers, "_event", event)
    result = handler._cmd_install_model({"model_id": m.id, "source": preference})
    assert result["model_id"] == m.id and result["download"]["status"] == "done"
    assert install.call_args.kwargs["preference"] == preference
    assert "progress_callback" not in install.call_args.kwargs
    assert callable(install.call_args.kwargs["cancelled"])
    assert event.call_args.kwargs["status"] == "done"
    with pytest.raises(ValueError): handler._cmd_install_model({"model_id": m.id, "source": "invalid"})
    assert install.call_count == 1
