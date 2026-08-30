"""voxsub.asr —— 流式语音识别封装 (M3, 适配 sherpa-onnx 1.13.5)。

三个组件 (契约见 DESIGN.md「audio / asr 模块契约」):
- StreamingASR    : 包装 OnlineRecognizer.from_transducer, 负责流式解码
- WindowVAD       : 包装 VadModel.create(VadModelConfig), 逐窗口语音检测
- UtteranceSegmenter: VAD+ASR 组装, 把任意长度音频切成"句子"并通过回调输出

sherpa-onnx 1.13.5 API 事实 (M1-spike 实测, 与旧文档不同, 别按老教程写):
1. config 类 (OnlineRecognizerConfig / FeatureConfig 等) 已移除,
   改用工厂: OnlineRecognizer.from_transducer(tokens=str, encoder=str,
   decoder=str, joiner=str, decoding_method="greedy_search", provider="cpu", ...)
2. VAD 无 segments / accept_waveform 了: 它是逐窗口状态机,
   vad.window_size() 是方法 (带括号), vad.is_speech(窗口数组) -> bool, vad.reset()
3. stream.accept_waveform(sample_rate, waveform) —— 参数顺序是 (sr, wav)!
4. 所有模型路径参数必须是 str, 不能是 pathlib.Path

模型目录约定 (DESIGN.md): %LOCALAPPDATA%/VoxSub/models/{asr,vad}/,
asr 目录内多精度 (int8/fp32) 并存时优先选 *int8*.onnx。
"""
from __future__ import annotations

from collections import deque
import time
from pathlib import Path
from typing import Callable

import numpy as np
import sherpa_onnx

from voxsub.language_guard import language_name, normalize_language
from voxsub.logging_setup import get_logger
from voxsub.model_storage import resolve_models_root

logger = get_logger("asr")

#: 全项目统一音频采样率 (audio 模块同样输出 16k, 见 DESIGN.md)
SAMPLE_RATE = 16000

#: silero VAD 配置窗口大小 (config 建议值)。
#: 注意 1.13.5 实际生效窗口以 VadModel.window_size() 为准 (本机实测 576,
#: 非 512) —— 所有切窗循环必须读取 window_size 属性, 勿硬编码。
_VAD_WINDOW_SIZE = 512


def models_dir() -> Path:
    """Return VoxSub's configured, upgrade-safe model root."""
    return resolve_models_root()


def _find_onnx(model_dir: Path, pattern: str, prefer_int8: bool = True) -> Path:
    """按文件名模式定位 onnx 模型; 多精度并存时优先 int8 (体积小、推理快)。

    Raises:
        FileNotFoundError: 目录中没有任何匹配文件。
    """
    hits = sorted(model_dir.glob(pattern))
    if not hits:
        logger.warning("ASR 模型缺失: 目录 %s 中未找到匹配 %r 的模型文件",
                       model_dir.name, pattern)
        raise FileNotFoundError(f"在 {model_dir} 中未找到匹配 {pattern!r} 的模型文件")
    if prefer_int8:
        for hit in hits:
            if "int8" in hit.name:
                return hit
    else:
        for hit in hits:
            if "int8" not in hit.name:
                return hit
    return hits[0]


