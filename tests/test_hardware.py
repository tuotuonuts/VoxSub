"""Accelerator ordering and llama.cpp backend discovery tests."""
from __future__ import annotations

from pathlib import Path

from voxsub.hardware import (
    GIB,
    HardwareProfile,
    _is_integrated_gpu,
    _is_virtual_display,
    _windows_ram_gb,
    intel_llama_npu_driver_outdated,
    llama_accelerators,
    select_llama_runtime,
)


def _runtime(root: Path, name: str, marker: str = "") -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "llama-server.exe").write_bytes(b"MZ")
    if marker:
        (directory / marker).write_bytes(b"dll")


def test_llama_runtime_priority_gpu_npu_igpu_cpu(tmp_path: Path, monkeypatch) -> None:
    _runtime(tmp_path, "cpu")
    _runtime(tmp_path, "vulkan", "ggml-vulkan.dll")
    _runtime(tmp_path, "openvino", "ggml-openvino.dll")
    monkeypatch.setenv("VOXSUB_LLAMA_DIR", str(tmp_path))

    full = HardwareProfile(
        "cpu", 8, 16, 32.0, "NVIDIA RTX", 8.0, "DirectML",
        "Intel AI Boost", "OpenVINO", "Intel Arc Graphics", "DirectML",
        "32.0.100.4841")
    selected = select_llama_runtime(full)
    assert selected and selected.backend == "vulkan" and selected.target == "GPU"

    low_vram = HardwareProfile(
        "cpu", 8, 16, 32.0, "NVIDIA RTX", 4.0, "DirectML",
        "Intel AI Boost", "OpenVINO", "Intel Arc Graphics", "DirectML",
        "32.0.100.4841")
    selected = select_llama_runtime(low_vram, required_gb=6.0)
    assert selected and selected.backend == "openvino" and selected.target == "NPU"

    npu = HardwareProfile(
        "cpu", 4, 8, 16.0, npu_name="Intel AI Boost", npu_provider="OpenVINO",
        integrated_gpu_name="Intel Arc Graphics", integrated_gpu_provider="DirectML",
        npu_driver_version="32.0.100.4841")
    selected = select_llama_runtime(npu)
    assert selected and selected.backend == "openvino" and selected.target == "NPU"

    igpu = HardwareProfile(
        "cpu", 4, 8, 16.0,
        integrated_gpu_name="AMD Radeon Graphics", integrated_gpu_provider="DirectML")
    selected = select_llama_runtime(igpu)
    assert selected and selected.backend == "vulkan" and selected.target == "GPU"

    cpu = HardwareProfile("cpu", 2, 4, 8.0)
    selected = select_llama_runtime(cpu)
    assert selected and selected.backend == "cpu" and selected.target == "CPU"


def test_physical_npu_uses_bundled_openvino_without_ort_provider(
        tmp_path: Path, monkeypatch) -> None:
    _runtime(tmp_path, "openvino", "ggml-openvino.dll")
    _runtime(tmp_path, "cpu")
    monkeypatch.setenv("VOXSUB_LLAMA_DIR", str(tmp_path))
    profile = HardwareProfile(
        "cpu", 4, 8, 16.0, npu_name="Intel AI Boost",
        integrated_gpu_name="Intel Arc 130T GPU",
        npu_driver_version="32.0.100.4841",
    )
    selected = select_llama_runtime(profile)
    assert selected and selected.backend == "openvino" and selected.target == "NPU"


def test_npu_runtime_override_is_discovered_outside_repo(
        tmp_path: Path, monkeypatch) -> None:
    _runtime(tmp_path, "openvino", "ggml-openvino.dll")
    monkeypatch.setenv("VOXSUB_NPU_RUNTIME_DIR", str(tmp_path / "openvino"))
    monkeypatch.delenv("VOXSUB_LLAMA_DIR", raising=False)
    from voxsub.hardware import discover_llama_runtimes

    runtimes = discover_llama_runtimes()
    assert any(item.backend == "openvino" for item in runtimes)


