"""Inventory accuracy/privacy, failure boundaries, CPU branding and IPC contract."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from voxsub import hardware_inventory as inventory


def group(items, status="ok"):
    return {"status": status, "items": items}


def test_inventory_whitelists_fields_preserves_each_module_and_reports_failure():
    raw = {"cpu": group({"Name": "13th Gen Intel(R) Core(TM) i5-13600KF", "NumberOfCores": 14, "SerialNumber": "secret"}),
           "memory": group([{"Manufacturer": "Vendor", "PartNumber": " DIMM-A ", "Capacity": 16 * 2**30}, {"Manufacturer": "Vendor", "PartNumber": "DIMM-B", "Capacity": 16 * 2**30}]),
           "network": group({"Name": "Wi-Fi adapter", "MACAddress": "SECRET-MAC", "GUID": "SECRET-GUID"}),
           "disks": group({"Model": "SSD", "Size": "2000000000000", "SerialNumber": "SECRET-SERIAL"}),
           "sound": group([], "not_detected"), "monitors": group([], "unavailable")}
    data = inventory.normalize_inventory(raw)
    encoded = json.dumps(data)
    assert "SECRET" not in encoded and "SerialNumber" not in encoded and "MACAddress" not in encoded
    assert data["categories"]["cpu"]["items"][0]["Name"] == "13th Gen Intel Core i5-13600KF"
    assert len(data["categories"]["memory"]["items"]) == 2
    assert data["categories"]["disks"]["items"][0]["Size"] == 2e12
    assert data["categories"]["sound"]["status"] == "not_detected"
    assert data["categories"]["monitors"]["status"] == "unavailable"


@pytest.mark.parametrize("value", [None, [], "broken", {"cpu": None}, {"cpu": {"status": "ok", "items": "invalid"}}])
def test_invalid_payload_does_not_become_a_positive_empty_inventory(value):
    data = inventory.normalize_inventory(value)
    assert data["categories"]["cpu"]["status"] == "unavailable"


@pytest.mark.parametrize("value", ["unknown", "Default string", "To be filled by O.E.M.", "undefined"])
def test_placeholder_model_names_are_not_shown_as_products(value):
    assert inventory.clean_model_name(value) == ""


def test_cpu_model_uses_registry_product_not_cpuid_label(monkeypatch):
    class Key:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    fake = SimpleNamespace(HKEY_LOCAL_MACHINE=1, OpenKey=lambda *args: Key(),
                           QueryValueEx=lambda *args: ("13th Gen Intel(R) Core(TM) i5-13600KF", 1))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(inventory, "os", SimpleNamespace(name="nt"))
    assert inventory.processor_model("Intel64 Family 6 Model 183 Stepping 1, GenuineIntel") == "13th Gen Intel Core i5-13600KF"


def test_unknown_cpu_does_not_guess_a_model_from_family(monkeypatch):
    monkeypatch.setattr(inventory, "os", SimpleNamespace(name="other"))
    assert inventory.processor_model("Intel64 Family 6 Model 183 Stepping 1, GenuineIntel") == ""
    assert inventory.processor_model("AMD Ryzen 7 7840HS") == "AMD Ryzen 7 7840HS"


def test_query_is_bounded_hidden_read_only_and_utf8(monkeypatch):
    calls=[]
    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(stdout=b'{"cpu":{"status":"not_detected","items":[]}}')
    monkeypatch.setattr(inventory.subprocess, "run", run)
    assert inventory._query_cim()["cpu"]["status"] == "not_detected"
    command, options = calls[0]
    assert command[1:3] == ["-NoProfile", "-NonInteractive"]
    assert options["timeout"] == 15 and options["check"] is True
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert "SerialNumber" not in command[-1] and "MACAddress" not in command[-1]
    assert "AdapterRAM" not in command[-1] and "Set-CimInstance" not in command[-1]


def test_nvidia_memory_queries_true_capacity_instead_of_wmi_uint32(monkeypatch):
    monkeypatch.setattr(inventory.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=b'NVIDIA GeForce RTX 4060, 8192\nNVIDIA RTX A2000, 6144\n'))
    assert inventory._nvidia_memory() == {"NVIDIA GeForce RTX 4060": 8, "NVIDIA RTX A2000": 6}


def test_timeout_and_non_windows_return_unknown_not_absence(monkeypatch):
    monkeypatch.setattr(inventory, "os", SimpleNamespace(name="nt"))
    def timeout(): raise subprocess.TimeoutExpired("query", 15)
    monkeypatch.setattr(inventory, "_query_cim", timeout)
    data = inventory.collect_inventory()
    assert all(g["status"] == "unavailable" for g in data["categories"].values())
    monkeypatch.setattr(inventory, "os", SimpleNamespace(name="other"))
    assert inventory.collect_inventory()["source"] == "unavailable"


def test_cache_is_bounded_and_returns_detached_snapshots(monkeypatch):
    clock=[10.0];calls=[]
    monkeypatch.setattr(inventory, "_CACHE", None)
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(inventory, "collect_inventory", lambda: calls.append(1) or inventory.normalize_inventory({}))
    first=inventory.hardware_inventory(); first["categories"].clear()
    assert inventory.hardware_inventory()["categories"] and len(calls) == 1
    clock[0]=71
    assert inventory.hardware_inventory()["categories"] and len(calls) == 2


def test_ipc_prefers_cim_cpu_product_and_follows_result_schema(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "frontend/backend"))
    from handlers.diagnostics import DiagnosticsHandlers
    from contract_validation import validate
    from voxsub import hardware
    monkeypatch.setattr(hardware, "detect_hardware", lambda: hardware.HardwareProfile("generic cpu", 14, 20, 32))
    data=inventory.normalize_inventory({"cpu": group({"Name": "Intel Core i5-13600KF"})})
    monkeypatch.setattr(inventory, "hardware_inventory", lambda: data)
    result=DiagnosticsHandlers()._cmd_hardware_profile({})
    assert result["cpu"] == "Intel Core i5-13600KF" and result["physicalCores"] == 14
    root=json.loads((Path(__file__).resolve().parents[1] / "contracts/commands.json").read_text(encoding="utf-8"))
    validate(result, root["commands"]["hardware_profile"]["result"], root=root)


def test_environment_snapshot_reuses_same_privacy_safe_inventory(monkeypatch):
    from voxsub.diagnostic_runtime import environment_snapshot
    data=inventory.normalize_inventory({"motherboard": group({"Product": "MAG B760M MORTAR WIFI II", "SerialNumber": "SECRET-SERIAL"})})
    monkeypatch.setattr(inventory, "hardware_inventory", lambda: data)
    result=environment_snapshot()
    assert result["hardware"] == data
    assert result["devices"]["board"][0]["Product"] == "MAG B760M MORTAR WIFI II"
    assert "SECRET-SERIAL" not in json.dumps(result)
    assert result["device_inventory_status"] == "partial_or_unavailable"


def test_same_named_gpus_with_different_memory_are_not_mispaired():
    rows = [["NVIDIA GeForce RTX 3060", "8192"], ["NVIDIA GeForce RTX 3060", "12288"], ["NVIDIA GeForce RTX 4060", "8192"]]
    assert inventory._unique_gpu_memory(rows) == {"NVIDIA GeForce RTX 4060": 8}
