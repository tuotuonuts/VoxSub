"""Source selection constrains decoding, not which information the user may see."""
from types import SimpleNamespace
import logging
import time
import numpy as np
import pytest
from voxsub import asr
from voxsub.contextual_text import ContextualSegment
from voxsub.file_transcriber import FileRecognizer
from voxsub.pipeline import Pipeline
from voxsub.language_guard import text_matches_language

SOURCES = ["Thanks", "Okay", "Good morning!", "Run!", "NASA", "42", "Hello 世界", "こんにちは", "Det är en ofördag i dödberik."]

@pytest.mark.parametrize("text", SOURCES)
def test_nonempty_source_is_never_lost_to_a_language_guess(text, caplog):
    pipe = Pipeline(); pipe.set_langs("en", "zh")
    statuses = []; pipe.on_status(statuses.append)
    with caplog.at_level(logging.INFO):
        pipe._on_sentence(text)
    item = pipe._translation_queue.get_nowait()
    assert item.text == text
    assert item.snapshot.pair == ("en", "zh")
    assert not any("忽略" in s or "丢弃" in s for s in statuses)
    if len(text) > 5:
        assert text not in caplog.text

@pytest.mark.parametrize("text", ["Thanks", "42", "Hello 世界", "こんにちは"])
def test_partial_and_stabilized_context_keep_uncertain_source(text):
    pipe = Pipeline(); pipe.set_langs("en", "zh")
    partials = []; pipe.on_partial(partials.append)
    pipe._on_asr_partial(text)
    assert partials == [text]
    pipe._commit_context_segments([ContextualSegment(text, text)], time.monotonic())
    assert pipe._translation_queue.get_nowait().text == text

@pytest.mark.parametrize("reply,expected", [("中文译文", "中文译文"), ("Wrong target language", "")])
def test_final_attempts_translation_but_still_enforces_target(reply, expected):
    pipe = Pipeline(); pipe.set_langs("en", "zh")
    calls = []; emitted = []
    pipe._translator = SimpleNamespace(translate=lambda text, src, dst, **kw: calls.append((text, src, dst)) or reply)
    pipe._trans_kind = "mock"; pipe._trans_pair = ("en", "zh")
    pipe.on_utterance(lambda *pair: emitted.append(pair))
    pipe._translate_sentence("Hello 世界")
    assert calls == [("Hello 世界", "en", "zh")]
    assert emitted == [("Hello 世界", expected)]

@pytest.mark.parametrize("text", ["Thanks", "42", "こんにちは"])
def test_file_local_keeps_source_without_leaking_it_to_logs(text, caplog):
    lines = []
    with caplog.at_level(logging.INFO):
        FileRecognizer._append_local_result(lines, SimpleNamespace(decode=lambda _: text), object(), 16000, "en", audio=np.full(512,.01,dtype=np.float32))
    assert len(lines) == 1 and lines[0].text == text and lines[0].ts_ms == 1000
    if len(text) > 5:
        assert text not in caplog.text

class SpeechVad:
    window_size = 512
    def is_speech(self, _): return True
    def reset(self): pass


def test_file_cloud_keeps_uncertain_source_and_translates():
    calls = []
    client = SimpleNamespace(transcribe_samples=lambda audio, source_lang: calls.append(source_lang) or "こんにちは")
    lines = FileRecognizer.cloud(np.full(1024,.01,dtype=np.float32), vad=SpeechVad(), cloud_stt=client,
        translator=SimpleNamespace(translate=lambda *a, **kw: "你好"), source_lang="en", target_lang="zh",
        tuning={"silence_ms":500,"max_utterance_ms":12000}, validate_translation=True)
    assert calls == ["en"]
    assert [(line.text,line.translation) for line in lines] == [("こんにちは","你好")]

@pytest.fixture
def qwen_adapter(tmp_path, monkeypatch):
    for name in ["conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx"]: (tmp_path/name).touch()
    (tmp_path/"tokenizer").mkdir()
    natives = []
    class Native:
        def __init__(self): self.options={}; self.events=[]; self.result=SimpleNamespace(text="Thanks"); self.decodes=0
        def set_option(self,key,value): self.options[key]=value; self.events.append("option")
        def accept_waveform(self,*args): self.events.append("audio")
    class Recognizer:
        def create_stream(self): native=Native(); natives.append(native); return native
        def decode_stream(self,native): native.decodes+=1; native.events.append("decode")
    monkeypatch.setattr(asr.sherpa_onnx,"OfflineRecognizer",SimpleNamespace(from_qwen3_asr=lambda **kw:Recognizer()))
    def make(language): return asr.OfflineGenerativeASR(tmp_path,"sherpa-qwen3-asr",source_lang=language)
    return make,natives

