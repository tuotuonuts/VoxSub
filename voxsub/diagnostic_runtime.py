"""Read-only quick diagnostics and explicit environment snapshot. No model loading."""
from __future__ import annotations

from voxsub.language_registry import split_language_pair
from datetime import datetime, timezone
import json
import platform
from typing import Any, Mapping

from voxsub.diagnostic_privacy import safe_snapshot
from voxsub.diagnostic_trace import snapshot


def pipeline_snapshot(pipeline: Any) -> dict[str, Any]:
    if pipeline is None:
        return {"state": "not_loaded", "inference_verified": False, "queues": {}, "models": {}}
    state = getattr(pipeline, "state", "unknown")
    queues = {}
    for name in ("capture", "recognition", "context", "translation"):
        queue = getattr(pipeline, "_queue" if name == "capture" else "_" + name + "_queue", None)
        if queue is not None:
            queues[name] = {"size": queue.qsize(), "capacity": queue.maxsize}
    models = {}
    for name, attr in (("asr", "_asr"), ("translation", "_translator")):
        component = getattr(pipeline, attr, None)
        # A requested provider is not execution evidence. Runtime properties are declarations only.
        models[name] = {"loaded": component is not None,
                        "runtime": str(getattr(component, "runtime", "unknown")) if component is not None else "not_loaded",
                        "declared_provider": str(getattr(component, "provider", "unverified")) if component is not None else "unverified"}
    models["speech"] = dict(getattr(pipeline, "_speech_runtime_state", {"loaded": False, "phase": "not_loaded"}))
    return {"state": str(getattr(state, "value", state)), "queues": queues, "models": models,
            "inference_verified": False, "verification_boundary": "Loaded/provider declarations are not device execution proof",
            "recording": dict(pipeline.recording_state),
            "generation": getattr(pipeline, "config_generation", 0),
            "session_id": getattr(pipeline, "_diagnostic_session_id", ""),
            "capture": dict(getattr(pipeline, "_diagnostic_capture", {})),
            "latency": pipeline._latency.snapshot() if hasattr(pipeline, "_latency") else {"status": "not_checked"},
            "hardware_dropped_frames": "unverified",
            "requested_provider": str(getattr(pipeline, "_provider", "unverified")),
            "effective_translation": str(getattr(pipeline, "_trans_kind", "not_loaded"))}


def _selected_model_check(label: str, model_id: str, cloud: bool) -> dict[str, Any]:
    from voxsub.model_catalog import ModelMarketplace, get_model
    item = {"check": label, "status": "not_run", "detail": "cloud" if cloud else model_id,
            "impact": "当前配置的模型可用性", "suggestion": "启动任务后查看实际加载和请求结果"}
    if cloud:
        return item
    try:
        model = get_model(model_id)
        if model is None:
            item.update(status="fail", detail=model_id + "：未知模型", suggestion="在设置中选择已支持的模型")
            return item
        missing = ModelMarketplace().missing_paths(model)
        item.update(status="fail" if missing else "ok",
                    detail=model_id + ("：缺少或损坏 " + str(len(missing)) + " 个文件" if missing else "：文件存在性/大小检查通过（不代表加载成功）"),
                    suggestion="在模型广场检查并确认修复；不自动下载" if missing else "运行后确认模型加载与语言输出")
    except Exception as exc:
        item.update(status="fail", detail=type(exc).__name__, suggestion="查看日志并检查模型目录权限")
    return item


def _language_check(config: Mapping[str, Any], mode: str) -> dict[str, Any]:
    from voxsub.language_capabilities import language_capabilities
    try:
        capabilities = language_capabilities(config, mode=mode)
        source, target = split_language_pair(config.get("lang_pair", "zh-en"))
        valid = target in capabilities.get("targets", {}).get(source, ())
        return {"check": "语言兼容性", "status": "ok" if valid else "fail", "detail": source + " → " + target,
                "impact": "识别与翻译是否支持当前语言组合", "suggestion": "无需处理" if valid else "选择两个模型共同支持的语言组合"}
    except Exception as exc:
        return {"check": "语言兼容性", "status": "fail", "detail": type(exc).__name__, "suggestion": "检查模型设置"}