class StreamingASR:
    """流式 zipformer transducer 识别器封装。

    用法::

        asr = StreamingASR(models_dir() / "asr")          # 自动定位 int8 模型
        stream = asr.create_stream()
        asr.feed(stream, pcm_chunk)    # 任意长度 float32 mono 16k 块
        text = asr.decode(stream)      # 任意时刻取当前累计文本
        asr.reset(stream)              # 句子结束复位, 流对象可复用

    流对象 (stream) 之间完全独立, 单个 StreamingASR 可同时服务多路流
    (例如与 UtteranceSegmenter 配对逐句解码)。
    """

    def __init__(self, model_dir: Path, provider: str = "cpu", num_threads: int = 4,
                 decoding_method: str = "modified_beam_search",
                 max_active_paths: int = 4, source_lang: str = "auto"):
        self._model_dir = Path(model_dir)
        self.provider = provider
        source_lang = normalize_language(source_lang)
        self.runtime = "sherpa-streaming-transducer"
        tokens = self._model_dir / "tokens.txt"
        if not tokens.exists():
            logger.warning("ASR 模型不完整: 缺少 token 表 %s (目录 %s)",
                           tokens.name, self._model_dir.name)
            raise FileNotFoundError(f"缺少 token 表: {tokens}")
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(tokens),
            encoder=str(_find_onnx(self._model_dir, "*encoder*.onnx")),
            # sherpa 官方示例明确建议 transducer decoder 保留 FP32；encoder / joiner
            # 使用 int8 可保留大部分速度与内存收益。旧版三件套全选 int8 会明显
            # 放大这个 small bilingual 模型的解码损失。
            decoder=str(_find_onnx(self._model_dir, "*decoder*.onnx", prefer_int8=False)),
            joiner=str(_find_onnx(self._model_dir, "*joiner*.onnx")),
            decoding_method=decoding_method,
            max_active_paths=max_active_paths,
            provider=provider,
            num_threads=num_threads,
        )
        self.source_lang = source_lang
        logger.info(
            "ASR 模型加载成功 (provider=%s, threads=%d, decoding=%s, paths=%d, "
            "source_lang=%s, precision=int8/fp32/int8, 目录=%s)",
            provider, num_threads, decoding_method, max_active_paths,
            source_lang, self._model_dir.name,
        )

    def create_stream(self) -> object:
        """新建一条独立解码流。"""
        return self._recognizer.create_stream()

    def feed(self, stream, samples: np.ndarray) -> None:
        """送入一段 PCM 并增量解码。

        内部: 转 float32 mono -> accept_waveform(16000, wav) (注意参数顺序 sr 在前)
        -> 立即 decode 一次, 保证之后 get_result 拿到含本次输入的累计文本。
        """
        wav = np.asarray(samples, dtype=np.float32).reshape(-1)
        if wav.size == 0:
            return
        stream.accept_waveform(SAMPLE_RATE, wav)
        self.decode(stream)

    def decode(self, stream) -> str:
        """耗尽当前可解码帧, 返回流的最新累计文本 (流式部分结果)。"""
        try:
            while self._recognizer.is_ready(stream):
                self._recognizer.decode_stream(stream)
        except Exception:
            logger.exception("ASR 解码循环异常 (decode_stream)")
            raise  # 行为不变: 原样上抛, 仅补 traceback 记录
        return self._recognizer.get_result(stream)

    def get_result(self, stream) -> str:
        """直接读取当前累计结果, 不触发解码 (轻量, 适合高频调用)。"""
        return self._recognizer.get_result(stream)

    def is_endpoint(self, stream) -> bool:
        """sherpa 端点检测 (供需要时使用; 句子切分由 Segmenter 的静音阈值负责)。"""
        return self._recognizer.is_endpoint(stream)

    def reset(self, stream) -> None:
        """复位流的解码状态 (清空历史), 流对象可复用。"""
        self._recognizer.reset(stream)


class _OfflineBuffer:
    """Small adapter object matching the stream methods used by the segmenter."""

    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []
        self.result = ""


