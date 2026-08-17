"""Hardware inventory and local llama.cpp backend selection.

The physical device and the executable backend are deliberately tracked
separately.  Seeing an NPU in Device Manager does not mean that a particular
model can run on it; the matching execution provider/runtime must also exist.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from voxsub.logging_setup import get_logger

logger = get_logger("hardware")
GIB = 1024 ** 3


@dataclass(frozen=True)
class HardwareProfile:
    cpu_name: str
    physical_cores: int
    logical_cores: int
    ram_gb: float
    gpu_name: str = ""
    vram_gb: float = 0.0
    gpu_provider: str = ""
    npu_name: str = ""
    npu_provider: str = ""
    integrated_gpu_name: str = ""
    integrated_gpu_provider: str = ""

    @property
    def has_discrete_gpu(self) -> bool:
        return bool(self.gpu_name and self.vram_gb >= 1.0)

    @property
    def has_npu(self) -> bool:
        return bool(self.npu_name)

    @property
    def has_npu_runtime(self) -> bool:
        return bool(self.npu_name and self.npu_provider)

    @property
    def has_integrated_gpu(self) -> bool:
        return bool(self.integrated_gpu_name)


@dataclass(frozen=True)
class LlamaRuntime:
    server_exe: Path
    backend: str       # cuda | hip | vulkan | openvino | sycl | cpu
    target: str = ""  # OpenVINO: GPU | NPU | CPU

    @property
    def accelerator(self) -> str:
        if self.backend == "openvino" and self.target == "NPU":
            return "npu"
        if self.backend in {"cuda", "hip"}:
            return "gpu"
        if self.backend in {"vulkan", "sycl"}:
            return "gpu"
        return "cpu"


def _run_powershell(script: str, timeout: float = 4.0) -> str:
    if os.name != "nt":
        return ""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout, check=False,
            creationflags=flags,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _video_controllers() -> list[dict]:
    raw = _run_powershell(
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM,AdapterCompatibility | ConvertTo-Json -Compress")
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except (json.JSONDecodeError, TypeError):
        return []


def _npu_devices() -> list[str]:
    raw = _run_powershell(
        "$rx='\\bNPU\\b|Neural Processing|AI Boost|Ryzen AI|Hexagon'; "
        "Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match $rx } | "
        "Select-Object -ExpandProperty Name | ConvertTo-Json -Compress")
    if not raw:
        return []
    try:
        value = json.loads(raw)
        values = value if isinstance(value, list) else [value]
        pattern = re.compile(
            r"\bnpu\b|neural processing|ai boost|ryzen ai|hexagon", re.IGNORECASE)
        return [name for item in values
                if (name := str(item).strip()) and pattern.search(name)]
    except (json.JSONDecodeError, TypeError):
        return []


def _is_integrated_gpu(name: str) -> bool:
    lower = name.casefold()
    if "nvidia" in lower or "geforce" in lower or "quadro" in lower:
        return False
    if "radeon rx" in lower or "radeon pro" in lower:
        return False
    if "intel" in lower and any(token in lower for token in (" arc a", " arc b")):
        return False
    return any(token in lower for token in (
        "intel", "iris", "uhd", "integrated", "radeon graphics", "radeon 6",
        "radeon 7", "radeon 8", "adreno", "qualcomm",
    ))


def _ort_inventory() -> tuple[set[str], list[tuple[str, str, str]]]:
    """Return available EP names and (EP, hardware type, vendor) devices."""
    try:
        import onnxruntime as ort

        providers = set(ort.get_available_providers())
        devices: list[tuple[str, str, str]] = []
        get_devices = getattr(ort, "get_ep_devices", None)
        if get_devices:
            for item in get_devices():
                kind = str(item.device.type).rsplit(".", 1)[-1].lower()
                vendor = str(getattr(item.device, "vendor", "") or
                             getattr(item, "ep_vendor", ""))
                devices.append((str(item.ep_name), kind, vendor))
        return providers, devices
    except Exception:
        logger.debug("ORT 加速器枚举失败", exc_info=True)
        return set(), []


@lru_cache(maxsize=1)
def detect_hardware() -> HardwareProfile:
    """Detect CPU, discrete GPU, NPU and integrated GPU without changing state."""
    physical = logical = os.cpu_count() or 1
    ram_gb = 8.0
    try:
        import psutil

        physical = psutil.cpu_count(logical=False) or physical
        logical = psutil.cpu_count(logical=True) or logical
        ram_gb = psutil.virtual_memory().total / GIB
    except Exception:
        logger.debug("psutil 硬件检测失败", exc_info=True)

    cpu_name = platform.processor().strip() or platform.machine() or "未知 CPU"
    providers, ep_devices = _ort_inventory()
    gpu_name = ""
    vram_gb = 0.0
    integrated_name = ""

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=False,
            creationflags=flags,
        )
        if result.returncode == 0 and result.stdout.strip():
            first = result.stdout.strip().splitlines()[0]
            gpu_name, memory = [part.strip() for part in first.rsplit(",", 1)]
            vram_gb = float(memory) / 1024.0
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    for item in _video_controllers():
        name = str(item.get("Name") or "").strip()
        if not name or "basic display" in name.casefold():
            continue
        if _is_integrated_gpu(name):
            integrated_name = integrated_name or name
        elif not gpu_name:
            gpu_name = name
            try:
                vram_gb = max(1.0, int(item.get("AdapterRAM") or 0) / GIB)
            except (TypeError, ValueError):
                vram_gb = 1.0

    npu_names = _npu_devices()
    npu_name = npu_names[0] if npu_names else ""
    npu_ep = next((ep for ep, kind, _vendor in ep_devices if kind == "npu"), "")
    if not npu_ep and npu_name:
        for candidate in ("QNNExecutionProvider", "VitisAIExecutionProvider",
                          "OpenVINOExecutionProvider", "NPUExecutionProvider"):
            if candidate in providers:
                npu_ep = candidate
                break

    if "CUDAExecutionProvider" in providers:
        gpu_provider = "CUDA"
    elif gpu_name and "DmlExecutionProvider" in providers:
        gpu_provider = "DirectML"
    else:
        gpu_provider = ""

    integrated_provider = ""
    if integrated_name:
        if "DmlExecutionProvider" in providers and not gpu_name:
            integrated_provider = "DirectML"
        elif "OpenVINOExecutionProvider" in providers and "intel" in integrated_name.casefold():
            integrated_provider = "OpenVINO"

    profile = HardwareProfile(
        cpu_name, physical, logical, ram_gb,
        gpu_name, vram_gb, gpu_provider,
        npu_name, npu_ep.replace("ExecutionProvider", ""),
        integrated_name, integrated_provider,
    )
    logger.info(
        "硬件画像: gpu=%s/%s npu=%s/%s igpu=%s/%s cpu=%s",
        profile.gpu_name or "none", profile.gpu_provider or "no-runtime",
        profile.npu_name or "none", profile.npu_provider or "no-runtime",
        profile.integrated_gpu_name or "none",
        profile.integrated_gpu_provider or "no-runtime", profile.cpu_name,
    )
    return profile


def _runtime_roots() -> list[Path]:
    roots: list[Path] = []
    override = os.environ.get("VOXSUB_LLAMA_DIR")
    if override:
        roots.append(Path(override))
    roots.extend([
        Path(sys.executable).resolve().parent / "tools" / "llama",
        Path(__file__).resolve().parents[1] / "tools" / "llama",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "tools" / "llama",
    ])
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _classify_llama_backend(directory: Path) -> str:
    names = {path.name.casefold() for path in directory.glob("*.dll")}
    joined = " ".join(names)
    if "ggml-cuda" in joined:
        return "cuda"
    if "ggml-hip" in joined or "rocblas" in joined:
        return "hip"
    if "ggml-vulkan" in joined:
        return "vulkan"
    if "ggml-openvino" in joined or "openvino.dll" in names:
        return "openvino"
    if "ggml-sycl" in joined or "sycl" in joined:
        return "sycl"
    return "cpu"


def discover_llama_runtimes() -> list[LlamaRuntime]:
    found: list[LlamaRuntime] = []
    seen: set[Path] = set()
    for root in _runtime_roots():
        candidates = [root / "llama-server.exe"]
        if root.exists():
            candidates.extend(root.glob("*/llama-server.exe"))
        for exe in candidates:
            resolved = exe.resolve()
            if resolved in seen or not exe.is_file():
                continue
            seen.add(resolved)
            found.append(LlamaRuntime(exe, _classify_llama_backend(exe.parent)))
    return found


def select_llama_runtime(profile: HardwareProfile,
                         explicit: Path | str | None = None,
                         required_gb: float = 0.0) -> LlamaRuntime | None:
    """Select GGUF runtime using GPU -> NPU -> integrated GPU -> CPU."""
    if explicit:
        exe = Path(explicit)
        return LlamaRuntime(exe, _classify_llama_backend(exe.parent))
    runtimes = discover_llama_runtimes()

    def first(backends: tuple[str, ...], target: str = "") -> LlamaRuntime | None:
        for runtime in runtimes:
            if runtime.backend in backends:
                return LlamaRuntime(runtime.server_exe, runtime.backend, target)
        return None

    if profile.has_discrete_gpu and profile.vram_gb >= required_gb:
        lower = profile.gpu_name.casefold()
        preferred = (("cuda", "vulkan") if "nvidia" in lower else
                     ("hip", "vulkan") if "amd" in lower or "radeon" in lower else
                     ("sycl", "openvino", "vulkan"))
        picked = first(preferred, "GPU")
        if picked:
            return picked
    if (profile.has_npu and "intel" in profile.npu_name.casefold() and
            profile.ram_gb >= required_gb + 4.0):
        picked = first(("openvino",), "NPU")
        if picked:
            return picked
    if profile.has_integrated_gpu and profile.ram_gb >= required_gb + 4.0:
        lower = profile.integrated_gpu_name.casefold()
        preferred = (("sycl", "openvino", "vulkan") if "intel" in lower else
                     ("hip", "vulkan") if "amd" in lower or "radeon" in lower else
                     ("vulkan",))
        picked = first(preferred, "GPU")
        if picked:
            return picked
    return first(("cpu",), "CPU")


def llama_accelerators(profile: HardwareProfile) -> tuple[str, ...]:
    """Return usable GGUF accelerator classes in product priority order."""
    runtimes = discover_llama_runtimes()
    backends = {runtime.backend for runtime in runtimes}
    result: list[str] = []
    if profile.has_discrete_gpu:
        lower = profile.gpu_name.casefold()
        compatible = ({"cuda", "vulkan"} if "nvidia" in lower else
                      {"hip", "vulkan"} if "amd" in lower or "radeon" in lower else
                      {"sycl", "openvino", "vulkan"})
        if backends & compatible:
            result.append("gpu")
    if (profile.has_npu and "intel" in profile.npu_name.casefold() and
            "openvino" in backends):
        result.append("npu")
    if profile.has_integrated_gpu:
        lower = profile.integrated_gpu_name.casefold()
        compatible = ({"sycl", "openvino", "vulkan"} if "intel" in lower else
                      {"hip", "vulkan"} if "amd" in lower or "radeon" in lower else
                      {"vulkan"})
        if backends & compatible:
            result.append("igpu")
    result.append("cpu")
    return tuple(result)


__all__ = [
    "HardwareProfile", "LlamaRuntime", "detect_hardware",
    "discover_llama_runtimes", "select_llama_runtime", "llama_accelerators",
]