def quick_checks(config: Mapping[str, Any], pipeline: Any = None) -> list[dict[str, Any]]:
    from voxsub.diagnostics import _check_resources, _check_ort_providers
    from voxsub.translate.factory import kind_for_tier
    mode = str(config.get("mode", "a"))
    translate_kind = kind_for_tier(str(config.get("translate_tier", "fast")), dict(config))
    effective_model = "mt-opus-fast-builtin" if translate_kind == "opus-fast" else str(config.get("translate_model_id", ""))
    results = [_selected_model_check("识别模型", str(config.get("asr_model_id", "")), config.get("stt_provider") == "cloud"),
               _selected_model_check("翻译模型", effective_model, translate_kind == "cloud"),
               _language_check(config, mode)]
    from voxsub.speech_contract import single_model
    if single_model(config, mode):
        from voxsub.speech_runtime import runtime_status
        results = [_selected_model_check("语音翻译模型", str(config.get("speech_model_id", "")), False),
                   _language_check(config, mode), {"check": "语音翻译运行组件", **runtime_status(),
                   "impact": "文件专用，CPU；不启动独立翻译器，不采集和朗读音频", "suggestion": "缺失时请安装完整版本"}]
    results.append({"check": "音源与录音", "status": "not_run", "detail": json.dumps({"mode": mode,
                    "microphone": str(config.get("mic_device_id") or "default"), "output_device": str(config.get("loopback_device_id") or "default"),
                    "application_pid": int(config.get("capture_process_id") or 0), "save_audio": bool(config.get("record_with_translation"))}, ensure_ascii=False),
                    "impact": "仅检查选择状态，不枚举/开启采集设备", "suggestion": "开始任务后确认采集状态；硬件采集尚未验证"})
    runtime = pipeline_snapshot(pipeline)
    results.append({"check": "实际推理设备", "status": "not_run", "detail": json.dumps(runtime["models"], ensure_ascii=False),
                    "impact": "检测到设备、模型已加载都不等于模型在该设备上执行", "suggestion": "结合模型运行追踪与运行时设备证据确认；不推测 GPU/NPU"})
    resources = _check_resources()
    if resources.get("status") == "warn":
        resources = {**resources, "status": "resource_limited"}
    results.extend([resources, _check_ort_providers()])
    return results


def environment_snapshot() -> dict[str, Any]:
    """Collect model names / OS and driver versions only. No serial/host/user IDs."""
    import psutil
    from importlib.metadata import version, PackageNotFoundError
    packages = {}
    for name in ("onnxruntime-directml", "onnxruntime", "sherpa-onnx", "numpy", "transformers", "torch", "llama-cpp-python"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not_installed"
    data = {"packages": packages, "os": platform.system(), "os_version": platform.version(), "architecture": platform.machine(),
            "python": platform.python_version(), "memory_total_mb": round(psutil.virtual_memory().total / 2**20),
            "memory_available_mb": round(psutil.virtual_memory().available / 2**20), "devices": [],
            "device_inventory_status": "not_run", "scope": "Device model/driver inventory, CPU, board, BIOS and OS versions; no serials/identifiers"}
    from voxsub.hardware_inventory import hardware_inventory
    inventory = hardware_inventory()
    data["hardware"] = inventory
    groups = inventory["categories"]
    data["devices"] = {key: groups[category]["items"] for key, category in
                       (("cpu", "cpu"), ("board", "motherboard"), ("bios", "bios"), ("os", "os"), ("drivers", "drivers"))}
    data["device_inventory_status"] = "ok" if all(group["status"] != "unavailable" for group in groups.values()) else "partial_or_unavailable"
    data["scope"] = "System-reported hardware model/capacity and driver inventory; no serials, MAC, host or user IDs; inference unverified"
    return safe_snapshot(data)


def diagnostic_snapshot(config: Mapping[str, Any], pipeline: Any, *, environment: bool = False) -> dict[str, Any]:
    from voxsub import __version__
    from voxsub.logging_setup import diagnostic_session_snapshot
    import psutil
    process_memory = psutil.Process().memory_info()
    result = {"process_rss_mb": round(process_memory.rss / 2**20),
              "available_memory_mb": round(psutil.virtual_memory().available / 2**20),
              "language_detection_boundary": "Script heuristic only; code-switching, names and short utterances may cause false positives",
              "version": __version__, "checked_at": datetime.now(timezone.utc).isoformat(),
              "boundary": "Metadata only. No audio, transcript, translation body, history or credentials. Device execution may be unverified.",
              "configuration": {key: config.get(key) for key in ("file_translation_mode", "speech_model_id", "mode", "lang_pair", "asr_model_id", "translate_model_id", "stt_provider", "translate_tier", "record_with_translation", "log_limit_mb")},
              "pipeline": pipeline_snapshot(pipeline), "trace": snapshot(), "verbose_session": diagnostic_session_snapshot()}
    if environment:
        result["environment"] = environment_snapshot()
    return safe_snapshot(result)
