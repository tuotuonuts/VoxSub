"""voxsub.pipeline —— 三模式编排 (M6)。

线程模型 (DESIGN.md「Pipeline 编排设计」):
  [采集线程] audio.read_chunk() 循环 ──queue──▶ [处理线程] segmenter.feed() → asr
      ──on_utterance(原文)──▶ translate ──▶ 订阅回调 (queue 桥接, 推理线程绝不直接碰 UI)

集成安全: 翻译模块 (M4) 未落地时用 _NoopTranslator 占位(原文直通+标记),
Pipeline 全链路不因缺模块崩溃 —— 翻译就绪后由 TranslatorFactory 注入。
"""
from __future__ import annotations

import inspect
import os
import queue
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import numpy as np

from voxsub.audio import (
    AudioSource,
    CHUNK_FRAMES,
    LoopbackSource,
    MicSource,
    list_loopbacks,
    list_microphones,
)
from voxsub.asr import (
    AudioUtteranceSegmenter,
    UtteranceSegmenter,
    WindowVAD,
    create_asr,
    models_dir,
    SAMPLE_RATE,
)
from voxsub.bootstrap_models import ensure_bundled_vad
from voxsub.cloud_stt import CloudSTT
from voxsub.contextual_text import ContextualSegment, ContextualTextProcessor
from voxsub.file_transcriber import FileAudioDecoder, FileRecognizer
from voxsub.language_guard import guard_text, normalize_language, text_matches_language
from voxsub.live_draft import DraftTranslationRequest, DraftView, LiveDraftState
from voxsub.logging_setup import get_logger
from voxsub.recording import WaveSessionRecorder
from voxsub.realtime_builder import RealtimeBuildSpec, build_realtime_components
from voxsub.subtitles import SubtitleExporter, SubtitleLine
from voxsub.tts_worker import TTSWorker

logger = get_logger("pipeline")

_PAUSE_MARKER = object()
_CAPTURE_QUEUE_MAX = 20_000       # 10 minutes at 30 ms/chunk
_RECOGNITION_QUEUE_MAX = 16       # complete utterance waveforms can be large
_CONTEXT_QUEUE_MAX = 128          # decoded acoustic fragments awaiting semantics
_TRANSLATION_QUEUE_MAX = 128      # text only, but must never grow forever

# ---------------------------------------------------------------- 调优预设
#
# 这四个档位各自固定一组基础参数。定义在模块级而不是 _effective_asr_tuning
# 内部，是因为界面也要用它们 —— 档位非 custom 时，用户存的基础参数**不会生效**
# （被这里覆盖），界面必须显示这里的目标值，否则用户看到 0.35 而实际跑 0.32。
ASR_TUNING_PRESETS: dict[str, dict[str, float | int]] = {
    "responsive": {"vad_threshold": 0.45, "silence_ms": 350,
                   "max_utterance_ms": 6000, "beam_paths": 2},
    "balanced": {"vad_threshold": 0.35, "silence_ms": 650,
                 "max_utterance_ms": 12000, "beam_paths": 4},
    "accuracy": {"vad_threshold": 0.25, "silence_ms": 900,
                 "max_utterance_ms": 20000, "beam_paths": 6},
    "context": {"vad_threshold": 0.32, "silence_ms": 500,
                "max_utterance_ms": 18000, "beam_paths": 6},
}

# 受档位控制的参数（键名同配置，不带 asr_ 前缀）。
#
# 只有这些键会被档位限制；不在表里的键（hotwords、max_new_tokens）**任何档位
# 都可改** —— 它们有非上下文的生效路径（生成式 ASR 直接读，见 asr.py）。
#
# 每条依据都核对过后端调用点：
#   · 四个基础参数在预设档下由 ASR_TUNING_PRESETS 覆盖，只有 custom 才走用户值。
#   · context_hold_ms / context_correction / filler_mode 只传给
#     ContextualTextProcessor，而它仅在 context 档创建。
#   · live_draft_enabled 由 _live_draft_enabled() 读，该函数要求
#     profile == "context"。
PROFILE_CONTROLLED_KEYS: frozenset[str] = frozenset({
    "vad_threshold", "silence_ms", "max_utterance_ms", "beam_paths",
    "context_hold_ms", "context_correction", "live_draft_enabled", "filler_mode",
})

# 每个档位下，上述受控参数中**用户仍可改**的那些。
PROFILE_EDITABLE_KEYS: dict[str, frozenset[str]] = {
    "responsive": frozenset(),
    "balanced": frozenset(),
    "accuracy": frozenset(),
    "context": frozenset({
        "context_hold_ms", "context_correction", "live_draft_enabled", "filler_mode",
    }),
    "custom": frozenset({
        "vad_threshold", "silence_ms", "max_utterance_ms", "beam_paths",
    }),
}


def asr_tuning_metadata() -> dict[str, object]:
    """界面所需的调优元数据（单一来源，避免前端硬编码后与后端漂移）。

    返回的键名一律带 ``asr_`` 前缀，与配置文件、界面控件一致 ——
    内部用短名，只在跨层时统一。

    三项内容：
      · presets    —— 每个预设档位固定的基础参数值
      · controlled —— 受档位控制的键（不在其中的键任何档位都可改）
      · editable   —— 每个档位下 controlled 里仍可改的子集
    """
    return {
        "presets": {
            profile: {f"asr_{key}": value for key, value in values.items()}
            for profile, values in ASR_TUNING_PRESETS.items()
        },
        "controlled": sorted(f"asr_{key}" for key in PROFILE_CONTROLLED_KEYS),
        "editable": {
            profile: sorted(f"asr_{key}" for key in keys)
            for profile, keys in PROFILE_EDITABLE_KEYS.items()
        },
    }


# 配置键 ↔ 内部调优键的映射。界面与配置文件用 asr_ 前缀，pipeline 内部用短名。
TUNING_KEY_MAP: dict[str, str] = {
    "asr_tuning_profile": "profile",
    "asr_vad_threshold": "vad_threshold",
    "asr_silence_ms": "silence_ms",
    "asr_max_utterance_ms": "max_utterance_ms",
    "asr_beam_paths": "beam_paths",
    "asr_max_new_tokens": "max_new_tokens",
    "asr_hotwords": "hotwords",
    "asr_context_hold_ms": "context_hold_ms",
    "asr_live_draft_enabled": "live_draft_enabled",
    "asr_context_correction": "context_correction",
    "asr_filler_mode": "filler_mode",
}

# effective_tuning_for 报告哪些键（顺序即界面顺序，便于人工核对）
_EFFECTIVE_UI_KEYS: tuple[str, ...] = (
    "vad_threshold", "silence_ms", "max_utterance_ms", "beam_paths",
    "max_new_tokens", "hotwords",
    "context_hold_ms", "live_draft_enabled", "context_correction", "filler_mode",
)


def tuning_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """从配置字典提取调优参数，转成 pipeline 内部的短名。"""
    return {
        short: config[key]
        for key, short in TUNING_KEY_MAP.items()
        if key in config
    }


def resolve_tuning_values(tuning: Mapping[str, Any], generative: bool) -> dict[str, Any]:
    """把档位 + 用户参数解析成具体生效值。

    提成模块级函数（原本是 Pipeline 的实例方法）是为了让**界面也能算**：
    界面需要显示"这一项现在实际是多少"，而那取决于配置里的档位，不是
    pipeline 实例的内存状态（实例可能还没启动过，`_asr_tuning` 还是构造时的
    默认值 —— 实测踩到：配置是 context，报告出来的却是 auto 档的 0.5/4）。
    """
    profile = str(tuning.get("profile", "auto"))
    if profile == "auto":
        values = ({"vad_threshold": 0.35, "silence_ms": 700,
                   "max_utterance_ms": 12000, "beam_paths": 4}
                  if generative else
                  {"vad_threshold": 0.5, "silence_ms": 350,
                   "max_utterance_ms": 4500, "beam_paths": 4})
    elif profile in ASR_TUNING_PRESETS:
        values = dict(ASR_TUNING_PRESETS[profile])
    else:
        values = {
            "vad_threshold": float(tuning.get("vad_threshold", 0.35)),
            "silence_ms": int(tuning.get("silence_ms", 650)),
            "max_utterance_ms": int(tuning.get("max_utterance_ms", 12000)),
            "beam_paths": int(tuning.get("beam_paths", 4)),
        }
    values.update({
        "vad_threshold": max(0.01, min(0.99, float(values["vad_threshold"]))),
        "silence_ms": max(50, min(5000, int(values["silence_ms"]))),
        "max_utterance_ms": max(1000, min(120000, int(values["max_utterance_ms"]))),
        "beam_paths": max(1, min(16, int(values["beam_paths"]))),
        "max_new_tokens": max(32, min(4096, int(tuning.get("max_new_tokens", 512)))),
        "hotwords": str(tuning.get("hotwords", "")).strip(),
        "context_enabled": profile == "context",
        "live_draft_enabled": bool(tuning.get("live_draft_enabled", True)),
        # Reading the already-decoded Zipformer hypothesis is cheap.  The
        # context profile samples it near word cadence; legacy profiles
        # retain their existing update frequency.
        "partial_interval_ms": 140 if profile == "context" else 360,
        "context_hold_ms": max(200, min(4000, int(tuning.get("context_hold_ms", 1800)))),
        "context_correction": bool(tuning.get("context_correction", True)),
        "filler_mode": (
            str(tuning.get("filler_mode", "light"))
            if str(tuning.get("filler_mode", "light")) in {"off", "light"} else "light"
        ),
    })
    return values


def effective_tuning_for(config: Mapping[str, Any]) -> dict[str, Any]:
    """按**配置**解析出生效值，键名带 asr_ 前缀（界面直接显示）。

    用配置而不是 Pipeline 实例状态：实例可能从未启动过，其 `_asr_tuning`
    还是构造时的默认值，据此报告出来的数字与实际会用到的完全不同。
    """
    values = resolve_tuning_values(tuning_from_config(config), generative=False)
    return {f"asr_{key}": values[key] for key in _EFFECTIVE_UI_KEYS}


@dataclass(frozen=True)
class _LangSnapshot:
    """某个**任务提交点**的语言/配置快照（不可变）。

    缺陷 #9 的根因：``Pipeline._src_lang`` / ``_dst_lang`` 是**可变实例状态**，
    翻译工作线程在执行时才回读它。用户在会话中途切语言（或换档位/模型/调优）
    时，已经排在队列里的句子于是被**新配置**解释 —— 用户看到的是"我明明选了
    中文→英文，这句中文却被当成日文被丢掉 / 被当成新语言翻"。

    修法：每个在途任务在**提交时**括下一份快照，执行时只认自己这份，绝不回读
    实例字段。

    ``generation`` 是"会影响结果的配置"的代数（见 :attr:`Pipeline.config_generation`）。
    它把"同一段文字 + 不同配置"区分开，是失败签名与任何翻译结果缓存的键的一
    部分：同一代内配置保证一致（缓存命中是安全的），代次一变旧键自然失效。
    """

    src: str
    dst: str
    generation: int

    @property
    def pair(self) -> tuple[str, str]:
        return (self.src, self.dst)


