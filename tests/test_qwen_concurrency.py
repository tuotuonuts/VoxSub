"""QwenQualityTranslator 并发安全回归测试。

问题（独立审查发现的 TOCTOU 竞态）: _ensure() 检查在锁外做, _spawn() 无条件执行
→ 并发初次调用可双开 llama-server 孤儿化第一个。

修复: double-checked locking。本测试 monkeypatch _spawn 只做计数,
验证"并发 N 线程首次 _ensure 只 spawn 一次"——不启动真进程避免真实 spawn 语义。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxsub.hardware import HardwareProfile, LlamaRuntime  # noqa: E402
from voxsub.translate._http_client import OpenAICompatError  # noqa: E402
from voxsub.translate.base import TranslationError  # noqa: E402
from voxsub.translate.qwen import QwenQualityTranslator  # noqa: E402
from voxsub.translate.qwen import _clean, _invalid_translation  # noqa: E402
from voxsub.translate import qwen as qwen_module  # noqa: E402


class _FakeProc:
    """最小"存活进程"替身: 提供 _ensure 依赖的 poll() (返回 None = 存活)。"""

    def __init__(self, pid: int = 1) -> None:
        self.pid = pid

    def poll(self):  # noqa: A003 - 对齐 subprocess.Popen.poll 语义
        return None  # None = 仍在运行

    def wait(self, timeout: float | None = None) -> int:
        return 0  # 假进程立即退出

    def terminate(self) -> None:
        pass


def _make_qwen(tmp_path: Path) -> QwenQualityTranslator:
    """构造未就绪的 translator (model_path 指向存在的伪 gguf, server 可递归寻找)。"""
    tools = tmp_path / "tools" / "llama"
    tools.mkdir(parents=True)
    fake_exe = tools / "llama-server.exe"
    fake_exe.write_bytes(b"MZ fake")
    models = tmp_path / "models" / "llm"
    models.mkdir(parents=True)
    gguf = models / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    gguf.write_bytes(b"x" * 600_000)
    # 强制 monkeypatch 默认路径, 让构造不因缺模型抛错
    q = QwenQualityTranslator(model_path=gguf, n_ctx=64, n_threads=1)
    return q


def test_concurrent_first_ensure_spawns_once(tmp_path: Path, monkeypatch) -> None:
    """8 线程并发首次 _ensure: 只有一次 _spawn (双检锁回归)。"""
    q = _make_qwen(tmp_path)
    # 用最小假 server: monkeypatch _spawn 使其快速"成功"并记录调用次数
    spawned = {"n": 0}

    def fake_spawn(self) -> None:
        spawned["n"] += 1
        self._proc = _FakeProc(9999)      # 带 poll() 的假进程
        self._port = 9999
        self._endpoint = "http://127.0.0.1:9999/v1/chat/completions"

    monkeypatch.setattr(QwenQualityTranslator, "_spawn", fake_spawn)
    monkeypatch.setattr(QwenQualityTranslator, "close", lambda self: None)

    barrier = threading.Barrier(8)
    endpoints: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        barrier.wait()
        try:
            endpoints.append(q._ensure())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    # _spawn 必须恰好执行一次 (并发竞态修复前会是多次)
    assert spawned["n"] == 1, f"并发首次调用应只 spawn 1 次, 实际 {spawned['n']}"
    # 所有线程拿到同一 endpoint
    assert len(errors) == 0, f"不应有线程抛错: {errors}"
    assert all(e == "http://127.0.0.1:9999/v1/chat/completions" for e in endpoints)
    assert len(endpoints) == 8


def test_healthy_reuses_endpoint_no_respawn(tmp_path: Path, monkeypatch) -> None:
    """server 已就绪时反复 _ensure 不重复 spawn。"""
    q = _make_qwen(tmp_path)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = _FakeProc(1)
    q._port = 9999
    n = {"val": 0}

    def fake_spawn(self) -> None:
        n["val"] += 1

    monkeypatch.setattr(QwenQualityTranslator, "_spawn", fake_spawn)
    for _ in range(5):
        assert q._ensure() == q._endpoint
    assert n["val"] == 0  # 就绪时绝不重 spawn


def test_health_rejects_corrupt_catalog_model_before_runtime_probe(tmp_path: Path,
                                                                    monkeypatch) -> None:
    q = _make_qwen(tmp_path)
    q._expected_size = 1
    q._expected_sha256 = "0" * 64
    monkeypatch.setattr(qwen_module, "detect_hardware",
                        lambda: (_ for _ in ()).throw(AssertionError(
                            "corrupt model must fail before hardware probe")))
    status = q.health()
    assert "不完整" in status or "校验失败" in status


def test_first_ensure_cold_start_no_deadlock(tmp_path: Path, monkeypatch) -> None:
    """回归: 首次冷启动 _ensure 不死锁 (2026-08-17 冒烟抓到)。

    曾把 close() 放在 with self._lock 内调用, 而 close() 内部也拿同一把
    非可重入锁 -> 死锁卡死。修复: close() 移到锁外。
    """
    q = _make_qwen(tmp_path)
    # close() 需能容忍 _FakeProc (无真进程), 走快速路径
    monkeypatch.setattr(QwenQualityTranslator, "close", lambda self: None)

    spawned = {"n": 0}

    def fake_spawn(self) -> None:
        spawned["n"] += 1
        self._proc = _FakeProc(1)
        self._port = 9999
        self._endpoint = "http://127.0.0.1:9999/v1/chat/completions"

    monkeypatch.setattr(QwenQualityTranslator, "_spawn", fake_spawn)
    # 冷启动(未就绪) -> _ensure 必须在超时内返回, 不死锁
    endpoint = q._ensure()
    assert endpoint == "http://127.0.0.1:9999/v1/chat/completions"
    assert spawned["n"] == 1


def test_port_picker_falls_back_when_preferred_range_is_busy(
        tmp_path: Path, monkeypatch) -> None:
    """All 8080-8089 ports being busy must not disable local translation."""
    q = _make_qwen(tmp_path)

    class _FakeSocket:
        def __init__(self, *_args, **_kwargs) -> None:
            self._port = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def setsockopt(self, *_args) -> None:
            pass

        def bind(self, address) -> None:
            if address[1] != 0:
                raise OSError("preferred port occupied")
            self._port = 49152

        def getsockname(self):
            return ("127.0.0.1", self._port)

    monkeypatch.setattr(qwen_module.socket, "socket", _FakeSocket)
    assert q._pick_free_port() == 49152


def test_port_picker_changes_random_port_after_collision(
        tmp_path: Path, monkeypatch) -> None:
    """A busy random candidate is skipped instead of reused."""
    q = _make_qwen(tmp_path)
    candidates = iter([848, 849])  # 50000, then 50001

    class _FakeSocket:
        def __init__(self, *_args, **_kwargs) -> None:
            self._port = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def setsockopt(self, *_args) -> None:
            pass

        def bind(self, address) -> None:
            if address[1] == 50000:
                raise OSError("random candidate occupied")
            self._port = address[1]

        def getsockname(self):
            return ("127.0.0.1", self._port)

    monkeypatch.setattr(qwen_module.secrets, "randbelow", lambda _span: next(candidates))
    monkeypatch.setattr(qwen_module.socket, "socket", _FakeSocket)
    assert q._pick_free_port() == 50001


def test_spawn_retries_after_port_race(tmp_path: Path, monkeypatch) -> None:
    """A bind race after selection gets a new port before backend fallback."""
    q = _make_qwen(tmp_path)
    attempts = {"count": 0}

    def fake_spawn_once(self) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            self._server_output_tail.append("bind failed: Address already in use")
            raise TranslationError("llama-server 端口启动失败")
        self._proc = _FakeProc(42)
        self._port = 50001
        self._endpoint = "http://127.0.0.1:50001/v1/chat/completions"

    monkeypatch.setattr(QwenQualityTranslator, "_spawn_once", fake_spawn_once)
    q._spawn()
    assert attempts["count"] == 2
    assert q._endpoint.endswith("50001/v1/chat/completions")


def test_failed_accelerator_falls_back_once(tmp_path: Path, monkeypatch) -> None:
    """A crashed accelerator is blacklisted before the next spawn attempt."""
    q = _make_qwen(tmp_path)
    monkeypatch.setattr(QwenQualityTranslator, "close", lambda self: None)
    attempts: list[tuple[str, str]] = []

    def fake_spawn(self) -> None:
        if not attempts:
            runtime = LlamaRuntime(Path("npu/llama-server.exe"), "openvino", "NPU")
            self._runtime = runtime
            attempts.append((runtime.backend, runtime.target))
            raise TranslationError("exit code 0xC0000005")
        runtime = LlamaRuntime(Path("cpu/llama-server.exe"), "cpu", "CPU")
        self._runtime = runtime
        attempts.append((runtime.backend, runtime.target))
        self._proc = _FakeProc(2)
        self._port = 9998
        self._endpoint = "http://127.0.0.1:9998/v1/chat/completions"

    monkeypatch.setattr(QwenQualityTranslator, "_spawn", fake_spawn)
    assert q._ensure().endswith("9998/v1/chat/completions")
    assert attempts == [("openvino", "NPU"), ("cpu", "CPU")]
    assert ("openvino", "NPU") in q._failed_runtimes
    q._proc = None
    q._endpoint = None


def test_translation_retries_same_sentence_after_accelerator_failure(
        tmp_path: Path, monkeypatch) -> None:
    """A live accelerator request failure must fall back before dropping text."""
    q = _make_qwen(tmp_path)
    runtimes = [
        LlamaRuntime(Path("npu/llama-server.exe"), "openvino", "NPU"),
        LlamaRuntime(Path("cpu/llama-server.exe"), "cpu", "CPU"),
    ]
    attempts: list[tuple[str, str]] = []

    def fake_ensure() -> str:
        runtime = runtimes.pop(0)
        q._runtime = runtime
        q._proc = _FakeProc()
        q._endpoint = f"http://127.0.0.1:{len(attempts) + 1}/v1/chat/completions"
        return q._endpoint

    def fake_request(*_args, **_kwargs) -> str:
        assert q._runtime is not None
        attempts.append((q._runtime.backend, q._runtime.target))
        if q._runtime.target == "NPU":
            raise OpenAICompatError("NPU graph execution failed")
        return "Hello."

    monkeypatch.setattr(q, "_ensure", fake_ensure)
    monkeypatch.setattr(q, "_request_translation", fake_request)
    monkeypatch.setattr(q, "close", lambda: None)

    assert q.translate("你好。", "zh", "en") == "Hello."
    assert attempts == [("openvino", "NPU"), ("cpu", "CPU")]
    assert ("openvino", "NPU") in q._failed_runtimes
    q._proc = None
    q._endpoint = None


def test_spawn_requests_openvino_device_and_disables_npu_fallback(
        tmp_path: Path, monkeypatch) -> None:
    """NPU launches must select OPENVINO0 and reject silent CPU fallback."""
    q = _make_qwen(tmp_path)
    fake_server = tmp_path / "tools" / "llama" / "llama-server.exe"
    runtime = LlamaRuntime(fake_server, "openvino", "NPU")
    q._runtime = runtime
    q._server_exe = runtime.server_exe
    q._model_path = tmp_path / "model.gguf"
    q._model_path.write_bytes(b"model")
    captured: dict = {}

    class _Proc(_FakeProc):
        stdout = None
        returncode = None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return _Proc(42)

    monkeypatch.setattr(q, "_pick_free_port", lambda: 8090)
    wait_ready: dict = {}

    def fake_wait_ready(port, **kwargs) -> None:
        wait_ready["port"] = port
        wait_ready.update(kwargs)

    monkeypatch.setattr(q, "_wait_ready", fake_wait_ready)
    monkeypatch.setattr(q, "_probe_runtime_inference", lambda **_kwargs: None)
    monkeypatch.setattr("voxsub.translate.qwen.detect_hardware", lambda: HardwareProfile(
        "test cpu", 4, 8, 16.0, npu_name="Intel AI Boost"))
    monkeypatch.setattr("voxsub.translate.qwen.select_llama_runtime",
                        lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr("voxsub.translate.qwen.subprocess.Popen", fake_popen)
    q._spawn()

    assert captured["cmd"][1:3] == ["--device", "OPENVINO0"]
    assert captured["cmd"][-2:] == ["--parallel", "1"]
    assert captured["env"]["GGML_OPENVINO_DEVICE"] == "NPU"
    assert captured["env"]["GGML_OPENVINO_ENABLE_FALLBACK"] == "0"
    assert captured["env"]["GGML_OPENVINO_STATEFUL_EXECUTION"] == "0"
    assert captured["env"]["GGML_OPENVINO_MEMORY_OPTIMIZE"] == "1"
    assert captured["cmd"][captured["cmd"].index("--ctx-size") + 1] == "64"
    assert wait_ready == {
        "port": 8090,
        "timeout": 600.0,
        "backend": "openvino",
        "target": "NPU",
    }
    q.close()


def test_quality_translation_uses_system_constraint(tmp_path: Path, monkeypatch) -> None:
    q = _make_qwen(tmp_path)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = _FakeProc(1)
    captured: dict = {}

    def fake_chat(_endpoint, *, messages, **_kwargs):
        captured["messages"] = messages
        return "Hello, world."

    monkeypatch.setattr("voxsub.translate.qwen.chat_completion", fake_chat)
    out = q.translate("你好，世界。", "zh", "en")
    assert out == "Hello, world."
    assert captured["messages"][0]["role"] == "system"
    assert "only the translated text" in captured["messages"][0]["content"]


def test_hy_mt2_prompt_uses_system_role_and_source_boundary(
        tmp_path: Path, monkeypatch) -> None:
    q = _make_qwen(tmp_path)
    q._prompt_style = "hy-mt2"
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = _FakeProc(1)
    captured: dict = {}

    def fake_chat(_endpoint, *, messages, **_kwargs):
        captured["messages"] = messages
        return "Hello."

    monkeypatch.setattr("voxsub.translate.qwen.chat_completion", fake_chat)
    assert q.translate("你好。", "zh", "en") == "Hello."
    assert captured["messages"][0]["role"] == "system"
    assert "<source>" in captured["messages"][1]["content"]
    assert "</source>" in captured["messages"][1]["content"]


def test_selected_gpu_backend_offloads_layers(tmp_path: Path) -> None:
    q = _make_qwen(tmp_path)
    profile = HardwareProfile(
        "Intel Core Ultra", 4, 8, 16.0,
        integrated_gpu_name="Intel Arc Graphics",
    )
    runtime = LlamaRuntime(tmp_path / "llama-server.exe", "vulkan", "GPU")
    assert q._auto_gpu_layers(profile, runtime) == 999


def test_clean_removes_prompt_echo_and_control_tokens() -> None:
    echoed = (
        "从输入文本中检测源语言，然后仅将其翻译为中文。不要翻译成其他任何语言。"
        "只输出翻译结果。 关于此事的某些事情。"
    )
    assert _clean(echoed) == "关于此事的某些事情。"
    assert _clean("创造发明，实现，而这些。<|endoftext|>Humanity。") == (
        "创造发明，实现，而这些。"
    )
    assert _invalid_translation("これは文です", echoed, "ja", "zh")


def test_quality_translation_rejects_explanatory_answer(tmp_path: Path, monkeypatch) -> None:
    q = _make_qwen(tmp_path)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = _FakeProc(1)
    answers = iter([
        "Here's the English translation:\nHello.\n\nThis translation attempts to explain it.",
        "Hello.",
    ])
    monkeypatch.setattr("voxsub.translate.qwen.chat_completion",
                        lambda *_args, **_kwargs: next(answers))
    assert q.translate("你好。", "zh", "en") == "Hello."
    assert _invalid_translation("你好。", "Here's the translation and a note", "zh", "en")


def test_quality_ocr_batch_uses_one_request_and_preserves_order(
        tmp_path: Path, monkeypatch) -> None:
    q = _make_qwen(tmp_path)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = _FakeProc(1)
    calls: list[dict] = []

    def fake_chat(_endpoint, **kwargs):
        calls.append(kwargs)
        return '["Hello.","World."]'

    monkeypatch.setattr("voxsub.translate.qwen.chat_completion", fake_chat)

    translated = q.translate_many(["你好。", "世界。"], "zh", "en")

    assert translated == ["Hello.", "World."]
    assert len(calls) == 1
    assert "exactly 2" in calls[0]["messages"][-1]["content"]


def test_quality_ocr_single_paragraph_uses_large_batch_budget(
        tmp_path: Path, monkeypatch) -> None:
    q = _make_qwen(tmp_path)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = _FakeProc(1)
    calls: list[dict] = []

    def fake_chat(_endpoint, **kwargs):
        calls.append(kwargs)
        return '["这是一段经过整体翻译的长正文。"]'

    monkeypatch.setattr("voxsub.translate.qwen.chat_completion", fake_chat)

    source = "A long document paragraph needs one coherent translation. " * 5
    translated = q.translate_many([source], "en", "zh")

    assert translated == ["这是一段经过整体翻译的长正文。"]
    assert len(calls) == 1
    assert calls[0]["max_tokens"] > 128
    assert "exactly 1" in calls[0]["messages"][-1]["content"]
