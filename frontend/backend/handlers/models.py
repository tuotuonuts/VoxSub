"""模型库：列表、安装、卸载、目录、导入（IPC 适配层的一个业务域）。

模型列表的硬件评估、序列化与文件扫描分开；安装/卸载等操作保持既有契约。
共享的协议与工具依赖收在 ipc_protocol / ipc_support 里。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ipc_protocol import _event, _cancel_requested
from ipc_support import _dir_size, _human_size


def _profile_for_cards(failures: list[str]) -> Any:
    from voxsub.hardware import detect_hardware  # noqa: PLC0415
    try:
        return detect_hardware()
    except Exception as error:  # noqa: BLE001 - leave models visible, mark assessment unknown
        failures.append(f"硬件推荐评估失败: {type(error).__name__}: {error}")
        return None


def _rating_for_card(model: Any, profile: Any, failures: list[str]) -> dict[str, object]:
    from voxsub.catalog_cards import card_assessment  # noqa: PLC0415
    if profile is not None:
        try:
            return card_assessment(model, profile)
        except Exception as error:  # noqa: BLE001 - isolate one model's failure
            failures.append(f"{model.id} 推荐评估失败: {type(error).__name__}: {error}")
    return {"level": "unknown", "loadPercent": None, "reason": "暂时无法评估本机配置"}


def _catalog_item(model: Any, size: int, installed: bool, installed_bytes: int,
                  recommendation: dict[str, object]) -> dict[str, Any]:
    return {
        "id": model.id,
        "name": getattr(model, "name", model.id),
        "task": getattr(model, "task", "unknown"),
        "quality": int(getattr(model, "quality_score", 0) or 0),
        "sizeLabel": getattr(model, "size_label", "") or _human_size(size),
        "sizeBytes": size,
        "installedBytes": installed_bytes,
        "installed": installed,
        "builtin": bool(getattr(model, "builtin", False)),
        "runtime": getattr(model, "runtime", "") or "",
        "license": getattr(model, "license", "") or "",
        "languages": getattr(model, "languages", "") or "",
        "description": getattr(model, "description", "") or "",
        "tags": list(getattr(model, "tags", ()) or ()),
        "officialRepo": getattr(model, "official_repo", "") or "",
        "externalRuntime": getattr(model, "external_runtime", ""),
        "usageUrl": getattr(model, "usage_url", ""),
        "recommendation": recommendation,
        # 硬件支持必须如实呈现，禁止把"未验证"显示成"可用"
        "gpuSupported": bool(getattr(model, "gpu_supported", False)),
        "igpuSupported": bool(getattr(model, "igpu_supported", False)),
        "npuSupported": bool(getattr(model, "npu_supported", False)),
        "minRamGb": float(getattr(model, "min_ram_gb", 0) or 0),
    }


def _download_for_card(downloads: Any, model: Any, installed: bool, failures: list[str]) -> dict | None:
    if installed:
        return None
    try:
        return downloads.snapshot(model)
    except Exception as error:  # noqa: BLE001 - one damaged task must not hide the entire catalog
        failures.append(f"{model.id} 下载状态读取失败: {error}")
        return None


class ModelsHandlers:
    """模型库：列表、安装、卸载、目录、导入。"""

    def _cmd_list_models(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.model_catalog import CATALOG  # noqa: PLC0415

        marketplace = self._marketplace(args)

        downloads = self._downloads_for(args)
        items = []
        failures: list[str] = []
        profile = _profile_for_cards(failures)
        for model in CATALOG:
            size = int(getattr(model, "download_bytes", 0) or 0)

            # is_installed 收的是 ModelSpec 对象。先前误传 model.id（字符串）会抛
            # AttributeError，而当时的 except 把它吞成 installed=False —— 界面因此
            # 静默显示"全部未安装"。这里改为：失败要留痕，绝不假装成正常结果。
            try:
                installed = bool(marketplace.is_installed(model))
            except Exception as error:  # noqa: BLE001 - 需上报而不是吞掉
                installed = False
                failures.append(f"{model.id}: {type(error).__name__}: {error}")

            installed_bytes = 0
            if installed:
                try:
                    directory = marketplace.available_model_dir(model)
                    installed_bytes = _dir_size(directory)
                except Exception as error:  # noqa: BLE001
                    failures.append(f"{model.id} 体积统计失败: {error}")

            recommendation = _rating_for_card(model, profile, failures)
            item = _catalog_item(model, size, installed, installed_bytes, recommendation)
            if model.task == "speech":
                from voxsub.speech_runtime import runtime_status
                item["runtimeAvailable"] = not model.external_runtime and runtime_status()["status"] != "fail"
            item["download"] = _download_for_card(downloads, model, installed, failures)
            items.append(item)

        for line in failures:
            print(f"[list_models] {line}", file=sys.stderr)

        from voxsub.config_store import ConfigStore
        config = ConfigStore().load()
        selected = [str(config.get(key, "")) for key in
                    ("asr_model_id", "translate_model_id", "ocr_model_id", "tts_model_id_zh", "tts_model_id_en")]
        if config.get("file_translation_mode") == "single":
            selected.append(str(config.get("speech_model_id", "")))
        return {
            "selectedModelIds": selected,
            "models": items,
            "modelsRoot": str(marketplace.models_dir.resolve()),
            "lookupRoots": [str(p) for p in marketplace._lookup_roots],
            "diagnostics": failures,
        }

    def _downloads_for(self, args: dict[str, Any]) -> Any:
        from voxsub.model_downloads import ModelDownloads  # noqa: PLC0415
        marketplace = self._marketplace(args)
        # The writer always targets exactly this root. List/select may look up legacy roots,
        # but an explicit download must not silently reuse a model in a different directory.
        if getattr(marketplace, "_uses_default_root", False):
            from voxsub.model_catalog import ModelMarketplace  # noqa: PLC0415
            marketplace = ModelMarketplace(marketplace.models_dir)
        key = str(marketplace.models_dir.resolve())
        with self._lock:
            if key not in self._model_downloads:
                self._model_downloads[key] = ModelDownloads(marketplace, self._emit_model_download)
            return self._model_downloads[key]

    @staticmethod
    def _emit_model_download(state: dict) -> None:
        _event("download", modelId=state["modelId"], completed=state["completed"],
               total=state["total"], stage=state["stage"], status=state["status"],
               modelsRoot=state["modelsRoot"], token=state["token"], revision=state["revision"],
               source=state["source"], error=state["error"])

    def _cmd_prepare_model_download(self, args: dict[str, Any]) -> dict[str, Any]:
        model = self._spec(str(args.get("model_id", "")))
        downloads = self._downloads_for(args)
        if self._marketplace(args).is_installed(model):
            return {"model_id": model.id, "download": None}
        state = downloads.prepare(model, str(args.get("source", "auto")))
        return {"model_id": model.id, "download": state}

    def _cmd_install_model(self, args: dict[str, Any]) -> dict[str, Any]:
        model = self._spec(str(args.get("model_id", "")))
        downloads = self._downloads_for(args)
        token = str(args.get("token") or "")
        if not token:
            if self._marketplace(args).is_installed(model):
                return {"model_id": model.id, "download": None}
            token = downloads.prepare(model, str(args.get("source", "auto")))["token"]
        state = downloads.run(model, token, _cancel_requested)
        return {"model_id": model.id, "download": state}

    def _cmd_pause_model_download(self, args: dict[str, Any]) -> dict[str, Any]:
        model = self._spec(str(args.get("model_id", "")))
        state = self._downloads_for(args).pause(model, str(args.get("token", "")))
        return {"model_id": model.id, "download": state}

    def _cmd_delete_model_download(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("confirm") is not True:
            raise ValueError("删除未完成下载须确认")
        model = self._spec(str(args.get("model_id", "")))
        state = self._downloads_for(args).delete(model, str(args.get("token", "")))
        return {"model_id": model.id, "download": state}

    def _cmd_uninstall_model(self, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("model_id", ""))
        marketplace = self._marketplace(args)
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None and not pipeline._may_replace_resources():
            raise RuntimeError("请先结束当前任务，再卸载模型")
        marketplace.uninstall(self._spec(model_id))
        return {"model_id": model_id}

    def _cmd_model_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("model_id", ""))
        marketplace = self._marketplace(args)
        spec = self._spec(model_id)
        # 已安装时给真实所在目录（可能在旧存储位置），未安装时给即将写入的位置
        directory = (
            marketplace.available_model_dir(spec)
            if marketplace.is_installed(spec)
            else marketplace.model_dir(spec)
        )
        return {"path": str(directory), "installed": marketplace.is_installed(spec)}

    def _cmd_import_models(self, args: dict[str, Any]) -> dict[str, Any]:
        """把别处的模型并入当前模型目录（Qt 版「迁移已有模型」）。"""
        # migrate_models 定义在 voxsub.model_storage，**不在** model_catalog。
        # 之前写错模块名，打包版实测直接 ImportError：
        #   ImportError: cannot import name 'migrate_models' from 'voxsub.model_catalog'
        from voxsub.model_storage import migrate_models  # noqa: PLC0415

        source = Path(str(args.get("source", "")))
        if not source.is_dir():
            raise FileNotFoundError(f"源目录不存在：{source}")

        destination = Path(
            str(args.get("destination") or self._marketplace(args).models_dir)
        )
        destination.mkdir(parents=True, exist_ok=True)

        result = migrate_models(source, destination)
        return {
            "moved": int(getattr(result, "moved_paths", 0) or 0),
            "skipped": int(getattr(result, "kept_existing_paths", 0) or 0),
            "destination": str(destination),
        }