class OfflineGenerativeASR:
    """Sentence-level adapter for offline sherpa ASR models.

    Qwen3-ASR, Fun-ASR-Nano, and SenseVoice are non-streaming recognizers. The
    existing VAD segmenter still provides live sentence boundaries and feeds
    audio into this buffer; inference happens once at the boundary so the
    decoder is never rerun every few hundred milliseconds for a partial result.
    """

    def __init__(self, model_dir: Path, runtime: str, provider: str = "cpu",
                 num_threads: int = 4, source_lang: str = "auto",
                 max_new_tokens: int = 512, hotwords: str = "") -> None:
        self._model_dir = Path(model_dir)
        self.runtime = runtime
        self.provider = provider
        source_lang = normalize_language(source_lang)
        threads = max(1, int(num_threads))
        if runtime == "sherpa-qwen3-asr":
            tokenizer = self._model_dir / "tokenizer"
            required = {
                "conv_frontend": self._model_dir / "conv_frontend.onnx",
                "encoder": self._model_dir / "encoder.int8.onnx",
                "decoder": self._model_dir / "decoder.int8.onnx",
                "tokenizer": tokenizer,
            }
            self._require(required.values())
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
                conv_frontend=str(required["conv_frontend"]),
                encoder=str(required["encoder"]),
                decoder=str(required["decoder"]),
                tokenizer=str(required["tokenizer"]),
                num_threads=threads,
                provider=provider,
                max_total_len=512,
                max_new_tokens=max(64, min(512, int(max_new_tokens))),
                temperature=1e-6,
                top_p=0.8,
                hotwords=str(hotwords or ""),
            )
        elif runtime == "sherpa-funasr-nano":
            tokenizer = self._model_dir / "Qwen3-0.6B"
            required = {
                "encoder_adaptor": self._model_dir / "encoder_adaptor.int8.onnx",
                "llm": self._model_dir / "llm.int8.onnx",
                "embedding": self._model_dir / "embedding.int8.onnx",
                "tokenizer": tokenizer,
            }
            self._require(required.values())
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_funasr_nano(
                encoder_adaptor=str(required["encoder_adaptor"]),
                llm=str(required["llm"]),
                embedding=str(required["embedding"]),
                tokenizer=str(required["tokenizer"]),
                num_threads=threads,
                provider=provider,
                system_prompt=(
                    f"You are a precise speech transcription engine. "
                    f"Transcribe only {language_name(source_lang)} speech. "
                    "Do not translate, paraphrase, or output another language."
                ),
                user_prompt=(
                    f"Transcribe this audio in {language_name(source_lang)} only. "
                    "Return the spoken words and nothing else."
                ),
                max_new_tokens=max(64, min(512, int(max_new_tokens))),
                temperature=1e-6,
                top_p=0.8,
                language=(normalize_language(source_lang)
                          if normalize_language(source_lang) != "auto" else ""),
                itn=True,
                hotwords=str(hotwords or ""),
            )
        elif runtime == "sherpa-sense-voice":
            required = {
                "model": self._model_dir / "model.int8.onnx",
                "tokens": self._model_dir / "tokens.txt",
            }
            self._require(required.values())
            language = source_lang if source_lang in {"zh", "en", "ja", "ko", "yue"} else ""
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(required["model"]),
                tokens=str(required["tokens"]),
                num_threads=threads,
                provider=provider,
                language=language,
                use_itn=True,
            )
        else:
            raise ValueError(f"不支持的离线 ASR runtime: {runtime}")
        self.source_lang = normalize_language(source_lang)
        logger.info("生成式 ASR 加载成功 (runtime=%s provider=%s threads=%d 目录=%s)",
                    runtime, provider, threads, self._model_dir.name)

    @staticmethod
    def _require(paths) -> None:
        missing = [str(path) for path in paths if not Path(path).exists()]
        if missing:
            raise FileNotFoundError("生成式 ASR 模型不完整: " + ", ".join(missing))

    def create_stream(self) -> _OfflineBuffer:
        return _OfflineBuffer()

    def feed(self, stream: _OfflineBuffer, samples: np.ndarray) -> None:
        wav = np.asarray(samples, dtype=np.float32).reshape(-1)
        if wav.size:
            stream.chunks.append(wav.copy())

    def decode(self, stream: _OfflineBuffer) -> str:
        if stream.result:
            return stream.result
        if not stream.chunks:
            return ""
        wav = np.concatenate(stream.chunks)
        started = time.perf_counter()
        logger.debug(
            "生成式 ASR 开始解码: runtime=%s provider=%s audio_ms=%.1f samples=%d "
            "peak=%.4f rms=%.4f",
            self.runtime, self.provider, wav.size * 1000.0 / SAMPLE_RATE,
            wav.size, float(np.max(np.abs(wav))) if wav.size else 0.0,
            float(np.sqrt(np.mean(np.square(wav)))) if wav.size else 0.0,
        )
        native = self._recognizer.create_stream()
        native.accept_waveform(SAMPLE_RATE, wav)
        self._recognizer.decode_stream(native)
        result = native.result
        text = getattr(result, "text", result)
        stream.result = str(text or "").strip()
        logger.info(
            "生成式 ASR 解码完成: runtime=%s provider=%s audio_ms=%.1f "
            "decode_ms=%.1f chars=%d",
            self.runtime, self.provider, wav.size * 1000.0 / SAMPLE_RATE,
            (time.perf_counter() - started) * 1000.0,
            len(stream.result),
        )
        return stream.result

    def get_result(self, stream: _OfflineBuffer) -> str:
        # Do not repeatedly run a full generative decode for UI partials.
        return stream.result

    @staticmethod
    def is_endpoint(stream: _OfflineBuffer) -> bool:
        return False

    @staticmethod
    def reset(stream: _OfflineBuffer) -> None:
        stream.chunks.clear()
        stream.result = ""


