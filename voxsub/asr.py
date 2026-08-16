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

import os
from pathlib import Path
from typing import Callable

import numpy as np
import sherpa_onnx

from voxsub.logging_setup import get_logger

logger = get_logger("asr")

#: 全项目统一音频采样率 (audio 模块同样输出 16k, 见 DESIGN.md)
SAMPLE_RATE = 16000

#: silero VAD 配置窗口大小 (config 建议值)。
#: 注意 1.13.5 实际生效窗口以 VadModel.window_size() 为准 (本机实测 576,
#: 非 512) —— 所有切窗循环必须读取 window_size 属性, 勿硬编码。
_VAD_WINDOW_SIZE = 512


def models_dir() -> Path:
    """返回本地模型根目录 %LOCALAPPDATA%/VoxSub/models (DESIGN.md 约定)。"""
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "models"


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

    def __init__(self, model_dir: Path, provider: str = "cpu", num_threads: int = 1):
        self._model_dir = Path(model_dir)
        tokens = self._model_dir / "tokens.txt"
        if not tokens.exists():
            logger.warning("ASR 模型不完整: 缺少 token 表 %s (目录 %s)",
                           tokens.name, self._model_dir.name)
            raise FileNotFoundError(f"缺少 token 表: {tokens}")
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(tokens),
            encoder=str(_find_onnx(self._model_dir, "*encoder*.onnx")),
            decoder=str(_find_onnx(self._model_dir, "*decoder*.onnx")),
            joiner=str(_find_onnx(self._model_dir, "*joiner*.onnx")),
            decoding_method="greedy_search",
            provider=provider,
            num_threads=num_threads,
        )
        logger.info("ASR 模型加载成功 (provider=%s, num_threads=%d, 目录=%s)",
                    provider, num_threads, self._model_dir.name)

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
                max_speech_duration=10,
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
                 on_utterance: Callable[[str], None], min_silence_ms: int = 500):
        self._asr = asr
        self._vad = vad
        self._on_utterance = on_utterance
        self._min_silence_samples = int(SAMPLE_RATE * min_silence_ms / 1000.0)
        self._stream = None                       # 当前活跃解码流; None = 静音态
        self._buffer = np.zeros(0, dtype=np.float32)  # 不足一窗的剩余样本
        self._silence_samples = 0                 # 当前段尾部累计静音样本数

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
            if self._stream is None:
                self._stream = self._asr.create_stream()
            self._asr.feed(self._stream, chunk)
        elif self._stream is not None:
            # 静音且段活跃: 仍送入 ASR (帮助解码收敛), 累计静音判定句边界
            self._asr.feed(self._stream, chunk)
            self._silence_samples += len(chunk)
            if self._silence_samples >= self._min_silence_samples:
                self._end_utterance()

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