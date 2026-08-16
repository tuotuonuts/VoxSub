"""voxsub.router 模块测试 (M8)。

- 本机 onnxruntime-directml: providers = [DmlExecutionProvider, CPUExecutionProvider]
- select_device 的实测冒烟会真实加载模型 (ASR ~0.8s), 属可接受范围;
  确定性分支用 monkeypatch 隔离。
"""
from __future__ import annotations

import pytest

import voxsub.router as router
from voxsub.router import DeviceInfo, enumerate_devices, select_device


def test_enumerate_devices_contains_cpu() -> None:
    """onnxruntime 必含 CPUExecutionProvider (兜底执行器)。"""
    providers = [d.provider for d in enumerate_devices()]
    assert "cpu" in providers


def test_enumerate_devices_structure() -> None:
    """每个 DeviceInfo 字段合法: provider 非空, name 非空, score_ms 初值 None。"""
    devs = enumerate_devices()
    assert len(devs) >= 1
    for d in devs:
        assert isinstance(d, DeviceInfo)
        assert d.provider
        assert d.name
        assert d.score_ms is None


def test_select_device_real_returns_valid_provider() -> None:
    """真实环境下 asr 选择结果必须是合法 provider (本机期望 dml)。"""
    dev = select_device("asr")
    assert dev.provider in {"cpu", "dml", "cuda", "npu"}
    assert dev.name


def test_select_device_unknown_task_raises() -> None:
    with pytest.raises(ValueError):
        select_device("bogus-task")


def test_select_device_dml_preferred_when_available(monkeypatch) -> None:
    """有 DML 且任务模型就绪 -> 必须选 dml (降级链第一级)。"""
    monkeypatch.setattr(router, "enumerate_devices", lambda: [
        DeviceInfo("dml", "DirectML", None),
        DeviceInfo("cpu", "CPU", None),
    ])
    monkeypatch.setattr(router, "_task_model_ready", lambda task: True)
    monkeypatch.setattr(router, "_smoke_score", lambda task, provider: 12.3)
    dev = select_device("asr")
    assert dev.provider == "dml"
    assert dev.name == "DirectML"
    assert dev.score_ms == 12.3


def test_select_device_falls_back_to_cpu_when_model_missing(monkeypatch, tmp_path) -> None:
    """DML 存在但任务模型缺失 -> 降级 cpu, score_ms=None。"""
    monkeypatch.setattr(router, "enumerate_devices", lambda: [
        DeviceInfo("dml", "DirectML", None),
        DeviceInfo("cpu", "CPU", None),
    ])
    monkeypatch.setattr(router, "models_dir", lambda: tmp_path)  # 空模型目录
    dev = select_device("tts")
    assert dev.provider == "cpu"
    assert dev.score_ms is None


def test_select_device_cpu_only_when_no_dml(monkeypatch) -> None:
    """无 DML 时即使模型就绪也只能选 cpu。"""
    monkeypatch.setattr(router, "enumerate_devices", lambda: [
        DeviceInfo("cpu", "CPU", None),
    ])
    monkeypatch.setattr(router, "_task_model_ready", lambda task: True)
    monkeypatch.setattr(router, "_smoke_score", lambda task, provider: 50.0)
    dev = select_device("asr")
    assert dev.provider == "cpu"
    assert dev.score_ms == 50.0
