"""会话与设备：启停、暂停、模式、语言、音频设备、TTS、导出（IPC 适配层的一个业务域）。

方法体是从 ipc_server.py **原样搬移**过来的，只改了所在文件；
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

from ipc_protocol import _event, _now_iso


class SessionHandlers:
    """会话与设备：启停、暂停、模式、语言、音频设备、TTS、导出。"""

    def _cmd_start(self, pipeline: Any, _args: dict[str, Any]) -> dict[str, Any]:
        pipeline.start()
        # 通知界面"新的会话开始"：前端据此重置时间基准（导出 SRT 需要相对时间）。
        #
        # **只在真的在跑时才发**：这条事件会让渲染层清空整场字幕并重置时间基准，
        # 一旦 start() 在某个状态下静默不做（例如"停止中"），发了就等于告诉界面
        # "新会话开始了、旧字幕可以扔了" —— 用户会看到"开始了但字幕全没了"。
        # start() 现在会在这类状态下抛错，这里再加一道，防的是以后又出现
        # "静默不启动"的路径。
        if pipeline.is_running():
            _event("session", action="start")
        else:
            # 日志事件必须带 ts：UI 的 LogEntry 要它排序与显示时间。
            # （ipc_loop 的 log 事件有统一注入点，但这条不在那条路径上。）
            _event("log", level="WARNING", ts=_now_iso(),
                   message="start 之后 pipeline 并未进入运行态，已跳过 session=start 事件")
        return self._state_payload(pipeline)

    def _cmd_stop(self, pipeline: Any, _args: dict[str, Any]) -> dict[str, Any]:
        pipeline.stop()
        _event("session", action="stop")
        return self._state_payload(pipeline)

    def _cmd_pause(self, pipeline: Any, _args: dict[str, Any]) -> dict[str, Any]:
        pipeline.pause()
        return self._state_payload(pipeline)

    def _cmd_resume(self, pipeline: Any, _args: dict[str, Any]) -> dict[str, Any]:
        pipeline.resume()
        return self._state_payload(pipeline)

    def _cmd_state(self, pipeline: Any, _args: dict[str, Any]) -> dict[str, Any]:
        return self._state_payload(pipeline)

    def _cmd_set_mode(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_mode(str(args.get("mode", "a")))

    def _cmd_set_langs(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.apply_language_pair(str(args.get("source", "auto")),
                                     str(args.get("target", "zh")))

    def _cmd_set_input_file(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_input_file(str(args.get("path", "")))

    def _cmd_list_audio_devices(self, args: dict[str, Any]) -> dict[str, Any]:
        """列出可选音频设备。

        id 必须是 ``AudioDeviceInfo.id``（WASAPI 端点 ID）—— 这是配置里持久化、
        也是 ``Pipeline._find_device`` 用来比对的**同一个值**。此前读的是
        ``device_id``，而 AudioDeviceInfo 没有这个字段，于是回落成设备**名字**：
        用户在设置里选一次麦克风，写进配置的是名字，开始会话时
        ``_find_device`` 拿名字去比 WASAPI ID 必然不匹配，直接抛
        "已选择的麦克风当前不可用，请在设置中重新选择" —— 也就是这个选择
        功能实际是坏的。

        少数假设备没有端点 ID（``id`` 为空串），此时回落用名字，避免多个设备
        的 id 都为空而互相撞车。
        """
        from voxsub.audio import list_loopbacks, list_microphones  # noqa: PLC0415

        def _entry(device: Any, kind: str) -> dict[str, str]:
            endpoint_id = str(getattr(device, "id", "") or "")
            return {
                "id": endpoint_id or str(device.name),
                "name": str(device.name),
                "kind": kind,
            }

        return {
            "microphones": [_entry(d, "mic") for d in list_microphones(include_loopback=False)],
            "loopbacks": [_entry(d, "loopback") for d in list_loopbacks()],
        }

    def _cmd_list_capture_targets(self, args: dict[str, Any]) -> dict[str, Any]:
        """可捕获的可见窗口（用于 B 模式按应用隔离）。

        正确位置是 voxsub.process_audio。原先写的是 voxsub.ui.view_models ——
        那个模块并不导出此函数，ImportError 被下面的 except 静默吞掉，
        导致"按应用隔离"从未真正生效（界面永远只有空列表）。
        """
        try:
            from voxsub.process_audio import list_capture_targets  # noqa: PLC0415
        except ImportError as error:
            print(f"[capture] 无法导入窗口枚举: {error}", file=sys.stderr)
            return {"targets": [], "error": str(error)}

        try:
            targets = list_capture_targets()
        except Exception as error:  # noqa: BLE001 - 枚举失败不应中断命令
            print(f"[capture] 枚举窗口失败: {error}", file=sys.stderr)
            return {"targets": [], "error": str(error)}

        return {
            "targets": [
                {
                    "pid": int(getattr(t, "pid", 0) or 0),
                    "processName": str(getattr(t, "process_name", "")),
                    "windowTitle": str(getattr(t, "window_title", "")),
                    "label": str(getattr(t, "label", "") or getattr(t, "process_name", "")),
                }
                for t in targets
            ],
        }

    def _cmd_set_audio_devices(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_audio_devices(
            str(args.get("microphone", "") or ""),
            str(args.get("loopback", "") or ""))

    def _cmd_set_capture_process(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_capture_process(int(args.get("pid", 0) or 0),
                                     str(args.get("title", "") or ""))

    def _cmd_set_stt(self, pipeline: Any, args: dict[str, Any]) -> None:
        from voxsub.config_store import ConfigStore

        config = {**dict(ConfigStore().load()), **(args.get("config") or {})}
        if pipeline.set_stt(str(args.get("provider", "local")), config) is False:
            raise RuntimeError("会话运行中或仍在收尾，请结束后再修改模型设置")

    def _cmd_set_translator(self, pipeline: Any, args: dict[str, Any]) -> None:
        """切换翻译档位/模型。

        优先接受档位 id（fast/quality/cloud），由后端映射成翻译器 kind ——
        映射规则要看配置（质量档在 translate_model_id=mt-opus-fast-builtin 时
        实际创建的是 OPUS 翻译器），放前端会算错。kind 保留兼容旧调用。

        **参数里的 config 一律先与已保存的配置合并**，再把同一个对象同时用于
        档位映射和翻译器创建。必要性：两个界面入口都传 `config: {}`——
          · 设置页点档位单选（只传 tier）
          · 模型广场选翻译模型（先写 translate_model_id，再传 kind）
        此前合并结果只用于档位映射，创建时仍用参数里的空 dict，于是
        factory 拿不到 translate_model_id、退回默认 Qwen 模型 —— 实测两条
        路径加载的模型不同：启动路径用用户选的 hy-mt2，点一下档位就换成
        旧模型；在模型广场选完模型也要重启才生效。
        """
        config = args.get("config") or {}
        tier = args.get("tier")
        try:
            from voxsub.config_store import ConfigStore  # noqa: PLC0415

            config = {**dict(ConfigStore().load()),
                      **(config if isinstance(config, dict) else {})}
        except Exception:  # noqa: BLE001 - 拿不到配置就按传入的算
            pass

        if tier:
            from voxsub.translate.factory import kind_for_tier  # noqa: PLC0415

            kind = kind_for_tier(str(tier), config)
        else:
            kind = str(args.get("kind", "opus-fast"))
        if pipeline.set_translator(kind, config) is False:
            raise RuntimeError("会话运行中或仍在收尾，请结束后再修改模型设置")

    def _cmd_set_asr_model(self, pipeline: Any, args: dict[str, Any]) -> None:
        from voxsub.config_store import ConfigStore

        model_id = str(args.get("model_id", ""))
        if pipeline.set_asr_model(model_id) is False:
            raise RuntimeError("会话运行中或仍在收尾，请结束后再修改模型设置")
        ConfigStore().update({"asr_model_id": model_id})

    def _cmd_set_asr_tuning(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_asr_tuning(args.get("tuning") or {})

    def _cmd_asr_tuning_meta(self, _args: dict[str, Any]) -> dict[str, Any]:
        """界面渲染「识别调优」分页所需的全部信息。

        三项内容：
          · presets    —— 每个预设档位固定的基础参数值
          · controlled —— 受档位控制的键（不在其中的键任何档位都可改）
          · editable   —— 每个档位下 controlled 里仍可改的子集（其余要置灰）
          · effective  —— 当前档位下**实际生效**的值（界面显示这个，而不是用户
                          存的值：预设档下用户存的基础参数根本不生效）

        生效值按**配置**算，不读 pipeline 实例状态：实例可能从未启动过，
        它的 `_asr_tuning` 还是构造时的默认值（auto 档），据此报出来的数字
        与实际会用到的完全不同 —— 实测踩到：配置是 context，报出来却是 0.5/4。

        为什么由后端提供而不是前端硬编码：这些都取决于后端实际怎么读配置。
        前端各写一份的话，改了一处忘另一处就会出现"界面显示的值和实际跑的
        值对不上"。
        """
        from voxsub.config_store import ConfigStore  # noqa: PLC0415
        from voxsub.pipeline import asr_tuning_metadata, effective_tuning_for  # noqa: PLC0415

        config = dict(ConfigStore().load())
        return {
            **asr_tuning_metadata(),
            "effective": effective_tuning_for(config),
        }

    def _cmd_set_tts(self, pipeline: Any, args: dict[str, Any]) -> None:
        pipeline.set_tts(bool(args.get("enabled", False)))

    def _cmd_set_tts_models(self, pipeline: Any, args: dict[str, Any]) -> None:
        models = args.get("models") or {}
        pipeline.set_tts_models({str(k): str(v) for k, v in models.items()})

    def _cmd_set_recording(self, pipeline: Any, args: dict[str, Any]) -> dict[str, Any]:
        """Return the backend's effective WAV-saving state, never the UI intent."""
        pipeline.set_recording(bool(args.get("enabled", False)),
                               args.get("directory"))
        return pipeline.recording_state

    def _cmd_language_capabilities(self, args: dict[str, Any]) -> dict[str, Any]:
        from voxsub.config_store import ConfigStore
        from voxsub.language_capabilities import language_capabilities

        mode = str(args.get("mode") or "a")
        pipeline = getattr(self, "_pipeline", None)
        if pipeline is not None and mode != "d":
            return pipeline.language_capabilities
        return language_capabilities(dict(ConfigStore().load()), mode=mode)

    def _cmd_translate_tiers(self, args: dict[str, Any]) -> dict[str, Any]:
        """各翻译档位支持的语言对（供界面提示，只读配置不加载模型）。

        必要性：快档（OPUS-MT）只有 zh↔en 的模型，而界面允许把语言选成
        日文/韩文。此前用户选了这个组合，每一句都失败且只看到原文 ——
        界面必须在选择时就告诉他，而不是等他跑起来看满屏报错。
        """
        from voxsub.config_store import ConfigStore  # noqa: PLC0415
        from voxsub.translate.factory import tier_capabilities  # noqa: PLC0415

        try:
            config = dict(ConfigStore().load())
        except Exception:  # noqa: BLE001 - 拿不到配置就按默认值算
            config = {}

        # 语言对可以显式传（界面在用户改语言后立刻要新结论），
        # 否则用配置里保存的那一对。
        src = str(args.get("source") or "")
        dst = str(args.get("target") or "")
        if not src or not dst:
            pair = str(config.get("lang_pair") or "zh-en")
            head, _, tail = pair.partition("-")
            src = src or head or "zh"
            dst = dst or tail or "en"
        return tier_capabilities(src, dst, config)

    def _cmd_last_recording(self, pipeline: Any, _args: dict[str, Any]) -> dict[str, Any]:
        # last_recording_path 是 @property，不是方法 —— 加括号会得到
        # `None()` → TypeError: 'NoneType' object is not callable，
        # 每次结束会话（前端都会查一次）都报一条假错误。
        path = pipeline.last_recording_path
        return {"path": str(path) if path else None}

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
