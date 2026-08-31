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
    selection_reason: str = ""

    @property
    def accelerator(self) -> str:
        if self.backend == "openvino" and self.target == "NPU":
            return "npu"
        if self.backend in {"cuda", "hip", "vulkan", "sycl"}:
            return "gpu"
        if self.backend == "openvino" and self.target == "GPU":
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


def _windows_ram_gb() -> float | None:
    """Read physical memory without psutil (portable probe/frozen fallback)."""
    raw = _run_powershell(
        "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory")
    if not raw:
        return None
    try:
        value = int(raw.strip()) / GIB
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


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
        # A failed inventory changes routing decisions and is actionable; keep
        # it visible to both the local diagnostics page and optional Sentry.
        logger.warning("ORT 加速器枚举失败，设备能力可能不完整", exc_info=True)
        return set(), []


def _system_resources() -> tuple[str, int, int, float]:
    """Return CPU identity, physical/logical cores and physical RAM."""
    physical = logical = os.cpu_count() or 1
    ram_gb = 8.0
    try:
        import psutil

        physical = psutil.cpu_count(logical=False) or physical
        logical = psutil.cpu_count(logical=True) or logical
        ram_gb = psutil.virtual_memory().total / GIB
    except Exception:
        # Missing/failed resource probing affects routing and should be visible
        # in diagnostics; the conservative fallback remains unchanged.
        logger.warning("psutil 硬件检测失败，使用保守资源默认值", exc_info=True)
        ram_gb = _windows_ram_gb() or ram_gb
    cpu_name = platform.processor().strip() or platform.machine() or "未知 CPU"
    return cpu_name, physical, logical, ram_gb


def _nvidia_inventory() -> tuple[str, float]:
    """Read the first NVIDIA adapter and its dedicated memory, if present."""
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
            name, memory = [part.strip() for part in first.rsplit(",", 1)]
            return name, float(memory) / 1024.0
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return "", 0.0


def _controller_inventory(gpu_name: str, vram_gb: float) -> tuple[str, float, str]:
    """Merge Windows display-controller data with a higher quality GPU probe."""
    integrated_name = ""
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
    return gpu_name, vram_gb, integrated_name


def _newest_npu_driver(npu_name: str) -> str:
    drivers = _npu_drivers()
    if "intel" in npu_name.casefold():
        drivers = [item for item in drivers
                   if "intel" in item["name"].casefold()] or drivers
    parsed = [
        (version, item["version"])
        for item in drivers
        if (version := _driver_version_tuple(item["version"])) is not None
    ]
    return max(parsed)[1] if parsed else ""


def _npu_provider(providers: set[str],
                  ep_devices: list[tuple[str, str, str]],
                  npu_name: str) -> str:
    provider = next((ep for ep, kind, _vendor in ep_devices if kind == "npu"), "")
    if provider or not npu_name:
        return provider
    candidates = (
        "QNNExecutionProvider", "VitisAIExecutionProvider",
        "OpenVINOExecutionProvider", "NPUExecutionProvider",
    )
    return next((candidate for candidate in candidates if candidate in providers), "")


def _gpu_provider(providers: set[str], gpu_name: str) -> str:
    if "CUDAExecutionProvider" in providers:
        return "CUDA"
    if gpu_name and "DmlExecutionProvider" in providers:
        return "DirectML"
    return ""


def _integrated_gpu_provider(providers: set[str], name: str) -> str:
    if not name:
        return ""
    if "DmlExecutionProvider" in providers:
        return "DirectML"
    if "OpenVINOExecutionProvider" in providers and "intel" in name.casefold():
        return "OpenVINO"
    return ""


