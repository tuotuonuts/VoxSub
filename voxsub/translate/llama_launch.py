"""Pure llama-server command/environment construction."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from voxsub.hardware import LlamaRuntime


@dataclass(frozen=True)
class LlamaLaunchPlan:
    command: tuple[str, ...]
    environment: dict[str, str]
    gpu_layers: int
    context_size: int


def _configure_openvino(command: list[str], environment: dict[str, str],
                        runtime: LlamaRuntime) -> None:
    command[1:1] = ["--device", "OPENVINO0"]
    environment["GGML_OPENVINO_DEVICE"] = runtime.target or "CPU"
    if runtime.target in {"GPU", "NPU"}:
        environment["GGML_OPENVINO_ENABLE_FALLBACK"] = "0"
    if runtime.target == "GPU":
        environment["GGML_OPENVINO_STATEFUL_EXECUTION"] = "1"
        return
    if runtime.target == "NPU":
        environment["GGML_OPENVINO_STATEFUL_EXECUTION"] = "0"
        environment["GGML_OPENVINO_MEMORY_OPTIMIZE"] = "1"
        cache_dir = (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "VoxSub" / "cache" / "openvino-compiled"
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        environment["GGML_OPENVINO_COMPILED_MODEL_CACHE_DIR"] = str(cache_dir)


def build_llama_launch_plan(
    *,
    server_exe: Path,
    model_path: Path,
    port: int,
    context_size: int,
    threads: int,
    gpu_layers: int,
    runtime: LlamaRuntime | None,
) -> LlamaLaunchPlan:
    """Build an immutable launch description without creating a process."""
    effective_context = context_size
    if runtime is not None and runtime.backend == "openvino" and runtime.target == "NPU":
        effective_context = min(effective_context, 1024)
    command = [
        str(server_exe), "--model", str(model_path),
        "--host", "127.0.0.1", "--port", str(port),
        "--ctx-size", str(effective_context),
        "--n-gpu-layers", str(gpu_layers), "--threads", str(threads),
    ]
    if runtime is not None and runtime.backend == "openvino" and runtime.target == "NPU":
        command.extend(["--parallel", "1"])
    environment = os.environ.copy()
    if runtime is not None and runtime.backend == "openvino":
        _configure_openvino(command, environment, runtime)
    return LlamaLaunchPlan(
        tuple(command), environment, gpu_layers, effective_context)
