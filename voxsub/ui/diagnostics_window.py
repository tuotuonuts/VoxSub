"""诊断页（M7 组件清单 #5，M8 接入点的展示骨架）。

- 读取 voxsub.diagnostics.run_self_check()（M8 实现）；未实现时显示占位说明。
- 结果卡：✅ / ⚠️ / ❌ + 一句话处置（DESIGN.md 明确允许这三字符作为状态符）。
- 一键导出纯文本报告（voxsub.diagnostics.export_report 可用时）。
"""
from __future__ import annotations

import importlib

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QFileDialog,
)

_STATUS_CHAR = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
_STATUS_COLOR = {"ok": "#34D399", "warn": "#FBBF24", "fail": "#F87171"}

#: 构造函数哨兵：默认自动探测 voxsub.diagnostics
_AUTO = object()


class DiagnosticsWindow(QWidget):
    """诊断页：设备清单 + 自检结果卡 + 导出报告。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        diagnostics_module: object = _AUTO,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("diagnosticsWindow")
        self.setWindowTitle("诊断 — 语幕 VoxSub")
        self.resize(680, 560)
        self._results: list[dict] = []
        if diagnostics_module is _AUTO:
            self._load_module()
        else:
            self._module = diagnostics_module  # None → 占位分支（测试注入用）

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        head = QLabel("自检结果", self)
        head.setObjectName("sectionTitle")
        root.addWidget(head)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("subtitleScroll")
        self.scroll.setWidgetResizable(True)
        self._container = QWidget(self.scroll)
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setContentsMargins(4, 4, 12, 4)
        self._vbox.setSpacing(10)
        self._vbox.addStretch(1)
        self.scroll.setWidget(self._container)
        root.addWidget(self.scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.export_btn = QPushButton("导出报告 (txt)", self)
        self.export_btn.setObjectName("inputBox")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._export_report)
        btn_row.addWidget(self.export_btn)
        root.addLayout(btn_row)

        self._render()

    # ------------------------------------------------------------------
    def _load_module(self) -> None:
        """尝试加载 M8 诊断模块；失败则保持占位。"""
        try:
            self._module = importlib.import_module("voxsub.diagnostics")
        except Exception:
            self._module = None

    def _render(self) -> None:
        # 清空旧卡
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        if self._module is None or not hasattr(self._module, "run_self_check"):
            self._add_placeholder()
            return

        try:
            self._results = self._module.run_self_check() or []
        except Exception:
            self._results = [{"check": "自检执行", "status": "fail", "detail": "run_self_check 抛异常"}]
        for item in self._results:
            self._add_result_card(item)

    def _add_placeholder(self) -> None:
        card = QFrame(self._container)
        card.setObjectName("settingsCard")
        card.setStyleSheet(
            "QFrame#settingsCard { background-color: rgba(255,255,255,0.04);"
            " border: 1px dashed rgba(255,255,255,0.12); border-radius: 16px; }"
        )
        box = QVBoxLayout(card)
        box.setContentsMargins(24, 28, 24, 28)
        box.setSpacing(8)
        t = QLabel("诊断模块尚未实现", card)
        t.setObjectName("sectionTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d = QLabel(
            "M8 里程碑将在此接入 voxsub.diagnostics.run_self_check() 的"
            "模型完整性 / ORT providers / ASR·VAD·TTS 冒烟 / 磁盘余量检查结果。",
            card,
        )
        d.setObjectName("secondaryLabel")
        d.setWordWrap(True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(t)
        box.addWidget(d)
        self._vbox.insertWidget(self._vbox.count() - 1, card)

    def _add_result_card(self, item: dict) -> None:
        check = str(item.get("check", "未知检查项"))
        status = str(item.get("status", "warn"))
        detail = str(item.get("detail", ""))
        color = _STATUS_COLOR.get(status, _STATUS_COLOR["warn"])
        card = QFrame(self._container)
        card.setObjectName("settingsCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(20, 14, 20, 14)
        row.setSpacing(12)
        mark = QLabel(_STATUS_CHAR.get(status, "⚠️"), card)
        mark.setStyleSheet(f"font-size: 16px; color: {color};")
        row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        mid = QVBoxLayout()
        name = QLabel(check, card)
        name.setObjectName("sectionTitle")
        det = QLabel(detail, card)
        det.setObjectName("secondaryLabel")
        det.setWordWrap(True)
        mid.addWidget(name)
        mid.addWidget(det)
        row.addLayout(mid, 1)
        self._vbox.insertWidget(self._vbox.count() - 1, card)

    # ------------------------------------------------------------------
    def _export_report(self) -> None:
        """导出一份纯文本报告（M8 export_report 可用时）。"""
        try:
            if self._module is not None and hasattr(self._module, "export_report"):
                text = self._module.export_report()
            else:
                lines = ["语幕 VoxSub 诊断报告（诊断模块未实现，以下为骨架内容）"]
                for item in self._results:
                    lines.append(
                        f"[{item.get('status', 'warn')}] {item.get('check', '')} — {item.get('detail', '')}"
                    )
                text = "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            text = f"导出失败: {exc}"

        path, _ = QFileDialog.getSaveFileName(
            self, "导出诊断报告", "voxsub_diagnostics.txt", "文本文件 (*.txt)"
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass  # 用户取消路径等场景静默

    def refresh(self) -> None:
        self._load_module()
        self._render()