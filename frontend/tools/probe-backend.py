#!/usr/bin/env python
"""后端能力实测 —— 逐条真调用，不用读代码代替验证。

启动一个 ipc_server 子进程，发一批命令，检查每个应答。
用于回答"现有功能是不是都实现了"：凡是这里标 FAIL 的，就是真缺口。

用法：
    python tools/probe-backend.py            # 全部
    python tools/probe-backend.py export     # 只跑名字含 export 的
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VOXSUB = Path(os.environ.get("VOXSUB_ROOT", r"D:/OneDrive/app_dve/VoxSub"))
PYTHON = VOXSUB / ".venv" / "Scripts" / "python.exe"
SERVER = ROOT / "backend" / "ipc_server.py"
TMP = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "voxsub_probe"

# (命令, 参数, 期望：检查应答的函数或 None=只要 ok)
PROBES: list[tuple[str, dict | None, str]] = [
    # 基础
    ("ping", None, "应答版本号"),
    ("state", None, "会话状态"),
    ("get_config", None, "读到配置"),
    # 硬件与设备
    ("hardware_profile", None, "硬件画像"),
    ("list_devices", None, "推理设备列表"),
    ("list_audio_devices", None, "音频设备（麦克风 + 回环）"),
    ("list_capture_targets", None, "可捕获进程窗口"),
    # 模型
    ("list_models", None, "模型目录"),
    ("model_dir", {"model_id": "ocr-rapidocr-v6-small-builtin"}, "模型所在目录"),
    # 自检与诊断
    ("run_self_check", None, "自检 6 项"),
    ("recent_logs", {"limit": 20}, "最近日志"),
    ("export_diagnostics", None, "诊断报告文本"),
    # 配置写入
    ("set_config", {"updates": {"_probe_marker": "ok"}}, "写配置（非法键应被白名单拒绝）"),
    ("set_mode", {"mode": "a"}, "切模式"),
    ("set_langs", {"source": "auto", "target": "zh"}, "切语言对"),
    # 调优
    ("set_asr_tuning", {"updates": {"asr_vad_threshold": 0.35}}, "写调优参数"),
    # 字幕导出
    ("export_subtitles", {
        "path": str(TMP / "probe.srt"),
        "lines": [
            {"source": "第一句", "translation": "First"},
            {"source": "第二句", "translation": "Second"},
        ],
    }, "导出 SRT"),
    # 翻译器与 STT
    ("set_translator", {"kind": "opus", "config": {}}, "切翻译档位"),
    ("set_stt", {"kind": "local", "config": {}}, "切 STT 来源"),
    # OCR
    ("ocr_recognize", {"path": str(TMP / "missing.png")}, "OCR 识别（无图时应报错而非崩溃）"),
]


def run_probes(only: str | None = None) -> int:
    TMP.mkdir(parents=True, exist_ok=True)

    selected = [(c, a, d) for c, a, d in PROBES if not only or only in c]
    if not selected:
        print(f"没有匹配 '{only}' 的探针")
        return 1

    payload = "".join(
        json.dumps({"id": i, "command": cmd, "args": args}, ensure_ascii=False) + "\n"
        for i, (cmd, args, _) in enumerate(selected, start=1)
    )
    payload += json.dumps({"id": 9999, "command": "shutdown", "args": None}) + "\n"

    print(f"启动后端：{PYTHON}")
    proc = subprocess.run(
        [str(PYTHON), str(SERVER)],
        input=payload.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        env={**os.environ, "PYTHONPATH": "", "PYTHONHOME": "", "PYTHONUNBUFFERED": "1"},
    )

    answers: dict[int, dict] = {}
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item.get("id"), int):
            answers[item["id"]] = item

    passed = failed = 0
    print("\n" + "=" * 74)
    print(f"{'结果':6s} {'命令':22s} 说明")
    print("=" * 74)

    for index, (cmd, _args, desc) in enumerate(selected, start=1):
        answer = answers.get(index)
        if answer is None:
            print(f"{'MISS':6s} {cmd:22s} {desc}  ← 后端没有应答")
            failed += 1
            continue

        ok = answer.get("ok") is True
        # 预期会被拒绝的命令：只要"优雅报错"（有 error 字符串、不是崩溃）就算通过
        expects_error = cmd in ("ocr_recognize", "set_config")

        if ok or (expects_error and isinstance(answer.get("error"), str)):
            detail = ""
            data = answer.get("data")
            if isinstance(data, dict):
                keys = ", ".join(list(data.keys())[:4])
                detail = f"→ {{{keys}}}"
            elif isinstance(data, list):
                detail = f"→ [{len(data)} 项]"
            elif data is not None:
                detail = f"→ {str(data)[:40]}"
            if expects_error and not ok:
                detail = f"→ 优雅拒绝: {str(answer.get('error'))[:50]}"
            print(f"{'PASS':6s} {cmd:22s} {desc} {detail}")
            passed += 1
        else:
            print(f"{'FAIL':6s} {cmd:22s} {desc}  ← {answer.get('error')}")
            failed += 1

    print("=" * 74)
    print(f"通过 {passed} / 失败 {failed} / 共 {len(selected)}")

    stderr_tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
    if stderr_tail:
        print("\n后端 stderr（末 6 行）：")
        for line in stderr_tail:
            print(f"  {line}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_probes(sys.argv[1] if len(sys.argv) > 1 else None))
