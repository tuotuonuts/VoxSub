"""QwenQualityTranslator 并发安全回归测试。

测试纪律 (缺陷 #7 工作单)
------------------------
* 被测的生命周期/同步逻辑必须真跑: ``_ensure`` / ``_ensure_instance`` / ``_spawn`` /
  ``_spawn_once`` / ``_activate_server`` / ``_clear_server_state`` / ``close`` 不被替换。
* 只替换外部边界: ``subprocess.Popen`` (OS 进程)、``urllib.request.urlopen`` 与
  ``chat_completion`` (HTTP)、硬件/运行时探测 (系统设备)。
* 历史教训: 旧版本把 ``_spawn`` 与 ``close`` 一起换成假实现 (``close`` 直接返回 None),
  于是"并发首次调用只 spawn 一次"的断言恒真, 而 ``_ensure`` 在锁外调用 ``close()``
  摘除新实例的真实缺陷被彻底掩盖。这里不再 mock 生命周期方法。

共享替身/工厂见 ``tests/test_qwen_lifecycle.py``。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_qwen_lifecycle import (  # noqa: E402
    FakeProcess,
    install_boundaries,
    install_cpu_runtime,
    make_translator,
    prepare,
)
from voxsub.hardware import HardwareProfile, LlamaRuntime  # noqa: E402
from voxsub.llama_runtime import RuntimeStatus  # noqa: E402
from voxsub.translate import qwen as qwen_module  # noqa: E402
from voxsub.translate._http_client import OpenAICompatError  # noqa: E402
from voxsub.translate.base import TranslationError  # noqa: E402
from voxsub.translate.qwen import (  # noqa: E402
    QwenQualityTranslator,
    _clean,
    _invalid_translation,
    _requested_llama_target,
)


def _cpu_and_gpu_runtimes(tmp_path: Path, translator: QwenQualityTranslator):
    """(加速器运行时, CPU 运行时) —— 两者都指向真实存在的假 exe 文件。"""
    gpu_exe = tmp_path / "openvino" / "llama-server.exe"
    gpu_exe.parent.mkdir(parents=True, exist_ok=True)
    gpu_exe.write_bytes(b"MZ fake openvino llama-server")
    return (
        LlamaRuntime(gpu_exe, "openvino", "GPU"),
        LlamaRuntime(Path(translator._server_exe), "cpu", "CPU"),
    )


def _install_runtime_sequence(monkeypatch, runtimes) -> None:
    """按顺序返回给定运行时 (硬件/运行时发现边界替身)。"""
    remaining = list(runtimes)

    def select(*_args, **_kwargs):
        return remaining.pop(0) if remaining else runtimes[-1]

    monkeypatch.setattr(qwen_module, "detect_hardware",
                        lambda: HardwareProfile("test cpu", 4, 8, 16.0))
    monkeypatch.setattr(qwen_module, "discover_llama_runtimes", lambda: list(runtimes))
    monkeypatch.setattr(qwen_module, "select_llama_runtime", select)


# ---------------------------------------------------------------------------
# 并发初始化: 真实生命周期 (不再 mock _spawn / close)
# ---------------------------------------------------------------------------
def test_concurrent_first_ensure_spawns_once(tmp_path: Path, monkeypatch) -> None:
    """8 线程并发首次 _ensure: 只启动一个子进程, 全部复用同一 endpoint。"""
    import threading

    translator, boundary = prepare(tmp_path, monkeypatch)
    barrier = threading.Barrier(8)
    endpoints: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait(timeout=10.0)
        try:
            endpoints.append(translator._ensure())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert errors == [], f"不应有线程抛错: {errors}"
    assert len(endpoints) == 8
    assert len(boundary.launches) == 1, (
        f"并发首次调用应只启动一个子进程, 实际 {len(boundary.launches)}")
    assert set(endpoints) == {translator._endpoint}
    assert boundary.launches[0].proc.terminate_calls == 0
    assert translator._proc is boundary.launches[0].proc


def test_healthy_reuses_endpoint_no_respawn(tmp_path: Path, monkeypatch) -> None:
    """server 已就绪时反复 _ensure 不重复 spawn (真实 spawn 一次 + 复用)。"""
    translator, boundary = prepare(tmp_path, monkeypatch)
    first = translator._ensure()
    assert len(boundary.launches) == 1

    for _ in range(5):
        assert translator._ensure() == first

    assert len(boundary.launches) == 1, "就绪时绝不重 spawn"
    assert boundary.launches[0].proc.terminate_calls == 0


def test_health_rejects_corrupt_catalog_model_before_runtime_probe(tmp_path: Path,
                                                                    monkeypatch) -> None:
    q = make_translator(tmp_path, monkeypatch)
    q._expected_size = 1
    q._expected_sha256 = "0" * 64
    monkeypatch.setattr(qwen_module, "detect_hardware",
                        lambda: (_ for _ in ()).throw(AssertionError(
                            "corrupt model must fail before hardware probe")))
    status = q.health()
    assert "不完整" in status or "校验失败" in status


def test_first_ensure_cold_start_no_deadlock(tmp_path: Path, monkeypatch) -> None:
    """回归: 首次冷启动 _ensure 不死锁 (2026-08-17 冒烟抓到)。

    旧实现把 close() 放在非可重入的 self._lock 内调用会自死锁。这里用**真实**的
    close()/摘除路径并发跑冷启动: 必须在超时内返回。
    """
    import threading

    translator, boundary = prepare(tmp_path, monkeypatch)
    done = threading.Event()
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["endpoint"] = translator._ensure()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=run, name="cold-start")
    thread.start()
    assert done.wait(timeout=10.0), "冷启动 _ensure 卡死 (疑似死锁)"
    assert "error" not in outcome, outcome.get("error")
    assert outcome["endpoint"] == translator._endpoint
    assert len(boundary.launches) == 1


# ---------------------------------------------------------------------------
# 端口选择 / 端口竞争重试
# ---------------------------------------------------------------------------
def test_port_picker_falls_back_when_preferred_range_is_busy(
        tmp_path: Path, monkeypatch) -> None:
    """All 8080-8089 ports being busy must not disable local translation."""
    q = make_translator(tmp_path, monkeypatch)

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
    q = make_translator(tmp_path, monkeypatch)
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
    """A bind race after selection gets a new port before backend fallback.

    真实 ``_spawn``/``_spawn_once``: 第一个子进程因端口占用启动失败 (stdout 有
    bind 报错 → 状态清空 + 进程回收), 第二个用新端口起成功。
    """
    import threading

    ports = iter([50100, 50101])
    translate = make_translator(tmp_path, monkeypatch)
    install_cpu_runtime(monkeypatch, translate)
    boundary = install_boundaries(monkeypatch, plan=[
        {"kwargs": {
            "exit_code": 1,
            "stdout_lines": ["llama-server: error: failed to bind to 127.0.0.1:50100"],
            "drain_event": threading.Event(),
        }},
        {},
    ])
    monkeypatch.setattr(translate, "_pick_free_port", lambda: next(ports))

    translate._spawn()

    assert [launch.port for launch in boundary.launches] == [50100, 50101]
    assert boundary.launches[0].proc.terminate_calls == 1, "端口竞争失败的进程必须回收"
    assert translate._endpoint.endswith(":50101/v1/chat/completions")
    assert translate._port == 50101
    assert translate._proc is boundary.launches[1].proc


def test_failed_accelerator_falls_back_once(tmp_path: Path, monkeypatch) -> None:
    """A crashed accelerator is blacklisted before the next spawn attempt."""
    translator = make_translator(tmp_path, monkeypatch)
    gpu_runtime, cpu_runtime = _cpu_and_gpu_runtimes(tmp_path, translator)
    _install_runtime_sequence(monkeypatch, [gpu_runtime, cpu_runtime])
    boundary = install_boundaries(monkeypatch, plan=[
        {"kwargs": {"exit_code": 0xC0000005,
                    "stdout_lines": ["llama: openvino GPU graph execution failed"]}},
        {},
    ])

    endpoint = translator._ensure()

    assert endpoint == translator._endpoint
    assert len(boundary.launches) == 2, "加速后端失败后应换后端重建一个实例"
    assert boundary.launches[0].proc.terminate_calls == 1, "崩溃实例必须被回收"
    assert ("openvino", "GPU") in translator._failed_runtimes
    assert translator._server_exe == cpu_runtime.server_exe
    assert boundary.launches[1].command[0] == str(cpu_runtime.server_exe)
    assert translator._proc is boundary.launches[1].proc


# ---------------------------------------------------------------------------
# 运行时选择 (真实 _select_runtime, 只替换硬件发现边界)
# ---------------------------------------------------------------------------
def test_select_runtime_repairs_missing_openvino_before_npu_selection(
        tmp_path: Path, monkeypatch) -> None:
    """An Intel NPU first provisions OpenVINO, then is selected after rediscovery."""
    q = make_translator(tmp_path, monkeypatch)
    server = tmp_path / "openvino" / "llama-server.exe"
    server.parent.mkdir(parents=True)
    server.write_bytes(b"MZ")
    npu_runtime = LlamaRuntime(server, "openvino", "NPU")
    profile = HardwareProfile(
        "test cpu", 4, 8, 16.0, npu_name="Intel AI Boost",
        npu_driver_version="32.0.101.5763",
    )
    discovered = iter(([], [npu_runtime]))
    selected = iter((None, npu_runtime))
    calls: list[str] = []

    monkeypatch.setattr(qwen_module, "detect_hardware", lambda: profile)
    monkeypatch.setattr(
        qwen_module, "discover_llama_runtimes", lambda: next(discovered))
    monkeypatch.setattr(
        qwen_module, "select_llama_runtime", lambda *_args, **_kwargs: next(selected))
    monkeypatch.setattr(
        qwen_module, "ensure_openvino_runtime",
        lambda: (calls.append("bootstrap") or RuntimeStatus(
            server.parent, True, "download")),
    )

    selected_runtime = q._select_runtime()[1]

    assert selected_runtime == npu_runtime
    assert q._runtime == npu_runtime
    assert q._server_exe == server
    assert calls == ["bootstrap"]


def test_select_runtime_falls_back_when_openvino_repair_fails(
        tmp_path: Path, monkeypatch) -> None:
    """A failed bootstrap is logged and leaves the normal CPU fallback intact."""
    q = make_translator(tmp_path, monkeypatch)
    cpu_server = tmp_path / "cpu" / "llama-server.exe"
    cpu_server.parent.mkdir(parents=True)
    cpu_server.write_bytes(b"MZ")
    cpu_runtime = LlamaRuntime(cpu_server, "cpu", "CPU")
    profile = HardwareProfile(
        "test cpu", 4, 8, 16.0, npu_name="Intel AI Boost",
        npu_driver_version="32.0.101.5763",
    )
    calls: list[str] = []

    monkeypatch.setattr(qwen_module, "detect_hardware", lambda: profile)
    monkeypatch.setattr(qwen_module, "discover_llama_runtimes", lambda: [])
    monkeypatch.setattr(
        qwen_module, "select_llama_runtime", lambda *_args, **_kwargs: cpu_runtime)
    monkeypatch.setattr(
        qwen_module, "ensure_openvino_runtime",
        lambda: (calls.append("bootstrap") or RuntimeStatus(
            tmp_path / "openvino", False, "error", "network unavailable")),
    )

    selected_runtime = q._select_runtime()[1]

    assert selected_runtime == cpu_runtime
    assert q._runtime == cpu_runtime
    assert q._server_exe == cpu_server
    assert calls == ["bootstrap"]


def test_requested_llama_target_accepts_known_values_and_ignores_invalid(
        monkeypatch) -> None:
    monkeypatch.delenv("VOXSUB_LLAMA_TARGET", raising=False)
    assert _requested_llama_target() is None
    monkeypatch.setenv("VOXSUB_LLAMA_TARGET", " NPU ")
    assert _requested_llama_target() == "npu"
    monkeypatch.setenv("VOXSUB_LLAMA_TARGET", "auto")
    assert _requested_llama_target() is None
    monkeypatch.setenv("VOXSUB_LLAMA_TARGET", "tpu")
    assert _requested_llama_target() is None


def test_forced_npu_reports_unavailable_runtime_without_cpu_fallback(
        tmp_path: Path, monkeypatch) -> None:
    q = make_translator(tmp_path, monkeypatch)
    profile = HardwareProfile("test cpu", 4, 8, 16.0, npu_name="Intel AI Boost")
    select_calls: list[dict] = []
    monkeypatch.setenv("VOXSUB_LLAMA_TARGET", "npu")
    monkeypatch.setattr(qwen_module, "detect_hardware", lambda: profile)
    monkeypatch.setattr(qwen_module, "discover_llama_runtimes", lambda: [])

    def select_none(*_args, **kwargs):
        select_calls.append(kwargs)
        return None

    monkeypatch.setattr(qwen_module, "select_llama_runtime", select_none)
    monkeypatch.setattr(
        qwen_module, "ensure_openvino_runtime",
        lambda: RuntimeStatus(tmp_path / "openvino", False, "error", "unavailable"),
    )

    with pytest.raises(TranslationError, match="强制 llama 目标 NPU"):
        q._select_runtime()
    assert select_calls[0]["preferred_target"] == "npu"


# ---------------------------------------------------------------------------
# 请求期失败降级 (真实 _ensure + 真实 close)
# ---------------------------------------------------------------------------
def test_translation_retries_same_sentence_after_accelerator_failure(
        tmp_path: Path, monkeypatch) -> None:
    """A live accelerator request failure must fall back before dropping text."""
    translator = make_translator(tmp_path, monkeypatch)
    gpu_runtime, cpu_runtime = _cpu_and_gpu_runtimes(tmp_path, translator)
    _install_runtime_sequence(monkeypatch, [gpu_runtime, cpu_runtime])
    boundary = install_boundaries(monkeypatch)
    endpoints: list[str] = []

    def fake_chat(endpoint, **_kwargs) -> str:
        endpoints.append(endpoint)
        assert translator._runtime is not None
        if translator._runtime.target == "GPU":
            raise OpenAICompatError("GPU graph execution failed")
        return "Hello."

    monkeypatch.setattr(qwen_module, "chat_completion", fake_chat)

    assert translator.translate("你好。", "zh", "en") == "Hello."
    assert len(endpoints) == 2 and endpoints[0] != endpoints[1]
    assert ("openvino", "GPU") in translator._failed_runtimes
    assert len(boundary.launches) == 2
    assert boundary.launches[0].proc.terminate_calls == 1, "失败实例必须被摘除回收"
    assert translator._proc is boundary.launches[1].proc
    assert translator._endpoint == endpoints[1]


def test_spawn_requests_openvino_device_and_disables_npu_fallback(
        tmp_path: Path, monkeypatch) -> None:
    """NPU launches must select OPENVINO0 and reject silent CPU fallback."""
    q = make_translator(tmp_path, monkeypatch)
    fake_server = tmp_path / "tools" / "llama" / "llama-server.exe"
    runtime = LlamaRuntime(fake_server, "openvino", "NPU")
    q._runtime = runtime
    q._server_exe = runtime.server_exe
    q._model_path = tmp_path / "model.gguf"
    q._model_path.write_bytes(b"model")
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return FakeProcess(pid=42)

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


# ---------------------------------------------------------------------------
# 提示词 / 输出清洗 (不涉及生命周期)
# ---------------------------------------------------------------------------
def test_quality_translation_uses_system_constraint(tmp_path: Path, monkeypatch) -> None:
    q = make_translator(tmp_path, monkeypatch)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = FakeProcess(pid=1)
    captured: dict = {}

    def fake_chat(_endpoint, *, messages, **_kwargs):
        captured["messages"] = messages
        return "Hello, world."

    monkeypatch.setattr("voxsub.translate.qwen.chat_completion", fake_chat)
    out = q.translate("你好，世界。", "zh", "en")
    assert out == "Hello, world."
    assert captured["messages"][0]["role"] == "system"
    assert "only the translated text" in captured["messages"][0]["content"]


def test_hy_mt2_uses_official_single_user_prompt_and_sampling(
        tmp_path: Path, monkeypatch) -> None:
    q = make_translator(tmp_path, monkeypatch)
    q._prompt_style = "hy-mt2"
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = FakeProcess(pid=1)
    captured: dict = {}

    def fake_chat(_endpoint, *, messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return "Hello."

    monkeypatch.setattr("voxsub.translate.qwen.chat_completion", fake_chat)
    assert q.translate("你好。", "zh", "en") == "Hello."
    assert captured["messages"] == [
        {"role": "user", "content": (
            "The source language is Chinese. Translate the following segment into English, "
            "without additional explanation.\n\n你好。"
        )},
    ]
    assert "<source>" not in captured["messages"][0]["content"]
    assert captured["temperature"] == 0.7
    assert captured["top_p"] == 0.6
    assert captured["top_k"] == 20
    assert captured["repeat_penalty"] == 1.05
    assert captured["stop"]


def test_hy_mt2_retry_keeps_direct_prompt_and_expands_long_output_budget(
        tmp_path: Path, monkeypatch) -> None:
    q = make_translator(tmp_path, monkeypatch)
    q._prompt_style = "hy-mt2"
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = FakeProcess(pid=1)
    calls: list[dict] = []
    answers = iter(["This translation has an explanation.", "Hello."])

    def fake_chat(_endpoint, **kwargs):
        calls.append(kwargs)
        return next(answers)

    monkeypatch.setattr("voxsub.translate.qwen.chat_completion", fake_chat)
    source = "你好" * 240
    assert q.translate(source, "zh", "en") == "Hello."
    assert len(calls) == 2
    assert all(len(call["messages"]) == 1 for call in calls)
    assert all(call["messages"][0]["role"] == "user" for call in calls)
    assert all("<source>" not in call["messages"][0]["content"] for call in calls)
    assert calls[0]["max_tokens"] > 128


def test_selected_gpu_backend_offloads_layers(tmp_path: Path) -> None:
    profile = HardwareProfile(
        "Intel Core Ultra", 4, 8, 16.0,
        integrated_gpu_name="Intel Arc Graphics",
    )
    translator = QwenQualityTranslator(model_path=tmp_path / "m.gguf")
    runtime = LlamaRuntime(tmp_path / "llama-server.exe", "vulkan", "GPU")
    assert translator._auto_gpu_layers(profile, runtime) == 999


def test_clean_removes_prompt_echo_and_control_tokens() -> None:
    echoed = (
        "从输入文本中检测源语言，然后仅将其翻译为中文。不要翻译成其他任何语言。"
        "只输出翻译结果。 关于此事的某些事情。"
    )
    assert _clean(echoed) == "关于此事的某些事情。"
    assert _clean("创造发明，实现，而这些。<|endoftext|>Humanity。") == (
        "创造发明，实现，而这些。"
    )
    assert _clean(
        "The source language is English. Translate the following segment into Chinese, "
        "without additional explanation.\n\nHumanity.\n人类。",
        source="Humanity.",
    ) == "人类。"
    assert _invalid_translation("これは文です", echoed, "ja", "zh")


def test_quality_translation_rejects_explanatory_answer(tmp_path: Path, monkeypatch) -> None:
    q = make_translator(tmp_path, monkeypatch)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = FakeProcess(pid=1)
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
    q = make_translator(tmp_path, monkeypatch)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = FakeProcess(pid=1)
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
    q = make_translator(tmp_path, monkeypatch)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = FakeProcess(pid=1)
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


def test_hy_mt2_ocr_batch_avoids_system_role_and_source_tags(
        tmp_path: Path, monkeypatch) -> None:
    q = make_translator(tmp_path, monkeypatch)
    q._prompt_style = "hy-mt2"
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = FakeProcess(pid=1)
    captured: dict = {}

    def fake_chat(_endpoint, **kwargs):
        captured.update(kwargs)
        return '["Hello."]'

    monkeypatch.setattr("voxsub.translate.qwen.chat_completion", fake_chat)
    assert q.translate_many(["你好。"], "zh", "en") == ["Hello."]
    assert [message["role"] for message in captured["messages"]] == ["user"]
    assert "<source>" not in captured["messages"][0]["content"]
    assert captured["temperature"] == 0.7
    assert captured["top_p"] == 0.6
