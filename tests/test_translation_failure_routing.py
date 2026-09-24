"""Offline regression contracts for routing after quality-engine failure."""
import pytest

from voxsub import pipeline as module


class FakeTranslator:
    def __init__(self, pairs=(), *, ready=True, broken=False):
        self.pairs = pairs
        self.available = ready
        self.broken = broken
        self.calls = []
        self.closed = 0

    def supports(self, src, dst):
        return (src, dst) in self.pairs

    def ready(self):
        return self.available

    def warmup(self):
        return not self.broken

    def translate(self, text, src, dst):
        self.calls.append((text, src, dst))
        if self.broken:
            raise RuntimeError("deterministic quality failure")
        assert self.supports(src, dst)
        return "有效译文"

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("entry", ["sentence", "warmup", "draft"])
@pytest.mark.parametrize("pair,cloud_ready,cloud_supports,expected", [
    (("ja", "zh"), True, True, "cloud"),
    (("ja", "zh"), False, True, None),
    (("ja", "zh"), True, False, None),
    (("en", "zh"), True, True, "opus-fast"),
])
def test_quality_failure_routes_only_to_usable_candidates(
        monkeypatch, entry, pair, cloud_ready, cloud_supports, expected):
    pipe = module.Pipeline()
    pipe._src_lang, pipe._dst_lang = pair
    broken = FakeTranslator([pair], broken=True)
    opus = FakeTranslator([("en", "zh"), ("zh", "en")])
    cloud = FakeTranslator([pair] if cloud_supports else [], ready=cloud_ready)
    pipe._translator, pipe._trans_kind = broken, "qwen-quality"
    pipe._trans_pair = pair
    loaded = []

    def load(kind, config=None):
        loaded.append(kind)
        # A failed quality engine must not be recreated, even if it advertises
        # support/readiness. Otherwise each sentence restarts the same engine.
        assert kind != "qwen-quality"
        return {"opus-fast": opus, "cloud": cloud}[kind], kind

    monkeypatch.setattr(module, "_load_translator", load)
    statuses = []
    pipe.on_status(statuses.append)
    if entry == "warmup":
        pipe._warmup_translator()
    elif entry == "draft":
        from voxsub.live_draft import DraftTranslationRequest
        pipe._translate_draft(DraftTranslationRequest(1, "こんにちは" if pair[0] == "ja" else "Hello"))
    else:
        pipe._translate_sentence("こんにちは" if pair[0] == "ja" else "Hello")

    assert pipe._trans_kind == expected
    assert broken.closed == 1
    assert pipe._trans_pair == pair
    assert statuses
    if expected is None:
        assert isinstance(pipe._translator, module._NoopTranslator)
        assert pipe._translator.translate("原文", *pair) == "原文 〔翻译待装〕"
        assert any("原文" in status for status in statuses)
        assert opus.closed == cloud.closed == 1
    else:
        assert pipe._translator.translate("こんにちは", *pair) == "有效译文"
        if expected == "cloud":
            assert opus.closed == 1
            assert cloud.closed == 0
    if pair[0] == "ja":
        assert not opus.calls
    pipe._disable_quality_translator()
    assert len(loaded) == len(set(loaded))
    assert broken.closed == 1