def create_asr(model_id: str, models_root: Path, provider: str = "cpu",
               num_threads: int = 4, source_lang: str = "auto",
               tuning: dict | None = None):
    """Create the runtime adapter for a selected catalog model."""
    from voxsub.model_catalog import ModelMarketplace, get_model

    model = get_model(model_id)
    if model is None or model.task != "asr":
        logger.warning("未知 ASR 模型 %r，回落内置 Zipformer", model_id)
        model = get_model("asr-zipformer-bilingual-fast")
    assert model is not None
    tuning = tuning or {}
    model_dir = ModelMarketplace(models_root).available_model_dir(model)
    if model.runtime == "sherpa-streaming-transducer":
        return StreamingASR(
            model_dir, provider=provider, num_threads=num_threads,
            decoding_method="modified_beam_search",
            max_active_paths=max(1, min(8, int(tuning.get("beam_paths", 4)))),
            source_lang=source_lang,
        )
    return OfflineGenerativeASR(
        model_dir, runtime=model.runtime, provider=provider,
        num_threads=num_threads, source_lang=source_lang,
        max_new_tokens=int(tuning.get("max_new_tokens", 512)),
        hotwords=str(tuning.get("hotwords", "")),
    )


class WindowVAD:
    """silero VAD 封装 (sherpa VadModel) —— 逐窗口语音检测。

    注意 1.13.5 起 VadModel 不再管理 segments: 调用方须自备窗口缓冲,
    每次恰好喂入 ``window_size`` 个样本的 float32 数组。
    跨窗口的状态记忆由模型内部维护, 什么时候复位由调用方决定
    (UtteranceSegmenter 在每句结束后自动 reset)。
    """

    def __init__(self, model_path: str, threshold: float = 0.5,
                 min_silence: float = 0.5, min_speech: float = 0.25):
        cfg = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(
                model=str(model_path),
                threshold=threshold,
                min_silence_duration=min_silence,
                min_speech_duration=min_speech,
                window_size=_VAD_WINDOW_SIZE,
                # Sentence limits are owned by our segmenters.  Keep sherpa's
                # internal guard comfortably above the UI's 30 s maximum.
                max_speech_duration=60,
            ),
            sample_rate=SAMPLE_RATE,
            num_threads=2,
            provider="cpu",
        )
        self._vad = sherpa_onnx.VadModel.create(cfg)
        logger.info("VAD 模型加载成功 (模型=%s)", Path(model_path).name)

    @property
    def window_size(self) -> int:
        """单次喂入所需样本数 (silero 固定 512)。

        1.13.5 中 window_size 是方法, 此处封装为属性, 调用方写
        ``vad.window_size`` 即可。
        """
        return self._vad.window_size()

    def is_speech(self, chunk: np.ndarray) -> bool:
        """判断一个窗口是否为语音。

        Raises:
            ValueError: 窗口长度不是 window_size (上游切窗错误应尽早暴露)。
        """
        arr = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if arr.size != self.window_size:
            raise ValueError(
                f"VAD 窗口长度必须恰为 {self.window_size}, 实际 {arr.size}; "
                f"请按 window_size 切窗 (或使用 UtteranceSegmenter 自动切窗)"
            )
        return bool(self._vad.is_speech(arr))

    def reset(self) -> None:
        """复位 VAD 内部状态机 (音频源切换 / 句子结束后调用)。"""
        self._vad.reset()


