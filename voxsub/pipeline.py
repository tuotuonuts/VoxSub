"""voxsub.pipeline —— 三模式编排 (M6)。

线程模型 (DESIGN.md「Pipeline 编排设计」):
  [采集线程] audio.read_chunk() 循环 ──queue──▶ [处理线程] segmenter.feed() → asr
      ──on_utterance(原文)──▶ translate ──▶ 订阅回调 (queue 桥接, 推理线程绝不直接碰 UI)

集成安全: 翻译模块 (M4) 未落地时用 _NoopTranslator 占位(原文直通+标记),
Pipeline 全链路不因缺模块崩溃 —— 翻译就绪后由 TranslatorFactory 注入。
"""
from __future__ import annotations

import queue
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from voxsub.audio import AudioSource, LoopbackSource, MicSource, resample_16k
from voxsub.asr import StreamingASR, UtteranceSegmenter, WindowVAD, models_dir, SAMPLE_RATE
from voxsub.logging_setup import get_logger

logger = get_logger("pipeline")


# ---------- 数据模型 ----------

@dataclass
class SubtitleLine:
    """一条字幕: 原文 + 译文 + 相对时间戳(ms, C 模式有效)。"""
    text: str
    translation: str = ""
    ts_ms: int = 0
    is_final: bool = True


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


def _load_translator() -> object:
    """延迟加载翻译层: M4 落地后自动接管, 否则返回占位实现。"""
    try:
        from voxsub.translate.factory import TranslatorFactory  # type: ignore[import-not-found]
        for kind in ("opus-fast", "qwen-quality", "cloud"):
            try:
                return TranslatorFactory.create(kind, None), kind
            except Exception as exc:
                logger.warning("翻译档位 %s 创建失败: %s", kind, exc)
                continue
        logger.warning("无可用的翻译档位, 退回占位实现 (仅原文直通)")
        return _NoopTranslator(), None
    except ImportError as exc:
        logger.debug("翻译层未安装, 用占位实现: %s", exc)
        return _NoopTranslator(), None


# ---------- Pipeline ----------

