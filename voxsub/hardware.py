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
INTEL_LLAMA_NPU_MIN_DRIVER = (32, 0, 100, 4778)
INTEL_NPU_DRIVER_URL = (
    "https://www.intel.com/content/www/us/en/download/794734/"
    "intel-npu-driver-windows.html"
)


def _driver_version_tuple(value: str) -> tuple[int, int, int, int] | None:
    """Parse a Windows driver version without accepting partial versions."""
    parts = value.strip().split(".")
    if len(parts) != 4 or any(not part.isdigit() for part in parts):
        return None
    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))


def intel_llama_npu_driver_outdated(version: str) -> bool:
    """Return true only when a known Intel NPU driver is below our floor."""
    parsed = _driver_version_tuple(version)
    return parsed is not None and parsed < INTEL_LLAMA_NPU_MIN_DRIVER


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
    npu_driver_version: str = ""

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
    def has_llama_npu(self) -> bool:
        return bool(
            self.npu_name and
            "intel" in self.npu_name.casefold() and
            not intel_llama_npu_driver_outdated(self.npu_driver_version)
        )

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


def _npu_drivers() -> list[dict[str, str]]:
    raw = _run_powershell(
        "$rx='\\bNPU\\b|Neural Processing|AI Boost|Ryzen AI|Hexagon'; "
        "Get-CimInstance Win32_PnPSignedDriver | "
        "Where-Object { $_.DeviceName -match $rx } | "
        "Select-Object DeviceName,DriverVersion | ConvertTo-Json -Compress")
    if not raw:
        return []
    try:
        value = json.loads(raw)
        values = value if isinstance(value, list) else [value]
        return [
            {
                "name": str(item.get("DeviceName") or "").strip(),
                "version": str(item.get("DriverVersion") or "").strip(),
            }
            for item in values if isinstance(item, dict)
        ]
    except (json.JSONDecodeError, TypeError):
        return []


def _is_integrated_gpu(name: str) -> bool:
    lower = name.casefold()
    if "nvidia" in lower or "geforce" in lower or "quadro" in lower:
        return False
    if "radeon rx" in lower or "radeon pro" in lower:
        return False
    if "intel" in lower:
        # Arc A/B desktop cards are discrete; newer Core Ultra graphics are
        # reported as ``Arc 130T/140T`` or simply ``Arc Graphics``.
        if re.search(r"\barc\s*[ab]\s*\d", lower) or "arc pro" in lower:
            return False
        return True
    return any(token in lower for token in (
        "iris", "uhd", "integrated", "radeon graphics", "radeon 6",
        "radeon 7", "radeon 8", "adreno", "qualcomm",
    ))


def _is_virtual_display(name: str) -> bool:
    """Exclude display adapters that cannot execute inference workloads."""
    lower = name.casefold()
    return (("idd" in lower and "desk" in lower) or any(marker in lower for marker in (
        "indirect display", "microsoft basic display",
        "remote display", "virtual display", "parsec display",
        "spacedesk", "usb display",
    )))


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
        if not name or _is_virtual_display(name):
            logger.debug("忽略非计算显示设备: %s", name)
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
    npu_drivers = _npu_drivers()
    if "intel" in npu_name.casefold():
        npu_drivers = [item for item in npu_drivers
                       if "intel" in item["name"].casefold()] or npu_drivers
    parsed_drivers = [
        (parsed, item["version"])
        for item in npu_drivers
        if (parsed := _driver_version_tuple(item["version"])) is not None
    ]
    npu_driver_version = max(parsed_drivers)[1] if parsed_drivers else ""
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
        if "DmlExecutionProvider" in providers:
            integrated_provider = "DirectML"
        elif "OpenVINOExecutionProvider" in providers and "intel" in integrated_name.casefold():
            integrated_provider = "OpenVINO"

    profile = HardwareProfile(
        cpu_name=cpu_name,
        physical_cores=physical,
        logical_cores=logical,
        ram_gb=ram_gb,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        gpu_provider=gpu_provider,
        npu_name=npu_name,
        npu_provider=npu_ep.replace("ExecutionProvider", ""),
        integrated_gpu_name=integrated_name,
        integrated_gpu_provider=integrated_provider,
        npu_driver_version=npu_driver_version,
    )
    logger.info(
        "硬件画像: gpu=%s/%s npu=%s/%s driver=%s igpu=%s/%s cpu=%s",
        profile.gpu_name or "none", profile.gpu_provider or "no-runtime",
        profile.npu_name or "none", profile.npu_provider or "no-runtime",
        profile.npu_driver_version or "unknown",
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
                         required_gb: float = 0.0,
                         excluded: set[tuple[str, str]] | None = None) -> LlamaRuntime | None:
    """Select GGUF runtime using GPU -> NPU -> integrated GPU -> CPU."""
    if explicit:
        exe = Path(explicit)
        return LlamaRuntime(exe, _classify_llama_backend(exe.parent))
    runtimes = discover_llama_runtimes()
    excluded = excluded or set()

    def first(backends: tuple[str, ...], target: str = "") -> LlamaRuntime | None:
        for runtime in runtimes:
            if runtime.backend in backends:
                if (runtime.backend, target) in excluded:
                    logger.info("llama 运行时跳过: backend=%s target=%s 已记录启动失败",
                                runtime.backend, target or "CPU")
                    continue
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
    # The bundled llama.cpp OpenVINO runtime is independent from the Python
    # ONNX Runtime package. Requiring OpenVINOExecutionProvider here made
    # Intel NPU laptops fall through even though the packaged NPU plugin was
    # available and usable by llama-server.
    openvino_runtime = any(runtime.backend == "openvino" for runtime in runtimes)
    if profile.has_npu and "intel" in profile.npu_name.casefold():
        if intel_llama_npu_driver_outdated(profile.npu_driver_version):
            minimum = ".".join(str(part) for part in INTEL_LLAMA_NPU_MIN_DRIVER)
            logger.warning(
                "llama NPU 跳过: Intel NPU 驱动过旧 current=%s minimum=%s update=%s",
                profile.npu_driver_version, minimum, INTEL_NPU_DRIVER_URL,
            )
        elif not openvino_runtime:
            logger.info("llama NPU 跳过: 检测到 Intel NPU，但未找到随包 OpenVINO 运行时")
        elif profile.ram_gb < required_gb + 4.0:
            logger.info("llama NPU 跳过: 内存不足 required_gb=%.2f ram_gb=%.2f",
                        required_gb + 4.0, profile.ram_gb)
        else:
            picked = first(("openvino",), "NPU")
            if picked:
                logger.info("llama NPU 选择: 使用随包 OpenVINO runtime=%s",
                            picked.server_exe)
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
    # This is the llama.cpp path, so its bundled OpenVINO runtime is the
    # capability check; ORT providers describe the separate ONNX path.
    if profile.has_llama_npu and "openvino" in backends:
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
    "INTEL_LLAMA_NPU_MIN_DRIVER", "INTEL_NPU_DRIVER_URL",
    "intel_llama_npu_driver_outdated",
]
