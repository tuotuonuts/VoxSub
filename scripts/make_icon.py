#!/usr/bin/env python
"""生成语幕应用图标 (Soft Premium 风格): 圆角渐变底 + 麦克风/字幕双线符号。

输出: assets/icon.png (512x512) + assets/icon.ico (多尺寸, Pillow 转)。
运行: cd VoxSub && unset PYTHONPATH PYTHONHOME && .venv/Scripts/python.exe scripts/make_icon.py

设计令牌 (DESIGN.md「UI 设计规范」): accent teal #14B8A6 → 深梯度 #0D9488;
深色底 #131313; 不用紫蓝渐变; 图标禁用 emoji。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QApplication

SIZE = 512
ROOT = Path(__file__).resolve().parents[1]


def paint_icon() -> None:
    import numpy as np  # noqa: F401  (仅确保 numpy 可用; 实际未用到)
    app = QApplication.instance() or QApplication(sys.argv)

    from PySide6.QtGui import QImage
    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # 圆角基底: 深空表面 + 描边
    grad = QLinearGradient(0, 0, SIZE, SIZE)
    grad.setColorAt(0.0, QColor("#131313"))
    grad.setColorAt(1.0, QColor("#0D0D0D"))
    p.setBrush(grad)
    p.setPen(QPen(QColor(255, 255, 255, 26), 10))
    radius = 110
    p.drawRoundedRect(QRectF(12, 12, SIZE - 24, SIZE - 24), radius, radius)

    # 高光弧 (inset 高光, Soft Premium 双嵌套)
    p.setPen(QPen(QColor(255, 255, 255, 38), 6))
    p.drawArc(QRectF(48, 48, SIZE - 96, SIZE - 96), 220 * 16, 240 * 16)

    # 麦克风 (teal 渐变): 圆头话筒 + 支架 = 抽象"声+字"
    m_grad = QLinearGradient(0, 150, 0, 380)
    m_grad.setColorAt(0.0, QColor("#14B8A6"))
    m_grad.setColorAt(1.0, QColor("#0D9488"))
    p.setBrush(m_grad)
    p.setPen(Qt.PenStyle.NoPen)

    cx = SIZE // 2
    # 话筒头 (胶囊)
    p.drawRoundedRect(QRectF(cx - 52, 140, 104, 150), 52, 52)
    # 支架 (垂直 + 底座水平线 = 字幕行意象)
    p.drawRoundedRect(QRectF(cx - 14, 282, 28, 92), 14, 14)
    p.drawRoundedRect(QRectF(cx - 120, 374, 240, 22), 11, 11)
    # 两侧声波短线
    p.setBrush(QColor(255, 255, 255, 170))
    p.drawRoundedRect(QRectF(cx - 96, 200, 22, 72), 11, 11)
    p.drawRoundedRect(QRectF(cx + 74, 200, 22, 72), 11, 11)

    # 底部首行字幕文本条: 几何横条示意"字幕"意象 (不画真实文字防轮廓锯齿)
    p.setBrush(QColor(20, 184, 166, 90))
    p.drawRoundedRect(QRectF(cx - 130, 420, 96, 16), 8, 8)
    p.setBrush(QColor(255, 255, 255, 60))
    p.drawRoundedRect(QRectF(cx - 24, 420, 154, 16), 8, 8)
    p.end()
    img.save(str(ROOT / "assets" / "icon.png"))


def make_ico() -> None:
    """用 Pillow 从 png 生成多尺寸 ico (Windows 安装包图标必需)。"""
    from PIL import Image
    png = ROOT / "assets" / "icon.png"
    im = Image.open(png).convert("RGBA")
    im.save(ROOT / "assets" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32),
                                                 (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    (ROOT / "assets").mkdir(exist_ok=True)
    paint_icon()
    make_ico()
    print("图标已生成: assets/icon.png + assets/icon.ico")