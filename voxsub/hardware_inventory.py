"""Read-only hardware models, separate from routing and inference verification.

One bounded, hidden CIM query. Whitelisted output never contains serial numbers,
MAC addresses, adapter identifiers, host names or user paths. Per-category failures
retain the successful categories; absence is different from unavailable telemetry.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any

GIB = 1024 ** 3
_FIELDS = {
    "cpu": ("Name", "NumberOfCores", "NumberOfLogicalProcessors"),
    "motherboard": ("Manufacturer", "Product"),
    "memory": ("Manufacturer", "PartNumber", "Capacity", "ConfiguredClockSpeed", "Speed", "SMBIOSMemoryType"),
    "gpus": ("Name", "DriverVersion"),
    "monitors": ("Name", "Manufacturer", "ProductCode", "SizeInches"),
    "disks": ("Model", "Size", "InterfaceType"),
    "sound": ("Name", "Manufacturer"),
    "network": ("Name", "Manufacturer"),
    "os": ("Caption", "Version", "BuildNumber"),
    "bios": ("Manufacturer", "SMBIOSBIOSVersion"),
    "drivers": ("DeviceName", "DeviceClass", "DriverVersion", "DriverDate", "Manufacturer"),
}
_SCRIPT = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
function Read-Group([scriptblock]$Query) {
    try { $rows=@(& $Query); return @{status= $(if($rows.Count){'ok'}else{'not_detected'});items=$rows} }
    catch { return @{status='unavailable';items=@()} }
}
function Edid-Text($chars) {
    return (-join @($chars | Where-Object {$_ -ge 32 -and $_ -le 126} | ForEach-Object {[char]$_})).Trim()
}
$data=@{}
$data.cpu=Read-Group { Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors }
$data.motherboard=Read-Group { Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product }
$data.memory=Read-Group { Get-CimInstance Win32_PhysicalMemory | Select-Object Manufacturer,PartNumber,Capacity,ConfiguredClockSpeed,Speed,SMBIOSMemoryType }
$data.gpus=Read-Group { Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion }
$data.monitors=Read-Group {
    $sizes=@{}
    try { Get-CimInstance -Namespace root/wmi WmiMonitorBasicDisplayParams | Where-Object Active | ForEach-Object {$sizes[$_.InstanceName]=$_} } catch {}
    Get-CimInstance -Namespace root/wmi WmiMonitorID | Where-Object Active | ForEach-Object {
        $size=$sizes[$_.InstanceName]; $inches=$null
        if($size -and $size.MaxHorizontalImageSize -gt 0 -and $size.MaxVerticalImageSize -gt 0) {
            $inches=[math]::Round([math]::Sqrt([math]::Pow($size.MaxHorizontalImageSize,2)+[math]::Pow($size.MaxVerticalImageSize,2))/2.54,1)
        }
        [pscustomobject]@{Name=(Edid-Text $_.UserFriendlyName);Manufacturer=(Edid-Text $_.ManufacturerName);ProductCode=(Edid-Text $_.ProductCodeID);SizeInches=$inches}
    }
}
$data.disks=Read-Group { Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,InterfaceType }
$data.sound=Read-Group { Get-CimInstance Win32_SoundDevice | Select-Object Name,Manufacturer }
$data.network=Read-Group { Get-CimInstance Win32_NetworkAdapter | Where-Object PhysicalAdapter | Select-Object Name,Manufacturer }
$data.os=Read-Group { Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber }
$data.bios=Read-Group { Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion }
$data.drivers=Read-Group { Get-CimInstance Win32_PnPSignedDriver | Select-Object -First 256 DeviceName,DeviceClass,DriverVersion,DriverDate,Manufacturer }
$data | ConvertTo-Json -Depth 5 -Compress
"""
_CACHE_LOCK = threading.Lock()
_CACHE: tuple[float, dict[str, Any]] | None = None