def test_project_temp_npu_runtime_is_discovered_without_environment_variable(
        tmp_path: Path, monkeypatch) -> None:
    temp_root = tmp_path / "temp"
    _runtime(temp_root, "VoxSub_npu_runtime_b10470", "ggml-openvino.dll")
    monkeypatch.delenv("VOXSUB_NPU_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("VOXSUB_LLAMA_DIR", raising=False)
    monkeypatch.setattr("voxsub.hardware.tempfile.gettempdir", lambda: str(temp_root))
    from voxsub.hardware import discover_llama_runtimes

    runtimes = discover_llama_runtimes()
    assert any(
        item.backend == "openvino" and
        item.server_exe.parent.name == "VoxSub_npu_runtime_b10470"
        for item in runtimes
    )


def test_fallback_scores_cpu_when_shared_memory_headroom_is_too_small(
        tmp_path: Path, monkeypatch) -> None:
    _runtime(tmp_path, "openvino", "ggml-openvino.dll")
    _runtime(tmp_path, "cpu")
    monkeypatch.setenv("VOXSUB_LLAMA_DIR", str(tmp_path))
    profile = HardwareProfile(
        "Core Ultra", 4, 8, 8.0,
        integrated_gpu_name="Intel Arc Graphics",
        integrated_gpu_provider="DirectML",
    )

    selected = select_llama_runtime(profile, required_gb=6.0)

    assert selected and selected.backend == "cpu" and selected.target == "CPU"
    assert "CPU" in selected.selection_reason


def test_fallback_prefers_integrated_gpu_with_healthy_headroom(
        tmp_path: Path, monkeypatch) -> None:
    _runtime(tmp_path, "openvino", "ggml-openvino.dll")
    _runtime(tmp_path, "cpu")
    monkeypatch.setenv("VOXSUB_LLAMA_DIR", str(tmp_path))
    profile = HardwareProfile(
        "Core Ultra", 4, 8, 16.0,
        integrated_gpu_name="Intel Arc Graphics",
        integrated_gpu_provider="DirectML",
    )

    selected = select_llama_runtime(
        profile, required_gb=6.0, excluded={("openvino", "NPU")},
    )

    assert selected and selected.backend == "openvino" and selected.target == "GPU"
    assert "降级评分" in selected.selection_reason


def test_openvino_gpu_is_reported_as_gpu_accelerator(
        tmp_path: Path) -> None:
    from voxsub.hardware import LlamaRuntime

    runtime = LlamaRuntime(tmp_path / "llama-server.exe", "openvino", "GPU")
    assert runtime.accelerator == "gpu"


def test_outdated_intel_npu_driver_falls_back_to_igpu(
        tmp_path: Path, monkeypatch) -> None:
    _runtime(tmp_path, "openvino", "ggml-openvino.dll")
    _runtime(tmp_path, "vulkan", "ggml-vulkan.dll")
    _runtime(tmp_path, "cpu")
    monkeypatch.setenv("VOXSUB_LLAMA_DIR", str(tmp_path))
    profile = HardwareProfile(
        "cpu", 4, 8, 16.0,
        npu_name="Intel AI Boost",
        integrated_gpu_name="Intel Arc 130T GPU",
        npu_driver_version="32.0.100.3159",
    )

    assert intel_llama_npu_driver_outdated(profile.npu_driver_version)
    selected = select_llama_runtime(profile)
    assert selected and selected.backend in {"openvino", "vulkan"}
    assert selected.target == "GPU"
    assert llama_accelerators(profile) == ("igpu", "cpu")


def test_unknown_intel_npu_driver_keeps_legacy_detection(
        tmp_path: Path, monkeypatch) -> None:
    _runtime(tmp_path, "openvino", "ggml-openvino.dll")
    monkeypatch.setenv("VOXSUB_LLAMA_DIR", str(tmp_path))
    profile = HardwareProfile("cpu", 4, 8, 16.0, npu_name="Intel AI Boost")

    assert not intel_llama_npu_driver_outdated("")
    assert not intel_llama_npu_driver_outdated("32.0.100.4778")
    assert not intel_llama_npu_driver_outdated("32.0.100.4841")
    selected = select_llama_runtime(profile)
    assert selected and selected.backend == "openvino" and selected.target == "NPU"


def test_virtual_display_is_not_a_gpu_and_arc_130t_is_integrated() -> None:
    assert _is_virtual_display("IddDesk Device")
    assert not _is_integrated_gpu("IddDesk Device")
    assert _is_integrated_gpu("Intel(R) Arc(TM) 130T GPU")
    assert not _is_integrated_gpu("Intel Arc A770")


def test_windows_ram_fallback_parses_total_physical_memory(monkeypatch) -> None:
    monkeypatch.setattr(
        "voxsub.hardware._run_powershell", lambda _script: str(32 * GIB))

    assert _windows_ram_gb() == 32.0


def test_windows_ram_fallback_rejects_missing_or_invalid_values(monkeypatch) -> None:
    for value in ("", "not-a-number", "0", "-1"):
        monkeypatch.setattr(
            "voxsub.hardware._run_powershell", lambda _script, value=value: value)
        assert _windows_ram_gb() is None
