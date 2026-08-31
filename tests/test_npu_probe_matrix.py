from __future__ import annotations

from pathlib import Path

import voxsub.hardware as hardware


def test_npu_name_filter_accepts_pnp_friendly_names() -> None:
    assert hardware._filter_npu_names([
        {"FriendlyName": "Intel(R) AI Boost", "Name": "fallback"},
        {"Name": "AMD Ryzen AI", "Status": "OK"},
        "USB Audio Device",
    ]) == ["Intel(R) AI Boost", "AMD Ryzen AI"]


def test_npu_inventory_uses_pnp_when_wmi_returns_nothing(monkeypatch) -> None:
    calls: list[str] = []

    def fake_powershell(script: str, timeout: float = 4.0) -> str:
        calls.append(script)
        if "Get-PnpDevice" in script and "DriverVersion" not in script:
            return '{"FriendlyName":"Intel(R) AI Boost","Name":"Intel NPU"}'
        return ""

    monkeypatch.setattr(hardware, "_run_powershell", fake_powershell)
    monkeypatch.setattr(hardware, "_pnputil_npu_devices", lambda: [])

    names, source, detail = hardware._npu_device_inventory()

    assert names == ["Intel(R) AI Boost"]
    assert source == "pnp"
    assert "wmi" in detail
    assert len(calls) == 2


def test_npu_inventory_uses_pnputil_as_last_resort(monkeypatch) -> None:
    monkeypatch.setattr(hardware, "_run_powershell", lambda _script, timeout=4.0: "")
    monkeypatch.setattr(hardware, "_pnputil_npu_devices", lambda: ["Intel AI Boost"])

    names, source, _detail = hardware._npu_device_inventory()

    assert names == ["Intel AI Boost"]
    assert source == "pnputil"


def test_npu_probe_matrix_script_runs_direct_and_forced_app_paths() -> None:
    script = Path(__file__).parents[1] / "scripts" / "npu_probe_matrix.ps1"
    source = script.read_text(encoding="utf-8")
    assert "[int]$Iterations = 3" in source
    assert "npu_probe.ps1" in source
    assert "--force', 'npu'" in source
    assert "matrix-summary.json" in source
    assert "automatic_voxsub is informational" in source
    assert "production_openvino0_env_npu" in source
    assert "explicit_npu_env_npu" in source
    assert "openvino0_without_device_env" in source
    assert "direct_variants" in source
    assert "diagnosis =" in source
    assert "SkipHardwarePreflight" in source
    assert "probe_summary_stale_or_invalid" in source
    assert "run_id = [string]$summary.run_id" in source


def test_npu_probe_script_records_variant_and_reason_code() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "npu_probe.ps1").read_text(
        encoding="utf-8")
    assert "[string]$DeviceArgument = 'OPENVINO0'" in script
    assert "[string]$OpenVinoDevice = 'NPU'" in script
    assert "SkipOpenVinoDevice" in script
    assert "process_access_violation" in script
    assert "unsupported_openvino_option" in script
    assert "probe-summary.json" in script
    assert "hardware_preflight_skipped" in script
    assert "npu_device_not_detected" in script
    assert "npu_device_inventory_access_denied" in script
    assert "npu_device_inventory_error" in script
    assert "hardware_inventory_errors" in script
    assert "npuInventoryAccessDenied" in script
    assert "$ProbeSchemaVersion = 2" in script
    assert "$RunId = [guid]::NewGuid().ToString('N')" in script
    assert "Remove-Item -LiteralPath $artifact" in script
    assert "npuInventoryPnpUtilExitCode" in script
    assert "pnputil diagnostic output:" in script
    assert "inventoryErrorText = ($npuInventoryErrors | ForEach-Object" in script
    assert "Register-NpuInventoryError" in script
    assert "HRESULT" in script
    assert "npu_inventory_commands" in script
    assert "probeReasonCode -eq 'npu_device_inventory_access_denied'" in script
    assert "function Test-AccessDeniedText" in script
    assert r"\u62d2\u7edd\u8bbf\u95ee" in script


def test_npu_runtime_builder_verifies_source_and_built_binary_fingerprint() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "build_npu_runtime.ps1").read_text(
        encoding="utf-8")
    assert "Find-PrivateNpuOptions" in script
    assert "Find-PrivateNpuOptionsInBinary" in script
    assert "runtime-build.json" in script
    assert "Test-ReusableRuntime" in script
    assert "patch_status = 'no-npuw'" in script