class Pipeline:
    """三模式实时/离线翻译管线 (契约见 DESIGN.md「Pipeline 契约」)。"""

    def __init__(self, provider: str = "cpu", models: Optional[Path] = None) -> None:
        self._provider = provider
        self._models_dir = Path(models) if models else models_dir()
        self._mode = "a"
        self._running = False
        self._in_path: Optional[Path] = None          # C 模式输入文件
        self._src_lang, self._dst_lang = "zh", "en"   # 默认中→英
        self._tts_enabled = False

        self._queue: queue.Queue = queue.Queue(maxsize=1024)
        self._stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []

        self._cb_utterance: list[Callable[[str, str], None]] = []
        self._cb_status: list[Callable[[str], None]] = []

        # 惰性组件 (首次 start 时构建)
        self._asr = None
        self._vad = None
        self._seg = None
        self._translator = None
        self._trans_kind = None

    # ---- 配置 ----
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode in ("a", "b", "c") and not self._running:
            self._mode = mode

    def set_langs(self, src: str, dst: str) -> None:
        self._src_lang, self._dst_lang = src, dst

    def set_input_file(self, path: str | Path) -> None:
        self._in_path = Path(path)

    def set_tts(self, enabled: bool) -> None:
        self._tts_enabled = enabled

    def is_running(self) -> bool:
        return self._running

    # ---- 回调 (UI 订阅) ----
    def on_utterance(self, cb: Callable[[str, str], None]) -> None:
        self._cb_utterance.append(cb)

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._cb_status.append(cb)

    def _emit_status(self, msg: str) -> None:
        for cb in self._cb_status:
            cb(msg)

    def _emit_utterance(self, text: str, translation: str) -> None:
        for cb in self._cb_utterance:
            cb(text, translation)

    # ---- 组件构造 ----
    def _ensure_translator(self) -> None:
        if self._translator is None:
            self._translator, self._trans_kind = _load_translator()

    def _build_real_time(self) -> None:
        """构建 A/B 模式实时组件 (惰性, 只建一次)。"""
        if self._asr is not None:
            return
        asr_dir = self._models_dir / "asr"
        vad_dir = self._models_dir / "vad"
        self._asr = StreamingASR(asr_dir, provider=self._provider)
        vad_model = next(vad_dir.glob("*.onnx"), None)
        if vad_model is None:
            raise FileNotFoundError(f"缺少 VAD 模型: {vad_dir}")
        self._vad = WindowVAD(str(vad_model))
        self._seg = UtteranceSegmenter(self._asr, self._vad, self._on_sentence)
        self._ensure_translator()

    def _on_sentence(self, text: str) -> None:
        """处理线程回调: 一句话识别完成 → 翻译 → 推送给 UI。"""
        try:
            translation = self._translator.translate(text, self._src_lang, self._dst_lang)
        except Exception as exc:
            logger.error("翻译失败 text=%r: %s", text, exc, exc_info=True)
            translation = text + " 〔翻译失败〕"
            self._emit_status("翻译失败(已保留原文)")
        self._emit_utterance(text, translation)
        # TODO(M5): tts_enabled 时送 TTSEngine 朗读 (tts 模块落地后接入)

    # ---- 启停 ----
    def start(self) -> None:
        if self._running:
            return
        self._stop_evt.clear()
        self._running = True
        self._emit_status("启动中…")
        if self._mode == "c":
            self._threads.append(threading.Thread(
                target=self._run_file_mode, name="pipeline-file", daemon=True))
        else:
            self._build_real_time()
            self._threads.append(threading.Thread(
                target=self._capture_loop, name="pipeline-capture", daemon=True))
            self._threads.append(threading.Thread(
                target=self._process_loop, name="pipeline-process", daemon=True))
        for t in self._threads:
            t.start()
        self._emit_status("运行中")

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_evt.set()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads.clear()
        if self._seg is not None:
            try:
                self._seg.flush()  # 处理尾句
            except Exception as exc:
                logger.error("flush 尾句失败: %s", exc, exc_info=True)
        self._running = False
        self._emit_status("已停止")

    # ---- A/B 模式线程 ----
    def _make_source(self) -> AudioSource:
        if self._mode == "b":
            loopbacks = list_loopbacks()
            if not loopbacks:
                raise RuntimeError("未找到系统声音(loopback)设备")
            return LoopbackSource(device=loopbacks[0].device)
        return MicSource()

    def _capture_loop(self) -> None:
        try:
            source = self._make_source()
        except Exception as exc:
            logger.error("音频设备启动失败: %s", exc, exc_info=True)
            self._emit_status(f"音频设备错误: {exc}")
            self._stop_evt.set()
            return
        source.start()
        try:
            while not self._stop_evt.is_set():
                chunk = source.read_chunk()
                if chunk is None:
                    break
                try:
                    self._queue.put_nowait(chunk)
                except queue.Full:
                    pass  # 处理线程积压时丢块保实时, 不阻塞采集
        finally:
            source.stop()
            source.close()

    def _process_loop(self) -> None:
        try:
            seg = self._seg
            while not self._stop_evt.is_set():
                try:
                    chunk = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                seg.feed(chunk)
        finally:
            # 采集结束时由 stop() 统一 flush
            pass

    # ---- C 模式 (文件 → 双语字幕) ----
    def _run_file_mode(self) -> None:
        if self._in_path is None or not self._in_path.exists():
            self._emit_status("文件不存在")
            self._running = False
            return
        try:
            lines, wav_path = self._transcribe_file(self._in_path)
            self._ensure_translator()
            out = self._in_path.with_suffix(".srt")
            self.write_srt(lines, out)
            self._emit_status(f"完成 → {out}")
            self._emit_utterance(f"已导出 {len(lines)} 条字幕", str(out))
        except Exception as exc:
            logger.error("文件处理失败 path=%s: %s", self._in_path, exc, exc_info=True)
            self._emit_status(f"文件处理失败: {exc}")
        finally:
            self._running = False

    def _transcribe_file(self, path: Path) -> tuple[list[SubtitleLine], Optional[Path]]:
        """对文件离线识别: 支持 .wav 直读(采样率自动转 16k), 其他格式需 ffmpeg。

        返回 (字幕行列表, 中间 wav 路径)。分句按 VAD, 时间戳按样本计数精确。
        """
        import shutil

        wav_path: Optional[Path] = None
        if path.suffix.lower() == ".wav":
            pcm, sr = self._read_wav(path)
        else:
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg is None:
                raise RuntimeError("非 wav 格式需要 ffmpeg (PATH 中未找到)")
            wav_path = path.with_suffix(".voxsub_tmp.wav")
            import subprocess
            subprocess.run([ffmpeg, "-y", "-i", str(path), "-ar", "16000",
                            "-ac", "1", "-f", "wav", str(wav_path)],
                           check=True, capture_output=True)
            pcm, sr = self._read_wav(wav_path)

        if sr != SAMPLE_RATE:
            pcm = resample_16k(pcm, sr)
        return self._recognize_streaming(pcm), wav_path

    @staticmethod
    def _read_wav(path: Path) -> tuple[np.ndarray, int]:
        """读 16-bit PCM wav 为 float32 mono + 采样率。"""
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            n_ch, width = w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if n_ch > 1:
            data = data.reshape(-1, n_ch).mean(axis=1)  # 混音到单声道
        return data, sr

    def _recognize_streaming(self, pcm: np.ndarray) -> list[SubtitleLine]:
        """整段流式识别 + VAD 分句 + 时间戳(样本计数)。"""
        self._build_real_time()
        vad, asr = self._vad, self._asr

        lines: list[SubtitleLine] = []
        stream = asr.create_stream()
        seg_start_sample: Optional[int] = None
        silence = 0
        min_silence = int(SAMPLE_RATE * 0.5)
        win = vad.window_size

        for i in range(0, pcm.size - win + 1, win):
            chunk = pcm[i:i + win]
            if vad.is_speech(chunk):
                if seg_start_sample is None:
                    seg_start_sample = i
                silence = 0
                asr.feed(stream, chunk)
            elif seg_start_sample is not None:
                asr.feed(stream, chunk)
                silence += win
                if silence >= min_silence:
                    text = asr.decode(stream).strip()
                    if text:
                        lines.append(SubtitleLine(text=text, ts_ms=int(seg_start_sample * 1000 / SAMPLE_RATE)))
                    asr.reset(stream)
                    seg_start_sample = None
                    silence = 0
                    vad.reset()
        # 尾段
        if seg_start_sample is not None:
            text = asr.decode(stream).strip()
            if text:
                lines.append(SubtitleLine(text=text, ts_ms=int(seg_start_sample * 1000 / SAMPLE_RATE)))

        # 批量翻译 (stub 未就绪时原文直通, 不阻塞导出)
        self._ensure_translator()
        for ln in lines:
            try:
                ln.translation = self._translator.translate(ln.text, self._src_lang, self._dst_lang)
            except Exception:
                ln.translation = ln.text + " 〔翻译失败〕"
        return lines

    # ---- srt / vtt / txt 导出 (模块级函数, 便于单测) ----
    @staticmethod
    def _fmt_ts(ms: int) -> str:
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms2 = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"

    @classmethod
    def write_srt(cls, lines: list[SubtitleLine], out: Path, dur_ms: int = 1500) -> None:
        """写 srt: 序号 + 时间轴(每句固定 1.5s 展示) + 双语两行。"""
        body = []
        for idx, ln in enumerate(lines, start=1):
            # 每条展示 dur_ms; 末句放宽到 3s, 便于阅读
            end = ln.ts_ms + (3000 if idx == len(lines) else dur_ms)
            body.append(f"{idx}\n{cls._fmt_ts(ln.ts_ms)} --> {cls._fmt_ts(end)}\n"
                        f"{ln.text}\n{ln.translation}\n")
        out.write_text("\n".join(body), encoding="utf-8-sig")

    @classmethod
    def write_vtt(cls, lines: list[SubtitleLine], out: Path) -> None:
        """写 vtt (WebVTT)。"""
        body = ["WEBVTT\n"]
        for idx, ln in enumerate(lines, start=1):
            body.append(f"{cls._fmt_ts(ln.ts_ms).replace(',', '.')} --> "
                        f"{cls._fmt_ts(ln.ts_ms + 3000).replace(',', '.')}\n"
                        f"{ln.text}\n{ln.translation}\n")
        out.write_text("\n".join(body), encoding="utf-8")

    @classmethod
    def write_txt(cls, lines: list[SubtitleLine], out: Path) -> None:
        """写纯文本: 原文 ⇄ 译文, tab 分隔。"""
        out.write_text("\n".join(f"{ln.text}\t{ln.translation}" for ln in lines),
                       encoding="utf-8")