"""应用图标（程序化生成，无二进制资源文件）。

主图标：teal 圆角方块 + 白色「幕」字 —— 与软高级感主色一致，同时满足
「禁 emoji 图标」（DESIGN.md M7 图标规范：QFluentWidgets FluentIcons 或
自绘矢量，本项目一律 QPainter 自绘，避免字体图标主题变色问题）。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap


def make_app_icon(size: int = 64) -> QIcon:
    """生成应用图标：accent 圆角方块 + 白色「幕」字。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # 圆角方块（外壳圆角 = 32px 分级中较大的弹窗级）
    p.setPen(QPen(QColor("#0D9488"), 2))
    p.setBrush(QColor("#14B8A6"))
    p.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), size * 0.22, size * 0.22)
    # 白色「幕」字
    p.setPen(Qt.GlobalColor.white)
    font = QFont("Microsoft YaHei UI", int(size * 0.52), QFont.Weight.DemiBold)
    p.setFont(font)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "幕")
    p.end()
    return QIcon(pm)