@dataclass(frozen=True)
class _QueuedAudio:
    """A VAD-complete waveform plus its enqueue timestamp for diagnostics."""
    audio: np.ndarray
    queued_at: float
    #: 入队那一刻的语言快照；裸 PCM 兼容路径为 None（退化成"执行时读当前值"）。
    snapshot: _LangSnapshot | None = None


@dataclass(frozen=True)
class _QueuedText:
    """A recognized acoustic fragment plus its first-ready timestamp."""
    text: str
    queued_at: float
    #: 这句识别结果**进入管线时**的语言快照；上下文阶段必须继续用它，
    #: 否则用户切语言会让正在等待稳定的旧句子按新语言被过滤掉。
    snapshot: _LangSnapshot | None = None


@dataclass(frozen=True)
class _QueuedTranslation:
    """终句 + 它**入队那一刻**的不可变语言/配置快照。

    队列条目以前是裸 ``str``，于是"用哪一对语言翻译"只能等到出队时去读实例
    字段 —— 那一刻用户可能已经切过语言了。把快照挂在条目上，语义就变成
    "这句话按它提交时的配置翻"，与用户预期一致（新句子用新语言，旧句子保持）。
    """

    text: str
    snapshot: _LangSnapshot
    queued_at: float | None = None


@dataclass
class _CaptureMetrics:
    started: float
    last_heartbeat: float
    chunks: int = 0
    peak: float = 0.0
    warned_silence: bool = False

    def observe(self, chunk: np.ndarray) -> None:
        self.chunks += 1
        if chunk.size:
            self.peak = max(self.peak, float(np.max(np.abs(chunk))))


# ---------- 翻译占位 (M4 就绪后替换) ----------

class _NoopTranslator:
    """M4 翻译层未安装时的容错占位: 原文直通并加标记, 保证管线不中断。"""

    name = "noop"
    langs = ("zh", "en")

    def translate(self, text: str, src_lang: str, dst_lang: str, *,
                  timeout_ms: int = 15000) -> str:
        return f"{text} 〔翻译待装〕"

    def close(self) -> None:
        pass

    def health(self) -> str:
        return "翻译模块未安装 (M4 待集成)"


class PipelineState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


#: ``stop()`` 等待 worker 退出的预算（秒）。
#:
#: 这是"别让界面显得卡死"的预算，**不是**"原生推理一定能在这之内停"的承诺 ——
#: 云端请求或 llama-server 的一次 completion 都可能更长。超时之后保持"停止中"
#: 并由 :meth:`Pipeline._watch_settlement` 以更宽的上限继续等。
_STOP_JOIN_SECONDS = 8.0

#: 停止超时之后，后台还会再等多久才判定"真的收尾失败"。
_SETTLE_WATCH_SECONDS = 30.0

#: 收尾观察者的轮询间隔。刻意**轮询**而不是再次 join：join 的超时账本归
#: :meth:`Pipeline.stop` 主路径所有，观察者只负责回答"人走没走"，不该再去
#: 分配截止时间（否则两条路径会各算一套超时）。
_SETTLE_POLL_SECONDS = 0.1


def _thread_label(thread: object) -> str:
    """日志里用的线程名。

    线程对象的形态不保证（测试会塞只有 ``is_alive``/``join`` 的替身），取不到就
    退化成类型名 —— **一行日志不该有能力把停止路径带崩**。
    """
    return str(getattr(thread, "name", type(thread).__name__))


def _load_translator(kind: str = "opus-fast", config=None) -> object:
    """按用户选择延迟加载翻译层；失败时保留原文，不让管线崩溃。"""
    try:
        from voxsub.translate.factory import TranslatorFactory  # type: ignore[import-not-found]
        try:
            translator = TranslatorFactory.create(kind, config)
            effective_kind = kind
            # A corrupted GGUF can exist on disk while llama-server only
            # discovers the problem after spawning.  Validate the selected
            # quality translator before any worker starts; if the local fast
            # models are available, keep the pipeline useful and avoid a
            # per-sentence respawn storm.
            if kind == "qwen-quality":
                health = getattr(translator, "health", lambda: "ok")()
                if health != "ok":
                    logger.error("质量翻译档位不可用: %s", health)
                    fallback = TranslatorFactory.create("opus-fast", config)
                    fallback_health = getattr(fallback, "health", lambda: "ok")()
                    if fallback_health == "ok":
                        close = getattr(translator, "close", None)
                        if callable(close):
                            close()
                        logger.warning(
                            "质量翻译不可用，自动切换 OPUS 极速档: reason=%s",
                            health,
                        )
                        translator, effective_kind = fallback, "opus-fast"
                    else:
                        close = getattr(fallback, "close", None)
                        if callable(close):
                            close()
                        close = getattr(translator, "close", None)
                        if callable(close):
                            close()
                        logger.error(
                            "质量翻译和 OPUS 极速档均不可用，改用原文直通: "
                            "quality=%s opus=%s", health, fallback_health,
                        )
                        translator, effective_kind = _NoopTranslator(), None
            return translator, effective_kind
        except Exception as exc:
            logger.error("翻译档位 %s 创建失败，退回原文显示: %s", kind, exc,
                         exc_info=True)
            return _NoopTranslator(), None
    except ImportError as exc:
        logger.debug("翻译层未安装, 用占位实现: %s", exc)
        return _NoopTranslator(), None


def _close_quietly(obj: object | None) -> None:
    """关掉翻译器（可能没有 close，或关的时候抛异常）。"""
    close = getattr(obj, "close", None)
    if callable(close):
        close()


def _usable_for_pair(translator: object, src_lang: str, dst_lang: str) -> bool:
    """翻译器是否既**已就绪**、又**支持这个语言对**。

    就绪这一项只对云端有意义（只有 CloudTranslator 有 ready()）：不看它的话，
    没配 API key 的用户会被"换到云端"，然后照样失败 —— 从一个不支持的档位
    换到另一个用不了的档位，等于没换。
    """
    ready = getattr(translator, "ready", None)
    if callable(ready) and not ready():
        return False
    supports = getattr(translator, "supports", None)
    return not callable(supports) or bool(supports(src_lang, dst_lang))


def _candidate_tiers(requested_kind: str) -> list[str]:
    """候选档位顺序：用户所选永远第一，其后按"更可能支持该语言对"排列。"""
    from voxsub.translate.factory import TIER_KINDS, _FALLBACK_KINDS  # noqa: PLC0415

    ordered = [requested_kind]
    for candidate_kind in _FALLBACK_KINDS:
        for tier_id, mapped in TIER_KINDS.items():
            if mapped == candidate_kind and tier_id not in ordered:
                ordered.append(tier_id)
    return ordered


def _load_translator_for_pair(kind: str, src_lang: str, dst_lang: str,
                              config=None) -> tuple[object, str | None, str | None]:
    """按**语言对**挑一个真能用的翻译器。

    返回 ``(translator, 生效 kind, 被替换掉的档位或 None)``。

    为什么要这一层：快档（OPUS-MT）只有 zh↔en 两个方向的模型，而界面允许把
    源/目标语言选成日文/韩文。此前每句话都会抛 "快档不支持语言对"，用户只看到
    原文 + 满屏 ERROR 回溯。这里在加载时就把档位换成支持该语言对的
    （质量档 / 云端），而不是让每句都失败。

    逐个候选都**实际创建**再问 ``supports()``，而不是只看档位名：质量档在
    translate_model_id=mt-opus-fast-builtin 时创建出来的其实是 OPUS 翻译器，
    只看名字会误判成"支持日语"。
    """
    from voxsub.translate.factory import kind_for_tier  # noqa: PLC0415

    requested_kind = kind_for_tier(kind, config)
    first_failure: object | None = None
    for tier_id in _candidate_tiers(kind):
        translator, effective = _load_translator(kind_for_tier(tier_id, config), config)
        if translator is None or effective is None:
            continue  # 这个档位加载失败（缺模型/缺凭据），试下一个
        if _usable_for_pair(translator, src_lang, dst_lang):
            # 找到了。先前留下的失败翻译器（用户所选那个）要关掉，否则
            # OPUS 模型实例会一直挂着不释放。
            _close_quietly(first_failure)
            substituted = kind if tier_id != kind else None
            if substituted is not None:
                logger.warning(
                    "档位 %s 不支持 %s→%s，已改用 %s",
                    kind, src_lang, dst_lang, tier_id,
                )
            return translator, effective, substituted
        # 支持不了这个语言对：留一个作最后兜底，其余的关掉
        if first_failure is None:
            first_failure = translator
        else:
            _close_quietly(translator)

    logger.error("没有档位支持语言对 %s→%s，翻译将保留原文", src_lang, dst_lang)
    if first_failure is not None:
        return first_failure, requested_kind, None
    return _NoopTranslator(), None, None


# ---------- Pipeline ----------

