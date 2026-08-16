"""语幕 VoxSub —— UI 包（M7 壳层）。

目录结构（对应 DESIGN.md『模块划分』的 ui/）:
- theme.py            设计令牌表 + QFluentWidgets 主题桥接 (Soft Premium)
- config_store.py     本地配置读写 (%LOCALAPPDATA%\\VoxSub\\config.json)
- pipeline_client.py  Pipeline 契约接入点（M6 未实现时以鸭子类型 stub 顶替）
- main_window.py      主窗：左右分栏 + 模式三卡片 + 实时字幕流 + 胶囊 CTA
- subtitle_overlay.py 无边框置顶半透明字幕浮窗 (Double-Bezel 双层壳)
- tray.py             系统托盘（模式快捷切换 / 显示主窗 / 退出）
- settings_window.py  设置页（翻译 / 语音 / 外观 / 关于）
- diagnostics_window.py 诊断页骨架（M8 接入点）
- app.py              入口：python -m voxsub.ui.app
"""

__version__ = "0.1.0-m7"