def clean_model_name(value: Any) -> str:
    """Keep reported product names, remove trademark clutter; never infer models."""
    text = str(value or "").replace("\x00", "")
    text = re.sub(r"\((?:R|TM)\)|[®™]", "", text, flags=re.IGNORECASE)
    text = " ".join(text.split())[:256]
    if text.casefold() in {"unknown", "undefined", "default string", "to be filled by o.e.m.", "not specified"}:
        return ""
    return text


def processor_model(fallback: str = "") -> str:
    """Windows registry exposes the product name instead of platform's CPUID label."""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                name = clean_model_name(winreg.QueryValueEx(key, "ProcessorNameString")[0])
            if name:
                return name
        except (OSError, ValueError):
            pass
    name = clean_model_name(fallback)
    if re.search(r"\bFamily\s+\d+\s+Model\s+\d+", name, re.IGNORECASE):
        return ""
    return name


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if 0 < number < 1e18 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _normalize_item(category: str, raw: dict) -> dict:
    names = {"Name", "Model", "Product", "PartNumber", "DeviceName"}
    numeric = {"Capacity", "Size", "SizeInches", "ConfiguredClockSpeed", "Speed", "SMBIOSMemoryType", "NumberOfCores", "NumberOfLogicalProcessors"}
    item = {}
    for field in _FIELDS[category]:
        value = raw.get(field)
        if field in numeric:
            item[field] = _number(value)
        elif field in names:
            item[field] = clean_model_name(value)
        else:
            item[field] = " ".join(str(value or "").split())[:256]
    return item


def normalize_inventory(raw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"checkedAt": datetime.now(timezone.utc).isoformat(), "source": "Windows CIM", "categories": {}}
    groups = raw if isinstance(raw, dict) else {}
    for category in _FIELDS:
        group = groups.get(category, {})
        group = group if isinstance(group, dict) else {}
        status = group.get("status", "unavailable")
        status = status if status in {"ok", "not_detected", "unavailable"} else "unavailable"
        rows = group.get("items", [])
        rows = [rows] if isinstance(rows, dict) else rows
        rows = rows if isinstance(rows, list) else []
        items = [_normalize_item(category, row) for row in rows[:256] if isinstance(row, dict)] if status == "ok" else []
        if status == "ok" and not items:
            status = "unavailable"  # Corrupt success payload is not proof that nothing exists.
        data["categories"][category] = {"status": status, "items": items}
    return data


def _query_cim() -> Any:
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
                            capture_output=True, timeout=15, check=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("Hardware inventory response too large")
    return json.loads(result.stdout.decode("utf-8-sig"))


def _unique_gpu_memory(rows: Any) -> dict[str, float]:
    capacities: dict[str, set[float]] = {}
    for row in rows:
        if len(row) == 2 and _number(row[1]):
            capacities.setdefault(clean_model_name(row[0]), set()).add(float(row[1]) / 1024)
    # Identical names with different capacities cannot be paired to CIM safely.
    return {name: next(iter(values)) for name, values in capacities.items() if len(values) == 1}


def _nvidia_memory() -> dict[str, float]:
    """AdapterRAM is only uint32 / unreliable; accept NVIDIA's dedicated-memory query."""
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                capture_output=True, timeout=3, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = csv.reader(io.StringIO(result.stdout.decode("utf-8", errors="replace")))
        return _unique_gpu_memory(rows)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def collect_inventory() -> dict[str, Any]:
    if os.name != "nt":
        data = normalize_inventory({})
        data["source"] = "unavailable"
        return data
    try:
        data = normalize_inventory(_query_cim())
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        data = normalize_inventory({})
    gpu = data["categories"]["gpus"]
    memory = _nvidia_memory() if gpu["items"] else {}
    for item in gpu["items"]:
        item["vramGb"] = memory.get(item["Name"])
    return data


def hardware_inventory() -> dict[str, Any]:
    """Short-lived, in-memory cache; no config/file writes and no shared mutable results."""
    global _CACHE
    with _CACHE_LOCK:
        now = time.monotonic()
        if _CACHE is None or now - _CACHE[0] >= 60:
            _CACHE = (now, collect_inventory())
        return copy.deepcopy(_CACHE[1])
