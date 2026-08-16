"""voxsub.tts —— 离线 TTS 合成封装 (M5, 适配 sherpa-onnx 1.13.5 OfflineTts)。

契约 (DESIGN.md「TTS 契约（M5）」):
- class TTSEngine: __init__(model_dir, provider="cpu", num_threads=1)
  synthesize(text, lang="zh") -> np.ndarray | None
  health() -> str
- 返回统一 16k mono float32 (DESIGN.md 全项目统一采样率);
  合成失败一律返回 None, 调用方静默降级为仅字幕, 绝不阻断字幕流程。

sherpa-onnx 1.13.5 API 事实 (实测, 与旧文档不同):
1. TTS 用 OfflineTts: cfg = OfflineTtsConfig(model=OfflineTtsModelConfig(
     vits=OfflineTtsVitsModelConfig(model=..., tokens=..., lexicon=..., data_dir=...),
     provider=..., num_threads=...), rule_fsts="", max_num_sentences=1)
2. 合成: tts.generate(text, sid=0, speed=1.0) -> 含 .samples(float64 数组) / .sample_rate
3. **采样率随模型而异** (实测 zh-aishell3=8000, en-ljspeech=22050), 不是 16k,
   且 samples 是 float64 —— 必须自行重采样/转 float32 才能满足 16k float32 契约
4. 模型路径参数必须是 str, 不能是 pathlib.Path
5. 不同模型包结构不同: 有的带 lexicon.txt (aishell3), 有的带 espeak-ng-data 目录
   (ljspeech 等 piper 系) —— 探测时按存在性填充 lexicon/data_dir

模型目录约定: %LOCALAPPDATA%/VoxSub/models/tts/{zh,en}/ (model.onnx + tokens.txt)。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np

#: 全项目统一输出采样率 (audio/asr 同款, 见 DESIGN.md)
SAMPLE_RATE = 16000

_CANDIDATE_LANGS = ("zh", "en")


def models_dir() -> Path:
    """返回本地模型根目录 %LOCALAPPDATA%/VoxSub/models (DESIGN.md 约定)。"""
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "models"


def _resample_to_16k(pcm: np.ndarray, src_rate: int) -> np.ndarray:
    """线性插值重采样到 16k, 输出 float32 (与 tests/test_asr.py 的 load_wav16k 同法)。

    src_rate == 16000 时原样返回 (避免无谓拷贝)。
    """
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if src_rate == SAMPLE_RATE or pcm.size == 0:
        return pcm
    x_old = np.linspace(0.0, 1.0, pcm.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, int(pcm.size * SAMPLE_RATE / src_rate), endpoint=False)
    if x_new.size == 0:
        return pcm
    return np.interp(x_new, x_old, pcm).astype(np.float32)


class TTSEngine:
    """sherpa-onnx OfflineTts 封装 (vits 生态), 线程安全。

    用法::

        tts = TTSEngine(models_dir() / "tts")          # 自动探测 tts/{zh,en}
        pcm = tts.synthesize("你好，语幕", lang="zh")   # 16k mono float32; 失败 None
        assert tts.health() == "ok"

    线程安全: 内部锁保护模型懒加载与每次合成调用 (sherpa OfflineTts 非线程安全,
    UI/管线可能并发调用朗读)。模型按语言懒加载——缺模型的语种不报错,
    直到真正合成时才返回 None (降级友好)。
    """

    def __init__(self, model_dir: Path, provider: str = "cpu", num_threads: int = 1):
        self._model_dir = Path(model_dir)
        self._provider = provider
        self._num_threads = num_threads
        self._lock = threading.RLock()
        self._tts: dict[str, object] = {}       # lang -> OfflineTts 实例 (懒加载)
        self._ready: dict[str, bool] = {}       # lang -> 模型文件就绪?
        for lang in _CANDIDATE_LANGS:
            self._ready[lang] = bool(self._find_model_files(lang))

    # ------------------------------------------------------------------
    # 模型探测
    # ------------------------------------------------------------------

    def _lang_dir(self, lang: str) -> Path:
        """语种模型目录: <model_dir>/<lang> (契约约定 tts/{zh,en})。"""
        return self._model_dir / lang

    def _find_model_files(self, lang: str) -> Path | None:
        """定位一个语种的模型目录 (须含 model.onnx + tokens.txt), 否则 None。"""
        d = self._lang_dir(lang)
        if (d / "model.onnx").is_file() and (d / "tokens.txt").is_file():
            return d
        return None

    def _build_tts(self, lang: str) -> object | None:
        """构造该语种 OfflineTts; 模型缺失/构造失败返回 None (不抛)。"""
        d = self._find_model_files(lang)
        if d is None:
            return None
        try:
            import sherpa_onnx  # 延迟 import: 无模型环境的纯逻辑测试仍可导入本模块

            vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(d / "model.onnx"),
                tokens=str(d / "tokens.txt"),
                lexicon=str(d / "lexicon.txt") if (d / "lexicon.txt").is_file() else "",
                data_dir=str(d / "espeak-ng-data")
                if (d / "espeak-ng-data").is_dir() else "",
            )
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=vits, provider=self._provider, num_threads=self._num_threads,
                ),
                rule_fsts="",
                max_num_sentences=1,
            )
            return sherpa_onnx.OfflineTts(cfg)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def synthesize(self, text: str, lang: str = "zh") -> np.ndarray | None:
        """合成文本为 16k mono float32 PCM; 任何失败返回 None (调用方静默降级)。

        空文本 / 未知语种 / 模型缺失 / 底层合成异常均走 None 分支,
        本方法不抛异常 (契约: 失败只降级, 绝不阻断字幕流程)。
        """
        if not text or not text.strip():
            return None
        lang = lang.lower()
        if lang not in _CANDIDATE_LANGS:
            return None
        try:
            with self._lock:
                tts = self._tts.get(lang)
                if tts is None and self._ready.get(lang):
                    tts = self._build_tts(lang)
                    self._tts[lang] = tts
                if tts is None:
                    self._ready[lang] = False
                    return None
                result = tts.generate(text, sid=0, speed=1.0)
                if result is None or result.samples is None or len(result.samples) == 0:
                    return None
                return _resample_to_16k(np.asarray(result.samples), int(result.sample_rate))
        except Exception:
            return None

    def health(self) -> str:
        """自检摘要: \"ok\" 或缺陷描述 (诊断页展示用, 不抛异常)。"""
        parts = []
        for lang in _CANDIDATE_LANGS:
            name = "zh" if lang == "zh" else "en"
            if self._ready.get(lang):
                parts.append(f"{name}:模型就绪")
            else:
                parts.append(f"{name}:缺模型({self._lang_dir(lang).name})")
        return "ok" if all(self._ready.values()) else "; ".join(parts)