# -*- mode: python ; coding: utf-8 -*-
"""Electron 前端的 Python sidecar 打包配置。

与 Qt 版 VoxSub.spec 的区别（这是本次打包能瘦身的原因）：

  Qt 版                    本 spec
  ──────────────────────── ──────────────────────────────
  collect_all('PySide6')   excludes 掉 PySide6 / shiboken6 / qfluentwidgets
  collect_all('qfluentwidgets')
  入口 run_app.py（起 Qt）  入口 backend/ipc_server.py（起 IPC 服务）

`voxsub/` 核心层在 Qt 移除后已零 Qt 依赖（tests/test_architecture.py 守卫），
因此 sidecar 可以不带 Qt —— 省约 115MB。更新日志数据也从
voxsub/ui/release_notes.py 抽到了 voxsub/release_notes.py，不会再把 Qt 拖回来。

构建：
    ./.venv/Scripts/python.exe -m PyInstaller frontend/backend/ipc_server.spec --noconfirm
产物：
    frontend/backend/dist/VoxSubBackend/  （onedir 布局，整目录随安装包分发）
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    "voxsub.pipeline",
    "voxsub.translate.factory",
    "rapidocr.main",
    # ipc_server 内部用函数级 import，静态分析看不到，必须显式声明
    "voxsub.release_notes",
    "voxsub.process_audio",
    "voxsub.ocr_cache",
    "voxsub.model_storage",
    "voxsub.subtitles",
    "voxsub.config_store",
    "voxsub.file_io",
]

# 这些包内含二进制/数据文件，必须整体收集
for package in ("sherpa_onnx", "soundcard", "onnxruntime"):
    collected = collect_all(package)
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

# Qt 相关一律排除 —— sidecar 只做计算，不画界面。
# 显式列出来是为了防止某个传递依赖悄悄把它拉回来，
# 那样的结果是包无故大 115MB 而没人发现。
EXCLUDES = [
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "shiboken6",
    "qfluentwidgets",
    # 测试与开发工具不进发布物
    "pytest",
    "pytest_asyncio",
]

a = Analysis(
    ["ipc_server.py"],
    # SPECPATH 是 spec 文件所在目录（frontend/backend/），
    # voxsub 在仓库根 → 上跳两层。用绝对路径而不是 ".."，
    # 免得对"相对于谁"产生歧义。
    pathex=[str(Path(SPECPATH).resolve().parents[1])],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoxSubBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 必须保留控制台：IPC 走 stdin/stdout，没有它通道就不存在
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VoxSubBackend",
)
