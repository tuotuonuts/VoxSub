"""voxsub.translate.qwen —— 质量档翻译器: llama.cpp llama-server (GGUF)。

技术路线 (DESIGN.md 决策 #9 + 2026-08-17 路线调整):
- Qwen2.5 ONNX 权重在 HF 全 gated(401) → 走 llama.cpp GGUF。
- llama-cpp-python 在 Windows 无 MSVC 编译环境装不了 (CMake/nmake 失败) →
  改用 llama.cpp **官方预编译 llama-server.exe** (纯 CPU, 本机已就位:
  %LOCALAPPDATA%\\VoxSub\\tools\\llama\\llama-server.exe, 配套 DLL 同目录)。
- 本类是 HTTP 客户端: lazy spawn llama-server 子进程, 调用其
  OpenAI 兼容 /v1/chat/completions 端点, 解析 choices[0].message.content。

进程管理:
- lazy 启动: 首次 translate 时 spawn; 端口 8080 起, 占用则试 8081-8089。
- close(): terminate 子进程 (幂等)。
- 启动失败 (exe 缺失 / 端口全占 / 起不来) → 抛清晰 TranslationError。
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from voxsub.logging_setup import get_logger

from ._http_client import OpenAICompatError, chat_completion
from .base import TranslationError, Translator

logger = get_logger("translate.qwen")


def _default_models_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "models"


def _default_tools_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "tools" / "llama"


#: (src,dst) → 翻译指令 (llama-server 以 completion 方式续写, 简洁指令即可)
_PROMPTS = {
    ("zh", "en"): "Translate to English: {text}",
    ("en", "zh"): "Translate to Chinese: {text}",
}


class QwenQualityTranslator(Translator):
    """质量档: 通过 llama-server 子进程跑 Qwen GGUF, 中英互翻。"""

    name = "qwen-quality"
    langs = ("zh", "en")
    local = True

    def __init__(self, model_path: Path | str | None = None,
                 server_exe: Path | str | None = None,
                 n_ctx: int = 2048, n_threads: int = 4,
                 max_tokens: int = 128, fast_mode: bool = True,
                 port: int = 8080):
        self._model_path = Path(model_path) if model_path else (
            _default_models_dir() / "llm" / "qwen2.5-1.5b-instruct-q4_k_m.gguf")
        self._server_exe = Path(server_exe) if server_exe else (
            _default_tools_dir() / "llama-server.exe")
        self._n_ctx = n_ctx
        self._n_threads = n_threads
        self._max_tokens = max_tokens
        self._fast_mode = fast_mode
        self._start_port = port
        self._proc: subprocess.Popen | None = None
        self._port: int | None = None
        self._lock = threading.Lock()
        self._endpoint: str | None = None

    # ------------------------------------------------------------------
    def _pick_free_port(self) -> int:
        for port in range(self._start_port, self._start_port + 10):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        logger.warning("端口 %s-%s 全被占用, 无法启动新 llama-server",
                       self._start_port, self._start_port + 9)
        raise TranslationError(f"端口 {self._start_port}-{self._start_port + 9} 全被占用")

    def _spawn(self) -> None:
        if self._model_path is None or not self._model_path.exists():
            logger.warning("质量档模型缺失, 拒绝 spawn: %s (请用 scripts/model_fetch.py 下载)",
                           self._model_path)
            raise TranslationError(
                f"质量档模型缺失: {self._model_path} (请用 scripts/model_fetch.py 下载)")
        if not self._server_exe.exists():
            logger.warning("llama-server 缺失, 拒绝 spawn: %s (应含配套 DLL)",
                           self._server_exe)
            raise TranslationError(
                f"llama-server 缺失: {self._server_exe} (应含配套 DLL, 见 tools/llama/)")
        port = self._pick_free_port()
        cmd = [str(self._server_exe),
               "--model", str(self._model_path),
               "--host", "127.0.0.1",
               "--port", str(port),
               "--ctx-size", str(self._n_ctx),
               "--n-gpu-layers", "0",
               "--threads", str(self._n_threads),
               ]
        # 隐藏子进程控制台窗口, 避免抢占用户
        flags = 0
        try:
            import subprocess as sp
            flags = getattr(sp, "CREATE_NO_WINDOW", 0)
        except Exception:
            pass
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags)
        except OSError as exc:
            logger.exception("llama-server 进程启动失败 (exe=%s)", self._server_exe)
            raise TranslationError(f"llama-server 启动失败: {exc}") from exc
        self._port = port
        self._endpoint = f"http://127.0.0.1:{port}/v1/chat/completions"
        logger.info("llama-server 已启动 (port=%s, pid=%s)", port, self._proc.pid)
        self._wait_ready(port)

    def _wait_ready(self, port: int, timeout: float = 60.0) -> None:
        """轮询健康端点直到可用; 进程提前退出则报错。"""
        probe = f"http://127.0.0.1:{port}/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                logger.error("llama-server 进程提前退出 (port=%s, 退出码=%s)",
                             port, self._proc.returncode)
                raise TranslationError(
                    f"llama-server 进程提前退出, 退出码={self._proc.returncode}")
            try:
                import urllib.request as u
                with u.urlopen(probe, timeout=1.0) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            time.sleep(0.3)
        logger.error("llama-server %.0fs 内未就绪 (port=%s)", timeout, port)
        raise TranslationError(f"llama-server 60s 内未就绪 (port {port})")

    def _ensure(self) -> str:
        """保证 llama-server 就绪并返回 endpoint。

        并发安全 (double-checked locking): 检查在锁外做(快速路径), 但**重新检查**
        在锁内做——两个线程并发首次调用时, 只有第一个真正 spawn, 第二个
        看到 endpoint 已就绪直接复用, 避免双开 llama-server 导致孤儿进程
        (每孤儿 ~1.5GB 模型驻留, 且耗尽 8080-8089 端口范围)。
        """
        if self._endpoint is None or self._proc is None or self._proc.poll() is not None:
            with self._lock:
                # 锁内二次检查: 并发发起方可能已在等待时完成 spawn
                if self._endpoint is None or self._proc is None or self._proc.poll() is not None:
                    self.close()
                    self._spawn()
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
        prompt = _PROMPTS.get((src_lang, dst_lang))
        if prompt is None:
            raise TranslationError(f"质量档不支持语言对 {(src_lang, dst_lang)}")
        endpoint = self._ensure()
        try:
            with self._lock:
                out = chat_completion(
                    endpoint,
                    messages=[{"role": "user", "content": prompt.format(text=text)}],
                    temperature=0.0,
                    max_tokens=self._max_tokens,
                    timeout_sec=timeout_ms / 1000.0,
                )
        except OpenAICompatError as exc:
            raise TranslationError(f"qwen(server) 调用失败: {exc}") from exc
        return _clean(out)

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            port = self._port
            self._endpoint = None
            self._port = None
        if proc is not None:
            logger.info("关闭质量档 llama-server (pid=%s, port=%s)", proc.pid, port)
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("llama-server 5s 内未正常退出, 强制 kill (pid=%s)",
                                   proc.pid)
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                logger.exception("关闭 llama-server 时异常 (pid=%s)", proc.pid)

    def health(self) -> str:
        if not self._model_path.exists():
            return f"质量档模型缺失: {self._model_path}"
        if not self._server_exe.exists():
            return f"llama-server 缺失: {self._server_exe}"
        return "ok"

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _clean(text: str) -> str:
    text = text.strip()
    for q in ('"', "'"):
        if len(text) > 1 and text.startswith(q) and text.endswith(q):
            text = text[1:-1].strip()
            break
    for pref in ("English:", "Chinese:", "Translation:", "译文:", "翻译:"):
        if text.lower().startswith(pref.lower()) and len(text) > len(pref):
            text = text[len(pref):].strip()
    return text
