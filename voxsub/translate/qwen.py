"""VoxSub GGUF quality translator (compatibility module name: qwen).

技术路线:
- 使用 llama.cpp 官方预编译 llama-server；模型广场当前质量档为 Hy-MT2。
- 后端严格按独显 GPU -> Intel NPU -> 核显 -> CPU 选择；只有对应
  Vulkan/CUDA/HIP/OpenVINO/SYCL 运行时实际存在时才启用。
- 本类是 HTTP 客户端: lazy spawn llama-server 子进程, 调用其
  OpenAI 兼容 /v1/chat/completions 端点, 解析 choices[0].message.content。

进程管理:
- lazy 启动: 首次 translate 时 spawn; 每次从非热门动态端口区随机选择端口。
- close(): terminate 子进程 (幂等)。
- 启动失败 (exe 缺失 / 随机端口竞争 / 起不来) → 抛清晰 TranslationError。
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from voxsub.logging_setup import get_logger
from voxsub.hardware import (
    HardwareProfile,
    LlamaRuntime,
    detect_hardware,
    select_llama_runtime,
)
from voxsub.language_guard import normalize_language
from voxsub.model_storage import resolve_models_root

from ._http_client import OpenAICompatError, chat_completion
from .base import TranslationError, Translator, parse_translation_batch
from .llama_launch import build_llama_launch_plan

logger = get_logger("translate.qwen")

_DYNAMIC_PORT_MIN = 49_152
_DYNAMIC_PORT_MAX = 65_535
_POPULAR_PORTS = frozenset({
    80, 443, 3000, 5000, 8000, 8080, 8081, 8088, 8888, 9000,
})


def _default_models_dir() -> Path:
    return resolve_models_root()


def _default_tools_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "tools" / "llama"


#: 语言对 → 人类可读名称。质量档必须使用 chat system role 约束输出；旧版只有
#: ``Translate to ...`` 一句用户提示，1.5B 模型很容易追加说明和自我评价。
_LANG_NAMES = {
    ("zh", "en"): ("Chinese", "English"),
    ("en", "zh"): ("English", "Chinese"),
}

_SYSTEM_PROMPT = (
    "You are a professional machine-translation engine. Translate faithfully and "
    "concisely. Return only the translated text: no labels, no quotation marks, no "
    "explanation, no notes, and no discussion of the source. Preserve names and numbers. "
    "Never switch to a language that was not requested."
)


class QwenQualityTranslator(Translator):
    """质量档: 通过 llama-server 子进程运行所选 GGUF 翻译模型。"""

    name = "qwen-quality"
    langs = ("zh", "en")
    local = True

    def __init__(self, model_path: Path | str | None = None,
                 server_exe: Path | str | None = None,
                 n_ctx: int = 2048, n_threads: int = 4,
                 max_tokens: int = 128, fast_mode: bool = True,
                 port: int = 8080, prompt_style: str = "qwen",
                 model_name: str = "本地 GGUF 翻译模型",
                 n_gpu_layers: int | None = None):
        self._model_path = Path(model_path) if model_path else (
            _default_models_dir() / "translate" / "legacy-llm" /
            "qwen2.5-1.5b-instruct-q4_k_m.gguf")
        self._explicit_server_exe = Path(server_exe) if server_exe else None
        self._server_exe = (self._explicit_server_exe or
                            (_default_tools_dir() / "llama-server.exe"))
        self._runtime: LlamaRuntime | None = None
        self._n_ctx = n_ctx
        self._n_threads = n_threads
        self._max_tokens = max_tokens
        self._fast_mode = fast_mode
        self._prompt_style = prompt_style
        self._model_name = model_name
        self._n_gpu_layers = n_gpu_layers
        self._start_port = port
        self._proc: subprocess.Popen | None = None
        self._port: int | None = None
        self._lock = threading.Lock()
        self._endpoint: str | None = None
        # A backend that failed to start must not be retried for every subtitle.
        # The blacklist is scoped to this translator/model instance.
        self._failed_runtimes: set[tuple[str, str]] = set()
        self._server_output_tail: deque[str] = deque(maxlen=80)

    # ------------------------------------------------------------------
    def _pick_free_port(self) -> int:
        # Do not make local translation depend on a small, predictable port
        # range. Browser tools, dev servers, or an orphaned llama-server often
        # occupy 8080. Random dynamic ports also reduce collisions between
        # parallel VoxSub test/app instances.
        span = _DYNAMIC_PORT_MAX - _DYNAMIC_PORT_MIN + 1
        for _ in range(64):
            port = _DYNAMIC_PORT_MIN + secrets.randbelow(span)
            if port in _POPULAR_PORTS:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        # If random candidates all lost a race, let the OS select one from its
        # dynamic range as a final fallback.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", 0))
                port = int(s.getsockname()[1])
            except OSError as exc:
                raise TranslationError("无法为 llama-server 分配本地端口") from exc
        if port in _POPULAR_PORTS or not (_DYNAMIC_PORT_MIN <= port <= _DYNAMIC_PORT_MAX):
            raise TranslationError(f"系统分配了不适合的本地端口: {port}")
        return port

    @staticmethod
    def _runtime_key(runtime: LlamaRuntime | None) -> tuple[str, str] | None:
        if runtime is None:
            return None
        return runtime.backend, runtime.target or "CPU"

    def _drain_server_output(self, proc: subprocess.Popen) -> None:
        """Drain llama-server output so a verbose crash cannot block the pipe."""
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return
        try:
            for line in stream:
                line = str(line).strip()
                if line:
                    self._server_output_tail.append(line[-1000:])
                    lower = line.casefold()
                    if ("openvino" in lower or "npu" in lower or
                            "fallback" in lower or "device" in lower):
                        logger.info("llama-server: %s", line[-1200:])
                    else:
                        logger.debug("llama-server: %s", line[-1200:])
        except Exception:
            logger.debug("读取 llama-server 输出失败", exc_info=True)

    def _clear_server_state(self) -> tuple[subprocess.Popen | None, int | None]:
        proc, self._proc = self._proc, None
        port = self._port
        self._endpoint = None
        self._port = None
        return proc, port

    @staticmethod
    def _terminate_process(proc: subprocess.Popen | None, port: int | None) -> None:
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("llama-server 5s 内未正常退出, 强制 kill (pid=%s)",
                               getattr(proc, "pid", "?"))
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            logger.exception("关闭 llama-server 时异常 (pid=%s, port=%s)",
                             getattr(proc, "pid", "?"), port)

    def _spawn(self) -> None:
        """Start the server, retrying only when the selected port raced."""
        for attempt in range(4):
            try:
                self._spawn_once()
                return
            except TranslationError as exc:
                detail = " ".join(self._server_output_tail)
                haystack = f"{exc} {detail}".casefold()
                port_conflict = any(marker in haystack for marker in (
                    "address already in use",
                    "only one usage",
                    "failed to bind",
                    "cannot bind",
                    "bind failed",
                    "端口",
                ))
                if not port_conflict or attempt >= 3:
                    raise
                logger.warning(
                    "llama-server 端口竞争，重新随机端口重试 (%s/3): %s",
                    attempt + 1, exc,
                )

    def _select_runtime(self) -> tuple[HardwareProfile, LlamaRuntime | None]:
        if self._model_path is None or not self._model_path.exists():
            logger.warning("质量档模型缺失, 拒绝 spawn: %s (请用 scripts/model_fetch.py 下载)",
                           self._model_path)
            raise TranslationError(
                f"质量档模型缺失: {self._model_path} (请用 scripts/model_fetch.py 下载)")
        profile = detect_hardware()
        required_gb = self._model_path.stat().st_size / (1024 ** 3) * 1.18 + 0.5
        runtime = select_llama_runtime(
            profile, self._explicit_server_exe, required_gb=required_gb,
            excluded=self._failed_runtimes)
        if runtime is not None:
            self._runtime = runtime
            self._server_exe = runtime.server_exe
        elif self._explicit_server_exe is None:
            self._runtime = None
        if not self._server_exe.exists():
            logger.warning("llama-server 缺失, 拒绝 spawn: %s (应含配套 DLL)",
                           self._server_exe)
            raise TranslationError(
                f"llama-server 缺失: {self._server_exe} (应含配套 DLL, 见 tools/llama/)")
        return profile, runtime

    def _start_server_process(self, cmd: list[str], child_env: dict[str, str]) -> None:
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env)
        except OSError as exc:
            logger.exception("llama-server 进程启动失败 (exe=%s)", self._server_exe)
            raise TranslationError(f"llama-server 启动失败: {exc}") from exc

    def _activate_server(self, port: int, runtime: LlamaRuntime | None,
                         gpu_layers: int, effective_ctx: int) -> None:
        if self._proc is None:
            raise TranslationError("llama-server 进程创建后未返回句柄")
        self._port = port
        self._endpoint = f"http://127.0.0.1:{port}/v1/chat/completions"
        self._server_output_tail.clear()
        threading.Thread(target=self._drain_server_output, args=(self._proc,),
                         name="llama-server-log", daemon=True).start()
        logger.info("llama-server 进程已创建，等待运行时验证 "
                    "(model=%s backend=%s target=%s port=%s pid=%s "
                    "gpu_layers=%s ctx=%s reason=%s)", self._model_name,
                    runtime.backend if runtime else "cpu",
                    runtime.target if runtime else "CPU", port, self._proc.pid,
                    gpu_layers, effective_ctx,
                    runtime.selection_reason if runtime else "CPU fallback")

    def _validate_server_startup(self, port: int,
                                 runtime: LlamaRuntime | None) -> None:
        try:
            startup_timeout = 600.0 if (
                runtime is not None and
                runtime.backend == "openvino" and
                runtime.target == "NPU"
            ) else 60.0
            self._wait_ready(
                port,
                timeout=startup_timeout,
                backend=runtime.backend if runtime else "cpu",
                target=runtime.target if runtime else "CPU",
            )
            if (runtime is not None and runtime.backend == "openvino" and
                    runtime.target == "NPU"):
                self._probe_runtime_inference(timeout_sec=180.0)
            logger.info(
                "llama-server 运行时验证通过 "
                "(model=%s backend=%s target=%s health=ok inference=%s)",
                self._model_name,
                runtime.backend if runtime else "cpu",
                runtime.target if runtime else "CPU",
                "ok" if runtime and runtime.target == "NPU" else "deferred",
            )
        except Exception:
            proc, failed_port = self._clear_server_state()
            self._terminate_process(proc, failed_port)
            raise

    def _spawn_once(self) -> None:
        profile, runtime = self._select_runtime()
        port = self._pick_free_port()
        gpu_layers = (self._n_gpu_layers if self._n_gpu_layers is not None else
                      self._auto_gpu_layers(profile, runtime))
        plan = build_llama_launch_plan(
            server_exe=self._server_exe,
            model_path=self._model_path,
            port=port,
            context_size=self._n_ctx,
            threads=self._n_threads,
            gpu_layers=gpu_layers,
            runtime=runtime,
        )
        self._start_server_process(list(plan.command), plan.environment)
        self._activate_server(port, runtime, plan.gpu_layers, plan.context_size)
        self._validate_server_startup(port, runtime)

    def _probe_runtime_inference(self, timeout_sec: float = 180.0) -> None:
        """Require one real completion before accepting a candidate NPU route."""
        endpoint = self._endpoint
        if endpoint is None:
            raise TranslationError("NPU 推理探针缺少 llama-server endpoint")
        started = time.perf_counter()
        try:
            result = chat_completion(
                endpoint,
                messages=[{
                    "role": "user",
                    "content": (
                        "Translate the following text into English. Only output the "
                        "translated result and do not add explanations:\n你好"
                    ),
                }],
                temperature=0.0,
                max_tokens=8,
                timeout_sec=timeout_sec,
            )
        except OpenAICompatError as exc:
            logger.error("NPU 真实推理探针失败: %s", exc)
            raise TranslationError(f"NPU 真实推理探针失败: {exc}") from exc
        if not str(result or "").strip():
            logger.error("NPU 真实推理探针返回空结果")
            raise TranslationError("NPU 真实推理探针返回空结果")
        logger.info("NPU 真实推理探针通过: elapsed_ms=%.1f output_chars=%d",
                    (time.perf_counter() - started) * 1000.0, len(str(result)))

    def _auto_gpu_layers(self, profile, runtime: LlamaRuntime | None) -> int:
        """Offload only when a matching backend exists and memory is sufficient."""
        if runtime is None or runtime.backend == "cpu":
            return 0
        if runtime.backend == "openvino" and runtime.target == "NPU":
            return 999
        try:
            required_gb = self._model_path.stat().st_size / (1024 ** 3) * 1.18 + 0.5
            if runtime.target == "GPU" and not profile.has_discrete_gpu:
                return 999 if profile.ram_gb >= required_gb + 4.0 else 0
            if profile.has_discrete_gpu and profile.vram_gb >= required_gb:
                return 999
        except Exception:
            logger.debug("加速器内存评估失败，回落 CPU layers", exc_info=True)
        return 0

    def _wait_ready(self, port: int, timeout: float = 60.0, *,
                    backend: str = "cpu", target: str = "CPU") -> None:
        """轮询健康端点直到可用; 进程提前退出则报错。"""
        probe = f"http://127.0.0.1:{port}/health"
        started = time.monotonic()
        deadline = started + timeout
        next_progress_log = 30.0
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                detail = " | ".join(self._server_output_tail)
                logger.error(
                    "llama-server 进程提前退出 "
                    "(backend=%s target=%s port=%s 退出码=%s)",
                    backend, target, port, self._proc.returncode)
                if detail:
                    logger.error("llama-server 最近输出: %s", detail)
                raise TranslationError(
                    f"llama-server 进程提前退出, 退出码={self._proc.returncode}")
            try:
                import urllib.request as u
                with u.urlopen(probe, timeout=1.0) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            elapsed = time.monotonic() - started
            if elapsed >= next_progress_log:
                logger.info(
                    "llama-server 仍在初始化 "
                    "(backend=%s target=%s port=%s 已等待=%.0fs 最长等待=%.0fs)",
                    backend, target, port, elapsed, timeout)
                while next_progress_log <= elapsed:
                    next_progress_log += 30.0
            time.sleep(0.3)
        logger.error(
            "llama-server %.0fs 内未就绪 (backend=%s target=%s port=%s)",
            timeout, backend, target, port)
        detail = " | ".join(self._server_output_tail)
        if detail:
            logger.error("llama-server 最近输出: %s", detail)
        raise TranslationError(f"llama-server {timeout:.0f}s 内未就绪 (port {port})")

    def _ensure(self) -> str:
        """保证 llama-server 就绪并返回 endpoint。

        并发安全 (double-checked locking): 检查在锁外做(快速路径), 但**重新检查**
        在锁内做——两个线程并发首次调用时, 只有第一个真正 spawn, 第二个
        看到 endpoint 已就绪直接复用, 避免双开 llama-server 导致孤儿进程
        (每孤儿 ~1.5GB 模型驻留, 且耗尽 8080-8089 端口范围)。
        """
        if self._endpoint is None or self._proc is None or self._proc.poll() is not None:
            # close() 内部也拿 self._lock (threading.Lock 非可重入),
            # 故必须在锁外调用, 否则首次冷启动死锁 (2026-08-17 冒烟实测卡死)
            self.close()
            with self._lock:
                # 锁内二次检查: 并发发起方可能已在等待时完成 spawn
                if self._endpoint is None or self._proc is None or self._proc.poll() is not None:
                    last_error: TranslationError | None = None
                    while True:
                        try:
                            self._spawn()
                            break
                        except TranslationError as exc:
                            last_error = exc
                            key = self._runtime_key(self._runtime)
                            if (self._explicit_server_exe is not None or
                                    key is None or key[0] == "cpu"):
                                raise
                            self._failed_runtimes.add(key)
                            logger.warning(
                                "llama 运行时启动失败，切换下一后端: backend=%s target=%s error=%s",
                                key[0], key[1], exc)
                            self._runtime = None
                    if last_error is not None and self._endpoint is None:
                        raise last_error
                else:
                    logger.debug("_ensure 竞态收敛: 并发线程已先行完成 spawn, 复用 endpoint")
        # 若 _spawn 抛错, 此处不会到达; endpoint 由 _spawn 赋值
        endpoint = self._endpoint
        assert endpoint is not None
        return endpoint

    # ------------------------------------------------------------------
    def translate(self, text: str, src_lang: str, dst_lang: str, *,
                  timeout_ms: int = 15000) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        src_lang = normalize_language(src_lang)
        dst_lang = normalize_language(dst_lang)
        names = _LANG_NAMES.get((src_lang, dst_lang))
        if names is None:
            raise TranslationError(f"质量档不支持语言对 {(src_lang, dst_lang)}")
        last_error: OpenAICompatError | None = None
        for _backend_attempt in range(4):
            endpoint = self._ensure()
            try:
                with self._lock:
                    out = self._request_translation(endpoint, text, names, timeout_ms)
                    cleaned = _clean(out)
                    if _invalid_translation(text, cleaned, src_lang, dst_lang):
                        logger.warning(
                            "质量档输出越界，使用更严格提示重试: src_chars=%d out_chars=%d",
                            len(text), len(cleaned))
                        out = self._request_translation(
                            endpoint, text, names, timeout_ms, retry=True)
                        cleaned = _clean(out)
            except OpenAICompatError as exc:
                last_error = exc
                key = self._runtime_key(self._runtime)
                can_fallback = (
                    self._explicit_server_exe is None and key is not None
                    and key[0] != "cpu"
                )
                if not can_fallback:
                    raise TranslationError(f"本地翻译引擎调用失败: {exc}") from exc
                self._failed_runtimes.add(key)
                logger.warning(
                    "翻译请求期间加速后端失败，同一句立即降级重试: "
                    "backend=%s target=%s error=%s",
                    key[0], key[1], exc,
                )
                self.close()
                continue
            if _invalid_translation(text, cleaned, src_lang, dst_lang):
                raise TranslationError("质量档返回了说明性或异常长度内容，已拒绝显示")
            return cleaned
        raise TranslationError(f"本地翻译引擎所有后端均失败: {last_error}")

    def translate_many(
        self, texts: list[str], src_lang: str, dst_lang: str, *,
        timeout_ms: int = 15000,
    ) -> list[str]:
        """Translate one OCR batch in a single llama request.

        OCR preserves one geometry box per source line, so the response uses a
        strict JSON array contract. A malformed batch falls back to the proven
        single-line path instead of putting translations on the wrong boxes.
        """
        sources = [str(text or "").strip() for text in texts]
        if not sources:
            return []
        if len(sources) == 1:
            return [self.translate(
                sources[0], src_lang, dst_lang, timeout_ms=timeout_ms)]
        src_lang = normalize_language(src_lang)
        dst_lang = normalize_language(dst_lang)
        names = _LANG_NAMES.get((src_lang, dst_lang))
        if names is None:
            raise TranslationError(f"质量档不支持语言对 {(src_lang, dst_lang)}")
        last_error: OpenAICompatError | None = None
        for _backend_attempt in range(4):
            endpoint = self._ensure()
            try:
                with self._lock:
                    raw = self._request_translation_batch(
                        endpoint, sources, names, timeout_ms)
                translated = parse_translation_batch(raw, len(sources))
            except OpenAICompatError as exc:
                last_error = exc
                key = self._runtime_key(self._runtime)
                can_fallback = (
                    self._explicit_server_exe is None and key is not None
                    and key[0] != "cpu"
                )
                if not can_fallback:
                    raise TranslationError(
                        f"本地批量翻译引擎调用失败: {exc}") from exc
                self._failed_runtimes.add(key)
                logger.warning(
                    "OCR 批量翻译期间加速后端失败，立即降级重试: "
                    "backend=%s target=%s error=%s",
                    key[0], key[1], exc,
                )
                self.close()
                continue
            except TranslationError as exc:
                logger.warning(
                    "OCR 批量翻译格式无效，回退逐行: lines=%d error=%s",
                    len(sources), exc,
                )
                return [self.translate(
                    source, src_lang, dst_lang, timeout_ms=timeout_ms)
                    for source in sources]
            if any(
                _invalid_translation(source, target, src_lang, dst_lang)
                for source, target in zip(sources, translated)
            ):
                logger.warning(
                    "OCR 批量译文存在越界项，回退逐行: lines=%d", len(sources))
                return [self.translate(
                    source, src_lang, dst_lang, timeout_ms=timeout_ms)
                    for source in sources]
            return translated
        raise TranslationError(f"本地批量翻译所有后端均失败: {last_error}")

    def _request_translation_batch(
        self, endpoint: str, texts: list[str], names: tuple[str, str],
        timeout_ms: int,
    ) -> str:
        src_name, dst_name = names
        payload = json.dumps(texts, ensure_ascii=False, separators=(",", ":"))
        instruction = (
            f"The JSON items are neighboring OCR lines from one screen, ordered "
            f"top-to-bottom, and are written in {src_name}. Use adjacent items as "
            "context to resolve wording and conservatively repair only obvious OCR "
            f"character mistakes. Translate every item only into {dst_name}. "
            "Keep one output item for each input item. Return only one valid JSON array "
            f"with exactly {len(texts)} translated strings in the same order. "
            "Do not add explanations, labels, or change the array length.\n"
            f"{payload}"
        )
        if self._prompt_style == "hy-mt2":
            messages = [{"role": "user", "content": instruction}]
        else:
            messages = [
                {"role": "system", "content": (
                    "You are a machine-translation engine. Return only the valid "
                    "JSON array requested by the user, with no surrounding prose.")},
                {"role": "user", "content": instruction},
            ]
        output_budget = min(
            768,
            max(96, sum(max(4, len(text) * 2) for text in texts) + 32),
        )
        return chat_completion(
            endpoint,
            messages=messages,
            temperature=0.1,
            max_tokens=output_budget,
            timeout_sec=timeout_ms / 1000.0,
        )

    def _request_translation(self, endpoint: str, text: str,
                             names: tuple[str, str], timeout_ms: int,
                             retry: bool = False) -> str:
        src_name, dst_name = names
        if self._prompt_style == "hy-mt2":
            instruction = (
                f"The source text is written in {src_name}. Translate it only into {dst_name}. "
                "Do not identify, rewrite, or translate it into any other language. "
                "Only output the translated result and do not add explanations:\n"
                f"{text}"
            )
            messages = [{"role": "user", "content": instruction}]
        else:
            instruction = (
                f"Translate the text between <source> tags from {src_name} to {dst_name} only. "
                "The source language is fixed; do not use another source or target language. "
                "Output the translation only.\n<source>\n"
                f"{text}\n</source>"
            )
            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": instruction},
            ]
        if retry:
            instruction += ("\nIMPORTANT: Your entire response must be only the translated "
                            "sentence. Do not say 'translation' or explain anything.")
            messages[-1]["content"] = instruction
        return chat_completion(
            endpoint,
            messages=messages,
            temperature=0.2 if self._prompt_style == "hy-mt2" else 0.0,
            max_tokens=min(self._max_tokens, max(32, len(text) * 2)),
            timeout_sec=timeout_ms / 1000.0,
        )

    def close(self) -> None:
        with self._lock:
            proc, port = self._clear_server_state()
        if proc is not None:
            logger.info("关闭质量档 llama-server (pid=%s, port=%s)", proc.pid, port)
            self._terminate_process(proc, port)

    def warmup(self) -> None:
        """Start and health-check the local server before the first sentence."""
        started = time.perf_counter()
        try:
            self._ensure()
        except Exception:
            logger.exception("质量档翻译引擎预热失败")
            return
        logger.info("质量档翻译引擎预热完成: elapsed_ms=%.1f backend=%s target=%s",
                    (time.perf_counter() - started) * 1000.0,
                    self._runtime.backend if self._runtime else "cpu",
                    self._runtime.target if self._runtime else "CPU")

    def health(self) -> str:
        if not self._model_path.exists():
            return f"质量档模型缺失: {self._model_path}"
        required_gb = self._model_path.stat().st_size / (1024 ** 3) * 1.18 + 0.5
        runtime = select_llama_runtime(
            detect_hardware(), self._explicit_server_exe, required_gb=required_gb,
            excluded=self._failed_runtimes)
        if runtime is not None:
            self._runtime = runtime
            self._server_exe = runtime.server_exe
        if not self._server_exe.exists():
            return f"llama-server 缺失: {self._server_exe}"
        return "ok"

    def __del__(self):
        try:
            # Logging/capture streams may already be closed during interpreter
            # shutdown, so finalization performs only the idempotent cleanup.
            proc, port = self._clear_server_state()
            self._terminate_process(proc, port)
        except Exception:
            pass


def _clean(text: str) -> str:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.lower().startswith(("text\n", "translation\n")):
            text = text.split("\n", 1)[1].strip()
    for q in ('"', "'"):
        if len(text) > 1 and text.startswith(q) and text.endswith(q):
            text = text[1:-1].strip()
            break
    for pref in ("Here's the English translation:", "Here's the Chinese translation:",
                 "English translation:", "Chinese translation:", "English:", "Chinese:",
                 "Translation:", "译文:", "翻译:"):
        if text.lower().startswith(pref.lower()) and len(text) > len(pref):
            text = text[len(pref):].strip()
    return text


def _invalid_translation(source: str, output: str,
                         src_lang: str, dst_lang: str) -> bool:
    """拦截解释型回答、空结果和明显失控的长度，避免污染字幕。"""
    if not output:
        return True
    lower = output.casefold()
    forbidden = (
        "here's the", "here is the", "this translation", "the original text",
        "appears to be", "translation attempts", "note:", "译文如下", "翻译如下",
    )
    if any(marker in lower for marker in forbidden):
        return True
    if "\n\n" in output:
        return True
    # 中译英字符通常会膨胀，但超过 5.5 倍基本已是解释/续写；英译中应更短。
    limit = max(64, int(len(source) * (5.5 if dst_lang == "en" else 2.2)))
    if len(output) > limit:
        return True
    if dst_lang == "en" and len(source) >= 4:
        cjk = sum("\u4e00" <= ch <= "\u9fff" for ch in output)
        if cjk / max(1, len(output)) > 0.25:
            return True
    return False