class AudioUtteranceSegmenter:
    """Only segment audio with VAD; decode happens on a different worker.

    Generative recognizers such as Qwen3-ASR are sentence-level models.  Running
    their decoder inside the capture/VAD worker stalls segmentation and lets the
    raw-audio queue grow.  This class keeps that path cheap: it emits a complete
    waveform at a natural pause, and a dedicated recognition worker decodes it.

    A short pre-roll keeps consonants that begin just before VAD becomes certain.
    The hard duration limit is only a safety valve for continuous background
    audio; normal sentence boundaries are controlled by ``min_silence_ms``.
    """

    def __init__(self, vad: WindowVAD, on_audio: Callable[[np.ndarray], None], *,
                 min_silence_ms: int = 700, max_utterance_ms: int = 12000,
                 min_speech_ms: int = 250, pre_roll_ms: int = 240,
                 draft_asr: object | None = None,
                 on_partial: Callable[[str], None] | None = None,
                 partial_interval_ms: int = 140) -> None:
        self._vad = vad
        self._on_audio = on_audio
        self._min_silence_samples = int(SAMPLE_RATE * min_silence_ms / 1000.0)
        self._max_utterance_samples = int(SAMPLE_RATE * max_utterance_ms / 1000.0)
        self._min_speech_samples = int(SAMPLE_RATE * min_speech_ms / 1000.0)
        pre_roll_windows = max(
            1, int(np.ceil(SAMPLE_RATE * pre_roll_ms / 1000.0 / vad.window_size))
        )
        self._pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_windows)
        self._chunks: list[np.ndarray] = []
        self._buffer = np.zeros(0, dtype=np.float32)
        self._silence_samples = 0
        self._speech_samples = 0
        self._utterance_samples = 0
        # Sentence-level recognizers cannot expose interim hypotheses.  Smart
        # Context can therefore attach the bundled streaming Zipformer as a
        # lightweight draft sidecar while this segmenter continues to queue the
        # complete waveform for the selected high-quality final recognizer.
        self._draft_asr = draft_asr
        self._on_partial = on_partial
        self._partial_interval_samples = max(
            1, int(SAMPLE_RATE * partial_interval_ms / 1000.0))
        self._draft_stream = None
        self._draft_since_partial_samples = 0
        self._last_partial = ""

    def feed(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        data = np.concatenate([self._buffer, samples]) if self._buffer.size else samples
        self._buffer = np.zeros(0, dtype=np.float32)
        win = self._vad.window_size
        n_full = (data.size // win) * win
        for i in range(0, n_full, win):
            self._process_window(data[i:i + win])
        if data.size > n_full:
            self._buffer = data[n_full:].copy()

    def _process_window(self, chunk: np.ndarray) -> None:
        speech = self._vad.is_speech(chunk)
        if not self._chunks:
            self._pre_roll.append(chunk.copy())
            if not speech:
                return
            self._chunks = [part.copy() for part in self._pre_roll]
            self._pre_roll.clear()
            self._speech_samples = len(chunk)
            self._utterance_samples = sum(len(part) for part in self._chunks)
            self._silence_samples = 0
            self._start_draft_stream(self._chunks)
            return

        self._chunks.append(chunk.copy())
        self._utterance_samples += len(chunk)
        self._feed_draft(chunk)
        if speech:
            self._speech_samples += len(chunk)
            self._silence_samples = 0
        else:
            self._silence_samples += len(chunk)

        if self._silence_samples >= self._min_silence_samples:
            self._end_utterance("pause")
        elif self._utterance_samples >= self._max_utterance_samples:
            logger.debug("生成式 ASR 连续语音达到安全上限: %.2fs",
                         self._utterance_samples / SAMPLE_RATE)
            self._end_utterance("limit")

    def _start_draft_stream(self, chunks: list[np.ndarray]) -> None:
        if self._draft_asr is None or self._on_partial is None:
            return
        try:
            self._draft_stream = self._draft_asr.create_stream()
            for chunk in chunks:
                self._draft_asr.feed(self._draft_stream, chunk)
                self._draft_since_partial_samples += len(chunk)
            self._emit_draft_if_due()
        except Exception:
            # Draft recognition is optional.  A sidecar failure must never stop
            # recording or prevent the selected final recognizer from running.
            logger.warning("实时草稿识别旁路启动失败，本句回落到终句显示", exc_info=True)
            self._disable_draft_sidecar()

    def _feed_draft(self, chunk: np.ndarray) -> None:
        if self._draft_asr is None or self._draft_stream is None:
            return
        try:
            self._draft_asr.feed(self._draft_stream, chunk)
            self._draft_since_partial_samples += len(chunk)
            self._emit_draft_if_due()
        except Exception:
            logger.warning("实时草稿识别旁路运行失败，继续使用终句识别", exc_info=True)
            self._disable_draft_sidecar()

    def _emit_draft_if_due(self) -> None:
        if (
            self._draft_asr is None
            or self._draft_stream is None
            or self._on_partial is None
            or self._speech_samples < self._min_speech_samples
            or self._draft_since_partial_samples < self._partial_interval_samples
        ):
            return
        self._draft_since_partial_samples = 0
        text = str(self._draft_asr.get_result(self._draft_stream) or "").strip()
        if text and text != self._last_partial:
            self._last_partial = text
            self._on_partial(text)

    def _reset_draft_stream(self) -> None:
        stream, self._draft_stream = self._draft_stream, None
        if stream is not None and self._draft_asr is not None:
            try:
                self._draft_asr.reset(stream)
            except Exception:
                logger.debug("复位实时草稿识别流失败", exc_info=True)
        self._draft_since_partial_samples = 0
        self._last_partial = ""

    def _disable_draft_sidecar(self) -> None:
        self._reset_draft_stream()
        self._draft_asr = None

    def _end_utterance(self, reason: str) -> None:
        if not self._chunks:
            return
        chunks, self._chunks = self._chunks, []
        speech_samples = self._speech_samples
        self._silence_samples = 0
        self._speech_samples = 0
        self._utterance_samples = 0
        self._pre_roll.clear()
        self._vad.reset()
        if speech_samples < self._min_speech_samples:
            self._reset_draft_stream()
            logger.debug("忽略过短语音片段: %.0fms",
                         speech_samples * 1000.0 / SAMPLE_RATE)
            return
        audio = np.concatenate(chunks).astype(np.float32, copy=False)
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        logger.info(
            "生成式 ASR 自然分段: reason=%s duration=%.2fs speech_ms=%.1f "
            "peak=%.4f rms=%.4f",
            reason, audio.size / SAMPLE_RATE,
            speech_samples * 1000.0 / SAMPLE_RATE, peak, rms,
        )
        try:
            self._on_audio(audio)
        finally:
            self._reset_draft_stream()

    def flush(self) -> None:
        if self._buffer.size:
            win = self._vad.window_size
            pad = np.zeros(win - self._buffer.size, dtype=np.float32)
            self._process_window(np.concatenate([self._buffer, pad]))
            self._buffer = np.zeros(0, dtype=np.float32)
        self._end_utterance("flush")


class UtteranceSegmenter:
    """VAD + ASR 组装: 把任意长度 16k mono float32 音频流切分为"句子"。

    状态机::

        静音 ──(出现语音窗口)──▶ 活跃(建流, 逐窗 feed+decode 累积)
        活跃 ──(静音累计 >= min_silence_ms)──▶ on_utterance(final_text) 并复位
        活跃 ──(flush)──────────────────────▶ 同上 (强制结束)

    契约要点:
    - feed() 接受任意长度块; 内部按 vad.window_size 切窗,
      不足一窗的剩余样本缓存在 buffer, 与下一块拼接后继续 (跨块无缝隙)。
    - 语音期间每个语音窗口都送入 ASR 累积; 静音窗口同样送入
      (帮解码器收敛), 同时累计静音长度用于句子边界判定。
    - 每句结束后自动 reset ASR 流与 VAD 状态, 避免上一句残留影响下一句。
    """

    def __init__(self, asr: StreamingASR, vad: WindowVAD,
                 on_utterance: Callable[[str], None], min_silence_ms: int = 350,
                 max_utterance_ms: int = 4500,
                 on_partial: Callable[[str], None] | None = None,
                 partial_interval_ms: int = 360,
                 boundary_decider: Callable[[str], bool] | None = None,
                 semantic_hold_ms: int = 0):
        self._asr = asr
        self._vad = vad
        self._on_utterance = on_utterance
        self._on_partial = on_partial
        self._min_silence_samples = int(SAMPLE_RATE * min_silence_ms / 1000.0)
        self._max_utterance_samples = int(SAMPLE_RATE * max_utterance_ms / 1000.0)
        self._partial_interval_samples = int(SAMPLE_RATE * partial_interval_ms / 1000.0)
        self._boundary_decider = boundary_decider
        self._max_semantic_silence_samples = self._min_silence_samples + int(
            SAMPLE_RATE * max(0, semantic_hold_ms) / 1000.0)
        self._stream = None                       # 当前活跃解码流; None = 静音态
        self._buffer = np.zeros(0, dtype=np.float32)  # 不足一窗的剩余样本
        self._silence_samples = 0                 # 当前段尾部累计静音样本数
        self._utterance_samples = 0
        self._since_partial_samples = 0
        self._last_partial = ""
        self._defer_endpoint = False

    def feed(self, samples: np.ndarray) -> None:
        """送入任意长度音频块 (float32 mono 16k)。"""
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        # 拼接上次缓存, 凑满整个窗口逐窗处理
        data = np.concatenate([self._buffer, samples]) if self._buffer.size else samples
        self._buffer = np.zeros(0, dtype=np.float32)
        win = self._vad.window_size
        n_full = (data.size // win) * win
        for i in range(0, n_full, win):
            self._process_window(data[i:i + win])
        if data.size > n_full:
            self._buffer = data[n_full:].copy()

    def _process_window(self, chunk: np.ndarray) -> None:
        """处理一个完整 VAD 窗口。"""
        if self._vad.is_speech(chunk):
            # 语音: 清零静音计数, 需要时新建流, 累积识别
            self._silence_samples = 0
            self._defer_endpoint = False
            if self._stream is None:
                self._stream = self._asr.create_stream()
            self._asr.feed(self._stream, chunk)
        elif self._stream is not None:
            # 静音且段活跃: 仍送入 ASR (帮助解码收敛), 累计静音判定句边界
            self._asr.feed(self._stream, chunk)
            previous_silence = self._silence_samples
            self._silence_samples += len(chunk)
            if (previous_silence < self._min_silence_samples <=
                    self._silence_samples):
                self._defer_endpoint = self._should_defer_endpoint()
            if (self._silence_samples >= self._min_silence_samples and
                    (not self._defer_endpoint or
                     self._silence_samples >= self._max_semantic_silence_samples)):
                self._end_utterance()

        if self._stream is not None:
            self._utterance_samples += len(chunk)
            self._since_partial_samples += len(chunk)
            self._emit_partial_if_due()
            # 视频/直播常有背景声，VAD 可能几十秒都遇不到纯静音。硬上限确保
            # 字幕持续产出，而不是等停止按钮触发 flush 才出现一大段文字。
            if self._utterance_samples >= self._max_utterance_samples:
                logger.debug("ASR 连续语音达到硬切上限: %.2fs",
                             self._utterance_samples / SAMPLE_RATE)
                self._end_utterance()

    def _emit_partial_if_due(self) -> None:
        if self._on_partial is None or self._stream is None:
            return
        if self._since_partial_samples < self._partial_interval_samples:
            return
        self._since_partial_samples = 0
        text = self._asr.get_result(self._stream).strip()
        if text and text != self._last_partial:
            self._last_partial = text
            self._on_partial(text)

    def _should_defer_endpoint(self) -> bool:
        if self._boundary_decider is None or self._stream is None:
            return False
        text = self._asr.get_result(self._stream).strip()
        if not text:
            return False
        try:
            defer = bool(self._boundary_decider(text))
        except Exception:
            logger.exception("语义断句判断失败，回落到固定静音边界")
            return False
        if defer:
            logger.debug(
                "语义判断句子未完成，静音边界延长: text=%r max_silence_ms=%.0f",
                text[:120],
                self._max_semantic_silence_samples * 1000.0 / SAMPLE_RATE,
            )
        return defer

    def _end_utterance(self) -> None:
        """结束当前段: 取最终文本回调, 复位 ASR 流与 VAD。"""
        if self._stream is None:
            return
        text = self._asr.decode(self._stream).strip()
        if text:
            self._on_utterance(text)
        self._asr.reset(self._stream)
        self._stream = None
        self._silence_samples = 0
        self._utterance_samples = 0
        self._since_partial_samples = 0
        self._last_partial = ""
        self._defer_endpoint = False
        self._vad.reset()  # 清 VAD 状态, 避免上句尾部状态压低下句起检灵敏度

    def flush(self) -> None:
        """强制结束当前语音段 (处理完不足一窗的缓存后回调), 幂等。

        直播结尾 / 音频源停止时调用, 确保最后一句不因缺尾静音而丢失。
        """
        if self._buffer.size:
            win = self._vad.window_size
            pad = np.zeros(win - self._buffer.size, dtype=np.float32)
            self._process_window(np.concatenate([self._buffer, pad]))
            self._buffer = np.zeros(0, dtype=np.float32)
        self._end_utterance()
