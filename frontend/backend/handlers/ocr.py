"""屏幕 OCR：识别、译后成图、缓存目录、文件复制（IPC 适配层的一个业务域）。

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

from ipc_protocol import _event


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



class OcrHandlers:
    """屏幕 OCR：识别、译后成图、缓存目录、文件复制。"""

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

    def _cmd_ocr_translate(self, pipeline: Any, args: dict[str, Any]) -> dict[str, Any]:
        """把已识别的文本交给当前翻译配置。

        必须走 self._translator()：它会依次尝试「运行中会话的翻译器 →
        OCR 自己的缓存 → 按配置现建一个」。此前这里写的是
        ``pipeline.translator[1]`` —— 该属性**不存在**（Pipeline 内部叫
        _translator），于是只要 pipeline 建好过，OCR 翻译就必然抛
        AttributeError，整个功能不可用。
        """
        text = str(args.get("text", ""))
        translator = self._translator()
        if translator is None or not text:
            return {"translation": ""}
        translation = translator.translate(
            text,
            str(args.get("source", "auto")),
            str(args.get("target", "zh")))
        return {"translation": translation}

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
