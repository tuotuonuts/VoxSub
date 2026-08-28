"""voxsub.translate.opus —— 快档翻译器: Xenova OPUS-MT int8 + ORT 手写 seq2seq。

方案决策 (2026-08-17):
- Xenova 导出模型 tokenizer.json 内嵌 **Unigram**(sentencepiece) 词表,
  纯手写 Viterbi 分词 (见 tokenizer.py), 无需 transformers/sentencepiece 依赖。
- decoder 的 ONNX 导出只暴露 ``present.*`` 输出、**无对应 past.* 输入**
  (无法喂回 KV cache), 故退化为"整序列逐 token 增长"的 greedy 解码:
  每步把已生成的 decoder 序列整体喂入, 取尾部 logits 贪心采样。
  对 OPUS-MT (d=512, 6 层) 输出 ~20-30 token 已足够快。
- 一次性推理直接缺省, 不接 KV cache 引入的复杂度。

模型约定 (DESIGN.md): %LOCALAPPDATA%/VoxSub/models/nmt/<opus_xx_yy>/ 下
  encoder_model_int8.onnx / decoder_model_int8.onnx / config.json / tokenizer.json
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort

from voxsub.logging_setup import get_logger
from voxsub.language_guard import detect_text_language, normalize_language
from voxsub.model_storage import model_lookup_roots, resolve_models_root

from .base import TranslationError, Translator
from .tokenizer import UnigramTokenizer

logger = get_logger("translate.opus")


def _default_models_dir() -> Path:
    return resolve_models_root()


class OpusFastTranslator(Translator):
    """快档: ONNX Runtime 手写贪心 seq2seq 跑 Xenova OPUS-MT int8。

    目标 <0.5s/句 (典型短句)。两个方向 (zh→en / en→zh) 各自独立子目录,
    通过 dir 映射切换。加载是惰性的 (首次 translate 才建会话)。
    """

    name = "opus-fast"
    langs = ("zh", "en")
    local = True

    def __init__(self, model_dir: Path | None = None,
                 opus_map: dict[tuple[str, str], str] | None = None,
                 max_length: int = 128, threads: int = 2,
                 providers: list | None = None):
        # ``nmt`` was the pre-0.4.1 location.  Startup normally moves it to
        # ``translate/opus``; retaining the fallback means a power loss during
        # that move cannot make the built-in fast translator disappear.
        if model_dir is not None:
            self._bases = (Path(model_dir),)
        else:
            # Keep both the active root and any old per-user root visible
            # while an upgrade is being completed. The active root remains
            # the only destination for new downloads; this is lookup-only.
            self._bases = tuple(
                base
                for root in model_lookup_roots(_default_models_dir())
                for base in (root / "translate" / "opus", root / "nmt")
            )
        self._max_length = max_length
        self._threads = threads
        self._providers = providers or ["CPUExecutionProvider"]
        # 语言对 → 模型子目录名
        self._opus_map = opus_map or {
            ("zh", "en"): "opus_zh_en",
            ("en", "zh"): "opus_en_zh",
        }
        self._lock = threading.Lock()          # 保护 _states (session 非线程安全)
        self._states: dict[tuple[str, str], "_OpusModel"] = {}
        self._dir_by_pair: dict[tuple[str, str], Path] = {}
        self._ready_pairs: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    def _locate(self, pair) -> Path | None:
        """按 (src,dst) 定位可用的模型目录 (文件齐全才算就绪)。"""
        if pair in self._dir_by_pair:
            return self._dir_by_pair[pair]
        d = self._opus_map.get(pair)
        if not d:
            return None
        need = ["encoder_model_int8.onnx", "decoder_model_int8.onnx",
                "config.json", "tokenizer.json"]
        for base in self._bases:
            p = base / d
            if all((p / f).exists() for f in need):
                self._dir_by_pair[pair] = p
                self._ready_pairs.append(pair)
                return p
        return None

    def list_available_pairs(self) -> list[tuple[str, str]]:
        """探测已就绪的语言对 (不加载模型)。"""
        for pair in self._opus_map:
            self._locate(pair)
        return list(self._ready_pairs)

    def _model_for(self, pair) -> "_OpusModel":
        with self._lock:
            m = self._states.get(pair)
            if m is None:
                d = self._locate(pair)
                if d is None:
                    raise TranslationError(f"快档模型未就绪: {pair} -> {d}")
                m = _OpusModel(d, max_length=self._max_length, threads=self._threads,
                               providers=self._providers)
                self._states[pair] = m
            return m

    # ------------------------------------------------------------------
    def translate(self, text: str, src_lang: str, dst_lang: str, *,
                  timeout_ms: int = 15000) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        src_lang = normalize_language(src_lang)
        dst_lang = normalize_language(dst_lang)
        if dst_lang == "auto":
            raise TranslationError("快档目标语言必须明确，不能使用 auto")
        if src_lang == "auto":
            detected = detect_text_language(text)
            if detected == "auto":
                raise TranslationError(
                    "快档无法从文本判断源语言；请改用质量档或云档以支持更多语言")
            src_lang = detected
        if src_lang == dst_lang:
            return text
        pair = (src_lang, dst_lang)
        if pair not in self._opus_map:
            raise TranslationError(f"快档不支持语言对 {pair} (支持: {list(self._opus_map)})")
        try:
            return self._model_for(pair).translate_str(text, timeout_ms=timeout_ms)
        except TranslationError:
            raise
        except Exception as exc:  # ORT/形状/IO 异常一律包装为翻译失败
            logger.exception("快档翻译推理异常, 包装为 TranslationError (pair=%s)", pair)
            raise TranslationError(f"opus 推理失败: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._states.clear()   # 释放 ORT session (onnxruntime 无显式 close)

    def health(self) -> str:
        ready = self.list_available_pairs()
        if not ready:
            missing = [f"{s}->{d}" for s, d in self._opus_map if self._locate((s, d)) is None]
            return f"缺少快档模型: {missing} (models/nmt/ 下按 opus_zh_en/opus_en_zh 放置)"
        return "ok"


class _OpusModel:
    """单个 OPUS-MT 方向的会话封装 (ORT session + tokenizer 一次性就绪)。"""

    def __init__(self, model_dir: Path, max_length: int = 128, threads: int = 2,
                 providers: list | None = None):
        self.dir = Path(model_dir)
        self._max_length = max_length
        try:
            self._tok = UnigramTokenizer.from_file(self.dir / "tokenizer.json")
            cfg = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
            self._eos = cfg.get("eos_token_id", 0)
            self._pad = cfg.get("pad_token_id", 65000)
            self._decoder_start = cfg.get("decoder_start_token_id", 65000)
            self._max_position = cfg.get("max_position_embeddings", 512)
            selected = providers or ["CPUExecutionProvider"]
            raw_names = [item[0] if isinstance(item, tuple) else item for item in selected]

            def create_sessions(chosen: list):
                so = ort.SessionOptions()
                so.intra_op_num_threads = threads
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                chosen_names = [item[0] if isinstance(item, tuple) else item
                                for item in chosen]
                if "DmlExecutionProvider" in chosen_names:
                    so.enable_mem_pattern = False
                    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                encoder = ort.InferenceSession(
                    str(self.dir / "encoder_model_int8.onnx"),
                    sess_options=so, providers=chosen)
                decoder = ort.InferenceSession(
                    str(self.dir / "decoder_model_int8.onnx"),
                    sess_options=so, providers=chosen)
                return encoder, decoder

            try:
                self._enc, self._dec = create_sessions(selected)
            except Exception:
                if raw_names == ["CPUExecutionProvider"]:
                    raise
                logger.warning("OPUS 加速后端加载失败，回退 CPU: providers=%s",
                               raw_names, exc_info=True)
                selected = ["CPUExecutionProvider"]
                raw_names = selected
                self._enc, self._dec = create_sessions(selected)
            logger.info("OPUS 模型加载: dir=%s providers=%s", self.dir, raw_names)
        except Exception:
            # 模型加载失败: 记录根因后原样抛出, 由上层包装为 TranslationError
            logger.exception("快档模型加载失败 (model_dir=%s)", self.dir)
            raise

    # ------------------------------------------------------------------
    def translate_str(self, text: str, timeout_ms: int = 15000) -> str:
        input_ids = np.expand_dims(np.array(self._tok.encode(text), dtype=np.int64), 0)
        enc_len = input_ids.shape[1]
        if enc_len > self._max_position:
            input_ids = input_ids[:, :self._max_position]
            enc_len = self._max_position
        attention_mask = np.ones((1, enc_len), dtype=np.int64)
        enc_out = self._enc.run(None, {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        })[0]                                   # (1, enc_len, 512)

        decoder_ids = [self._decoder_start]
        out_tokens: list[int] = []
        for _ in range(self._max_length):
            dec_ids = np.expand_dims(np.array(decoder_ids, dtype=np.int64), 0)
            logits = self._dec.run(None, {
                "encoder_attention_mask": attention_mask,
                "input_ids": dec_ids,
                "encoder_hidden_states": enc_out,
            })[0]                                # (1, dec_len, vocab)
            next_logit = logits[0, -1, :]
            nxt = int(np.argmax(next_logit))
            if nxt == self._eos:
                break
            out_tokens.append(nxt)
            decoder_ids.append(nxt)
            if len(out_tokens) >= self._max_length:
                break
        return self._tok.decode(out_tokens)
