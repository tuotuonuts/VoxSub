"""M7 主题层：Soft Premium 设计令牌 + QFluentWidgets 主题桥接。

令牌表与 DESIGN.md『UI 设计规范（M7）』逐项对应：
- 深色档「暗 OLED 玻璃」：基底 #050505，surface 分层 #131313 / #1A1A1A
- 浅色档「柔和结构主义」：基底 #F7F7F5，surface 分层 #FFFFFF / #F2F2F2
- accent 唯一 teal #14B8A6（深梯度 #0D9488），语义低饱和三色
- 圆角分级（胶囊/卡片16/输入10/弹窗20/主壳32+内芯24）、4px 间距刻度
- 字体栈主 "Segoe UI Variable","Microsoft YaHei UI"；数据 mono "Cascadia Code"

约束（DESIGN.md）：禁 Inter/Roboto/Arial；禁紫蓝渐变背景；accent 仅 1 个。

实现说明：
- load_theme(app, theme) 同时作用于两层：
  1) QFluentWidgets 层 —— setTheme / setThemeColor（其组件自带 Fluent 样式）；
  2) 自定义 QSS 层 —— app.setStyleSheet(build_qss(...))，只作用于本项目
     自绘组件（objectName 定位），不与 QFW 组件的内联样式冲突。
- SYSTEM 档：QFW 侧设 Theme.AUTO 由 darkdetect 驱动；自定义 QSS 侧在
  qconfig.themeChanged 信号触发时按 darkdetect 当前值重建。
"""
from __future__ import annotations

import enum
from typing import Callable

from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, qconfig, setTheme, setThemeColor

_SYSTEM_HOOK_CONNECTED = False  # qconfig.themeChanged 只连接一次（避免重复连接告警）

# ---------------------------------------------------------------------------
# 设计令牌表（单点事实来源；tests/test_ui.py 逐项校验存在性）
# ---------------------------------------------------------------------------
DESIGN_TOKENS: dict[str, dict[str, str]] = {
    "dark": {
        # 基底 / surface 分层（OLED 玻璃）
        "bg_base": "#050505",
        "surface_1": "#131313",
        "surface_2": "#1A1A1A",
        # 文本主 / 次
        "text_primary": "#F2F2F2",
        "text_secondary": "#9CA3AF",
        # 边框（白 8% 透明度）
        "border": "rgba(255,255,255,0.08)",
        "border_strong": "rgba(255,255,255,0.16)",
        # accent（唯一 teal）
        "accent": "#14B8A6",
        "accent_deep": "#0D9488",
        "accent_rgb": "20,184,166",
        # 语义色（低饱和）
        "success": "#34D399",
        "warning": "#FBBF24",
        "error": "#F87171",
        # 圆角分级
        "radius_capsule": "999px",
        "radius_card": "16px",
        "radius_input": "10px",
        "radius_dialog": "20px",
        "radius_shell": "32px",
        "radius_inner": "24px",
        # 间距 / 内边距（4px 基准刻度）
        "spacing": "4px",
        "card_padding": "20px",
        "card_padding_lg": "28px",
        # 字体栈
        "font_family": '"Segoe UI Variable","Microsoft YaHei UI","Microsoft YaHei"',
        "font_mono": '"Cascadia Code","Consolas"',
        # 状态灯中性色（待机）
        "neutral": "#9CA3AF",
    },
    "light": {
        # 基底 / surface 分层（柔和结构主义）
        "bg_base": "#F7F7F5",
        "surface_1": "#FFFFFF",
        "surface_2": "#F2F2F2",
        # 文本主 / 次
        "text_primary": "#1A1A1A",
        "text_secondary": "#6B7280",
        # 边框（黑 8% 透明度）
        "border": "rgba(0,0,0,0.08)",
        "border_strong": "rgba(0,0,0,0.16)",
        # accent（唯一 teal，两档同值）
        "accent": "#14B8A6",
        "accent_deep": "#0D9488",
        "accent_rgb": "20,184,166",
        # 语义色（低饱和，两档同值）
        "success": "#34D399",
        "warning": "#FBBF24",
        "error": "#F87171",
        # 圆角分级
        "radius_capsule": "999px",
        "radius_card": "16px",
        "radius_input": "10px",
        "radius_dialog": "20px",
        "radius_shell": "32px",
        "radius_inner": "24px",
        # 间距 / 内边距
        "spacing": "4px",
        "card_padding": "20px",
        "card_padding_lg": "28px",
        # 字体栈
        "font_family": '"Segoe UI Variable","Microsoft YaHei UI","Microsoft YaHei"',
        "font_mono": '"Cascadia Code","Consolas"',
        # 状态灯中性色（待机）
        "neutral": "#9CA3AF",
    },
}