class Pipeline:
    """三模式实时/离线翻译管线 (契约见 DESIGN.md「Pipeline 契约」)。"""

    def __init__(self, provider: str = "auto", models: Optional[Path] = None) -> None:
        self._state_lock = threading.RLock()
        self._state = PipelineState.IDLE
        self._provider = provider
        self._models_dir = Path(models) if models else models_dir()
        self._mode = "a"
        self._in_path: Optional[Path] = None          # C 模式输入文件
        self._src_lang, self._dst_lang = "zh", "en"   # 默认中→英
        #: 「会影响翻译结果的配置」的代次（缺陷 #9）。见 :attr:`config_generation`。
        #: 语言对/翻译档位/ASR 模型/模型目录/ASR 调优**真的**变了才 +1；
        #: 反复提交同样的配置不加，否则所有基于代次的缓存/失败签名都会被白白冲掉。
        self._config_generation = 0
        self._tts_enabled = False
        self._tts_model_ids = {
            "zh": "tts-icefall-zh-aishell3",
            "en": "tts-icefall-en-ljspeech-low",
        }
        self._mic_device_id = ""
        self._loopback_device_id = ""
        self._capture_process_id = 0
        self._capture_window_title = ""
        self._requested_stt_provider = "local"
        self._stt_config = None
        self._requested_trans_kind = "opus-fast"
        self._requested_asr_model_id = "asr-zipformer-bilingual-fast"
        self._translator_config = None
        #: 因语言对不受支持而被替换掉的档位（None = 用的是用户所选）
        self._trans_substituted_from: str | None = None
        #: 加载翻译器时所用的语言对，用于判断语言变了要不要重挑档位
        self._trans_pair: tuple[str, str] | None = None
        #: 上一次"翻译失败"的原因签名；同一原因不再逐句刷 ERROR 与回溯
        self._pair_fail_key: tuple[str, str, str] | None = None

        # 10 minutes of 30 ms chunks is a hard memory safety cap, not a normal
        # latency policy.  We never silently discard captured speech.
        self._queue: queue.Queue = queue.Queue(maxsize=_CAPTURE_QUEUE_MAX)
        self._recognition_queue: queue.Queue = queue.Queue(
            maxsize=_RECOGNITION_QUEUE_MAX)
        self._context_queue: queue.Queue[_QueuedText] = queue.Queue(
            maxsize=_CONTEXT_QUEUE_MAX)
        self._translation_queue: queue.Queue[_QueuedTranslation] = queue.Queue(
            maxsize=_TRANSLATION_QUEUE_MAX)
        self._translation_times: dict[str, deque[float]] = defaultdict(deque)
        self._metrics_lock = threading.Lock()
        self._recognition_input_done = threading.Event()
        self._context_input_done = threading.Event()
        self._translation_input_done = threading.Event()
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._source: AudioSource | None = None

        self._cb_utterance: list[Callable[[str, str], None]] = []
        self._cb_partial: list[Callable[[str], None]] = []
        self._cb_draft: list[Callable[[str, str], None]] = []
        self._cb_status: list[Callable[[str], None]] = []
        self._cb_state: list[Callable[[], None]] = []
        self._cb_progress: list[Callable[[int, int, str], None]] = []
        self._live_draft = LiveDraftState()

        self._asr_tuning: dict = {"profile": "auto", "hotwords": ""}
        self._is_generative = False
        self._is_cloud_stt = False
        self._context_processor: ContextualTextProcessor | None = None
        self._recording_enabled = False
        self._recordings_dir: Path | None = None
        self._recorder: WaveSessionRecorder | None = None
        self._last_recording_path: Path | None = None
        self._tts_worker: TTSWorker | None = None

        # 惰性组件 (首次 start 时构建)
        self._asr = None
        self._cloud_stt = None
        self._vad = None
        self._seg = None
        self._translator = None
        self._trans_kind = None

    # ---- 只读访问器 ----

    @property
    def translator(self) -> object | None:
        """当前翻译器实例；尚未创建时为 None。

        供独立工作区取用（OCR 页可能在没点「开始」时就框选屏幕翻译，
        此时要用运行中会话的翻译器，没有则按配置现建 —— 见
        ipc_server.BackendService._translator）。

        做成公开属性而不是让调用方读 ``_translator``：内部字段会被
        换档位/换模型/切模型目录重置，调用方直接读私有名会写错
        （ipc_server 里就曾写成不存在的 ``pipeline.translator[1]``，
        导致 OCR 翻译整个不可用）。
        """
        return self._translator

    # ---- 配置 ----
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def state(self) -> PipelineState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: PipelineState) -> None:
        with self._state_lock:
            previous, self._state = self._state, state
        if previous != state:
            logger.info("Pipeline 生命周期: %s -> %s", previous.value, state.value)
            # 状态变了就通知订阅者。放在这里（而不是只在命令处理里发）是为了
            # 覆盖**自主转换**：例如 C 模式文件播完自己停下、出错后回到 IDLE。
            # 只在命令路径发通知的话，那些情况下界面会一直显示"结束"。
            self._emit_state()

    def _emit_state(self) -> None:
        """通知订阅者当前状态。订阅者自行读 is_running()/is_paused()。"""
        for cb in self._cb_state:
            try:
                cb()
            except Exception:
                logger.debug("状态回调失败", exc_info=True)

    @property
    def _running(self) -> bool:
        """Compatibility view for integrations that used the old private flag."""
        return self.is_running()

    @_running.setter
    def _running(self, value: bool) -> None:
        self._set_state(PipelineState.RUNNING if value else PipelineState.IDLE)

    def _is_settling(self) -> bool:
        """是否处在"停止超时、还在收尾"的窗口里。

        这个窗口里**任何**资源的销毁/替换都要停手：worker 可能仍在用它们。
        与 :meth:`_may_replace_resources` 的区别：那个回答"能不能换"（用于本来
        就只允许停机操作的入口），这个只回答"现在是不是收尾中"（用于运行中
        本来就可以热重载的入口，比如 TTS）。
        """
        return self.state is PipelineState.STOPPING

    def _workers_alive(self) -> list[threading.Thread]:
        """还活着的 worker，顺手清掉已退出的引用。

        **资源能否被替换的权威依据**：只要这里有东西，就不允许销毁/替换
        工作线程正在使用的翻译器、识别器或 TTS worker。
        """
        alive = [t for t in list(self._threads) if t.is_alive()]
        self._threads = alive
        return alive

    def _may_replace_resources(self) -> bool:
        """能否安全地销毁或替换被工作线程持有的资源。

        两个条件**都要满足**：
          · 状态不在运行中（STARTING/RUNNING/STOPPING 都算运行中）；
          · 没有 worker 还活着。

        为什么不能只看状态：``stop()`` 的 8 秒 join 超时之后，原生推理可能仍在
        收尾。以前这一步会直接把状态落成 IDLE，于是 ``set_translator`` /
        ``set_asr_model`` / ``set_models_dir`` 这些只判断 ``self._running`` 的
        入口就以为"已经空了"，把还在被使用的实例关掉或换掉 —— 缺陷 #6 的根因。

        这条规则**只在这里实现一次**，所有替换资源的入口都必须走它。
        """
        return not self.is_running() and not self._workers_alive()

    @property
    def config_generation(self) -> int:
        """影响翻译结果的配置代次（只读，供 IPC/界面观测）。

        每次「会影响结果的配置」**真的**变了就 +1：语言对、翻译档位、ASR 模型、
        模型目录、ASR 调优。反复提交同样的值**不加** —— 否则每次重发配置都会把
        所有基于代次的缓存与失败签名冲掉，等于没有缓存。

        语义（工作单 §3.5）：
          · 用户保存的配置 —— 配置文件里的值，本类不关心；
          · 当前实际生效的配置 —— ``self._src_lang/_dst_lang`` 等字段 + 本代次；
          · 某个在途任务持有的配置快照 —— :class:`_LangSnapshot`，它带上**它
            被创建时的代次**。代次不同就说明"这是另一个配置下的结果"，不能混用。
        """
        with self._state_lock:
            return self._config_generation

    def _bump_config_generation(self, reason: str) -> int:
        """配置代次 +1。**只在配置真的变了之后调用**（由调用方负责判断）。"""
        with self._state_lock:
            self._config_generation += 1
            value = self._config_generation
        logger.info("配置代次更新: generation=%d reason=%s", value, reason)
        return value

    def _lang_snapshot(self) -> _LangSnapshot:
        """取一份**当前**配置的不可变快照（在任务提交点调用）。

        与 :meth:`set_langs` 共用 ``_state_lock``，所以读到的永远是"某一刻的
        语言对 + 那一刻的代次"，不会是半更新状态。
        """
        with self._state_lock:
            return _LangSnapshot(self._src_lang, self._dst_lang,
                                 self._config_generation)

    def set_mode(self, mode: str) -> None:
        if mode in ("a", "b", "c") and not self._running:
            self._mode = mode

    def set_langs(self, src: str, dst: str) -> None:
        normalized = (normalize_language(src, strict=True),
                      normalize_language(dst, strict=True))
        # 语言对的写入与代次递增必须与 :meth:`_lang_snapshot` 互斥，否则取快照
        # 的线程可能读到"新语言 + 旧代次"这种半更新状态 —— 那种组合会让缓存键
        # 撒谎（同一代次下出现两种语言对）。
        with self._state_lock:
            changed = normalized != (self._src_lang, self._dst_lang)
            self._src_lang, self._dst_lang = normalized
            if changed:
                self._bump_config_generation("set_langs")
        if changed and not self._running:
            self._asr = None
            self._seg = None
            self._context_processor = None
        logger.info("语言约束更新: source=%s target=%s", self._src_lang, self._dst_lang)

    def set_input_file(self, path: str | Path) -> None:
        self._in_path = Path(path)

    def set_tts(self, enabled: bool) -> None:
        enabled = bool(enabled)
        # 收尾中一律不动 TTS worker：它可能正在被退出中的线程使用。
        hot_reload = self._running and not self._is_settling()
        if enabled == self._tts_enabled:
            # A previous worker may have exited because the selected model was
            # not installed yet.  Re-applying an enabled setting after Model
            # Hub changes is therefore also a cheap recovery trigger.
            if enabled and hot_reload and self._mode != "c":
                self._start_tts_worker()
            return
        self._tts_enabled = enabled
        if hot_reload and self._mode != "c":
            if enabled:
                self._start_tts_worker()
            else:
                self._stop_tts_worker()

    def set_tts_models(self, model_ids: dict[str, str] | None = None) -> None:
        """Select per-language TTS models and hot-reload the speech worker."""
        requested = dict(model_ids or {})
        normalized = {
            "zh": str(requested.get("zh", self._tts_model_ids.get("zh", ""))),
            "en": str(requested.get("en", self._tts_model_ids.get("en", ""))),
        }
        if normalized == self._tts_model_ids:
            return
        self._tts_model_ids = normalized
        # 运行中热重载 TTS 是**刻意支持**的（换模型立刻生效），所以这里不能用
        # _may_replace_resources（那会把热重载一起否掉）。但只要处在"收尾中"，
        # 就不能拆了重建 —— 那样会踩在正在退出的 worker 上。
        if (self._running and not self._is_settling()
                and self._mode != "c" and self._tts_enabled):
            self._stop_tts_worker()
            self._start_tts_worker()
        logger.info("TTS 模型选择已更新: %s", self._tts_model_ids)

    def set_models_dir(self, path: str | Path) -> None:
        """Switch model storage between runs and discard path-bound caches."""
        if not self._may_replace_resources():
            # 保守地保留原标题（测试与界面都在用），只在后面补上原因。
            raise RuntimeError(
                "识别运行中或后台任务仍在收尾，无法切换模型目录")
        new_root = Path(path)
        if new_root.resolve() == self._models_dir.resolve():
            return

        old_root = self._models_dir
        old_cloud, old_translator = self._cloud_stt, self._translator
        self._stop_tts_worker()
        self._models_dir = new_root
        self._asr = None
        self._cloud_stt = None
        self._vad = None
        self._seg = None
        self._context_processor = None
        self._translator = None
        self._trans_kind = None
        self._is_cloud_stt = False
        self._is_generative = False
        # 模型根变了 = 换了一整套权重：结果不可跨代复用（缓存/失败签名必须失效）。
        self._bump_config_generation("set_models_dir")
        for label, component in (("云 STT", old_cloud), ("翻译器", old_translator)):
            if component is None:
                continue
            try:
                component.close()
            except Exception:
                logger.debug("切换模型目录时关闭旧%s失败", label, exc_info=True)
        logger.info("Pipeline 模型目录已切换: old=%s new=%s", old_root, new_root)

    def set_audio_devices(self, mic_device_id: str = "",
                          loopback_device_id: str = "") -> None:
        if self._running:
            return
        self._mic_device_id = str(mic_device_id or "")
        self._loopback_device_id = str(loopback_device_id or "")

    def set_capture_process(self, process_id: int = 0, window_title: str = "") -> None:
        if self._running:
            return
        self._capture_process_id = max(0, int(process_id or 0))
        self._capture_window_title = str(window_title or "")

    def set_stt(self, provider: str = "local", config=None) -> None:
        """Select the speech-to-text side independently from translation."""
        if self._running:
            return
        normalized = "cloud" if str(provider or "").lower() == "cloud" else "local"
        snapshot = dict(config) if isinstance(config, dict) else config
        changed = normalized != self._requested_stt_provider or snapshot != self._stt_config
        self._requested_stt_provider = normalized
        self._stt_config = snapshot
        if not changed:
            return
        if self._cloud_stt is not None:
            try:
                self._cloud_stt.close()
            except Exception:
                logger.debug("关闭旧云 STT 失败", exc_info=True)
        self._cloud_stt = None
        self._asr = None
        self._vad = None
        self._seg = None
        self._context_processor = None
        self._is_cloud_stt = False
        self._is_generative = False

    def set_translator(self, kind: str, config=None) -> None:
        """选择翻译档位；下一次 start 前立即替换旧实例。"""
        if not self._may_replace_resources():
            # 运行中或还在收尾：不替换。原来的行为是静默 return（改动留到下次
            # start 生效），这里保持同一语义 —— 只是把"还在收尾"也纳入判断。
            # 旧实现在 stop 超时后会误判为空闲，把 worker 正在用的实例关掉。
            return
        normalized = kind if kind in ("opus-fast", "qwen-quality", "cloud") else "opus-fast"
        changed = normalized != self._requested_trans_kind or config != self._translator_config
        self._requested_trans_kind = normalized
        self._translator_config = config
        if changed:
            # 档位/凭据变了 → 换的是"谁来翻"，结果不可跨代。
            self._bump_config_generation("set_translator")
        if changed and self._translator is not None:
            try:
                self._translator.close()
            except Exception:
                logger.debug("关闭旧翻译器失败", exc_info=True)
            self._translator = None
            self._trans_kind = None

    def set_asr_model(self, model_id: str) -> None:
        """Select a catalog ASR model for the next run."""
        if not self._may_replace_resources():
            # 同 set_translator：连同"还在收尾"一起判断，避免把 worker 正在用的
            # 识别器置空后又被下一次读引用。
            return
        normalized = str(model_id or "asr-zipformer-bilingual-fast")
        if normalized == self._requested_asr_model_id:
            return
        self._requested_asr_model_id = normalized
        # 换识别模型 = 换"原文怎么来的"：下游翻译结果不可跨代复用。
        self._bump_config_generation("set_asr_model")
        # sherpa recognizers own native state; replace only while stopped.
        self._asr = None
        self._vad = None
        self._seg = None
        self._context_processor = None

    def set_asr_tuning(self, tuning: dict | None = None) -> None:
        """Apply inference/segmentation tuning on the next run.

        This is deliberately not model-weight training.  It controls VAD,
        sentence boundaries, decoder budget, beam width and domain hotwords.

        键名兼容两种写法：配置/界面用 ``asr_`` 前缀，pipeline 内部用短名。
        必须归一化 —— 此前直接存原始 dict，而 resolve_tuning_values 只读短名，
        于是界面保存的档位**完全不生效**：无论选哪档都落到 auto 预设
        （实测 vad=0.5/silence=350/max=4500，而用户存的是 context 的
        0.32/500/18000）。用户看到的是"保存了但没用"。
        """
        if self._running:
            return
        raw = dict(tuning or {})
        normalized = {TUNING_KEY_MAP.get(key, key): value for key, value in raw.items()}
        if normalized == self._asr_tuning:
            return
        self._asr_tuning = normalized
        # VAD/断句/热词会改变切出来的句子本身 → 结果不可跨代复用。
        self._bump_config_generation("set_asr_tuning")
        self._asr = None
        self._vad = None
        self._seg = None
        self._context_processor = None

    def effective_tuning(self) -> dict[str, object]:
        """当前档位下**实际生效**的调优值，键名带 ``asr_`` 前缀。

        界面用它显示"这一项现在真的是多少"。必要性：预设档位下用户存的基础
        参数会被 ASR_TUNING_PRESETS 覆盖，界面若显示用户存的值（例如 0.35），
        用户看到的就是一个不生效的数字。

        键名统一加前缀是为了跨层一致 —— 配置、界面控件都用 ``asr_`` 前缀。
        """
        values = self._effective_asr_tuning(generative=False)
        return {
            "asr_vad_threshold": values["vad_threshold"],
            "asr_silence_ms": values["silence_ms"],
            "asr_max_utterance_ms": values["max_utterance_ms"],
            "asr_beam_paths": values["beam_paths"],
            "asr_max_new_tokens": values["max_new_tokens"],
            "asr_hotwords": values["hotwords"],
            "asr_context_hold_ms": values["context_hold_ms"],
            "asr_live_draft_enabled": values["live_draft_enabled"],
            "asr_context_correction": values["context_correction"],
            "asr_filler_mode": values["filler_mode"],
        }

    def set_recording(self, enabled: bool, directory: str | Path | None = None) -> None:
        """Enable microphone recording alongside translation for the next run."""
        if self._running:
            return
        self._recording_enabled = bool(enabled)
        self._recordings_dir = Path(directory) if directory else None

    def pause(self) -> None:
        """Pause microphone recording and translation without closing the device."""
        if not self._running or self._mode == "c" or self._pause_evt.is_set():
            return
        self._pause_evt.set()
        # The marker is ordered after all already-captured chunks.  The process
        # worker flushes the current phrase so audio from both sides of a long
        # pause is never glued into one sentence.
        self._put_or_stop(
            self._queue,
            _PAUSE_MARKER,
            "识别队列已满，无法安全暂停；任务已停止",
        )
        self._emit_status("已暂停 · 点击继续恢复录音与翻译")
        # 暂停不改 PipelineState（用 _pause_evt 表达），所以不能只靠 _set_state
        # 的钩子，这里显式通知一次。
        self._emit_state()

    def resume(self) -> None:
        if not self._running or not self._pause_evt.is_set():
            return
        self._pause_evt.clear()
        self._emit_status("拾音中")
        self._emit_state()

    def is_paused(self) -> bool:
        return self._pause_evt.is_set()

    @property
    def last_recording_path(self) -> Path | None:
        return self._last_recording_path

    def is_running(self) -> bool:
        return self.state in {
            PipelineState.STARTING,
            PipelineState.RUNNING,
            PipelineState.STOPPING,
        }

    # ---- 回调 (UI 订阅) ----
    def on_utterance(self, cb: Callable[[str, str], None]) -> None:
        self._cb_utterance.append(cb)

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._cb_status.append(cb)

    def on_state(self, cb: Callable[[], None]) -> None:
        """Subscribe to lifecycle/pause state changes.

        Fires on every ``_set_state`` transition (including autonomous ones such as
        a file finishing) and on pause/resume.  Consumers read the current state via
        ``is_running()`` / ``is_paused()`` — no payload is passed, so there is no
        risk of a stale snapshot being cached by the listener.
        """
        self._cb_state.append(cb)

    def on_progress(self, cb: Callable[[int, int, str], None]) -> None:
        """Subscribe to offline audio/video progress (current, total, stage)."""
        self._cb_progress.append(cb)

    def on_partial(self, cb: Callable[[str], None]) -> None:
        self._cb_partial.append(cb)

    def on_draft(self, cb: Callable[[str, str], None]) -> None:
        """Subscribe to one replaceable interim ``(source, translation)`` row."""
        self._cb_draft.append(cb)

    def _emit_status(self, msg: str) -> None:
        logger.info("Pipeline 状态: %s", msg)
        for cb in self._cb_status:
            try:
                cb(msg)
            except Exception:
                logger.exception("状态回调异常: %r", cb)

    def _emit_progress(self, completed: int, total: int, stage: str) -> None:
        """Forward bounded file-mode progress without letting UI callbacks fail work."""
        safe_total = max(1, int(total))
        safe_completed = max(0, min(safe_total, int(completed)))
        logger.debug("文件处理进度: completed=%d total=%d stage=%s",
                     safe_completed, safe_total, stage)
        for cb in self._cb_progress:
            try:
                cb(safe_completed, safe_total, stage)
            except Exception:
                logger.exception("进度回调异常: %r", cb)

    def _emit_utterance(self, text: str, translation: str) -> None:
        logger.info("字幕已生成: src_chars=%d dst_chars=%d", len(text), len(translation))
        for cb in self._cb_utterance:
            try:
                cb(text, translation)
            except Exception:
                logger.exception("字幕回调异常: %r", cb)

    def _emit_partial(self, text: str) -> None:
        try:
            text = guard_text(text, self._src_lang, kind="STT partial")
        except ValueError:
            logger.debug("临时 STT 结果被语言约束过滤: source=%s text=%r",
                         self._src_lang, str(text)[:160])
            return
        if not text:
            return
        for cb in self._cb_partial:
            try:
                cb(text)
            except Exception:
                logger.exception("临时字幕回调异常: %r", cb)
        view = self._live_draft.update_source(text)
        if view is not None:
            self._emit_draft(view)

    def _emit_draft(self, view: DraftView) -> None:
        for cb in self._cb_draft:
            try:
                cb(view.source, view.translation)
            except Exception:
                logger.exception("实时字幕草稿回调异常: %r", cb)

    def _clear_draft(self) -> None:
        for cb in self._cb_draft:
            try:
                cb("", "")
            except Exception:
                logger.exception("清理实时字幕草稿回调异常: %r", cb)

    def _on_asr_partial(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        if self._src_lang != "auto" and not text_matches_language(text, self._src_lang, require_signal=False):
            return
        processor = self._context_processor
        preview = processor.preview(text) if processor is not None else text
        if self._src_lang != "auto" and not text_matches_language(preview, self._src_lang, require_signal=False):
            return
        self._emit_partial(preview)

    # ---- 组件构造 ----
    def _ensure_translator(self, snapshot: _LangSnapshot | None = None) -> None:
        """惰性创建翻译器。

        缺省按**当前**语言对挑档位（start / 文件模式入口都是这个语义）；传
        ``snapshot`` 时按**那条在途任务**的语言对挑 —— 用户切语言不该把正在
        执行的旧任务重新定向。
        """
        if self._translator is None:
            pair = (snapshot.pair if snapshot is not None
                    else (self._src_lang, self._dst_lang))
            self._translator, self._trans_kind, substituted = _load_translator_for_pair(
                self._requested_trans_kind, pair[0], pair[1],
                self._translator_config)
            self._trans_substituted_from = substituted
            # 记录"这个实例是按哪一对语言挑的"，而不是"现在配置是哪一对"。
            self._trans_pair = pair
            self._pair_fail_key = None

    def _translator_supports_pair(self, snapshot: _LangSnapshot | None = None) -> bool:
        """当前翻译器能否处理给定语言对（廉价判断，逐句调用）。

        传 ``snapshot`` 判断的是"这个翻译器能不能翻这条在途句子"，与用户刚切
        的语言无关。
        """
        src, dst = (snapshot.pair if snapshot is not None
                    else (self._src_lang, self._dst_lang))
        supports = getattr(self._translator, "supports", None)
        if not callable(supports):
            return True  # 判断不了就不阻断，交给真正的 translate 去试
        return bool(supports(src, dst))

    def _retune_translator_for_pair(
        self, snapshot: _LangSnapshot | None = None
    ) -> None:
        """语言对变了、当前档位不支持时，重新挑一个支持的档位。

        只在翻译工作线程调用（换翻译器要关旧的，跨线程关会撞上正在进行的
        translate）。用户在会话进行中改语言会走到这里：第一句触发换档，
        之后正常。
        """
        src, dst = (snapshot.pair if snapshot is not None
                    else (self._src_lang, self._dst_lang))
        failed = self._translator
        self._translator = None
        self._ensure_translator(snapshot)
        close = getattr(failed, "close", None)
        if callable(close) and failed is not self._translator:
            close()
        if self._trans_substituted_from is not None:
            self._emit_status(
                f"当前档位不支持 {src}→{dst}，已改用其他档位")
        elif self._trans_kind is None:
            self._emit_status("没有可用翻译档位，暂时显示原文")

    def _warmup_translator(self) -> None:
        """Warm up the selected translator and fall back once on failure."""
        warmup = getattr(self._translator, "warmup", None)
        if not callable(warmup):
            return
        try:
            result = warmup()
        except Exception:
            logger.exception("翻译引擎预热异常")
            result = False
        if result is not False or self._trans_kind != "qwen-quality":
            return
        logger.warning("质量翻译预热失败，尝试切换 OPUS 极速档")
        self._disable_quality_translator()

    def _disable_quality_translator(self) -> None:
        """Replace a failed quality translator so later sentences do not retry it."""
        if self._trans_kind != "qwen-quality":
            return
        failed = self._translator
        fallback, fallback_kind = _load_translator(
            "opus-fast", self._translator_config)
        self._translator, self._trans_kind = fallback, fallback_kind
        close = getattr(failed, "close", None)
        if callable(close):
            close()
        if fallback_kind == "opus-fast":
            self._emit_status("质量翻译不可用，已切换极速翻译")
        else:
            self._emit_status("本地翻译不可用，暂时显示原文")

    def _start_tts_worker(self) -> None:
        if not self._tts_enabled or self._mode == "c":
            return
        if self._tts_worker is None:
            self._tts_worker = TTSWorker(
                self._models_dir,
                external_stop=self._stop_evt,
                model_ids=self._tts_model_ids,
            )
        self._tts_worker.start()

    def _stop_tts_worker(self) -> None:
        worker, self._tts_worker = self._tts_worker, None
        if worker is not None:
            worker.stop()

    def _put_or_stop(self, target: queue.Queue, item: object, message: str) -> None:
        """Bound queue growth and make overload visible instead of losing data."""
        try:
            target.put(item, timeout=0.5)
        except queue.Full as exc:
            self._stop_evt.set()
            self._emit_status(message)
            raise RuntimeError(message) from exc

    @staticmethod
    def _drain_queue(target: queue.Queue) -> None:
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return

    def _effective_asr_tuning(self, generative: bool) -> dict:
        """Resolve friendly presets to concrete values with safe bounds."""
        return resolve_tuning_values(self._asr_tuning, generative)

    def _build_real_time(self) -> None:
        """构建 A/B 模式实时组件 (惰性, 只建一次)。"""
        cloud_ready = self._requested_stt_provider == "cloud" and self._cloud_stt is not None
        local_ready = self._requested_stt_provider != "cloud" and self._asr is not None
        if (self._vad is not None and self._seg is not None and
                (cloud_ready or local_ready)):
            self._ensure_translator()
            return
        # A failed model load must not leave a recognizer behind.  Otherwise a
        # retry skips setup and starts a process thread with ``_seg is None``.
        old_cloud = self._cloud_stt
        if old_cloud is not None:
            try:
                old_cloud.close()
            except Exception:
                logger.debug("重建实时链路前关闭旧云 STT 失败", exc_info=True)
        self._asr = None
        self._cloud_stt = None
        self._vad = None
        self._seg = None
        self._context_processor = None
        self._is_generative = False
        self._is_cloud_stt = False
        cloud_stt = self._requested_stt_provider == "cloud"
        from voxsub.model_catalog import get_model
        from voxsub.router import select_device

        generative_model = get_model(self._requested_asr_model_id)
        generative = cloud_stt or bool(
            generative_model and
            generative_model.runtime != "sherpa-streaming-transducer")
        tuning = self._effective_asr_tuning(generative)
        context_processor = (
            ContextualTextProcessor(
                source_lang=self._src_lang,
                hotwords=tuning["hotwords"],
                filler_mode=tuning["filler_mode"],
                correction_enabled=tuning["context_correction"],
                hold_ms=tuning["context_hold_ms"],
                defer_incomplete=generative,
            )
            if tuning["context_enabled"] else None
        )
        components = build_realtime_components(
            RealtimeBuildSpec(
                self._models_dir, self._requested_stt_provider, self._stt_config,
                self._requested_asr_model_id, self._provider, self._src_lang, tuning,
                generative,
            ),
            queue_audio=self._queue_generative_audio,
            on_sentence=self._on_sentence,
            on_partial=self._on_asr_partial,
            ensure_vad=ensure_bundled_vad,
            vad_factory=WindowVAD,
            asr_factory=create_asr,
            cloud_factory=CloudSTT,
            audio_segmenter_factory=AudioUtteranceSegmenter,
            streaming_segmenter_factory=UtteranceSegmenter,
            select_device=select_device,
            semantic_boundary=(
                context_processor.should_defer_endpoint
                if context_processor is not None and not generative else None),
        )
        self._asr = components.asr
        self._cloud_stt = components.cloud_stt
        self._vad = components.vad
        self._seg = components.segmenter
        self._context_processor = context_processor
        self._is_generative = components.generative
        self._is_cloud_stt = components.cloud
        logger.info(
            "STT 调优生效: provider=%s profile=%s generative=%s vad=%.2f silence=%dms max=%dms beam=%d context=%s",
            "cloud" if components.cloud else "local",
            self._asr_tuning.get("profile", "auto"), components.generative,
            tuning["vad_threshold"], tuning["silence_ms"],
            tuning["max_utterance_ms"], tuning["beam_paths"],
            tuning["context_enabled"],
        )
        self._ensure_translator()

    def _queue_generative_audio(self, audio: np.ndarray) -> None:
        """VAD callback: retain every utterance for the dedicated decoder."""
        arr = np.asarray(audio, dtype=np.float32)
        logger.info("VAD 语音段入队: audio_ms=%.1f queue=%d",
                    arr.size * 1000.0 / SAMPLE_RATE,
                    self._recognition_queue.qsize() + 1)
        self._put_or_stop(
            self._recognition_queue,
            _QueuedAudio(arr, time.monotonic(), self._lang_snapshot()),
            "识别后端持续落后，音频分段缓存已满；任务已停止，请切换更轻量的识别模型",
        )

    def _on_sentence(self, text: str) -> None:
        """Recognition callback: validate and route an acoustic fragment."""
        text = str(text or "").strip()
        if not text:
            return
        # **提交点快照**（缺陷 #9）：这句识别结果从这一刻起就固定用哪一对语言、
        # 哪一代配置。用户之后再切语言只影响**新**的句子，不会回头改写这句。
        snapshot = self._lang_snapshot()
        try:
            text = guard_text(text, snapshot.src, kind="STT")
        except ValueError as exc:
            logger.warning("STT 结果被语言约束拦截: source=%s text=%r reason=%s",
                           snapshot.src, text[:160], exc)
            self._emit_status("识别到其他语言，已忽略当前片段")
            return
        queued_at = time.monotonic()
        if self._context_processor is not None:
            logger.info(
                "STT 片段入上下文队列: chars=%d queue=%d",
                len(text), self._context_queue.qsize() + 1,
            )
            self._put_or_stop(
                self._context_queue,
                _QueuedText(text, queued_at, snapshot),
                "上下文处理持续积压，字幕缓存已满；任务已停止",
            )
            return
        self._queue_translation(text, queued_at, snapshot)

    def _queue_translation(self, text: str, queued_at: float,
                           snapshot: _LangSnapshot | None = None) -> None:
        """终句入翻译队列，并把**入队这一刻**的语言快照挂在条目上。

        ``snapshot`` 缺省时现取一份当前配置的快照（例如集成方直接调用）。
        """
        if snapshot is None:
            snapshot = self._lang_snapshot()
        self._live_draft.begin_final()
        with self._metrics_lock:
            self._translation_times[text].append(queued_at)
        logger.info("STT 终句入翻译队列: chars=%d lang=%s->%s generation=%d queue=%d",
                    len(text), snapshot.src, snapshot.dst, snapshot.generation,
                    self._translation_queue.qsize() + 1)
        try:
            self._put_or_stop(
                self._translation_queue,
                _QueuedTranslation(text, snapshot, queued_at),
                "翻译后端持续落后，字幕缓存已满；任务已停止，请切换更轻量的翻译模型",
            )
        except RuntimeError:
            view = self._live_draft.finish_final()
            if view is not None:
                self._emit_draft(view)
            with self._metrics_lock:
                timestamps = self._translation_times.get(text)
                if timestamps:
                    timestamps.pop()
                    if not timestamps:
                        self._translation_times.pop(text, None)
            raise

    def _context_loop(self) -> None:
        """Stabilize semantic fragments before the translation worker sees them."""
        processor = self._context_processor
        if processor is None:
            self._translation_input_done.set()
            return
        pending_since: float | None = None
        #: 当前这一轮"待稳定文本"是从哪份快照开始的（缺陷 #9）。上下文处理器
        #: 会把多条片段攒成一句话，这一句话必须整体按**开始那一刻**的配置解释；
        #: 否则用户中途切语言时，旧语言的句子会按新语言被过滤掉。
        run_snapshot: _LangSnapshot | None = None
        try:
            while (not self._context_input_done.is_set() or
                   not self._context_queue.empty()):
                item: _QueuedText | None = None
                try:
                    item = self._context_queue.get(timeout=0.1)
                except queue.Empty:
                    segments = processor.poll()
                else:
                    if run_snapshot is None:
                        run_snapshot = item.snapshot
                    segments, pending_since = self._consume_context_item(
                        processor, item, pending_since, run_snapshot)
                if segments:
                    self._commit_context_segments(
                        segments, pending_since or time.monotonic(),
                        run_snapshot)
                    pending_since = None
                    run_snapshot = None
            trailing = processor.flush()
            if trailing:
                self._commit_context_segments(
                    trailing, pending_since or time.monotonic(), run_snapshot)
        except Exception as exc:
            logger.exception("智能上下文处理线程失败")
            self._emit_status(f"上下文处理错误: {exc}")
            self._set_state(PipelineState.FAILED)
            self._stop_evt.set()
        finally:
            self._translation_input_done.set()

    def _consume_context_item(
        self,
        processor: ContextualTextProcessor,
        item: _QueuedText,
        pending_since: float | None,
        run_snapshot: _LangSnapshot | None = None,
    ) -> tuple[list[ContextualSegment], float | None]:
        # 这一条属于"当前这一轮待稳定文本"；有 run_snapshot 就用它（这一句话
        # 是从那里开始的），否则用条目自己的快照。
        snapshot = run_snapshot or item.snapshot or self._lang_snapshot()
        expired = processor.poll(now=item.queued_at)
        if expired:
            self._commit_context_segments(
                expired, pending_since or item.queued_at, snapshot)
            pending_since = None
        if not processor.pending_text:
            pending_since = item.queued_at
        segments = processor.submit(item.text, now=item.queued_at)
        if (not segments and processor.pending_text and
                self._live_draft_enabled()):
            if snapshot.src == "auto" or text_matches_language(processor.pending_text, snapshot.src, require_signal=False):
                self._emit_partial(processor.pending_text)
        if not segments and not processor.pending_text:
            pending_since = None
        return segments, pending_since

    def _commit_context_segments(
        self,
        segments: list[ContextualSegment],
        queued_at: float,
        snapshot: _LangSnapshot | None = None,
    ) -> None:
        """把稳定后的文本交给翻译队列。

        ``snapshot`` 是这批文本**开始时**的语言/配置快照；缺省（例如
        ``processor.poll()`` 现取的片段）用当前配置。
        """
        if snapshot is None:
            snapshot = self._lang_snapshot()
        for segment in segments:
            if not segment.text or not segment.text.strip():
                continue
            if snapshot.src != "auto" and not text_matches_language(segment.text, snapshot.src):
                logger.warning("上下文稳定文本被语言约束拦截: source=%s text=%r", snapshot.src, segment.text[:160])
                continue
            if segment.corrections or segment.fillers_removed:
                logger.info(
                    "上下文文本已稳定: raw=%r final=%r corrections=%s fillers=%d",
                    segment.raw_text[:160], segment.text[:160],
                    segment.corrections, segment.fillers_removed,
                )
            self._queue_translation(segment.text, queued_at, snapshot)

    def _take_translation_timestamp(self, text: str) -> float | None:
        with self._metrics_lock:
            timestamps = self._translation_times.get(text)
            if not timestamps:
                return None
            value = timestamps.popleft()
            if not timestamps:
                self._translation_times.pop(text, None)
            return value

    def _log_translate_failure(self, text: str, queue_wait_ms: float | None,
                               request_ms: float, exc: Exception,
                               snapshot: _LangSnapshot | None = None) -> None:
        """记录翻译失败，**同一原因只完整报一次**。

        此前是逐句 ERROR + 完整回溯：一段 5 分钟的日语音频能刷出上百条一模一样的
        回溯，把真正的错误淹掉，也让日志文件迅速膨胀。原因签名 = 档位 + 语言对
        **+ 配置代次**，所以换档位、换语言、或任何影响结果的配置变更之后都会重新
        完整报一次（那是新信息），而同代内的重复失败继续被折叠。

        ``generation`` 进键是工作单 §3.5 的要求："会影响结果的配置变更要进入缓存
        有效性判断"。同一代内配置保证一致，所以这个键既不会漏报新原因，也不会
        把不同配置下的失败混成同一个。
        """
        key = snapshot or self._lang_snapshot()
        fail_key = (str(self._trans_kind), key.src, key.dst, key.generation)
        first_time = fail_key != self._pair_fail_key
        self._pair_fail_key = fail_key
        waited = f"{queue_wait_ms:.1f}" if queue_wait_ms is not None else "na"
        (logger.error if first_time else logger.debug)(
            "翻译失败: src_chars=%d queue_wait_ms=%s request_ms=%.1f "
            "generation=%d error=%s%s",
            len(text), waited, request_ms, key.generation, exc,
            "" if first_time else "（同一原因，后续不再重复记录）",
            exc_info=first_time,
        )

    def _translate_sentence(self, text: str, queued_at: float | None = None,
                            snapshot: _LangSnapshot | None = None) -> None:
        """翻译单句并回调 UI；只在翻译工作线程调用。

        ``snapshot`` 是这条句子**入队时**的语言/配置快照（缺陷 #9）。缺省
        （直接调用，例如 TTS 路径或单测）时取当前配置。

        本函数内部**不再回读** ``self._src_lang`` / ``self._dst_lang``：用户
        在会话中途切语言，只该影响之后新入队的句子，不该改写已经在途的这一句。
        """
        if snapshot is None:
            snapshot = self._lang_snapshot()
        # 源语言约束：如果指定了源语言，但文本不符合该语言，直接丢弃不予翻译
        if snapshot.src != "auto" and not text_matches_language(text, snapshot.src):
            logger.warning("翻译前拦截非指定源语言文本: expected=%s text=%r",
                           snapshot.src, text[:160])
            return
        self._emit_status("翻译中…")
        started = time.perf_counter()
        queue_wait_ms = ((time.monotonic() - queued_at) * 1000.0
                         if queued_at is not None else None)
        # 语言对可能在会话中途被改（用户切了"识别语言/翻译为"），而档位是按
        # 旧语言对挑的。这里在真正翻译前确认一次：不支持就换档位，避免每句
        # 都抛"快档不支持语言对"（用户看到的就是满屏 ERROR 加原文）。
        # 判断用的是**这条任务自己的**快照，不是用户刚切过去的新语言对。
        if (self._trans_pair != snapshot.pair
                and not self._translator_supports_pair(snapshot)):
            self._retune_translator_for_pair(snapshot)
        translation, can_speak = self._run_translation(
            text, snapshot, queue_wait_ms, started)
        self._present_translation(text, translation, snapshot, speak=can_speak)

    def _run_translation(self, text: str, snapshot: _LangSnapshot,
                         queue_wait_ms: float | None, started: float
                         ) -> tuple[str, bool]:
        """调用翻译器，返回 ``(译文, 是否可朗读)``；失败时译文为空串。

        语言一律来自 ``snapshot``（缺陷 #9），成功/失败日志与"档位降级"都收在
        这里，:meth:`_translate_sentence` 只负责编排与回调。
        """
        try:
            translation = self._translator.translate(text, snapshot.src, snapshot.dst)
            if self._trans_kind is not None:
                translation = guard_text(
                    translation, snapshot.dst, kind="translation")
        except Exception as exc:
            self._log_translate_failure(
                text, queue_wait_ms, (time.perf_counter() - started) * 1000.0,
                exc, snapshot)
            self._disable_quality_translator()
            self._emit_status("翻译失败，未显示伪译文")
            return "", False
        self._pair_fail_key = None   # 恢复成功，下次失败要重新报
        logger.info("翻译完成: src_chars=%d dst_chars=%d lang=%s->%s "
                    "queue_wait_ms=%s request_ms=%.1f",
                    len(text), len(translation), snapshot.src, snapshot.dst,
                    f"{queue_wait_ms:.1f}" if queue_wait_ms is not None else "na",
                    (time.perf_counter() - started) * 1000.0)
        return translation, True

    def _present_translation(self, text: str, translation: str,
                             snapshot: _LangSnapshot, *, speak: bool) -> None:
        """把一条译文交代出去：字幕回调 → 草稿收尾 → TTS → 状态复位。

        TTS 用的是**这条句子的**目标语言快照：否则"用中文语音读英文句子"
        这类错配会在用户切语言之后出现。
        """
        self._emit_utterance(text, translation)
        view = self._live_draft.finish_final()
        if view is not None:
            self._emit_draft(view)
        worker = self._tts_worker
        if speak and worker is not None and translation:
            worker.submit(translation, snapshot.dst)
        if self._running:
            self._emit_status(
                "已暂停 · 点击继续恢复录音与翻译"
                if self._pause_evt.is_set() else "拾音中"
            )

    def _translate_draft(self, request: DraftTranslationRequest,
                         snapshot: _LangSnapshot | None = None) -> None:
        """Translate a revision and retain compatible lagging results.

        草稿是"现在这一行"的**实时视图**，所以默认用当前配置（用户切了语言，
        下一份草稿立刻按新语言对走）。但如果翻译过程中配置代次变了，这份结果
        就不再代表当前配置 —— 丢弃（等于取消这次在途请求），不能把旧语言对的
        译文贴在新语言对下面。
        """
        if snapshot is None:
            snapshot = self._lang_snapshot()
        if snapshot.src != "auto" and not text_matches_language(request.source, snapshot.src):
            return
        try:
            translation = self._translator.translate(
                request.source, snapshot.src, snapshot.dst)
            if self._trans_kind is not None:
                translation = guard_text(
                    translation, snapshot.dst, kind="draft translation")
        except Exception:
            logger.debug("实时草稿翻译失败: source=%r", request.source[:160],
                         exc_info=True)
            self._disable_quality_translator()
            return
        if snapshot.generation != self.config_generation:
            logger.debug(
                "实时草稿结果已过期（翻译期间配置代次变化），丢结果而不是贴旧配置的译文: "
                "generation=%d current=%d", snapshot.generation,
                self.config_generation)
            return
        view = self._live_draft.accept_translation(request, translation)
        if view is not None:
            self._emit_draft(view)

    def _live_draft_enabled(self) -> bool:
        return (
            str(self._asr_tuning.get("profile", "auto")) == "context"
            and bool(self._asr_tuning.get("live_draft_enabled", True))
        )

    def _live_draft_translation_enabled(self) -> bool:
        return (
            bool(self._cb_draft)
            and self._live_draft_enabled()
        )

    def _translate_queued_item(self, item: object) -> None:
        """执行一条翻译队列条目，用的是**条目自带**的快照。

        这是缺陷 #9 的落点：句子按它入队时的语言对/配置代次翻，而不是按出队
        那一刻的实例字段。
        """
        if isinstance(item, _QueuedTranslation):
            text, snapshot = item.text, item.snapshot
            queued_at = self._take_translation_timestamp(text)
            if queued_at is None:
                queued_at = item.queued_at
        else:
            # 兼容仍然直接 put 裸字符串的集成：只能退化成"出队时的配置"，
            # 也就是缺陷 #9 的旧行为。管线内部一律走 _QueuedTranslation。
            text = str(item)
            snapshot = self._lang_snapshot()
            queued_at = self._take_translation_timestamp(text)
        self._translate_sentence(text, queued_at, snapshot)

    def _translation_loop(self) -> None:
        """Prioritize final sentences, then translate only the latest live draft."""
        warmup = getattr(self._translator, "warmup", None)
        if callable(warmup):
            logger.info("翻译工作线程开始预热")
            self._warmup_translator()
        while True:
            try:
                item = self._translation_queue.get_nowait()
            except queue.Empty:
                item = None
            if item is not None:
                self._translate_queued_item(item)
                continue
            if self._translation_input_done.is_set():
                break
            request = (
                self._live_draft.take_translation_request()
                if self._live_draft_translation_enabled() else None
            )
            if request is not None:
                self._translate_draft(request, self._lang_snapshot())
                continue
            try:
                item = self._translation_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._translate_queued_item(item)

    def _recognition_loop(self) -> None:
        """Decode VAD-complete waveforms without ever blocking audio capture."""
        try:
            while (not self._recognition_input_done.is_set() or
                   not self._recognition_queue.empty()):
                try:
                    item = self._recognition_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                audio, queued_at, snapshot = self._recognition_item(item)
                backlog = self._recognition_queue.qsize()
                if backlog:
                    logger.info("STT 处理积压: segments=%d（音频已完整保留）", backlog)
                stt_started = time.perf_counter()
                wait_ms = ((time.monotonic() - queued_at) * 1000.0
                            if queued_at is not None else None)
                text = self._decode_recognition_audio(audio, snapshot)
                if text is None:
                    continue
                decode_ms = (time.perf_counter() - stt_started) * 1000.0
                self._log_recognition(audio, text, wait_ms, decode_ms)
                if text:
                    self._on_sentence(text)
        except Exception as exc:
            logger.exception("生成式 ASR 解码线程失败")
            self._emit_status(f"识别处理错误: {exc}")
            self._set_state(PipelineState.FAILED)
            self._stop_evt.set()
        finally:
            if self._context_processor is not None:
                self._context_input_done.set()
            else:
                self._translation_input_done.set()

    @staticmethod
    def _recognition_item(
        item,
    ) -> tuple[np.ndarray, float | None, _LangSnapshot | None]:
        if isinstance(item, _QueuedAudio):
            return item.audio, item.queued_at, item.snapshot
        # Keep integrations that enqueue raw PCM compatible with the queue.
        return np.asarray(item, dtype=np.float32), None, None

    def _decode_recognition_audio(self, audio: np.ndarray,
                                  snapshot: _LangSnapshot | None = None) -> str | None:
        client = self._cloud_stt if self._is_cloud_stt else self._asr
        if client is None:
            kind = "云 STT 客户端" if self._is_cloud_stt else "本地 STT 执行器"
            raise RuntimeError(f"{kind}未初始化")
        values = np.asarray(audio, dtype=np.float32).reshape(-1)
        if values.size:
            peak = float(np.max(np.abs(values)))
            rms = float(np.sqrt(np.mean(np.square(values))))
            near_silent = float(np.mean(np.abs(values) < 1e-4))
            clipped = float(np.mean(np.abs(values) >= 0.999))
        else:
            peak = rms = near_silent = clipped = 0.0
        logger.debug(
            "STT 片段开始: runtime=%s provider=%s audio_ms=%.1f samples=%d "
            "peak=%.4f rms=%.4f near_silent=%.3f clipped=%.3f",
            "cloud-stt" if self._is_cloud_stt else getattr(client, "runtime", "local-stt"),
            "cloud" if self._is_cloud_stt else getattr(client, "provider", "cpu"),
            values.size * 1000.0 / SAMPLE_RATE, values.size,
            peak, rms, near_silent, clipped,
        )
        if values.size and rms < 0.003:
            logger.warning("STT 片段音量很低: audio_ms=%.1f rms=%.5f peak=%.5f",
                           values.size * 1000.0 / SAMPLE_RATE, rms, peak)
        try:
            if self._is_cloud_stt:
                # 云 STT 也吃语言提示：用音频**入队时**的快照，避免用户在排队
                # 期间切语言导致这段音频被按新语言转写。
                source_lang = (snapshot.src if snapshot is not None
                               else self._src_lang)
                return client.transcribe_samples(
                    audio, source_lang=source_lang).strip()
            stream = client.create_stream()
            client.feed(stream, audio)
            text = client.decode(stream).strip()
            client.reset(stream)
            return text
        except Exception as exc:
            kind = "云" if self._is_cloud_stt else "本地"
            logger.error(
                "%s STT 片段失败: audio_ms=%.1f error=%s", kind,
                audio.size * 1000.0 / SAMPLE_RATE, exc, exc_info=True,
            )
            self._emit_status(f"{kind} STT 请求失败，已跳过当前片段")
            return None

    def _log_recognition(self, audio: np.ndarray, text: str,
                         wait_ms: float | None, decode_ms: float) -> None:
        runtime = ("cloud-stt" if self._is_cloud_stt else
                   getattr(self._asr, "runtime", "local-stt"))
        provider = ("cloud" if self._is_cloud_stt else
                    getattr(self._asr, "provider", "cpu"))
        logger.info(
            "STT 片段完成: runtime=%s provider=%s audio_ms=%.1f wait_ms=%s "
            "decode_ms=%.1f chars=%d source=%s",
            runtime, provider, audio.size * 1000.0 / SAMPLE_RATE,
            f"{wait_ms:.1f}" if wait_ms is not None else "na",
            decode_ms, len(text), self._src_lang,
        )

    # ---- 启停 ----
    def start(self) -> None:
        if self._is_settling():
            # 收尾窗口里**必须报错，不能静默 return**（缺陷 #6 的修复带出来的回归）。
            #
            # 状态是 STOPPING 时 is_running() 也为真，所以原来的 `if self.is_running(): return`
            # 会把它当成"已经在跑、无需重复启动"而静默返回；上层的 _cmd_start 随后
            # **无条件**发 session=start，渲染层据此清空整场字幕并重置时间基准 ——
            # 用户看到的是"好像开始了，但字幕全没了"，导出也随之变空。
            # 而且这个窗口比修复前更宽（8 秒 join + 30 秒观察者）。
            #
            # 旧语义（状态落 IDLE + 活线程）在这里是抛错的，这里恢复那个诚实的报错。
            names = ", ".join(_thread_label(t) for t in self._workers_alive())
            raise RuntimeError(f"上一任务仍在安全收尾（{names or '停止中'}），请稍后再开始")
        if self.is_running():
            return
        # 清理上次异常退出留下的线程引用与旧音频块。
        self._threads = [t for t in self._threads if t.is_alive()]
        if self._threads:
            names = ", ".join(t.name for t in self._threads)
            raise RuntimeError(f"上一任务仍在安全收尾（{names}），请稍后再开始")
        self._drain_queue(self._queue)
        self._drain_queue(self._recognition_queue)
        self._drain_queue(self._context_queue)
        self._drain_queue(self._translation_queue)
        self._live_draft.reset()
        self._clear_draft()
        with self._metrics_lock:
            self._translation_times.clear()
        self._stop_evt.clear()
        self._pause_evt.clear()
        self._recognition_input_done.clear()
        self._context_input_done.clear()
        self._translation_input_done.clear()
        self._set_state(PipelineState.STARTING)
        self._emit_status("启动中…")
        try:
            new_threads = (self._new_file_threads() if self._mode == "c"
                           else self._new_realtime_threads())
            self._set_state(PipelineState.RUNNING)
            self._threads.extend(new_threads)
            for thread in new_threads:
                thread.start()
            self._emit_status("处理中…" if self._mode == "c" else "正在连接音频设备…")
        except Exception as exc:
            self._set_state(PipelineState.FAILED)
            self._stop_evt.set()
            self._stop_tts_worker()
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                recorder.close()
            logger.exception("Pipeline 启动失败: mode=%s", self._mode)
            self._emit_status(f"启动失败: {exc}")
            raise

    def _new_file_threads(self) -> list[threading.Thread]:
        if self._in_path is None or not self._in_path.exists():
            raise FileNotFoundError("请先选择要处理的音频或视频文件")
        return [threading.Thread(
            target=self._run_file_mode, name="pipeline-file", daemon=True)]

    def _new_realtime_threads(self) -> list[threading.Thread]:
        self._build_real_time()
        if self._context_processor is not None:
            self._context_processor.reset()
        self._start_tts_worker()
        if self._mode == "a" and self._recording_enabled:
            self._recorder = WaveSessionRecorder(self._recordings_dir)
            self._last_recording_path = self._recorder.path
        threads = [
            threading.Thread(target=self._capture_loop,
                             name="pipeline-capture", daemon=True),
            threading.Thread(target=self._process_loop,
                             name="pipeline-process", daemon=True),
        ]
        if self._is_generative:
            threads.append(threading.Thread(
                target=self._recognition_loop,
                name="pipeline-recognize", daemon=True,
            ))
        if self._context_processor is not None:
            threads.append(threading.Thread(
                target=self._context_loop,
                name="pipeline-context", daemon=True,
            ))
        threads.append(threading.Thread(
            target=self._translation_loop,
            name="pipeline-translate", daemon=True,
        ))
        return threads

    def stop(self) -> bool:
        """Request shutdown and return whether every worker has exited.

        The public stop behavior remains best-effort and bounded.  The boolean
        result gives :meth:`close` the missing ownership signal: native
        translators must not be destroyed while a timed-out worker can still
        call them.
        """
        if not self.is_running() and not any(t.is_alive() for t in self._threads):
            return True
        self._set_state(PipelineState.STOPPING)
        self._stop_evt.set()
        source = self._source
        if source is not None:
            try:
                source.stop()
            except Exception:
                logger.debug("主动停止音频源失败", exc_info=True)
        # capture → process(flush) → translate 的拥有关系必须保持；处理线程是
        # segmenter 唯一拥有者，UI 线程绝不能再次 flush/reset 原生 sherpa 流。
        # Use one shared deadline.  A per-thread 8 second timeout used to stack
        # across capture/process/translation workers and could make application
        # shutdown appear hung for over half a minute during an update.
        join_deadline = time.monotonic() + _STOP_JOIN_SECONDS
        for t in self._threads:
            if t is not threading.current_thread():
                remaining = join_deadline - time.monotonic()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)
        self._threads = [t for t in self._threads if t.is_alive()]
        workers_stopped = not self._threads
        if workers_stopped:
            self._set_state(PipelineState.IDLE)
        else:
            # 超时：**不落 IDLE**（缺陷 #6 的修复点）。
            #
            # 原生推理与云端请求不能立即打断，线程可能仍在用翻译器/识别器。
            # 以前这里无条件进 IDLE，后果有两个：
            #   1. 界面显示"已停止"，而任务其实还在收尾 —— 状态在撒谎；
            #   2. set_translator / set_asr_model / set_models_dir 只判断
            #      self._running，于是把还在被使用的实例换掉或关掉。
            # 保持 STOPPING 同时解决这两点：is_running() 仍为真（门禁保持关闭），
            # 界面看到的是诚实的"停止中"。
            names = ", ".join(_thread_label(t) for t in self._threads)
            logger.warning("Pipeline 停止超时，保持“停止中”等待收尾: threads=%s", names)
            self._emit_status("停止中（等待后台任务收尾）")
            self._watch_settlement()
        self._live_draft.reset()
        self._clear_draft()
        self._stop_tts_worker()
        if workers_stopped:
            if self._last_recording_path is not None and self._recording_enabled:
                self._emit_status(f"已停止 · 录音已保存：{self._last_recording_path}")
            else:
                self._emit_status("已停止")
        return workers_stopped

    def _watch_settlement(self) -> None:
        """超时之后继续等：worker 真的退出了才落 IDLE。

        没有这个观察者，超时的 pipeline 会永远停在"停止中"，用户只能重启应用 ——
        那就把"诚实的状态"变成了"卡死"。观察者只等**本实例自己的**线程，
        有界（``_SETTLE_WATCH_SECONDS``），且同一时间只允许存在一个。
        """
        existing = getattr(self, "_settle_watcher", None)
        if existing is not None and existing.is_alive():
            return

        def wait_for_workers() -> None:
            # 观察者是 daemon 线程，异常没人接 —— 必须自己兜住，否则会在测试
            # 与控制台里留下"threading 未处理异常"的噪音，反而掩盖真问题。
            try:
                deadline = time.monotonic() + _SETTLE_WATCH_SECONDS
                while True:
                    alive = self._workers_alive()
                    if not alive:
                        break
                    if time.monotonic() >= deadline:
                        logger.error(
                            "Pipeline 工作线程在停止后 %ss 仍未退出，保持停止中: %s",
                            _SETTLE_WATCH_SECONDS,
                            ", ".join(_thread_label(t) for t in alive))
                        return
                    time.sleep(_SETTLE_POLL_SECONDS)
                self._set_state(PipelineState.IDLE)
                if self._last_recording_path is not None and self._recording_enabled:
                    self._emit_status(f"已停止 · 录音已保存：{self._last_recording_path}")
                else:
                    self._emit_status("已停止")
            except Exception:  # noqa: BLE001 - daemon 线程最后一道保险
                logger.warning("收尾观察者异常退出", exc_info=True)

        watcher = threading.Thread(target=wait_for_workers,
                                   name="pipeline-settle-watch", daemon=True)
        self._settle_watcher = watcher
        watcher.start()

    def close(self) -> bool:
        """Stop workers and release process-backed runtime components.

        ``stop()`` intentionally keeps lazily-created recognizers and
        translators reusable for the next run.  Application shutdown needs a
        stronger lifecycle boundary: the local llama-server must be closed
        before an installer replaces its DLLs, even when the pipeline is idle.
        Keep this method idempotent because Qt's ``aboutToQuit`` path owns
        application shutdown, while tests and embedding hosts may call it
        directly.
        """
        if not self.stop():
            # A worker can still be inside a cloud request or llama-server
            # completion after the shared shutdown deadline.  Releasing its
            # client here races the worker and can turn orderly cancellation
            # into a native-process crash.  A later idempotent close may clean
            # up once the worker has actually exited.
            logger.warning(
                "Pipeline 工作线程仍在收尾，暂不释放运行时组件: threads=%s",
                ", ".join(thread.name for thread in self._threads),
            )
            return False
        cloud_stt, self._cloud_stt = self._cloud_stt, None
        translator, self._translator = self._translator, None
        self._trans_kind = None
        for label, component in (("云 STT", cloud_stt), ("翻译器", translator)):
            if component is None:
                continue
            try:
                component.close()
            except Exception:
                logger.debug("关闭%s失败", label, exc_info=True)
        return True

    # ---- A/B 模式线程 ----
    def _make_source(self) -> AudioSource:
        if self._mode == "b":
            if self._capture_process_id > 0:
                from voxsub.process_audio import ProcessLoopbackSource

                return ProcessLoopbackSource(self._capture_process_id)
            if self._loopback_device_id:
                device = self._find_device(list_loopbacks(), self._loopback_device_id,
                                           "输出设备")
                return LoopbackSource(device=device)
            # 不再错误选择列表第一个；LoopbackSource() 会匹配系统默认扬声器。
            return LoopbackSource()
        if self._mic_device_id:
            device = self._find_device(list_microphones(), self._mic_device_id, "麦克风")
            return MicSource(device=device)
        return MicSource()

    @staticmethod
    def _find_device(devices: list, device_id: str, label: str) -> object:
        """按端点 ID 找设备；找不到时回退按名字匹配。

        为什么要有名字回退：界面曾经把设备**名字**当 id 存进配置
        （见 ipc_server._cmd_list_audio_devices 的说明）。那些已经存了名字的
        配置如果直接抛错，用户会看到"已选择的麦克风当前不可用"却不知道
        该改哪里 —— 明明列表里就有这个设备。按名字能匹配上就照常用。

        仍然匹配不到才抛错：那说明设备真的拔了/换了，报错并让用户重选是对的。
        """
        for info in devices:
            if str(getattr(info.device, "id", "")) == device_id:
                return info.device
        for info in devices:
            if str(getattr(info, "name", "")) == device_id:
                logger.info("%s按名字匹配到设备（配置里存的是旧格式的设备名）", label)
                return info.device
        raise RuntimeError(f"已选择的{label}当前不可用，请在设置中重新选择")

    def _capture_loop(self) -> None:
        try:
            source = self._make_source()
            self._source = source
            source.start()
            name = str(getattr(source, "device_name", type(source).__name__))
            logger.info("实时采集开始: mode=%s source=%s", self._mode, name)
            self._emit_status(f"拾音中 · {name}")
            started = time.monotonic()
            metrics = _CaptureMetrics(started, started)
            while not self._stop_evt.is_set():
                chunk = source.read_chunk()
                if chunk is None:
                    if not self._stop_evt.is_set():
                        raise RuntimeError("音频设备意外停止输出")
                    break
                metrics.observe(chunk)
                self._accept_capture_chunk(chunk)
                self._report_capture_health(metrics, name)
        except Exception as exc:
            logger.exception("音频采集失败: mode=%s", self._mode)
            self._emit_status(f"音频设备错误: {exc}")
            self._set_state(PipelineState.FAILED)
            self._stop_evt.set()
        finally:
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    logger.exception("结束同传录音失败")
            source = self._source
            self._source = None
            if source is not None:
                try:
                    source.stop()
                    source.close()
                except Exception:
                    logger.debug("释放音频源失败", exc_info=True)

    def _accept_capture_chunk(self, chunk: np.ndarray) -> None:
        if self._pause_evt.is_set():
            # Drain WASAPI while paused, but omit samples from recording/ASR.
            return
        if self._recorder is not None:
            self._recorder.write(chunk)
        try:
            self._queue.put(chunk, timeout=0.5)
        except queue.Full as exc:
            raise RuntimeError(
                "识别持续落后超过 10 分钟；为避免静默丢音已停止任务，"
                "请改用更轻量模型或硬件加速"
            ) from exc

    def _report_capture_health(self, metrics: _CaptureMetrics, name: str) -> None:
        now = time.monotonic()
        if now - metrics.last_heartbeat >= 1.0:
            backlog_s = self._queue.qsize() * CHUNK_FRAMES / SAMPLE_RATE
            logger.debug(
                "音频心跳: chunks=%d peak=%.6f queue=%d backlog=%.2fs",
                metrics.chunks, metrics.peak, self._queue.qsize(), backlog_s,
            )
            if backlog_s >= 5.0:
                logger.warning("识别暂时落后 %.1fs，音频仍完整缓冲、未丢失", backlog_s)
            metrics.last_heartbeat = now
        if (now - metrics.started >= 4.0 and metrics.peak < 1e-5 and
                not metrics.warned_silence):
            metrics.warned_silence = True
            logger.warning("音频已连接但持续静音: source=%s", name)
            self._emit_status("未检测到声音，请检查设备或播放内容")

    def _process_loop(self) -> None:
        try:
            seg = self._seg
            while not self._stop_evt.is_set() or not self._queue.empty():
                try:
                    chunk = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if seg is None:
                    raise RuntimeError("识别分句器未初始化，已取消本次任务")
                if chunk is _PAUSE_MARKER:
                    seg.flush()
                    continue
                seg.feed(chunk)
        except Exception as exc:
            logger.exception("ASR/VAD 处理线程失败")
            self._emit_status(f"识别处理错误: {exc}")
            self._set_state(PipelineState.FAILED)
            self._stop_evt.set()
        finally:
            try:
                if self._seg is not None:
                    self._seg.flush()
            except Exception:
                logger.exception("处理线程 flush 尾句失败")
            finally:
                if self._is_generative:
                    self._recognition_input_done.set()
                elif self._context_processor is not None:
                    self._context_input_done.set()
                else:
                    self._translation_input_done.set()

    # ---- C 模式 (文件 → 双语字幕) ----
    def _run_file_mode(self) -> None:
        if self._in_path is None or not self._in_path.exists():
            self._emit_status("文件不存在")
            self._set_state(PipelineState.FAILED)
            return
        wav_path: Optional[Path] = None
        try:
            self._emit_progress(0, 100, "正在准备音视频")
            self._emit_status(f"正在提取/读取音频: {self._in_path.name}")
            self._ensure_translator()
            if callable(getattr(self._translator, "warmup", None)):
                logger.info("文件模式开始预热翻译引擎")
                self._warmup_translator()
            lines, wav_path = self._transcribe_file(self._in_path)
            out = self._in_path.with_suffix(".srt")
            self._emit_progress(97, 100, "正在导出字幕")
            self.write_srt(lines, out)
            self._emit_progress(100, 100, "音视频处理完成")
            self._emit_status(f"完成 → {out}")
            self._emit_utterance(f"已导出 {len(lines)} 条字幕", str(out))
        except Exception as exc:
            logger.error("文件处理失败 path=%s: %s", self._in_path, exc, exc_info=True)
            self._emit_status(f"文件处理失败: {exc}")
            self._set_state(PipelineState.FAILED)
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("临时音频删除失败: %s", wav_path, exc_info=True)
            if self.state is not PipelineState.FAILED:
                self._set_state(PipelineState.IDLE)

    def _transcribe_file(self, path: Path) -> tuple[list[SubtitleLine], Optional[Path]]:
        """Decode one file, then delegate recognition to the selected backend."""
        self._emit_progress(5, 100, "正在读取音频")
        pcm, wav_path = FileAudioDecoder.decode(path)
        self._emit_progress(10, 100, "正在识别音视频")
        recognize = (self._recognize_cloud_file if self._requested_stt_provider == "cloud"
                     else self._recognize_streaming)
        return self._call_file_recognizer(recognize, pcm), wav_path

    def _call_file_recognizer(
        self,
        recognize: Callable[..., list[SubtitleLine]],
        pcm: np.ndarray,
    ) -> list[SubtitleLine]:
        """Pass progress to current recognizers without breaking old adapters."""
        try:
            accepts_progress = "progress" in inspect.signature(recognize).parameters
        except (TypeError, ValueError):
            accepts_progress = False
        if accepts_progress:
            return recognize(pcm, progress=self._emit_progress)
        return recognize(pcm)

    def _recognize_streaming(
        self,
        pcm: np.ndarray,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> list[SubtitleLine]:
        """Build local runtimes and delegate pure file recognition."""
        self._build_real_time()
        self._ensure_translator()
        return FileRecognizer.local(
            pcm,
            vad=self._vad,
            asr=self._asr,
            translator=self._translator,
            source_lang=self._src_lang,
            target_lang=self._dst_lang,
            validate_translation=self._trans_kind is not None,
            tuning=self._effective_asr_tuning(generative=self._is_generative),
            progress=progress,
        )

    def _recognize_cloud_file(
        self,
        pcm: np.ndarray,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> list[SubtitleLine]:
        """Build cloud runtimes and delegate VAD-split recognition."""
        self._build_real_time()
        if self._cloud_stt is None or self._vad is None:
            raise RuntimeError("云 STT 未正确初始化")
        self._ensure_translator()
        return FileRecognizer.cloud(
            pcm,
            vad=self._vad,
            cloud_stt=self._cloud_stt,
            translator=self._translator,
            source_lang=self._src_lang,
            target_lang=self._dst_lang,
            tuning=self._effective_asr_tuning(generative=True),
            validate_translation=self._trans_kind is not None,
            progress=progress,
        )

    # ---- srt / vtt / txt 导出 (模块级函数, 便于单测) ----
    @staticmethod
    def _fmt_ts(ms: int) -> str:
        return SubtitleExporter.format_timestamp(ms)

    @staticmethod
    def write_srt(lines: list[SubtitleLine], out: Path, dur_ms: int = 1500) -> None:
        SubtitleExporter.write_srt(lines, out, duration_ms=dur_ms)

    @staticmethod
    def write_vtt(lines: list[SubtitleLine], out: Path) -> None:
        SubtitleExporter.write_vtt(lines, out)

    @staticmethod
    def write_txt(lines: list[SubtitleLine], out: Path) -> None:
        SubtitleExporter.write_txt(lines, out)
