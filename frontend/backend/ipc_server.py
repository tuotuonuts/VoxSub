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
    """定位并注入 voxsub 的导入路径。

    两种运行形态，走不同的分支：

    **源码运行**（开发期）
        <repo>/frontend/backend/ipc_server.py   ← 本文件
        <repo>/voxsub/__init__.py               ← 要找的包
        把 <repo> 加进 sys.path。
        不写死"上跳几层"：那对目录调整很脆弱（迁入仓库时就是因为写死了
        parents[2] / "VoxSub" 而失效）。改为逐级向上找含 voxsub/__init__.py 的目录。

    **打包运行**（PyInstaller onedir）
        voxsub 已被打进 bundle，Python 包在 sys._MEIPASS/pyz 里，
        会随 sys.path 自动可见 —— 这里不需要做任何事。
        此时也不能再去找"仓库根"：安装后的机器上没有仓库。
    """
    if getattr(sys, "frozen", False):
        # 打包形态：voxsub 随 bundle 一起加载。
        # 返回 bundle 目录，仅用于诊断输出与日志。
        bundle = getattr(sys, "_MEIPASS", str(Path(sys.executable).parent))
        return bundle

    explicit = os.environ.get("VOXSUB_ROOT")
    if explicit and (Path(explicit) / "voxsub" / "__init__.py").is_file():
        if explicit not in sys.path:
            sys.path.insert(0, explicit)
        return explicit

    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / "voxsub" / "__init__.py").is_file():
            root = str(candidate)
            if root not in sys.path:
                sys.path.insert(0, root)
            return root

    # 兜底：保留旧行为，让后续导入报出可读的错误而不是静默失败
    fallback = str(here.parents[2]) if len(here.parents) > 2 else str(here.parent)
    if fallback not in sys.path:
        sys.path.insert(0, fallback)
    return fallback


VOXSUB_ROOT = _install_backend_path()

# Isolated read-only HWND helper; do not initialize config, Pipeline or IPC services.
if __name__ == "__main__" and sys.argv[1:2] == ["--overlay-native"]:
    from voxsub.overlay_native import main as _native_probe
    raise SystemExit(_native_probe(sys.argv[2]))

from voxsub.language_registry import split_language_pair
FROZEN = bool(getattr(sys, "frozen", False))

# ---- 保护协议通道 -----------------------------------------------------------
# 关键：stdout/stderr 在 Windows 上默认按控制台代码页（CP936/GBK）编码，
# 而协议是 UTF-8 JSON 行。不显式指定 encoding 的话，中文日志与字幕
# 会写成乱码（实测 sidecar 的 stderr 里中文日志全部变成问号与方块）。
#
# 顺序要紧：先拿到真实 stdout 的句柄，再把 sys.stdout 指向 stderr ——
# 这样 print() 不会污染协议通道。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    # 老解释器或被重定向到不可配置的流时跳过；不影响协议本身
    pass



# ---- 协议层（必须早于下面的 stdout 交换）-------------------------------
from ipc_protocol import (  # noqa: E402,F401
    _PROTOCOL_LOCK, _PROTOCOL_OUT, _cancel_requested, _emit, _event,
    _exit_process, _now_iso, set_event_dispatcher,
)
sys.stdout = sys.stderr

# ---- 兼容再导出 ---------------------------------------------------------
# 名字搬到了 ipc_support / handlers/*，但**引用点不搬**：
# `ipc_server._event`、`ipc_server._dir_size` 这类既有写法继续可用，
# 这就是工作单要求的“现有公共入口保留为兼容 facade”。
from ipc_support import (  # noqa: E402,F401
    _dir_size, _free_bytes, _human_size, _resolve_models_root,
)
from handlers.session import SessionHandlers  # noqa: E402,F401
from handlers.models import ModelsHandlers  # noqa: E402,F401
from handlers.migration import MigrationHandlers  # noqa: E402,F401
from handlers.ocr import OcrHandlers  # noqa: E402,F401
from handlers.diagnostics import DiagnosticsHandlers  # noqa: E402,F401
from handlers.jobs import JobsHandlers  # noqa: E402,F401