class AppTheme(enum.Enum):
    """主题三档（与 config.json 存储值一致）。"""

    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# QSS 模板 —— 以 @token 占位，构建时按主题令牌替换。
# 全部选择器限定在本项目自绘组件上（objectName / dynamic property），
# 不碰 QFluentWidgets 组件内部样式。
# ---------------------------------------------------------------------------
_QSS_TEMPLATE = r"""
/* ============ 语幕 VoxSub — Soft Premium 基础层 (theme: @theme_name) ============ */
QWidget {
    font-family: @font_family;
    font-size: 14px;
    color: @text_primary;
}
QMainWindow, QDialog {
    background-color: @bg_base;
}
QWidget#rootShell {
    background-color: @bg_base;
}
QLabel {
    background: transparent;
    color: @text_primary;
}
QLabel#secondaryLabel { color: @text_secondary; font-size: 13px; }
QLabel#accentLabel    { color: @accent; }
QLabel#sectionTitle   { font-size: 15px; font-weight: 600; }
QLabel#emptyHint      { color: @text_secondary; font-size: 13px; }
QLabel#statusText     { color: @text_secondary; font-size: 13px; }
QLabel#trayTipLabel   { color: @text_secondary; font-size: 12px; }

/* ---- 左侧栏 ---- */
QFrame#sidePanel {
    background-color: @surface_1;
    border: 1px solid @border;
    border-radius: @radius_card;
}

/* ---- 模式三卡片 ---- */
QFrame#modeCard {
    background-color: @surface_2;
    border: 1px solid @border;
    border-radius: @radius_card;
    padding: @card_padding;
}
QFrame#modeCard:hover {
    border: 1px solid rgba(@accent_rgb, 0.35);
}
QFrame#modeCard:pressed {
    background-color: rgba(@accent_rgb, 0.06);
}
QFrame#modeCard[active="true"] {
    border: 1px solid @accent;
    background-color: rgba(@accent_rgb, 0.12);
}
QLabel#modeBadge {
    color: @text_secondary;
    font-size: 12px;
    font-weight: 700;
}
QFrame#modeCard[active="true"] QLabel#modeBadge { color: @accent; }
QLabel#modeTitle {
    font-size: 15px;
    font-weight: 600;
}
QLabel#modeDesc { color: @text_secondary; font-size: 13px; }
QFrame#modeCard[active="true"] QLabel#modeTitle { color: @accent; }

/* ---- 实时字幕流 ---- */
QScrollArea#subtitleScroll { background: transparent; border: none; }
QScrollArea#subtitleScroll > QWidget > QWidget { background: transparent; }
QFrame#subRow {
    background: transparent;
    border-radius: @radius_input;
    padding: @spacing;
}
QFrame#subRow[newest="true"] {
    background-color: rgba(@accent_rgb, 0.10);
    border: 1px solid rgba(@accent_rgb, 0.25);
}
QLabel#srcText {
    color: @text_secondary;
    font-family: @font_mono;
    font-size: 13px;
}
QLabel#dstText {
    color: @text_primary;
    font-size: 15px;
    font-weight: 500;
}

/* ---- 输入 / 下拉（仅本项目实例，objectName 定位）---- */
QLineEdit#inputBox {
    background-color: @surface_2;
    border: 1px solid @border;
    border-radius: @radius_input;
    padding: 8px 12px;
    color: @text_primary;
    selection-background-color: @accent;
    selection-color: #FFFFFF;
}
QLineEdit#inputBox:focus { border: 1px solid @accent; }
QLineEdit#inputBox:disabled { color: @text_secondary; }

/* ---- 设置页 ---- */
QWidget#settingsTabs > QWidget { background: transparent; }
QFrame#settingsCard {
    background-color: @surface_1;
    border: 1px solid @border;
    border-radius: @radius_card;
    padding: @card_padding;
}
QLabel#fieldLabel { font-size: 13px; color: @text_secondary; }

/* ---- 单选按钮（Soft Premium 圆点）---- */
QRadioButton {
    color: @text_primary;
    spacing: 10px;
    padding: 2px 0;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 8px;
    border: 1px solid @text_secondary;
    background: transparent;
}
QRadioButton::indicator:hover { border: 1px solid @accent; }
QRadioButton::indicator:checked {
    border: 4px solid rgba(@accent_rgb, 0.22);
    background-color: @accent;
}

/* ---- Tab 页（设置/诊断）---- */
QTabWidget::pane {
    border: 1px solid @border;
    border-radius: @radius_card;
    background-color: @surface_1;
    top: -1px;
}
QTabBar::tab {
    padding: 9px 20px;
    color: @text_secondary;
    font-size: 13px;
    border-top-left-radius: @radius_input;
    border-top-right-radius: @radius_input;
    background: transparent;
}
QTabBar::tab:selected { color: @accent; font-weight: 600; border-bottom: 2px solid @accent; }
QTabBar::tab:hover:!selected { color: @text_primary; }

/* ---- 弹窗 / 菜单 ---- */
QMenu {
    background-color: @surface_2;
    border: 1px solid @border;
    border-radius: @radius_input;
    padding: 6px;
}
QMenu::item {
    color: @text_primary;
    padding: 8px 28px 8px 14px;
    border-radius: 8px;
}
QMenu::item:selected { background-color: rgba(@accent_rgb, 0.15); color: @text_primary; }
QMenu::separator { height: 1px; background: @border; margin: 6px 8px; }

QToolTip {
    background-color: @surface_2;
    color: @text_primary;
    border: 1px solid @border;
    border-radius: @radius_input;
    padding: 6px 10px;
}

/* ---- 状态灯（内核由 StatusLight 自绘，此处仅兜底）---- */
QLabel#statusDot { background: transparent; }
"""