@pytest.mark.parametrize("source,expected", [("en","English"),("zh","Chinese"),("ja","Japanese"),("ko","Korean"),("auto",None)])
def test_qwen_hint_is_on_each_native_stream_before_decode(qwen_adapter,source,expected):
    make,natives=qwen_adapter; model=make(source)
    stream=model.create_stream(); model.feed(stream,np.full(512,.01,dtype=np.float32))
    assert model.decode(stream)=="Thanks"; assert model.decode(stream)=="Thanks"
    assert natives[0].options==({"language":expected} if expected else {})
    assert natives[0].events==(["option"] if expected else [])+["audio","decode"]
    assert natives[0].decodes==1
    model.reset(stream); model.feed(stream,np.full(512,.01,dtype=np.float32)); model.decode(stream)
    assert len(natives)==2 and natives[1].options==natives[0].options


def test_queued_local_audio_and_result_keep_original_language_snapshot(qwen_adapter):
    make,natives=qwen_adapter; pipe=Pipeline(); pipe.set_langs("en","zh")
    pipe._asr=make("zh"); pipe._running=True  # Preserve the running adapter when changing settings.
    pipe._queue_generative_audio(np.full(512,.01,dtype=np.float32))
    pipe.set_langs("ja","en")
    pipe._recognition_input_done.set(); pipe._recognition_loop()
    assert natives[0].options=={"language":"English"}
    item=pipe._translation_queue.get_nowait()
    assert item.text=="Thanks" and item.snapshot.pair==("en","zh")


def test_file_stream_receives_selected_language_not_stale_adapter_default(qwen_adapter):
    make,natives=qwen_adapter
    lines=FileRecognizer.local(np.full(1024,.01,dtype=np.float32),vad=SpeechVad(),asr=make("zh"),
        translator=SimpleNamespace(translate=lambda *a, **kw:"谢谢"),source_lang="en",target_lang="zh",validate_translation=True)
    assert natives[0].options=={"language":"English"}
    assert [(line.text,line.translation) for line in lines]==[("Thanks","谢谢")]

@pytest.mark.parametrize("text",["Thanks","Okay","Good morning","NASA"])
def test_short_english_is_not_rejected_by_a_tiny_word_allowlist(text):
    assert text_matches_language(text,"en")

def test_hint_api_failure_is_visible_but_does_not_discard_audio(qwen_adapter, caplog):
    from voxsub import diagnostic_trace as trace
    make,natives=qwen_adapter; model=make("en")
    create=model._recognizer.create_stream
    def create_without_hint():
        native=create()
        def broken(*_): raise RuntimeError("private implementation detail")
        native.set_option=broken
        return native
    model._recognizer.create_stream=create_without_hint
    stream=model.create_stream(); model.feed(stream,np.full(512,.01,dtype=np.float32))
    with caplog.at_level(logging.INFO): assert model.decode(stream)=="Thanks"
    assert natives[0].decodes==1
    event=next(e for e in reversed(trace.snapshot()["events"]) if e["stage"]=="recognition_language")
    assert event["outcome"]=="hint_unavailable" and event["fallback"]=="unconstrained_decode"
    assert "private implementation detail" not in caplog.text


def test_uncertain_final_translation_exception_keeps_original():
    pipe=Pipeline(); pipe.set_langs("en","zh")
    def broken(*args,**kwargs): raise RuntimeError("backend unavailable")
    pipe._translator=SimpleNamespace(translate=broken)
    pipe._trans_kind="mock"; pipe._trans_pair=("en","zh")
    emitted=[]; pipe.on_utterance(lambda *pair:emitted.append(pair))
    pipe._translate_sentence("Hello 世界")
    assert emitted==[("Hello 世界","")]


def test_source_warning_is_bounded_but_every_fragment_is_retained():
    pipe=Pipeline();pipe.set_langs("en","zh");statuses=[];pipe.on_status(statuses.append)
    for _ in range(3):pipe._on_sentence("こんにちは")
    assert pipe._translation_queue.qsize()==3
    assert len([s for s in statuses if "已保留内容" in s])==1


def test_optional_draft_translation_does_not_filter_mixed_source():
    from voxsub.live_draft import LiveDraftState
    pipe=Pipeline();pipe.set_langs("en","zh")
    pipe._live_draft=LiveDraftState(debounce_seconds=0,min_interval_seconds=0)
    calls=[];pipe._translator=SimpleNamespace(translate=lambda text,*a,**kw:calls.append(text) or "混合内容")
    pipe._trans_kind="mock";pipe._trans_pair=("en","zh")
    pipe._live_draft.update_source("Hello 世界")
    request=pipe._live_draft.take_translation_request(now=time.monotonic()+3)
    assert request is not None
    pipe._translate_draft(request)
    assert calls==["Hello 世界"]