@pytest.mark.parametrize("entry", ["sentence", "warmup", "draft"])
@pytest.mark.parametrize("model_pair,cloud_ready,expected", [
    (None, True, "cloud"),
    (("zh", "en"), True, "cloud"),  # Other direction does not make en->zh ready.
    (None, False, None),
    (("en", "zh"), True, "opus-fast"),
])
def test_real_opus_availability_controls_quality_fallback(
        monkeypatch, tmp_path, entry, model_pair, cloud_ready, expected):
    from voxsub.translate import opus as opus_module

    # Exercise the real OPUS supports/availability interface, without models,
    # inference, network, or reliance on the user's installed model directory.
    if model_pair:
        directory = tmp_path / ("opus_" + "_".join(model_pair))
        directory.mkdir()
        for name in ("encoder_model_int8.onnx", "decoder_model_int8.onnx",
                     "config.json", "tokenizer.json"):
            (directory / name).touch()

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def translate_str(self, text, **kwargs):
            return "有效译文"

    monkeypatch.setattr(opus_module, "_OpusModel", Model)
    opus = opus_module.OpusFastTranslator(model_dir=tmp_path)
    closed = []
    close = opus.close
    def close_opus():
        closed.append(True)
        close()
    monkeypatch.setattr(opus, "close", close_opus)
    cloud = FakeTranslator([("en", "zh")], ready=cloud_ready)
    broken = FakeTranslator([("en", "zh")], broken=True)
    pipe = module.Pipeline()
    pipe._src_lang, pipe._dst_lang = "en", "zh"
    pipe._translator, pipe._trans_kind = broken, "qwen-quality"
    loaded = []
    def load(kind, config=None):
        loaded.append(kind)
        return {"opus-fast": opus, "cloud": cloud}[kind], kind
    monkeypatch.setattr(module, "_load_translator", load)
    statuses = []
    pipe.on_status(statuses.append)
    if entry == "warmup":
        pipe._warmup_translator()
    elif entry == "draft":
        from voxsub.live_draft import DraftTranslationRequest
        pipe._translate_draft(DraftTranslationRequest(1, "Hello"))
    else:
        pipe._translate_sentence("Hello")

    assert pipe._trans_kind == expected
    assert broken.closed == 1
    assert loaded == (["opus-fast"] if expected == "opus-fast"
                      else ["opus-fast", "cloud"])
    assert len(closed) == (0 if expected == "opus-fast" else 1)
    assert cloud.closed == (1 if expected is None else 0)
    if expected is None:
        assert isinstance(pipe._translator, module._NoopTranslator)
        assert pipe._translator.translate("Hello", "en", "zh") == "Hello 〔翻译待装〕"
        assert any("原文" in status for status in statuses)
    else:
        assert pipe._translator.translate("Hello", "en", "zh") == "有效译文"


@pytest.mark.parametrize("pair,expected", [
    (("auto", "zh"), False),
    (("auto", "en"), True),
    (("en", "zh"), False),
    (("zh", "en"), True),
    (("zh", "zh"), True),
    (("ja", "en"), False),
    (("zh", "auto"), False),
])
def test_real_opus_availability_is_directional(monkeypatch, tmp_path, pair, expected):
    from voxsub.translate.opus import OpusFastTranslator

    opus = OpusFastTranslator(model_dir=tmp_path)
    monkeypatch.setattr(opus, "list_available_pairs", lambda: [("zh", "en")])
    assert module._usable_for_pair(opus, *pair) is expected


@pytest.mark.parametrize("entry", ["sentence", "draft"])
def test_failure_fallback_uses_inflight_language_pair(monkeypatch, entry):
    pipe = module.Pipeline()
    pipe._src_lang, pipe._dst_lang = "en", "zh"
    snapshot = module._LangSnapshot("ja", "zh", pipe.config_generation)
    pipe._translator = FakeTranslator([snapshot.pair], broken=True)
    pipe._trans_kind = "qwen-quality"
    pipe._trans_pair = snapshot.pair
    opus = FakeTranslator([("en", "zh")])
    cloud = FakeTranslator([snapshot.pair])
    monkeypatch.setattr(module, "_load_translator", lambda kind, config=None:
                        ({"opus-fast": opus, "cloud": cloud}[kind], kind))
    if entry == "sentence":
        import time
        pipe._run_translation("こんにちは", snapshot, None, time.perf_counter())
    else:
        from voxsub.live_draft import DraftTranslationRequest
        pipe._translate_draft(DraftTranslationRequest(1, "こんにちは"), snapshot)
    assert pipe._trans_kind == "cloud"
    assert pipe._trans_pair == ("ja", "zh")

