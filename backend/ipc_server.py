#!/usr/bin/env python
"""语幕 VoxSub — Electron 前端的后端 IPC 服务。

契约（与 src/main/backend.ts 对应）：
  · stdin  收命令：每行一个 JSON ``{"id": 1, "command": "start", "args": {...}}``
  · stdout 发事件与应答：每行一个 JSON
      - 应答：``{"id": 1, "ok": true, "data": ...}``
      - 事件：``{"event": "utterance", "source": "...", "translation": "..."}``
  · stderr  诊断日志，由 Electron 侧转发到界面日志区

设计约束：
  · 本文件**不修改 voxsub 包任何内容**，只做协议适配
  · stdout 必须只输出 JSON。第三方库的 print 会污染协议，
    因此启动时把 sys.stdout 换成 stderr，真实 stdout 由 _emit 独占。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

# ---- 接入现有 voxsub 包 ----------------------------------------------------
def _install_backend_path() -> str:
    """约定：voxsub 包一行不改，通过 VOXSUB_ROOT 指向其仓库根目录。"""
    root = os.environ.get("VOXSUB_ROOT")
    if not root:
        # 开发期布局：与本项目同级的 VoxSub
        root = str(Path(__file__).resolve().parents[2] / "VoxSub")
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


VOXSUB_ROOT = _install_backend_path()

# ---- 保护协议通道 -----------------------------------------------------------
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr


def _emit(payload: dict[str, Any]) -> None:
    try:
        _PROTOCOL_OUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _PROTOCOL_OUT.flush()
    except (BrokenPipeError, ValueError):
        raise SystemExit(0) from None


def _event(kind: str, **fields: Any) -> None:
    _emit({"event": kind, **fields})


def _exit_process() -> None:
    """在计时器线程里结束进程（lambda 里不能 raise）。"""
    raise SystemExit(0)


def _dir_size(directory: Path) -> int:
    """递归求目录占用字节数；不可读的条目跳过（不因单文件失败丢掉整体统计）。"""
    total = 0
    try:
        for entry in directory.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


# ---------------------------------------------------------------- OCR 渲染辅助

def _box_to_list(box: Any) -> list[int]:
    """把 OCR 的框统一成 [left, top, right, bottom]。

    两种来源都要兼容：voxsub 的 OcrBox 是带 left/top/right/bottom 属性的对象；
    某些 RapidOCR 版本给的是四点列表 [[x,y], ...]。前端按扁平四元组消费。
    """
    if box is None:
        return []
    if all(hasattr(box, name) for name in ("left", "top", "right", "bottom")):
        return [int(box.left), int(box.top), int(box.right), int(box.bottom)]
    if isinstance(box, (list, tuple)) and box and isinstance(box[0], (list, tuple)):
        xs = [float(point[0]) for point in box if len(point) >= 2]
        ys = [float(point[1]) for point in box if len(point) >= 2]
        if xs and ys:
            return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        return [int(v) for v in box[:4]]
    return []


def _normalize_box(box: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    """把框统一成 (left, top, right, bottom) 并夹到图像范围内。"""
    if all(hasattr(box, name) for name in ("left", "top", "right", "bottom")):
        flat = [int(box.left), int(box.top), int(box.right), int(box.bottom)]
    elif isinstance(box, (list, tuple)) and len(box) >= 4:
        flat = _box_to_list(box)
    else:
        return None

    if len(flat) < 4:
        return None

    left = max(0, min(width - 1, flat[0]))
    top = max(0, min(height - 1, flat[1]))
    right = max(left + 1, min(width, flat[2]))
    bottom = max(top + 1, min(height, flat[3]))
    return left, top, right, bottom


def _sample_background(image: Any, rect: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """从原图采样框的背景色（Qt 版同款取点：四角 + 上下边中点，求均值）。"""
    left, top, right, bottom = rect
    width, height = image.size
    mid_x = (left + right) // 2
    points = (
        (left, top),
        (right - 1, top),
        (left, bottom - 1),
        (right - 1, bottom - 1),
        (mid_x, top),
        (mid_x, bottom - 1),
    )
    pixels = [
        image.getpixel((max(0, min(width - 1, x)), max(0, min(height - 1, y))))
        for x, y in points
    ]
    count = len(pixels)
    return (
        sum(pixel[0] for pixel in pixels) // count,
        sum(pixel[1] for pixel in pixels) // count,
        sum(pixel[2] for pixel in pixels) // count,
    )


def _luminance(color: tuple[int, int, int]) -> float:
    """感知亮度（Rec.709 系数），与 Qt 版 _contrasting_text 的判据一致。"""
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


def _load_font(size: int) -> Any:
    """按可用字体依次尝试；找不到就退回 PIL 内置位图字体。"""
    from PIL import ImageFont  # noqa: PLC0415

    candidates = (
        r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _resolve_models_root() -> Path:
    """复用应用自己的模型根目录解析。

    刻意不硬编码 %LOCALAPPDATA%：用户改过存储位置、或将来做便携版时，
    硬编码会让界面静默显示"全部未安装"——这类故障最难排查。
    """
    try:
        from voxsub.paths import resolve_models_root  # noqa: PLC0415

        return Path(resolve_models_root())
    except (ImportError, AttributeError):
        pass
    for module_name in ("voxsub.diagnostics", "voxsub.router", "voxsub.asr"):
        try:
            module = __import__(module_name, fromlist=["models_dir"])
            return Path(module.models_dir())
        except (ImportError, AttributeError):
            continue
    # 最后兜底：与应用默认布局一致
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(local) / "VoxSub" / "models"


class BackendService:
    """把 voxsub 的能力翻译成协议命令。

    命令按功能域分组，命名与 Qt 版 UI 的控件语义一致，
    Electron 侧不需要理解 voxsub 的内部结构。
    """

    def __init__(self) -> None:
        self._pipeline: Any = None
        self._lock = threading.RLock()
        self._log_buffer: list[dict[str, Any]] = []
        self._log_sink_installed = False

    # ------------------------------------------------------------------ 基础设施
    def ensure_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        from voxsub.pipeline import Pipeline  # noqa: PLC0415

        pipeline = Pipeline()
        pipeline.on_status(lambda text: _event("status", text=str(text)))
        pipeline.on_utterance(
            lambda src, dst: _event("utterance", source=src, translation=dst))
        pipeline.on_partial(lambda text: _event("partial", text=str(text)))
        pipeline.on_draft(
            lambda src, dst: _event("draft", source=src, translation=dst))
        pipeline.on_progress(
            lambda done, total, stage: _event(
                "progress", completed=done, total=total, stage=stage))
        self._pipeline = pipeline
        return pipeline

    def _install_log_sink(self) -> None:
        """把 voxsub 的日志钩到协议事件上，供诊断页实时日志使用。"""
        if self._log_sink_installed:
            return
        try:
            from voxsub.logging_setup import get_logger  # noqa: PLC0415

            logger = get_logger("ipc")

            class _Handler:
                def write(self, text: str) -> None:  # pragma: no cover - 适配器
                    line = str(text).rstrip()
                    if not line:
                        return
                    self._push(line)

                def _push(self, line: str) -> None:
                    entry = {"ts": _now_iso(), "level": _guess_level(line),
                             "message": line}
                    self._buffer.append(entry)
                    del self._buffer[:-500]
                    _event("log", **entry)

            # 复用 logging 的 handler 协议而不是重造轮子
            import logging  # noqa: PLC0415

            self._buffer: list[dict[str, Any]] = []

            class _Bridge(logging.Handler):
                def __init__(self, outer: "BackendService") -> None:
                    super().__init__()
                    self._outer = outer

                def emit(self, record: logging.LogRecord) -> None:
                    try:
                        message = self.format(record)
                    except Exception:  # noqa: BLE001
                        return
                    entry = {"ts": _now_iso(), "level": record.levelname,
                             "message": message}
                    self._outer._log_buffer.append(entry)  # noqa: SLF001
                    del self._outer._log_buffer[:-500]  # noqa: SLF001
                    _event("log", **entry)

            handler = _Bridge(self)
            handler.setLevel(logging.INFO)
            handler.setFormatter(
                logging.Formatter("%(name)s: %(message)s"))
            logging.getLogger().addHandler(handler)
            self._log_sink_installed = True
        except Exception:  # noqa: BLE001 - 日志桥失败不能阻断后端
            _event("log", level="warning",
                   message="日志桥安装失败", ts=_now_iso())

    # ------------------------------------------------------------------ 分发
    def handle(self, command: str, args: Any) -> Any:
        args = args or {}
        if command == "ping":
            from voxsub import __version__  # noqa: PLC0415

            return {"version": __version__}

        if command == "shutdown":
            self.close()
            threading.Timer(0.2, _exit_process).start()
            return None

        handler = getattr(self, f"_cmd_{command}", None)
        if handler is None:
            raise ValueError(f"未知命令: {command}")

        # 少数命令不需要 pipeline（配置、目录查询等）
        if getattr(handler, "_needs_pipeline", True):
            return handler(self.ensure_pipeline(), args)
        return handler(args)

    # ================================================================== 会话控制
    def _cmd_start(self, pipeline: Any, _args: dict[str, Any]) -> None:
        pipeline.start()
        # 通知界面"新的会话开始"：前端据此重置时间基准（导出 SRT 需要相对时间）
        _event("session", action="start")

    def _cmd_stop(self, pipeline: Any, _args: dict[str, Any]) -> None:
        pipeline.stop()
        _event("session", action="stop")

    def _cmd_pause(self, pipeline: Any, _args: dict[str, Any]) -> None:
        pipeline.pause()

    def _cmd_resume(self, pipeline: Any, _args: dict[str, Any]) -> None:
        pipeline.resume()

    def _cmd_state(self, pipeline: Any, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "running": bool(pipeline.is_running()),
            "paused": bool(pipeline.is_paused()),
            "mode": str(pipeline.mode),
            "state": str(getattr(pipeline.state, "value", pipeline.state)),
        }

    # ================================================================== 模式与语言
    def _cmd_set_mode(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_mode(str(args.get("mode", "a")))

    def _cmd_set_langs(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_langs(str(args.get("source", "auto")),
                           str(args.get("target", "zh")))

    def _cmd_set_input_file(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_input_file(str(args.get("path", "")))

    # ================================================================== 音频设备
    def _cmd_list_audio_devices(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.audio import list_loopbacks, list_microphones  # noqa: PLC0415

        mics = []
        for device in list_microphones(include_loopback=False):
            mics.append({"id": str(getattr(device, "device_id", "") or device.name),
                         "name": device.name, "kind": "mic"})
        loops = []
        for device in list_loopbacks():
            loops.append({"id": str(getattr(device, "device_id", "") or device.name),
                          "name": device.name, "kind": "loopback"})
        return {"microphones": mics, "loopbacks": loops}

    def _cmd_list_capture_targets(self, args: dict[str, Any]) -> dict[str, Any]:
        """可捕获的可见窗口（用于 B 模式按应用隔离）。"""
        try:
            from voxsub.ui.view_models import list_capture_targets  # noqa: PLC0415

            targets = list_capture_targets()  # type: ignore[attr-defined]
            return {"targets": targets}
        except Exception:  # noqa: BLE001 - 接口不存在时退回空列表
            return {"targets": []}

    def _cmd_set_audio_devices(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_audio_devices(
            str(args.get("microphone", "") or ""),
            str(args.get("loopback", "") or ""))

    def _cmd_set_capture_process(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_capture_process(int(args.get("pid", 0) or 0),
                                     str(args.get("title", "") or ""))

    # ================================================================== STT / 翻译
    def _cmd_set_stt(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_stt(str(args.get("provider", "local")),
                         args.get("config") or {})

    def _cmd_set_translator(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_translator(str(args.get("kind", "opus-fast")),
                                args.get("config") or {})

    def _cmd_set_asr_model(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_asr_model(str(args.get("model_id", "")))

    def _cmd_set_asr_tuning(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_asr_tuning(args.get("tuning") or {})

    def _cmd_set_tts(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_tts(bool(args.get("enabled", False)))

    def _cmd_set_tts_models(self, pipeline: Any, args: dict[str, Any]) -> None:
        models = args.get("models") or {}
        pipeline.set_tts_models({str(k): str(v) for k, v in models.items()})

    def _cmd_set_recording(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_recording(bool(args.get("enabled", False)),
                               args.get("directory"))

    def _cmd_last_recording(self, pipeline: Any, _args: dict[str, Any]) -> dict[str, Any]:
        path = pipeline.last_recording_path()
        return {"path": str(path) if path else None}

    # ================================================================== 模型广场
    def _marketplace(self, args: dict[str, Any]) -> Any:
        """构造 ModelMarketplace。

        注意 models_root 传 None（而不是解析后的绝对路径）：ModelMarketplace
        在 None 时才启用"多根目录查找"，能同时看到新存储位置（D:\\VoxSub\\Models）
        与旧的 LOCALAPPDATA 位置。传显式路径会把查找收窄成一个目录。
        """
        from voxsub.model_catalog import ModelMarketplace  # noqa: PLC0415

        root = args.get("models_root")
        return ModelMarketplace(Path(root) if root else None)

    def _spec(self, model_id: str) -> Any:
        """id → ModelSpec。所有 marketplace 方法都收 spec 对象而非字符串。"""
        from voxsub.model_catalog import get_model  # noqa: PLC0415

        spec = get_model(model_id)
        if spec is None:
            raise KeyError(f"未知模型 id：{model_id}")
        return spec

    def _cmd_list_models(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.model_catalog import CATALOG  # noqa: PLC0415

        marketplace = self._marketplace(args)

        items = []
        failures: list[str] = []
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

            items.append({
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
                # 硬件支持必须如实呈现，禁止把"未验证"显示成"可用"
                "gpuSupported": bool(getattr(model, "gpu_supported", False)),
                "igpuSupported": bool(getattr(model, "igpu_supported", False)),
                "npuSupported": bool(getattr(model, "npu_supported", False)),
                "minRamGb": float(getattr(model, "min_ram_gb", 0) or 0),
            })

        for line in failures:
            print(f"[list_models] {line}", file=sys.stderr)

        return {
            "models": items,
            "modelsRoot": str(marketplace.models_dir),
            "lookupRoots": [str(p) for p in marketplace._lookup_roots],
            "diagnostics": failures,
        }

    def _cmd_install_model(self, pipeline: Any, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("model_id", ""))
        marketplace = self._marketplace(args)
        spec = self._spec(model_id)

        def _progress(done: int, total: int, stage: str) -> None:
            _event("download", modelId=model_id, completed=done,
                   total=total, stage=str(stage))

        marketplace.install(spec, progress_callback=_progress)
        return {"model_id": model_id}

    def _cmd_uninstall_model(self, args: dict[str, Any]) -> dict[str, Any]:
        model_id = str(args.get("model_id", ""))
        marketplace = self._marketplace(args)
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

    # ================================================================== 配置
    def _cmd_get_config(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.config_store import ConfigStore  # noqa: PLC0415

        return dict(ConfigStore().load())

    def _cmd_set_config(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.config_store import ConfigStore  # noqa: PLC0415

        updates = args.get("updates") or {}
        store = ConfigStore()
        store.update({str(k): v for k, v in updates.items()})
        return dict(store.load())

    # ================================================================== 诊断
    def _cmd_run_self_check(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.diagnostics import run_self_check  # noqa: PLC0415

        results = []
        for item in run_self_check():
            results.append({
                "check": str(item.get("check", "")),
                "status": str(item.get("status", "")),
                "detail": str(item.get("detail", "")),
            })
        return {"results": results}

    def _cmd_export_diagnostics(self, args: dict[str, Any]) -> dict[str, Any]:
        """导出诊断报告；可附带日志文本（诊断页「导出日志」）。"""
        from voxsub.diagnostics import export_report  # noqa: PLC0415

        path = Path(str(args.get("path", "")))
        text = export_report()

        # 日志导出复用同一入口：把日志附在报告之后，而不是另建命令
        log_text = str(args.get("log_text") or "")
        if log_text:
            text = f"{text}\n\n{'=' * 60}\n日志快照\n{'=' * 60}\n{log_text}\n"

        if path.parent and str(path) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return {"path": str(path), "bytes": len(text.encode("utf-8"))}
        return {"text": text}

    def _cmd_recent_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        """最近的日志。

        source="memory"（默认）读本进程缓冲，响应快、只含本次运行；
        source="file" 读磁盘 voxsub.log 的尾部，能拿到历史运行记录——
        排障时用户要的通常是后者（崩溃发生在下次启动之前）。
        """
        limit = int(args.get("limit", 200) or 200)
        source = str(args.get("source", "memory"))

        if source == "file":
            from voxsub.logging_setup import tail_log_file  # noqa: PLC0415

            text = tail_log_file(limit)
            return {"text": text, "source": "file", "lines": len(text.splitlines())}

        return {"logs": self._log_buffer[-limit:], "source": "memory"}

    def _cmd_clear_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        """清除本机日志文件（保留模型、配置、凭据、已导出报告）。

        与 Qt 版「清除本机日志」语义一致：活动日志就地截断（应用可继续写），
        历史轮转文件删除。
        """
        from voxsub.logging_setup import clear_local_logs  # noqa: PLC0415

        result = clear_local_logs()
        self._log_buffer.clear()
        return dict(result) if isinstance(result, dict) else {"cleared": True}

    def _cmd_log_path(self, args: dict[str, Any]) -> dict[str, Any]:
        """日志文件位置，供界面「打开日志所在文件夹」。"""
        try:
            from voxsub.logging_setup import _log_dir  # noqa: PLC0415

            return {"path": str(_log_dir())}
        except (ImportError, AttributeError):
            local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            return {"path": str(Path(local) / "VoxSub" / "logs")}

    def _cmd_import_models(self, args: dict[str, Any]) -> dict[str, Any]:
        """把别处的模型并入当前模型目录（Qt 版「迁移已有模型」）。"""
        from voxsub.model_catalog import migrate_models  # noqa: PLC0415

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

    def _cmd_release_notes(self, args: dict[str, Any]) -> dict[str, Any]:
        """更新日志（默认只回最近一版；include_history=True 回全部）。"""
        from voxsub.ui.release_notes import RELEASE_HISTORY  # noqa: PLC0415

        english = str(args.get("language", "zh")).startswith("en")
        include_history = bool(args.get("include_history", False))
        notes = list(RELEASE_HISTORY)
        if not include_history:
            notes = notes[:1]

        items = []
        for note in notes:
            title = note.title_en if english else note.title_zh
            body_items = note.items_en if english else note.items_zh
            items.append({
                "version": note.version,
                "title": title,
                "body": "\n".join(f"· {line}" for line in body_items),
            })
        return {"notes": items, "total": len(RELEASE_HISTORY)}

    def _cmd_list_devices(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.router import enumerate_devices  # noqa: PLC0415

        devices = []
        for device in enumerate_devices():
            devices.append({
                "provider": str(device.provider),
                "name": str(device.name),
                "kind": str(getattr(device, "kind", "")),
                "scoreMs": getattr(device, "score_ms", None),
            })
        return {"devices": devices}

    def _cmd_hardware_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.hardware import detect_hardware  # noqa: PLC0415

        profile = detect_hardware()
        return {
            "cpu": str(profile.cpu_name),
            "physicalCores": int(profile.physical_cores),
            "logicalCores": int(profile.logical_cores),
            "ramGb": float(profile.ram_gb),
            "gpu": str(profile.gpu_name),
            "vramGb": float(profile.vram_gb),
            "gpuProvider": str(profile.gpu_provider),
            "npu": str(profile.npu_name),
        }

    # ================================================================== 文件字幕
    def _cmd_export_subtitles(self, args: dict[str, Any]) -> dict[str, Any]:
        """导出字幕：格式由扩展名决定（.srt/.vtt/.txt）。

        字段名注意：SubtitleLine 用的是 text / translation / ts_ms，
        不是 source / start_ms / end_ms（写错会在导出时才炸，静态检查看不出来）。
        """
        from voxsub.subtitles import SubtitleLine, SubtitleExporter  # noqa: PLC0415

        out = Path(str(args.get("path", "")))
        raw = args.get("lines") or []

        lines: list[Any] = []
        for index, item in enumerate(raw):
            # 逐句时间戳：调用方给了就用，没给就按序号推进（30s/句），
            # 保证 SRT 的时间轴单调递增而不是全 0。
            ts_ms = item.get("tsMs")
            if ts_ms is None:
                ts_ms = int(item.get("startMs") or index * 30_000)
            lines.append(
                SubtitleLine(
                    text=str(item.get("source", "")),
                    translation=str(item.get("translation", "")),
                    ts_ms=int(ts_ms),
                    is_final=bool(item.get("isFinal", True)),
                )
            )

        out.parent.mkdir(parents=True, exist_ok=True)
        suffix = out.suffix.lower()
        if suffix == ".txt":
            SubtitleExporter.write_txt(lines, out)
        elif suffix == ".vtt":
            SubtitleExporter.write_vtt(lines, out)
        else:
            SubtitleExporter.write_srt(lines, out)
        return {"path": str(out), "count": len(lines)}

    # ================================================================== OCR
    def _cmd_ocr_recognize(self, args: dict[str, Any]) -> dict[str, Any]:
        """对一张图片做识别（可选翻译），像素只在本机内存处理。

        返回行级数据（文本 + 框 + 译文），供前端做预览与覆盖渲染。
        译文按行翻译：整段送出去会让模型重排语序，覆盖回原框时对不上位置。
        """
        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        from voxsub.ocr import RapidOcrEngine  # noqa: PLC0415

        image_path = Path(str(args.get("path", "")))
        with Image.open(image_path) as handle:
            frame = np.asarray(handle.convert("RGB"))

        import time  # noqa: PLC0415

        started = time.monotonic()
        engine = RapidOcrEngine()
        result = engine.recognize(frame)
        ocr_ms = int((time.monotonic() - started) * 1000)

        raw_lines = list(getattr(result, "lines", ()) or ())
        lines = []
        for line in raw_lines:
            # OcrBox 是带 left/top/right/bottom 的对象，不是可迭代序列
            lines.append({
                "text": str(getattr(line, "text", "")),
                "box": _box_to_list(getattr(line, "box", None)),
                "translation": "",
            })

        # 翻译（可选）：逐行送，避免语序重排导致框位错配
        translate_ms = 0
        translator = self._translator()
        if translator is not None and str(args.get("translate", True)).lower() != "false":
            source = str(args.get("source", "auto"))
            target = str(args.get("target", "zh"))
            started = time.monotonic()
            for item in lines:
                if not item["text"].strip():
                    continue
                try:
                    item["translation"] = str(
                        translator.translate(item["text"], source, target) or "")
                except Exception as error:  # noqa: BLE001 - 单行失败不丢整张
                    item["translation"] = ""
                    print(f"[ocr] 行翻译失败: {error}", file=sys.stderr)
            translate_ms = int((time.monotonic() - started) * 1000)

        return {
            "text": "\n".join(item["text"] for item in lines),
            "translation": "\n".join(item["translation"] for item in lines),
            "lines": lines,
            "ocrElapsedMs": ocr_ms,
            "translateElapsedMs": translate_ms,
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
            "sourcePath": str(image_path),
        }

    def _cmd_copy_file(self, args: dict[str, Any]) -> dict[str, Any]:
        """复制文件（导出译后图片等）。目标已存在时覆盖。"""
        import shutil  # noqa: PLC0415

        source = Path(str(args.get("source", "")))
        target = Path(str(args.get("target", "")))
        if not source.is_file():
            raise FileNotFoundError(f"源文件不存在：{source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return {"path": str(target), "bytes": target.stat().st_size}

    def _cmd_ocr_cache_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        """OCR 临时图片目录（译后图与截图落盘位置，供界面拼输出路径）。"""
        try:
            from voxsub.ocr_cache import resolve_ocr_cache_root  # noqa: PLC0415

            path = resolve_ocr_cache_root()
            path.mkdir(parents=True, exist_ok=True)
            return {"path": str(path)}
        except Exception as error:  # noqa: BLE001 - 缓存目录不可用时给明确兜底
            print(f"[ocr] 缓存目录解析失败，改用兜底路径: {error}", file=sys.stderr)
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        fallback = Path(local) / "VoxSub" / "cache" / "ocr"
        fallback.mkdir(parents=True, exist_ok=True)
        return {"path": str(fallback), "fallback": True}

    def _cmd_render_ocr_image(self, args: dict[str, Any]) -> dict[str, Any]:
        """把译文画回原图，生成「译后图片」（Qt 版 render_translated_image 的等价实现）。

        为什么不用 Qt 那份：它在 voxsub/ui/ 里且依赖 QImage/QPainter。
        Electron 版没有 Qt，这里用 PIL 重写同一套算法：
          · 背景色从原图对应框的边框采样（取上下边中点与四角，求均值）
          · 文字颜色按背景亮度在浅/深之间二选一
          · 圆角矩形填充 + 内缩绘制文字
        """
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

        source_path = Path(str(args.get("source", "")))
        target_path = Path(str(args.get("target", "")))
        raw = args.get("lines") or []

        with Image.open(source_path) as handle:
            canvas = handle.convert("RGB").copy()

        if not raw:
            canvas.save(target_path)
            return {"path": str(target_path), "lines": 0}

        draw = ImageDraw.Draw(canvas)
        width, height = canvas.size
        painted = 0

        for item in raw:
            text = str(item.get("translation") or "").strip()
            box = _normalize_box(item.get("box"), width, height)
            if not text or box is None:
                continue
            left, top, right, bottom = box
            if right - left < 4 or bottom - top < 4:
                continue

            background = _sample_background(canvas, (left, top, right, bottom))
            text_color = (17, 24, 39) if _luminance(background) >= 150 else (249, 250, 251)

            radius = min(7, (bottom - top) // 3)
            draw.rounded_rectangle((left, top, right - 1, bottom - 1),
                                   radius=max(0, radius), fill=background)

            # 字号以框高为基准，逐级缩小直到文字能放进框内
            size = max(8, int((bottom - top) * 0.68))
            font = _load_font(size)
            inset = max(2, (bottom - top) // 8)
            while size > 8:
                measured = draw.textbbox((0, 0), text, font=font)
                if (measured[2] - measured[0]) <= (right - left - 2 * inset):
                    break
                size -= 1
                font = _load_font(size)

            draw.multiline_text(
                (left + inset, top + inset),
                text,
                font=font,
                fill=text_color,
                spacing=max(1, size // 5),
            )
            painted += 1

        target_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target_path)
        return {"path": str(target_path), "lines": painted, "width": width, "height": height}

    def _translator(self) -> Any:
        """取当前翻译器；未启动会话时按配置现建一个。

        OCR 是独立工作区：用户可能没点「开始」就直接框选屏幕，
        这时不能因为 pipeline 未启动就静默不翻译。
        """
        pipeline = self._pipeline
        if pipeline is not None:
            translator = getattr(pipeline, "translator", None)
            if isinstance(translator, tuple) and len(translator) > 1 and translator[1]:
                return translator[1]
            if translator is not None and not isinstance(translator, tuple):
                return translator

        if getattr(self, "_ocr_translator", None) is not None:
            return self._ocr_translator

        try:
            from voxsub.pipeline import _load_translator  # noqa: PLC0415

            config = {}
            try:
                from voxsub.config_store import ConfigStore  # noqa: PLC0415

                config = dict(ConfigStore().load())
            except Exception:  # noqa: BLE001 - 拿不到配置就用默认
                config = {}

            kind = str(config.get("translator_kind") or "opus-fast")
            result = _load_translator(kind, config)
            # _load_translator 返回 (translator, effective_kind) 或裸对象
            translator = result[0] if isinstance(result, tuple) else result
            if translator is not None:
                self._ocr_translator = translator
            return translator
        except Exception as error:  # noqa: BLE001 - 失败只降级，不阻断识别
            print(f"[ocr] 翻译器不可用，仅返回识别结果: {error}", file=sys.stderr)
            return None

    def _cmd_ocr_translate(self, pipeline: Any, args: dict[str, Any]) -> dict[str, Any]:
        """把已识别的文本交给当前翻译配置。"""
        text = str(args.get("text", ""))
        translator = pipeline.translator[1]
        if translator is None or not text:
            return {"translation": ""}
        translation = translator.translate(
            text,
            str(args.get("source", "auto")),
            str(args.get("target", "zh")))
        return {"translation": translation}

    # ================================================================== 关闭
    def close(self) -> None:
        with self._lock:
            pipeline, self._pipeline = self._pipeline, None
        if pipeline is None:
            return
        for action in ("stop", "close"):
            method = getattr(pipeline, action, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception:  # noqa: BLE001 - 退出路径尽力而为
                _event("log", level="error", ts=_now_iso(),
                       message=traceback.format_exc())


# ---- 标记哪些命令不需要 pipeline --------------------------------------------
for _name in (
    "list_models", "uninstall_model", "model_dir", "get_config", "set_config",
    "run_self_check", "export_diagnostics", "recent_logs",
    "clear_logs", "log_path", "import_models", "release_notes", "render_ocr_image", "copy_file", "ocr_cache_dir",
    "list_devices", "hardware_profile", "list_audio_devices",
    "list_capture_targets", "export_subtitles", "ocr_recognize",
):
    _fn = getattr(BackendService, f"_cmd_{_name}", None)
    if _fn is not None:
        _fn._needs_pipeline = False  # type: ignore[attr-defined]


def _now_iso() -> str:
    import datetime  # noqa: PLC0415

    return datetime.datetime.now().isoformat(timespec="seconds")


def _guess_level(line: str) -> str:
    upper = line.upper()
    for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        if level in upper:
            return level
    return "INFO"


def _human_size(num: int) -> str:
    if num <= 0:
        return "内置"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def main() -> int:
    service = BackendService()
    service._install_log_sink()  # noqa: SLF001
    try:
        from voxsub import __version__  # noqa: PLC0415

        _event("ready", version=__version__)
    except Exception as exc:  # noqa: BLE001
        _event("error", message=f"后端初始化失败: {type(exc).__name__}: {exc}")
        return 2

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _event("log", level="WARNING",
                   message=f"无法解析的输入: {line[:200]}", ts=_now_iso())
            continue

        req_id = message.get("id")
        command = str(message.get("command", ""))
        try:
            data = service.handle(command, message.get("args"))
        except Exception as exc:  # noqa: BLE001 - 单条命令失败不能杀死进程
            _emit({"id": req_id, "ok": False,
                   "error": f"{type(exc).__name__}: {exc}"})
            _event("log", level="ERROR", ts=_now_iso(),
                   message=traceback.format_exc())
        else:
            _emit({"id": req_id, "ok": True, "data": data})

    service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