def resolve_theme_name(theme: AppTheme, detector: Callable[[], str | None] | None = None) -> str:
    """把 AppTheme 归约到具体主题名 "dark" / "light"（SYSTEM 档查 darkdetect）。"""
    if theme is AppTheme.DARK:
        return "dark"
    if theme is AppTheme.LIGHT:
        return "light"
    # SYSTEM：跟随系统（darkdetect；取不到值时保守回落浅色）
    if detector is None:
        try:
            import darkdetect  # QFluentWidgets 的传递依赖

            detector = darkdetect.theme
        except Exception:  # pragma: no cover - 依赖缺失兜底
            detector = lambda: "Light"
    current = detector()
    if isinstance(current, str) and current.lower().startswith("dark"):
        return "dark"
    return "light"


def build_qss(theme_name: str) -> str:
    """按主题令牌渲染自定义 QSS。theme_name ∈ {"dark","light"}。

    注意：令牌替换必须按 key 长度降序（@accent 是 @accent_rgb 的前缀，
    否则长令牌会被短令牌污染）。
    """
    tokens = DESIGN_TOKENS.get(theme_name, DESIGN_TOKENS["dark"])
    qss = _QSS_TEMPLATE
    for key in sorted(tokens, key=len, reverse=True):
        qss = qss.replace(f"@{key}", tokens[key])
    qss = qss.replace("@theme_name", theme_name)
    return qss


def load_theme(app: QApplication, theme: AppTheme) -> None:
    """应用主题（QFW 层 + 自定义 QSS 层）。

    - 深/浅档：QFW setTheme 同步；SYSTEM 档：QFW Theme.AUTO + darkdetect 驱动，
      自定义 QSS 在 qconfig.themeChanged 时按当前系统主题重建。
    - accent 唯一 teal #14B8A6 恒定。
    """
    # -- QFW 层 --
    if theme is AppTheme.SYSTEM:
        setTheme(Theme.AUTO)
    else:
        setTheme(Theme.DARK if theme is AppTheme.DARK else Theme.LIGHT)
    setThemeColor(DESIGN_TOKENS["dark"]["accent"])  # 两档同值

    # -- 自定义 QSS 层 --
    theme_name = resolve_theme_name(theme)
    app.setStyleSheet(build_qss(theme_name))

    # SYSTEM 档：跟随系统主题切换重建 QSS（QFW 自身也监听该信号刷组件）
    global _SYSTEM_HOOK_CONNECTED
    if not _SYSTEM_HOOK_CONNECTED:
        qconfig.themeChanged.connect(_on_system_theme_changed)
        _SYSTEM_HOOK_CONNECTED = True


def _on_system_theme_changed() -> None:
    """qconfig.themeChanged 槽：重建自定义 QSS（仅 SYSTEM 档会触发重建）。"""
    app = QApplication.instance()
    if app is None:
        return
    # QFW 触发该信号时其内部主题已切换，这里按 darkdetect 现值重建自定义层。
    # 若当前 QFW 主题模式是 AUTO 才跟随重建；固定档由 load_theme 直接设定。
    theme_mode = getattr(qconfig, "themeMode", None)
    mode_value = getattr(theme_mode, "value", theme_mode)
    if str(mode_value).lower() == "auto":
        app.setStyleSheet(build_qss(resolve_theme_name(AppTheme.SYSTEM)))