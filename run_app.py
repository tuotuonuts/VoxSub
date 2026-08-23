#!/usr/bin/env python
"""语幕应用打包入口: python run_app.py 或 PyInstaller 均以此为入口。

为什么单独入口: python -m voxsub.ui.app 在 PyInstaller 环境下
模块解析行为不同, 单一入口文件最稳。
"""
import sys


def _run_ocr_smoke() -> int:
    """Load the bundled OCR path without creating or controlling a GUI window."""
    import cv2
    import numpy as np

    from voxsub.ocr import RapidOcrEngine

    image = np.full((150, 620, 3), 255, dtype=np.uint8)
    cv2.putText(
        image,
        "Hello VoxSub OCR",
        (20, 92),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.45,
        (0, 0, 0),
        3,
        cv2.LINE_AA,
    )
    frame = RapidOcrEngine().recognize(image)
    normalized = "".join(
        character for character in frame.text.lower() if character.isalnum()
    )
    return 0 if "hellovoxsubocr" in normalized else 2


def main() -> int:
    if "--ocr-smoke" in sys.argv[1:]:
        return _run_ocr_smoke()
    from voxsub.ui.app import main as ui_main
    return ui_main()


if __name__ == "__main__":
    sys.exit(main())