@lru_cache(maxsize=1)
def detect_hardware() -> HardwareProfile:
    """Detect CPU, discrete GPU, NPU and integrated GPU without changing state."""
    cpu_name, physical, logical, ram_gb = _system_resources()
    providers, ep_devices = _ort_inventory()
    gpu_name, vram_gb = _nvidia_inventory()
    gpu_name, vram_gb, integrated_name = _controller_inventory(gpu_name, vram_gb)
    npu_names = _npu_devices()
    npu_name = npu_names[0] if npu_names else ""
    profile = HardwareProfile(
        cpu_name=cpu_name,
        physical_cores=physical,
        logical_cores=logical,
        ram_gb=ram_gb,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        gpu_provider=_gpu_provider(providers, gpu_name),
        npu_name=npu_name,
        npu_provider=_npu_provider(providers, ep_devices, npu_name).replace(
            "ExecutionProvider", ""),
        integrated_gpu_name=integrated_name,
        integrated_gpu_provider=_integrated_gpu_provider(providers, integrated_name),
        npu_driver_version=_newest_npu_driver(npu_name),
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
    # The source build keeps the verified no-NPUW OpenVINO runtime outside
    # the repository. Honour the same override used by build.ps1 so a source
    # checkout can use that runtime without copying DLLs into Git.
    npu_override = os.environ.get("VOXSUB_NPU_RUNTIME_DIR")
    if npu_override:
        roots.append(Path(npu_override))
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


@dataclass(frozen=True)
class _RuntimeCatalog:
    runtimes: tuple[LlamaRuntime, ...]
    excluded: frozenset[tuple[str, str]]

    @classmethod
    def discover(cls, excluded: set[tuple[str, str]] | None) -> _RuntimeCatalog:
        return cls(tuple(discover_llama_runtimes()), frozenset(excluded or ()))

    @property
    def has_openvino(self) -> bool:
        return any(runtime.backend == "openvino" for runtime in self.runtimes)

    def first(self, backends: tuple[str, ...], target: str = "", *,
              reason: str = "") -> LlamaRuntime | None:
        for runtime in self.runtimes:
            if runtime.backend not in backends:
                continue
            if (runtime.backend, target) in self.excluded:
                logger.info(
                    "llama 运行时跳过: backend=%s target=%s 已记录启动失败",
                    runtime.backend, target or "CPU",
                )
                continue
            return LlamaRuntime(
                runtime.server_exe, runtime.backend, target,
                selection_reason=reason,
            )
        return None


def _preferred_gpu_backends(name: str, *, integrated: bool) -> tuple[str, ...]:
    lower = name.casefold()
    if "nvidia" in lower and not integrated:
        return ("cuda", "vulkan")
    if "amd" in lower or "radeon" in lower:
        return ("hip", "vulkan")
    if "intel" in lower:
        return ("sycl", "openvino", "vulkan")
    return ("vulkan",) if integrated else ("sycl", "openvino", "vulkan")


def _log_selected(runtime: LlamaRuntime) -> LlamaRuntime:
    logger.info(
        "llama 后端选择: backend=%s target=%s reason=%s",
        runtime.backend, runtime.target, runtime.selection_reason,
    )
    return runtime


def _discrete_runtime(profile: HardwareProfile, catalog: _RuntimeCatalog,
                      required_gb: float) -> LlamaRuntime | None:
    if not profile.has_discrete_gpu or profile.vram_gb < required_gb:
        return None
    reason = (
        f"独立显卡 {profile.gpu_name} 显存 {profile.vram_gb:.1f} GB "
        f"满足模型约 {required_gb:.1f} GB 运行需求"
    )
    return catalog.first(
        _preferred_gpu_backends(profile.gpu_name, integrated=False),
        "GPU", reason=reason,
    )


def _npu_runtime(profile: HardwareProfile, catalog: _RuntimeCatalog,
                 required_gb: float) -> LlamaRuntime | None:
    if not profile.has_npu or "intel" not in profile.npu_name.casefold():
        return None
    if intel_llama_npu_driver_outdated(profile.npu_driver_version):
        minimum = ".".join(str(part) for part in INTEL_LLAMA_NPU_MIN_DRIVER)
        logger.warning(
            "llama NPU 跳过: Intel NPU 驱动过旧 current=%s minimum=%s update=%s",
            profile.npu_driver_version, minimum, INTEL_NPU_DRIVER_URL,
        )
        return None
    if not catalog.has_openvino:
        logger.info("llama NPU 跳过: 检测到 Intel NPU，但未找到随包 OpenVINO 运行时")
        return None
    if profile.ram_gb < required_gb + 4.0:
        logger.info(
            "llama NPU 跳过: 内存不足 required_gb=%.2f ram_gb=%.2f",
            required_gb + 4.0, profile.ram_gb,
        )
        return None
    return catalog.first(
        ("openvino",), "NPU",
        reason=(f"检测到兼容 Intel NPU 与 OpenVINO 运行时，内存 "
                f"{profile.ram_gb:.1f} GB 满足候选执行条件"),
    )


def _integrated_candidates(profile: HardwareProfile, catalog: _RuntimeCatalog,
                           required_gb: float,
                           memory_margin: float) -> list[tuple[float, LlamaRuntime]]:
    if not profile.has_integrated_gpu or profile.ram_gb < required_gb + 2.5:
        return []
    preferred = _preferred_gpu_backends(
        profile.integrated_gpu_name, integrated=True)
    backend_bonus = {
        "sycl": 20.0, "openvino": 18.0, "hip": 18.0, "vulkan": 12.0,
    }
    candidates: list[tuple[float, LlamaRuntime]] = []
    for order, backend in enumerate(preferred):
        picked = catalog.first((backend,), "GPU")
        if picked is None:
            continue
        score = (
            86.0 + backend_bonus.get(backend, 0.0)
            + min(memory_margin, 16.0) * 1.3
            - min(required_gb, 16.0) * 0.8
            - order * 0.1
        )
        reason = (
            f"核显 {profile.integrated_gpu_name} 可用 {backend}；"
            f"共享内存余量约 {memory_margin:.1f} GB；降级评分 {score:.1f}"
        )
        candidates.append((score, LlamaRuntime(
            picked.server_exe, picked.backend, picked.target,
            selection_reason=reason,
        )))
    return candidates


def _cpu_candidate(profile: HardwareProfile, catalog: _RuntimeCatalog,
                   memory_margin: float) -> tuple[float, LlamaRuntime] | None:
    picked = catalog.first(("cpu",), "CPU")
    if picked is None:
        return None
    score = (
        72.0 + min(max(profile.logical_cores, 1), 32) * 2.0
        + min(memory_margin, 12.0) * 0.75
    )
    reason = (
        f"CPU {profile.logical_cores} 线程；内存余量约 {memory_margin:.1f} GB；"
        f"降级评分 {score:.1f}"
    )
    return score, LlamaRuntime(
        picked.server_exe, picked.backend, picked.target,
        selection_reason=reason,
    )


def _fallback_runtime(profile: HardwareProfile, catalog: _RuntimeCatalog,
                      required_gb: float) -> LlamaRuntime | None:
    memory_margin = max(0.0, profile.ram_gb - required_gb)
    candidates = _integrated_candidates(
        profile, catalog, required_gb, memory_margin)
    cpu = _cpu_candidate(profile, catalog, memory_margin)
    if cpu is not None:
        candidates.append(cpu)
    if not candidates:
        logger.warning("llama 后端选择失败: 没有未被排除的兼容运行时")
        return None
    for score, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        logger.info(
            "llama 降级候选: backend=%s target=%s score=%.1f reason=%s",
            candidate.backend, candidate.target, score, candidate.selection_reason,
        )
    return max(candidates, key=lambda item: item[0])[1]


def select_llama_runtime(profile: HardwareProfile,
                         explicit: Path | str | None = None,
                         required_gb: float = 0.0,
                         excluded: set[tuple[str, str]] | None = None) -> LlamaRuntime | None:
    """Select a GGUF runtime and explain accelerator/fallback decisions.

    A discrete GPU and Intel NPU retain product priority.  Once those are not
    usable, integrated-GPU and CPU candidates are scored from runtime
    compatibility, shared-memory headroom and CPU capacity.  This prevents a
    large model from being forced onto an iGPU when CPU execution is more
    likely to remain responsive, while still preferring a healthy iGPU for the
    smaller on-device models.
    """
    if explicit:
        exe = Path(explicit)
        return LlamaRuntime(
            exe, _classify_llama_backend(exe.parent),
            selection_reason="用户指定 llama-server，禁用自动后端选择",
        )
    catalog = _RuntimeCatalog.discover(excluded)
    selected = _discrete_runtime(profile, catalog, required_gb)
    if selected is None:
        # The bundled llama.cpp OpenVINO runtime is independent from the
        # Python ONNX Runtime provider inventory.
        selected = _npu_runtime(profile, catalog, required_gb)
    if selected is None:
        selected = _fallback_runtime(profile, catalog, required_gb)
    return _log_selected(selected) if selected is not None else None


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