# 模块级 logger：挂在 "voxsub" 下，因此既进 voxsub.log 文件，也经日志桥
# 进诊断页的实时日志（见 BackendService._install_log_sink）。
# 不能只 print 到 stderr —— 那条通路不带级别与时间戳，界面会把所有内容
# 都显示成 ERROR（已修，但新代码不该再依赖它）。
try:
    from voxsub.logging_setup import get_logger as _get_logger

    logger = _get_logger("ipc")
except Exception:  # noqa: BLE001 - 日志设施不可用时退回标准库
    import logging as _logging

    logger = _logging.getLogger("voxsub.ipc")














# ---------------------------------------------------------------- OCR 渲染辅助













from voxsub.diagnostic_trace import traced_command

class BackendService(SessionHandlers, ModelsHandlers, MigrationHandlers, OcrHandlers, DiagnosticsHandlers, JobsHandlers):
    """把 voxsub 的能力翻译成协议命令。

    命令按功能域分组，命名与 Qt 版 UI 的控件语义一致，
    Electron 侧不需要理解 voxsub 的内部结构。
    """

    def __init__(self) -> None:
        self._pipeline: Any = None
        self._lock = threading.RLock()
        self._log_buffer: list[dict[str, Any]] = []
        self._log_sink_installed = False
        # 后台作业执行器。由 main() 在启动时注入 —— 命令实现只读它，
        # 不自己创建线程，避免"谁都能起线程"的失控。
        self._job_runner: Any = None
        self._model_downloads: dict[str, Any] = {}
        self._developer_enabled = False
        self._diagnostic_results: list[dict[str, Any]] = []

    def bind_job_runner(self, runner: Any) -> None:
        """注入作业执行器（测试可直接传入假执行器）。"""
        self._job_runner = runner

    def has_command(self, command: str) -> bool:
        """命令是否存在。

        读循环用它做"未知命令"的**前置**校验：不存在的命令要在排队前就被拒，
        而不是变成一条注定失败的作业让调用方白等。
        """
        name = str(command or "").strip()
        if not name:
            return False
        if name in ("ping", "shutdown"):
            return True
        return callable(getattr(self, f"_cmd_{name}", None))

    # ------------------------------------------------------------------ 基础设施
    def ensure_pipeline(self) -> Any:
        """取（必要时创建）pipeline。

        **必须加锁**：`state` 是控制命令，在读循环线程上执行且需要 pipeline；
        而需要 pipeline 的耗时作业同时在 worker 线程上跑。本轮把命令从读循环
        移到 worker 之后，两个线程可能同时进这里 —— 各建一个 Pipeline 出来，
        其中一个会被 `_apply_saved_config` 影响后再被覆盖，表现为状态/配置偶发
        对不上。（冷启动实测 ensure_pipeline() 约 0.23s，窗口真实存在。）
        """
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline
            return self._create_pipeline_locked()

    def _create_pipeline_locked(self) -> Any:
        """在持锁状态下构造并装配 pipeline（构造期间不能放锁，见 ensure_pipeline）。"""
        from voxsub.pipeline import Pipeline  # noqa: PLC0415

        pipeline = Pipeline()
        pipeline.on_status(lambda text: _event("status", text=str(text)))
        pipeline.on_utterance(
            lambda src, dst: _event("utterance", source=src, translation=dst))
        pipeline.on_file_subtitle(lambda line: _event("utterance", source=line.text,
                                                     translation=line.translation,
                                                     startMs=line.ts_ms, endMs=line.end_ms))
        pipeline.on_partial(lambda text: _event("partial", text=str(text)))
        pipeline.on_draft(
            lambda src, dst: _event("draft", source=src, translation=dst))
        pipeline.on_progress(
            lambda done, total, stage: _event(
                "progress", completed=done, total=total, stage=stage))
        # 会话状态（运行中 / 已暂停）推给界面。
        #
        # 为什么必须有这条：渲染层原先从不查询也不接收状态，store 里的
        # running/paused 永远是初始的 false —— 于是主按钮永远显示"开始"、
        # 结束按钮永远隐藏、暂停/继续的代码分支永远走不到（表现为
        # "暂停功能没实现"+"所有模式都没有结束按钮"）。
        #
        # 由 pipeline 在状态真正变化时回调（_set_state 与 pause/resume），
        # 因此也覆盖 C 模式文件播完自动停止这类**自主转换**。
        pipeline.on_state(lambda: _event("state", **self._state_payload(pipeline)))
        self._pipeline = pipeline
        self._apply_saved_config(pipeline)
        return pipeline

    @staticmethod
    def _apply_saved_config(pipeline: Any) -> None:
        """把配置文件里的设置真正应用到 pipeline。

        为什么必须有这一步：设置只在用户**点击控件**时才推给后端
        （前端各处的 CMD.set* 调用），启动时前端只把配置读进界面，从不回推。
        于是重启后 pipeline 用自己硬编码的默认值跑，用户的设置形同虚设 ——
        实测症状：配置里 translate_tier=quality、translate_model_id=mt-hy-mt2-1.8b-q8，
        而会话实际用的是 opus-fast 快档，导致日→中每一句都抛
        "快档不支持语言对"（用户只看到原文 + 满屏 ERROR 回溯）。

        逐项应用，单项失败不影响其余项：一个坏值不该让整套设置都失效。
        """
        try:
            from voxsub.config_store import ConfigStore  # noqa: PLC0415

            config = dict(ConfigStore().load())
        except Exception:  # noqa: BLE001 - 拿不到配置就用 pipeline 默认值
            logger.debug("读取配置失败，沿用 pipeline 默认值", exc_info=True)
            return

        def _apply(label: str, fn: Any) -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - 单项失败不阻断其余设置
                logger.warning("应用配置项 %s 失败", label, exc_info=True)

        # ---- 语言对：配置里存成 "zh-en" / "auto-zh" 这样的单串
        pair = str(config.get("lang_pair") or "")
        if "-" in pair:
            src, dst = split_language_pair(pair)
            if src and dst:
                _apply("lang_pair", lambda: pipeline.set_langs(src, dst))

        _apply("mode", lambda: pipeline.set_mode(str(config.get("mode") or "a")))

        _apply("file_translation", lambda: pipeline.set_file_translation(config))

        # ---- 识别：provider / 模型 / 调优
        _apply("stt_provider",
               lambda: pipeline.set_stt(str(config.get("stt_provider") or "local"), config))
        _apply("asr_model_id",
               lambda: pipeline.set_asr_model(str(config.get("asr_model_id") or "")))
        # 调优：配置里是 asr_ 前缀的键，pipeline 内部用短名，
        # 用 tuning_from_config 转换（与 set_asr_tuning 的归一化同一套映射）。
        try:
            from voxsub.pipeline import tuning_from_config  # noqa: PLC0415

            tuning = tuning_from_config(config)
            if tuning:
                _apply("asr_tuning", lambda: pipeline.set_asr_tuning(tuning))
        except Exception:  # noqa: BLE001
            logger.warning("应用配置项 asr_tuning 失败", exc_info=True)

        # ---- 翻译：档位（按配置修正，例如质量档被配成 OPUS 兼容时）
        try:
            from voxsub.translate.factory import kind_for_tier  # noqa: PLC0415

            tier = str(config.get("translate_tier") or "fast")
            _apply("translate_tier",
                   lambda: pipeline.set_translator(kind_for_tier(tier, config), config))
        except Exception:  # noqa: BLE001
            logger.warning("应用配置项 translate_tier 失败", exc_info=True)

        # ---- 语音合成
        _apply("tts_enabled", lambda: pipeline.set_tts(bool(config.get("tts_enabled", True))))
        _apply("tts_models", lambda: pipeline.set_tts_models({
            "zh": str(config.get("tts_model_id_zh") or ""),
            "en": str(config.get("tts_model_id_en") or ""),
        }))

        # ---- 设备与捕获目标
        _apply("audio_devices", lambda: pipeline.set_audio_devices(
            str(config.get("mic_device_id") or ""),
            str(config.get("loopback_device_id") or "")))
        _apply("capture_process", lambda: pipeline.set_capture_process(
            int(config.get("capture_process_id") or 0),
            str(config.get("capture_window_title") or "")))

        # ---- C 模式输入文件
        last_input = str(config.get("last_input_file") or "")
        if last_input:
            _apply("last_input_file", lambda: pipeline.set_input_file(last_input))

        # ---- 录音
        _apply("record_with_translation",
               lambda: pipeline.set_recording(bool(config.get("record_with_translation", False))))

        logger.info(
            "已应用保存的配置: tier=%s lang_pair=%s mode=%s profile=%s",
            config.get("translate_tier"), config.get("lang_pair"),
            config.get("mode"), config.get("asr_tuning_profile"),
        )

    @staticmethod
    def _state_payload(pipeline: Any) -> dict[str, Any]:
        """当前会话状态。命令返回值与 state 事件共用，避免两处口径不一致。

        ``configGeneration`` 是"影响结果的配置代次"（语言对/翻译档位/ASR 模型/
        模型目录/调优档位），由 pipeline 拥有并只读暴露。放在 state 里的用途是
        让界面能判断：**在途任务用的是哪一代配置** —— 于是"运行中改了语言，
        旧任务还在用旧快照"这件事是可观测的，而不是只有日志里才有。
        """
        return {
            "running": bool(pipeline.is_running()),
            "paused": bool(pipeline.is_paused()),
            "mode": str(pipeline.mode),
            "state": str(getattr(pipeline.state, "value", pipeline.state)),
            "configGeneration": int(getattr(pipeline, "config_generation", 0) or 0),
            **pipeline.recording_state,
        }

    def _install_log_sink(self) -> None:
        """把 voxsub 的日志钩到协议事件上，供诊断页实时日志使用。

        两个关键点，都踩过坑：

        1. **必须挂在 "voxsub" logger 上，不能挂根 logger。**
           logging_setup.setup_logging() 把 handler 挂在 "voxsub" 上并设了
           `propagate = False`（不让日志泄漏到第三方库的根 logger）。挂在根
           logger 上的话永远收不到任何记录 —— 事件不发、`recent_logs(memory)`
           恒为空，而界面上完全看不出原因（只有 stderr 那条通路还在动，
           于是所有日志都被当成 stderr 而标成 ERROR）。

        2. **同时摘掉 stderr 控制台 handler。**
           否则同一条日志会到两次：一次是这里的结构化事件（级别/时间准确），
           一次是 stderr 原样输出（Electron 侧只能靠猜级别）。摘掉之后
           stderr 只承载非 logging 的输出（线程异常、warnings 模块），
           那部分由 Electron 侧解析，两者不重叠。
        """
        if self._log_sink_installed:
            return
        try:
            import logging  # noqa: PLC0415

            from voxsub.logging_setup import get_logger, log_timestamp  # noqa: PLC0415

            get_logger("ipc")  # 确保 setup_logging 已跑过，handler 已就位
            target = logging.getLogger("voxsub")

            class _Bridge(logging.Handler):
                def __init__(self, outer: "BackendService") -> None:
                    super().__init__()
                    self._outer = outer

                def emit(self, record: logging.LogRecord) -> None:
                    try:
                        message = self.format(record)
                    except Exception:  # noqa: BLE001
                        return
                    entry = {"ts": log_timestamp(record.created), "level": record.levelname,
                             "message": message, "run_id": getattr(record, "run_id", ""),
                             "session_id": getattr(record, "diagnostic_session_id", "-")}
                    self._outer._log_buffer.append(entry)  # noqa: SLF001
                    del self._outer._log_buffer[:-500]  # noqa: SLF001
                    _event("log", **entry)

            handler = _Bridge(self)
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter("%(name)s: %(message)s"))
            target.addHandler(handler)

            # 摘掉 stderr 控制台 handler（保留文件 handler —— voxsub.log 仍要写）。
            # FileHandler 是 StreamHandler 的子类，所以先排除它。
            for existing in list(target.handlers):
                if isinstance(existing, logging.StreamHandler) and not isinstance(
                    existing, logging.FileHandler
                ):
                    target.removeHandler(existing)

            self._log_sink_installed = True
        except Exception:  # noqa: BLE001 - 日志桥失败不能阻断后端
            _event("log", level="WARNING",
                   message="日志桥安装失败", ts=_now_iso())

    # ------------------------------------------------------------------ 分发
    @traced_command
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
    #
    # 四个命令都**返回当前状态**，并且 pipeline 的状态回调会另发 state 事件。
    # 两条路径都保留是刻意的：命令返回值让界面在点击后立刻更新（不必等事件），
    # 事件负责覆盖自主转换（文件播完、出错回 IDLE）。





    # ================================================================== 模式与语言



    # ================================================================== 音频设备




    # ================================================================== STT / 翻译










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





    # ================================================================== 配置
    def _cmd_get_config(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.config_store import ConfigStore  # noqa: PLC0415

        config = dict(ConfigStore().load())
        from voxsub.logging_setup import set_log_budget
        set_log_budget(config.get("log_limit_mb", 50))
        return config

    def _cmd_set_config(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.config_store import ConfigStore  # noqa: PLC0415

        updates = args.get("updates") or {}
        store = ConfigStore()
        if any(key in updates for key in ("file_translation_mode", "speech_model_id", "speech_device", "speech_output")):
            from voxsub.speech_contract import validate_selection
            validate_selection({**dict(store.load()), **updates})
        if self._pipeline is not None and any(
                key in ("asr_model_id", "file_translation_mode", "speech_model_id", "speech_device", "speech_output") or key.startswith(("stt_", "translate_"))
                for key in updates):
            config = {**dict(store.load()), **updates}
            self._pipeline.apply_language_model_config(config, updates)
        store.update({str(k): v for k, v in updates.items()})
        self._diagnostic_results = []
        self._diagnostic_checked_at = None
        config = dict(store.load())
        if "log_limit_mb" in updates:
            from voxsub.logging_setup import set_log_budget
            set_log_budget(config["log_limit_mb"])
        return config

    # ================================================================== 诊断









    # ================================================================== 文件字幕

    # ================================================================== OCR



    # ================================================================== 旧版迁移







    # ================================================================== 后台作业
    #
    # 这三个命令让"长任务"变成可观察、可取消的对象（工作单 §3.3）。
    # 它们本身是**控制命令**，在读循环线程上直接执行，不排进作业队列 ——
    # 否则"查询任务状态"要先等任务跑完，就成了自相矛盾。




    def _translator(self) -> Any:
        """取当前翻译器；未启动会话时按配置现建一个。

        OCR 是独立工作区：用户可能没点「开始」就直接框选屏幕，
        这时不能因为 pipeline 未启动就静默不翻译。
        """
        pipeline = self._pipeline
        if pipeline is not None:
            # 用 Pipeline 的公开只读属性（此前这里 getattr(pipeline, "translator")
            # 也读错了名字，永远拿不到运行中会话的翻译器）。
            translator = getattr(pipeline, "translator", None)
            if translator is not None:
                return translator

        from voxsub.config_store import ConfigStore
        from voxsub.ocr import translator_config_signature
        config = dict(ConfigStore().load())
        signature = translator_config_signature(config)
        cached = getattr(self, "_ocr_translator", None)
        if cached is not None and getattr(self, "_ocr_translator_signature", None) == signature:
            return cached
        self._ocr_translator = None
        self._close_quietly(cached, label="旧 OCR 翻译器")

        try:
            from voxsub.pipeline import _load_translator  # noqa: PLC0415

            config = {}
            try:
                from voxsub.config_store import ConfigStore  # noqa: PLC0415

                config = dict(ConfigStore().load())
            except Exception:  # noqa: BLE001 - 拿不到配置就用默认
                config = {}

            # 键名必须是 translate_tier：此前写的是 "translator_kind"，
            # 而这个键在配置里**根本不存在**，于是永远落到 opus-fast ——
            # 用户在设置里选了质量档/云端，OCR 翻译依然用快档（表现为
            # "OCR 翻译不支持我选的语言"）。
            from voxsub.translate.factory import kind_for_tier  # noqa: PLC0415

            kind = kind_for_tier(str(config.get("translate_tier") or "fast"), config)
            result = _load_translator(kind, config)
            # _load_translator 返回 (translator, effective_kind) 或裸对象
            translator = result[0] if isinstance(result, tuple) else result
            if translator is not None:
                self._ocr_translator = translator
                self._ocr_translator_signature = signature
            return translator
        except Exception as error:  # noqa: BLE001 - 失败只降级，不阻断识别
            print(f"[ocr] 翻译器不可用，仅返回识别结果: {error}", file=sys.stderr)
            return None


    # ================================================================== 关闭
    def close(self) -> None:
        with self._lock:
            # Keep the Pipeline owned until its close() confirms completion.
            # A false result means active workers still own runtime resources.
            pipeline = self._pipeline
            ocr_translator, self._ocr_translator = getattr(self, "_ocr_translator", None), None

        self._close_quietly(getattr(self, "_ocr_runtime", None), label="OCR runtime")

        # OCR 自建的翻译器**必须自己收**（工作单 §3.4：一个资源只有一个负责人）。
        # 以前的写法是 `if pipeline is None: return` —— 而 OCR 是独立工作区，
        # 用户可以不点"开始"就直接框选屏幕，这时 pipeline 从未创建，于是那个
        # 自建翻译器（每个可能是一个 llama-server 子进程）永远不会被关。
        self._close_quietly(ocr_translator, label="OCR 翻译器")

        if pipeline is None:
            return
        close_attempted = False
        close_succeeded = False
        for action in ("stop", "close"):
            method = getattr(pipeline, action, None)
            if not callable(method):
                continue
            try:
                result = method()
                if action == "close":
                    close_attempted = True
                    close_succeeded = result is not False
            except Exception:  # noqa: BLE001 - 退出路径尽力而为
                if action == "close":
                    close_attempted = True
                    close_succeeded = False
                _event("log", level="error", ts=_now_iso(),
                       message=traceback.format_exc())

        if close_attempted and close_succeeded:
            with self._lock:
                if self._pipeline is pipeline:
                    self._pipeline = None

    @staticmethod
    def _close_quietly(component: Any, *, label: str) -> None:
        """尽力回收一个组件；失败只记日志（退出路径不该因为一个组件抛错而中断）。"""
        if component is None:
            return
        close = getattr(component, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # noqa: BLE001
            _event("log", level="error", ts=_now_iso(),
                   message=f"{label}回收失败\n{traceback.format_exc()}")


# ---- 标记哪些命令不需要 pipeline --------------------------------------------
for _name in (
    "list_models", "install_model", "prepare_model_download", "pause_model_download",
    "delete_model_download", "uninstall_model", "model_dir", "get_config", "set_config",
    "run_self_check", "export_diagnostics", "recent_logs", "record_overlay_diagnostic",
    "developer_mode", "diagnostic_snapshot", "diagnostic_session",
    "clear_logs", "log_path", "import_models", "release_notes", "render_ocr_image", "copy_file", "ocr_cache_dir", "ocr_release",
    "detect_legacy", "plan_migration", "start_migration", "verify_copy",
    "write_model_snapshot", "cleanup_migrated_source", "migration_decision",
    # 后台作业查询/取消只是读执行器状态，不该为此拉起推理栈。
    "job_list", "job_status", "cancel_job",
    "list_devices", "hardware_profile", "list_audio_devices",
    "list_capture_targets", "export_subtitles", "ocr_recognize",
    # 调优元数据只读配置，不需要拉起 pipeline —— 界面上打开设置页就会调它，
    # 不该因此把整个推理栈初始化一遍。
    "asr_tuning_meta",
    # 同理：档位能力查询只是读配置算语言对支持情况。
    "translate_tiers",
    "language_capabilities",
):
    _fn = getattr(BackendService, f"_cmd_{_name}", None)
    if _fn is not None:
        _fn._needs_pipeline = False  # type: ignore[attr-defined]






def _ensure_first_run_defaults() -> dict[str, Any]:
    """首次运行时确定模型目录，并写进配置。

    为什么需要这一步：打包安装后 voxsub 的 install_models_root() 会算出
    ``<安装目录>/Models``，也就是 ``C:\\Program Files\\VoxSub\\Models``。那有两个问题：
      1. 需要管理员权限才能写 —— 普通用户下载模型会失败或被 UAC 打断
      2. 违背本项目"绝不写入系统盘"的既有原则（OCR 缓存那条注释就写明了）

    所以新装用户默认落到非系统盘：

        D:\\VoxSub\\Models            （D 盘存在时）
          ↓ D 盘不可用
        %LOCALAPPDATA%\\VoxSub\\models

    这样也顺带让新老用户路径统一 —— 老用户的配置里就是 D:\\VoxSub\\Models，
    将来从 Qt 版迁移到 Electron 版时那条绝对路径正好还能用。

    **只在配置里没有 models_root 时才写。** 已有配置的用户（包括所有老用户）
    一个字节都不动 —— 他们的模型库位置不能因为我们换前端而改变。
    """
    try:
        from voxsub.config_store import ConfigStore  # noqa: PLC0415

        store = ConfigStore()
        config = dict(store.load())
    except Exception as error:  # noqa: BLE001 - 初始化失败不该阻断启动
        print(f"[init] 读取配置失败: {error}", file=sys.stderr)
        return {"applied": False, "reason": str(error)}

    if str(config.get("models_root") or "").strip():
        # 老用户：位置已定，不动
        return {"applied": False, "reason": "configured", "models_root": config["models_root"]}

    # 选一个可写的非系统盘位置
    candidate_drives = []
    for letter in ("D", "E", "F"):
        drive = Path(f"{letter}:\\")
        if drive.exists():
            candidate_drives.append(drive / "VoxSub" / "Models")

    if candidate_drives:
        target = candidate_drives[0]
    else:
        local = os.environ.get("LOCALAPPDATA") or str(Path.home())
        target = Path(local) / "VoxSub" / "models"

    try:
        target.mkdir(parents=True, exist_ok=True)
        store.update({"models_root": str(target), "models_root_mode": "custom"})
        print(f"[init] 首次运行：模型目录设为 {target}", file=sys.stderr)
        _event("first_run", modelsRoot=str(target))
        return {"applied": True, "models_root": str(target)}
    except Exception as error:  # noqa: BLE001
        print(f"[init] 创建模型目录失败: {error}", file=sys.stderr)
        return {"applied": False, "reason": str(error)}


def main() -> int:
    service = BackendService()

    # 后台作业执行器：耗时命令交给它，读循环因此保持可响应。
    # 单 worker 是刻意的 —— OCR 引擎不能被并发调用，迁移也不能并行。
    # 真正执行什么、终态回给谁，由 IpcLoop 接管（它持有原始请求参数）。
    from ipc_loop import IpcLoop  # noqa: PLC0415
    from job_runner import JobRunner  # noqa: PLC0415

    runner = JobRunner()
    service.bind_job_runner(runner)
    loop = IpcLoop(service, runner, _emit)
    # Pipeline callbacks and handler modules call ipc_protocol._event directly;
    # route those producer events through the same schema boundary as loop events.
    set_event_dispatcher(loop.emit_producer_event)
    # Log-sink installation can emit a failure event, so install the validator first.
    service._install_log_sink()  # noqa: SLF001

    try:
        from voxsub import __version__  # noqa: PLC0415

        loop.handshake(version=__version__, frozen=FROZEN,
                       backend_generation=str(os.getpid()))
    except Exception as exc:  # noqa: BLE001
        _event("error", message=f"后端初始化失败: {type(exc).__name__}: {exc}")
        return 2

    # 打包安装的首次运行：确定模型目录（源码运行不做，避免污染开发环境）
    if FROZEN:
        try:
            _ensure_first_run_defaults()
        except Exception as exc:  # noqa: BLE001
            print(f"[init] 首启动初始化异常: {exc}", file=sys.stderr)

    runner.start()
    try:
        loop.serve(getattr(sys.stdin, "buffer", sys.stdin))
    finally:
        # 退出前不打断在途任务，但要给出共享截止时间，而不是逐个叠加长超时。
        runner.stop(timeout=5.0)
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
