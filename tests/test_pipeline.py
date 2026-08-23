"""Pipeline (M6) 测试: 状态机 / 导出格式 / 真机 C 模式与 A 模式启停。"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from voxsub.contextual_text import ContextualTextProcessor
from voxsub.pipeline import Pipeline, PipelineState, SubtitleLine


# ---------- 工具 ----------

class FakeSource:
    """假音频源: 反复吐出 16k 静音块, 直到 stop。"""

    sample_rate = 16000

    def __init__(self) -> None:
        self._stop = False

    def start(self) -> None:
        self._stop = False

    def read_chunk(self):
        if self._stop:
            return None
        return np.zeros(480, dtype=np.float32)

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        pass


def _test_wav() -> Path:
    """取模型包自带的真实中文语音样本 (M1 下载的 asr 包内含 test_wavs)。"""
    d = Path(os.environ["LOCALAPPDATA"]) / "VoxSub" / "models" / "asr" / "test_wavs"
    return d / "0.wav"


# ---------- 导出格式 ----------

def test_fmt_ts() -> None:
    assert Pipeline._fmt_ts(0) == "00:00:00,000"
    assert Pipeline._fmt_ts(61_500) == "00:01:01,500"
    assert Pipeline._fmt_ts(3_661_234) == "01:01:01,234"


def test_write_srt_content(tmp_path: Path) -> None:
    lines = [SubtitleLine(text="你好", translation="Hello", ts_ms=0),
             SubtitleLine(text="世界", translation="World", ts_ms=2000)]
    out = tmp_path / "t.srt"
    Pipeline.write_srt(lines, out)
    content = out.read_text(encoding="utf-8-sig")
    assert "1\n00:00:00,000 --> 00:00:01,500\n你好\nHello" in content
    assert "2\n00:00:02,000 --> 00:00:05,000\n世界\nWorld" in content  # 末句 3s


def test_write_vtt_and_txt(tmp_path: Path) -> None:
    lines = [SubtitleLine(text="你好", translation="Hello", ts_ms=0)]
    vtt = tmp_path / "t.vtt"
    txt = tmp_path / "t.txt"
    Pipeline.write_vtt(lines, vtt)
    Pipeline.write_txt(lines, txt)
    assert vtt.read_text(encoding="utf-8").startswith("WEBVTT")
    assert txt.read_text(encoding="utf-8") == "你好\tHello"


# ---------- 状态机 ----------

def test_mode_validation() -> None:
    p = Pipeline()
    p.set_mode("c")
    assert p.mode == "c"
    p.set_mode("invalid")
    assert p.mode == "c"  # 非法值被忽略


def test_models_dir_switch_discards_path_bound_components(tmp_path: Path) -> None:
    class _Closable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    p = Pipeline(models=tmp_path / "old-models")
    cloud = _Closable()
    translator = _Closable()
    p._asr = object()  # noqa: SLF001
    p._cloud_stt = cloud  # noqa: SLF001
    p._vad = object()  # noqa: SLF001
    p._seg = object()  # noqa: SLF001
    p._translator = translator  # noqa: SLF001

    p.set_models_dir(tmp_path / "new-models")

    assert p._models_dir == tmp_path / "new-models"  # noqa: SLF001
    assert p._asr is None and p._vad is None and p._seg is None  # noqa: SLF001
    assert p._cloud_stt is None and p._translator is None  # noqa: SLF001
    assert cloud.closed and translator.closed


def test_models_dir_switch_is_rejected_while_running(tmp_path: Path) -> None:
    p = Pipeline(models=tmp_path / "old-models")
    p._running = True  # noqa: SLF001

    with pytest.raises(RuntimeError, match="无法切换模型目录"):
        p.set_models_dir(tmp_path / "new-models")


def test_start_stop_with_fake_source(monkeypatch) -> None:
    """A 模式伪源启停: 线程不崩, 回调能收到(静音无识别结果, 仅验证生命周期)。"""
    p = Pipeline()
    msgs: list[str] = []
    p.on_status(msgs.append)
    monkeypatch.setattr(p, "_make_source", lambda: FakeSource())
    p.start()
    assert p.is_running()
    assert p.state is PipelineState.RUNNING
    time.sleep(1.0)
    p.stop()
    assert not p.is_running()
    assert p.state is PipelineState.IDLE
    assert any("启动" in m or "运行" in m or "停止" in m for m in msgs)


def test_loopback_default_does_not_pick_first_enumerated_device(monkeypatch) -> None:
    """B 模式无显式选择时交给 LoopbackSource 匹配系统默认扬声器。"""
    import voxsub.pipeline as pl

    sentinel = object()
    called: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(pl, "LoopbackSource",
                        lambda *args, **kwargs: called.append((args, kwargs)) or sentinel)
    p = Pipeline()
    p.set_mode("b")
    assert p._make_source() is sentinel  # noqa: SLF001
    assert called == [((), {})]


def test_selected_loopback_device_is_respected(monkeypatch) -> None:
    import voxsub.pipeline as pl
    from voxsub.audio import AudioDeviceInfo

    class _Device:
        id = "speaker-2"
        name = "会议耳机"

    selected = _Device()
    monkeypatch.setattr(pl, "list_loopbacks",
                        lambda: [AudioDeviceInfo(selected.name, "loopback", selected)])
    monkeypatch.setattr(pl, "LoopbackSource", lambda device=None: device)
    p = Pipeline()
    p.set_mode("b")
    p.set_audio_devices(loopback_device_id="speaker-2")
    assert p._make_source() is selected  # noqa: SLF001


def test_capture_start_failure_is_visible_and_stops_pipeline(monkeypatch) -> None:
    """设备 start 异常不能再静默杀死采集线程。"""
    class _BrokenSource(FakeSource):
        def start(self) -> None:
            raise RuntimeError("设备被占用")

    p = Pipeline()
    statuses: list[str] = []
    p.on_status(statuses.append)
    monkeypatch.setattr(p, "_build_real_time", lambda: None)
    monkeypatch.setattr(p, "_make_source", _BrokenSource)
    p.start()
    deadline = time.monotonic() + 2.0
    while p.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not p.is_running()
    assert any("音频设备错误" in msg and "设备被占用" in msg for msg in statuses)


def test_segmenter_flush_has_single_owner() -> None:
    """处理线程负责唯一一次 flush；stop/UI 线程不得再次触碰 sherpa 流。"""
    class _Seg:
        def __init__(self):
            self.flushes = 0

        def flush(self):
            self.flushes += 1

    p = Pipeline()
    seg = _Seg()
    p._seg = seg  # noqa: SLF001
    p._stop_evt.set()  # noqa: SLF001
    p._process_loop()  # noqa: SLF001
    p.stop()
    assert seg.flushes == 1


def test_realtime_setup_is_transactional_when_vad_is_missing(tmp_path: Path, monkeypatch) -> None:
    """A failed first load must not make the next Start use ``_seg = None``."""
    import voxsub.pipeline as pl

    p = Pipeline(provider="cpu", models=tmp_path / "models")
    created: list[object] = []
    monkeypatch.setattr(pl, "ensure_bundled_vad", lambda _root: None)
    monkeypatch.setattr(pl, "create_asr", lambda *_a, **_kw: created.append(object()) or created[-1])
    monkeypatch.setattr(p, "_ensure_translator", lambda: None)

    with pytest.raises(FileNotFoundError, match="基础 VAD"):
        p._build_real_time()  # noqa: SLF001
    assert p._asr is None and p._vad is None and p._seg is None  # noqa: SLF001

    vad_path = tmp_path / "models" / "vad" / "silero_vad_v5.onnx"
    vad_path.parent.mkdir(parents=True)
    vad_path.write_bytes(b"vad")

    class _Vad:
        pass

    class _Segmenter:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setattr(pl, "ensure_bundled_vad", lambda _root: vad_path)
    monkeypatch.setattr(pl, "WindowVAD", lambda *_a, **_kw: _Vad())
    monkeypatch.setattr(pl, "UtteranceSegmenter", _Segmenter)
    p._build_real_time()  # noqa: SLF001

    assert len(created) == 1
    assert p._asr is created[0]  # noqa: SLF001
    assert isinstance(p._vad, _Vad)  # noqa: SLF001
    assert isinstance(p._seg, _Segmenter)  # noqa: SLF001


def test_cloud_stt_builds_without_loading_local_asr(tmp_path: Path, monkeypatch) -> None:
    import voxsub.pipeline as pl

    p = Pipeline(provider="cpu", models=tmp_path / "models")
    p.set_stt("cloud", {
        "stt_api_key": "stt-key",
        "stt_base_url": "https://api.openai.com/v1",
        "stt_model": "whisper-1",
    })
    monkeypatch.setattr(p, "_ensure_translator", lambda: None)
    vad_path = tmp_path / "models" / "vad" / "silero_vad_v5.onnx"
    vad_path.parent.mkdir(parents=True)
    vad_path.write_bytes(b"vad")
    monkeypatch.setattr(pl, "ensure_bundled_vad", lambda _root: vad_path)

    class _Vad:
        window_size = 512

    class _Segmenter:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    class _Cloud:
        def __init__(self, _config) -> None:
            pass

        @staticmethod
        def ready() -> bool:
            return True

    monkeypatch.setattr(pl, "WindowVAD", lambda *_a, **_kw: _Vad())
    monkeypatch.setattr(pl, "AudioUtteranceSegmenter", _Segmenter)
    monkeypatch.setattr(pl, "CloudSTT", _Cloud)
    p._build_real_time()  # noqa: SLF001

    assert p._asr is None  # noqa: SLF001
    assert isinstance(p._cloud_stt, _Cloud)  # noqa: SLF001
    assert p._is_cloud_stt is True  # noqa: SLF001
    assert p._is_generative is True  # noqa: SLF001


def test_cloud_stt_recognition_feeds_the_independent_translation_queue() -> None:
    p = Pipeline()
    p._is_cloud_stt = True  # noqa: SLF001

    class _Cloud:
        @staticmethod
        def transcribe_samples(audio, *, source_lang):
            assert audio.size == 320
            assert source_lang == "zh"
            return "云端原文"

    p._cloud_stt = _Cloud()  # noqa: SLF001
    p._recognition_queue.put(np.ones(320, dtype=np.float32))  # noqa: SLF001
    p._recognition_input_done.set()  # noqa: SLF001
    p._recognition_loop()  # noqa: SLF001
    assert p._translation_queue.get_nowait() == "云端原文"  # noqa: SLF001


def test_translation_is_queued_outside_asr_thread() -> None:
    p = Pipeline()
    calls: list[str] = []

    class _Translator:
        def translate(self, text, *_args):
            calls.append(text)
            return "translated"

    p._translator = _Translator()  # noqa: SLF001
    p._on_sentence("原文")  # noqa: SLF001
    assert calls == []
    p._translation_input_done.set()  # noqa: SLF001
    p._translation_loop()  # noqa: SLF001
    assert calls == ["原文"]


def test_context_mode_translates_live_draft_and_emits_bilingual_revision() -> None:
    from voxsub.live_draft import LiveDraftState

    p = Pipeline()
    p.set_asr_tuning({"profile": "context"})
    p._translator = type("Translator", (), {  # noqa: SLF001
        "translate": lambda self, text, *_args: f"translated:{text}"
    })()
    p._live_draft = LiveDraftState(  # noqa: SLF001
        debounce_seconds=0.0, min_interval_seconds=0.0
    )
    drafts: list[tuple[str, str]] = []
    p.on_draft(lambda source, translation: drafts.append((source, translation)))

    p._emit_partial("逐词更新")  # noqa: SLF001
    request = p._live_draft.take_translation_request()  # noqa: SLF001
    assert request is not None
    p._translate_draft(request)  # noqa: SLF001

    assert drafts == [
        ("逐词更新", ""),
        ("逐词更新", "translated:逐词更新"),
    ]


def test_recognition_backpressure_stops_instead_of_growing_unbounded() -> None:
    p = Pipeline()
    p._recognition_queue = queue.Queue(maxsize=1)  # noqa: SLF001
    p._recognition_queue.put(object())  # noqa: SLF001

    with pytest.raises(RuntimeError, match="识别后端持续落后"):
        p._queue_generative_audio(np.ones(320, dtype=np.float32))  # noqa: SLF001

    assert p._stop_evt.is_set()  # noqa: SLF001


def test_translation_backpressure_stops_instead_of_growing_unbounded() -> None:
    p = Pipeline()
    p._translation_queue = queue.Queue(maxsize=1)  # noqa: SLF001
    p._translation_queue.put("已有字幕")  # noqa: SLF001

    with pytest.raises(RuntimeError, match="翻译后端持续落后"):
        p._on_sentence("新的字幕")  # noqa: SLF001

    assert p._stop_evt.is_set()  # noqa: SLF001
    assert "新的字幕" not in p._translation_times  # noqa: SLF001


def test_pipeline_rejects_stt_text_in_the_wrong_language() -> None:
    p = Pipeline()
    p.set_langs("zh", "en")
    statuses: list[str] = []
    p.on_status(statuses.append)

    p._on_sentence("यह एक हिन्दी वाक्य है")  # noqa: SLF001

    with pytest.raises(queue.Empty):
        p._translation_queue.get_nowait()  # noqa: SLF001
    assert any("其他语言" in status for status in statuses)


def test_pipeline_rejects_translation_in_the_wrong_language() -> None:
    p = Pipeline()
    p.set_langs("zh", "en")
    p._trans_kind = "mock"  # noqa: SLF001
    p._translator = type("Translator", (), {
        "translate": lambda self, *_args, **_kwargs: "यह एक हिन्दी वाक्य है",
    })()  # noqa: SLF001
    emitted: list[tuple[str, str]] = []
    p.on_utterance(lambda source, translation: emitted.append((source, translation)))

    p._translate_sentence("这是中文", None)  # noqa: SLF001

    assert emitted == [("这是中文", "这是中文 〔翻译失败〕")]


def test_generative_recognition_is_decoupled_from_vad_worker() -> None:
    p = Pipeline()

    class _ASR:
        @staticmethod
        def create_stream():
            return {"audio": None}

        @staticmethod
        def feed(stream, audio):
            stream["audio"] = audio.copy()

        @staticmethod
        def decode(stream):
            assert stream["audio"].size == 320
            return "完整的一句话"

        @staticmethod
        def reset(stream):
            stream.clear()

    p._asr = _ASR()  # noqa: SLF001
    p._recognition_queue.put(np.ones(320, dtype=np.float32))  # noqa: SLF001
    p._recognition_input_done.set()  # noqa: SLF001
    p._recognition_loop()  # noqa: SLF001
    assert p._translation_queue.get_nowait() == "完整的一句话"  # noqa: SLF001
    assert p._translation_input_done.is_set()  # noqa: SLF001


def test_asr_tuning_presets_keep_generative_context_longer() -> None:
    p = Pipeline()
    auto_qwen = p._effective_asr_tuning(generative=True)  # noqa: SLF001
    auto_zip = p._effective_asr_tuning(generative=False)  # noqa: SLF001
    assert auto_qwen["max_utterance_ms"] == 12_000
    assert auto_qwen["silence_ms"] == 700
    assert auto_zip["max_utterance_ms"] == 4_500
    assert auto_zip["partial_interval_ms"] == 360
    p.set_asr_tuning({"profile": "context"})
    context_zip = p._effective_asr_tuning(generative=False)  # noqa: SLF001
    assert context_zip["partial_interval_ms"] == 140
    p.set_asr_tuning({"profile": "custom", "vad_threshold": 0.2,
                      "silence_ms": 850, "max_utterance_ms": 18_000,
                      "beam_paths": 8, "max_new_tokens": 256,
                      "hotwords": "VoxSub"})
    custom = p._effective_asr_tuning(generative=True)  # noqa: SLF001
    assert custom["silence_ms"] == 850
    assert custom["max_utterance_ms"] == 18_000
    assert custom["hotwords"] == "VoxSub"

    p.set_asr_tuning({"profile": "custom", "vad_threshold": 0.02,
                      "silence_ms": 50, "max_utterance_ms": 120_000,
                      "beam_paths": 16, "max_new_tokens": 4096})
    wide = p._effective_asr_tuning(generative=True)  # noqa: SLF001
    assert wide["vad_threshold"] == 0.02
    assert wide["silence_ms"] == 50
    assert wide["max_utterance_ms"] == 120_000
    assert wide["beam_paths"] == 16
    assert wide["max_new_tokens"] == 4096


def test_smart_context_preset_enables_bounded_semantic_processing() -> None:
    p = Pipeline()
    p.set_asr_tuning({
        "profile": "context",
        "context_hold_ms": 2200,
        "context_correction": False,
        "filler_mode": "off",
    })

    tuning = p._effective_asr_tuning(generative=True)  # noqa: SLF001

    assert tuning["context_enabled"] is True
    assert tuning["context_hold_ms"] == 2200
    assert tuning["context_correction"] is False
    assert tuning["filler_mode"] == "off"
    assert tuning["silence_ms"] == 500
    assert tuning["max_utterance_ms"] == 18_000


def test_context_stage_merges_fragments_before_translation_queue() -> None:
    p = Pipeline()
    p._context_processor = ContextualTextProcessor(  # noqa: SLF001
        source_lang="zh", hold_ms=1800, defer_incomplete=True,
    )
    p._on_sentence("因为目前成本比较低")  # noqa: SLF001
    p._on_sentence("所以我们下周开始执行。")  # noqa: SLF001
    p._context_input_done.set()  # noqa: SLF001

    p._context_loop()  # noqa: SLF001

    assert p._translation_queue.get_nowait() == (  # noqa: SLF001
        "因为目前成本比较低所以我们下周开始执行。"
    )
    assert p._translation_input_done.is_set()  # noqa: SLF001


def test_existing_modes_bypass_context_stage() -> None:
    p = Pipeline()
    p.set_asr_tuning({"profile": "balanced"})
    assert p._effective_asr_tuning(generative=True)[  # noqa: SLF001
        "context_enabled"
    ] is False

    p._on_sentence("原有路径保持不变。")  # noqa: SLF001

    assert p._context_queue.empty()  # noqa: SLF001
    assert p._translation_queue.get_nowait() == "原有路径保持不变。"  # noqa: SLF001


# ---------- C 模式 (真实模型) ----------

@pytest.mark.integration
def test_file_mode_real_wav(tmp_path: Path) -> None:
    """真实中文 wav → 分句识别 → srt 导出, 全链路可用。"""
    wav = _test_wav()
    if not wav.exists():
        pytest.skip("缺少 test_wavs 样本")
    p = Pipeline()
    lines, _ = p._transcribe_file(wav)
    assert len(lines) >= 1
    assert all(ln.text.strip() for ln in lines)
    # 时间戳单调递增
    ts = [ln.ts_ms for ln in lines]
    assert ts == sorted(ts)
    # 导出
    out = tmp_path / "out.srt"
    Pipeline.write_srt(lines, out)
    assert out.exists() and out.stat().st_size > 0


@pytest.mark.integration
def test_file_mode_translation_fallback(tmp_path: Path) -> None:
    """翻译模块未就绪(或失败)时, 导出不阻塞: 译文带标记。"""
    wav = _test_wav()
    if not wav.exists():
        pytest.skip("缺少 test_wavs 样本")
    p = Pipeline()

    class _Broken:
        def translate(self, *a, **k):
            raise RuntimeError("boom")

    p._translator = _Broken()  # 注入故障翻译器模拟 M4 缺失
    lines, _ = p._transcribe_file(wav)
    assert all("翻译失败" in ln.translation or ln.translation == "" for ln in lines)


def test_pipeline_has_loopback_symbol() -> None:
    """回归: B 模式用 list_loopbacks() 但有缺失 import 会 NameError(装机实测踩过)。

    pipeline.py 之前只 import 了 LoopbackSource 而非 list_loopbacks 函数,
    导致 B 模式 _make_source 报 'name list_loopbacks is not defined'。
    """
    import voxsub.pipeline as pl
    # 模块级必须有该符号, 且可调用(不抛 NameError)
    assert hasattr(pl, "list_loopbacks")
    try:
        # 真机枚举至少能返回列表(可能空, 但不应 NameError/缺失)
        result = pl.list_loopbacks()
        assert isinstance(result, list)
    except Exception as exc:
        # 若 audio 初始化整体失败也接受被上层捕获, 但绝不能是 NameError
        assert not isinstance(exc, NameError), f"B 模式缺 import: {exc}"


@pytest.mark.integration
def test_video_audio_extraction_with_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    """常见视频容器能自动提取为 16k 单声道 WAV，再进入识别阶段。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("PATH 中没有 ffmpeg")
    video = tmp_path / "sample.mp4"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    made = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=160x90:d=0.5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(video)],
        check=False, capture_output=True, creationflags=flags,
    )
    if made.returncode != 0:
        pytest.skip(f"本机 ffmpeg 无法生成测试 MP4: {made.stderr[-200:]!r}")

    captured: dict[str, object] = {}
    p = Pipeline()

    def _recognize(pcm: np.ndarray):
        captured["pcm"] = pcm
        return []

    monkeypatch.setattr(p, "_recognize_streaming", _recognize)
    lines, extracted = p._transcribe_file(video)  # noqa: SLF001
    try:
        assert lines == []
        assert extracted is not None and extracted.exists()
        pcm = captured["pcm"]
        assert isinstance(pcm, np.ndarray)
        assert pcm.dtype == np.float32 and pcm.ndim == 1
        assert pcm.size >= 7000
    finally:
        if extracted is not None:
            extracted.unlink(missing_ok=True)
