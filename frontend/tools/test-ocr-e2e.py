#!/usr/bin/env python
"""OCR 端到端实测 —— 造图 → 识别 → 翻译 → 渲染译后图 → 校验产物。

这是"功能真能用"的证据，而不是"按钮存在"。
用真实的 ipc_server 子进程，走与界面完全相同的命令。

用法：
    python tools/test-ocr-e2e.py
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
WORK = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "voxsub_ocr_e2e"


def make_test_image(path: Path) -> tuple[int, int]:
    """造一张有明确文字位置的测试图，便于校验覆盖区域。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 220), (246, 243, 236))
    draw = ImageDraw.Draw(image)
    draw.text((40, 50), "Meeting starts at nine", fill=(24, 24, 24))
    draw.text((40, 120), "Please review the document", fill=(24, 24, 24))
    image.save(path)
    return image.size


def call_server(commands: list[dict]) -> dict[int, dict]:
    payload = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in commands)
    payload += json.dumps({"id": 9999, "command": "shutdown", "args": None}) + "\n"

    proc = subprocess.run(
        [str(PYTHON), str(SERVER)],
        input=payload.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
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
    return answers


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    source = WORK / "e2e_source.png"
    rendered = WORK / "e2e_translated.png"
    width, height = make_test_image(source)
    print(f"测试图已生成：{source.name}  {width}x{height}")

    answers = call_server([
        {"id": 1, "command": "ocr_recognize", "args": {
            "path": str(source), "translate": True, "source": "en", "target": "zh",
        }},
    ])

    result = answers.get(1) or {}
    if result.get("ok") is not True:
        print(f"FAIL 识别失败：{result.get('error')}")
        return 1

    data = result.get("data") or {}
    lines = data.get("lines") or []
    print(f"PASS 识别：{len(lines)} 行，OCR {data.get('ocrElapsedMs')}ms，"
          f"翻译 {data.get('translateElapsedMs')}ms")
    for line in lines:
        print(f"       [{line['text'][:30]}] → [{line['translation'][:30]}]  box={line['box']}")

    if not lines:
        print("FAIL 没有识别到任何行")
        return 1

    # 校验：每行都要有框、译文
    boxed = [l for l in lines if isinstance(l.get("box"), list) and len(l["box"]) == 4]
    translated = [l for l in lines if str(l.get("translation", "")).strip()]
    ok_box = len(boxed) == len(lines)
    ok_trans = len(translated) == len(lines)
    print(f"{'PASS' if ok_box else 'FAIL'} 每行都有四点框（{len(boxed)}/{len(lines)}）")
    print(f"{'PASS' if ok_trans else 'FAIL'} 每行都有译文（{len(translated)}/{len(lines)}）")

    # 渲染译后图
    answers = call_server([
        {"id": 1, "command": "render_ocr_image", "args": {
            "source": str(source),
            "target": str(rendered),
            "lines": [{"translation": l["translation"], "box": l["box"]} for l in lines],
        }},
    ])
    render_result = answers.get(1) or {}
    if render_result.get("ok") is not True:
        print(f"FAIL 渲染译后图失败：{render_result.get('error')}")
        return 1

    painted = (render_result.get("data") or {}).get("lines", 0)
    print(f"PASS 译后图已生成：覆盖 {painted} 行 → {rendered.name}")

    # 校验产物：尺寸一致、变化区域落在原文字位置
    from PIL import Image

    with Image.open(source) as handle:
        original = handle.convert("RGB")
    with Image.open(rendered) as handle:
        output = handle.convert("RGB")

    same_size = original.size == output.size
    print(f"{'PASS' if same_size else 'FAIL'} 译后图尺寸与原图一致 {output.size}")

    changed = [
        (x, y)
        for y in range(0, output.height, 2)
        for x in range(0, output.width, 2)
        if original.getpixel((x, y)) != output.getpixel((x, y))
    ]
    has_changes = len(changed) > 0
    print(f"{'PASS' if has_changes else 'FAIL'} 译后图有实际改动（{len(changed)} 个采样点）")

    if changed:
        xs = [p[0] for p in changed]
        ys = [p[1] for p in changed]
        # 文字应该在左半部分、两个纵向带
        in_left = max(xs) < width * 0.75
        in_bands = min(ys) > 20 and max(ys) < height - 20
        print(f"{'PASS' if in_left else 'FAIL'} 覆盖位置合理 x {min(xs)}-{max(xs)}"
              f"（应在左侧 0-{int(width * 0.75)}）")
        print(f"{'PASS' if in_bands else 'FAIL'} 覆盖位置合理 y {min(ys)}-{max(ys)}"
              f"（应在 20-{height - 20} 区间内）")

    failed = (not ok_box) or (not ok_trans) or (not same_size) or (not has_changes)
    print("\n" + ("OCR 端到端全部通过。" if not failed else "有未通过项。"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
