# ⏸ Qt 版前端 —— 已暂停更新

**这个分支是冻结的。** 它保存 VoxSub 使用 PySide6/Qt 前端时的完整状态。

## 为什么冻结

前端已重写为 Electron + TypeScript。原因不是"Qt 做不出好界面"，而是：

- QSS 没有变量、嵌套、过渡，设计令牌传播不到（实测同一份令牌有 4 处硬编码副本）
- 手绘控件（QPainter）与样式表混用，改一处颜色要同时改两个体系
- 成品组件生态缺失 —— 每做一个控件都要从零画

重写后前端在 `main` 分支。

## 这个分支里有什么

完整的 Qt 版源码，可以直接跑：

```
voxsub/ui/          Qt 界面（11,527 行）
  main_window.py        主窗
  settings_window.py    设置页 7 分页
  subtitle_overlay.py   字幕浮窗
  ocr_workspace.py      OCR 工作区
  diagnostics_window.py 诊断页
  theme.py              设计令牌
  i18n.py               中英文案
tests/test_ui.py    Qt 界面测试
scripts/            打包脚本（installer.iss / build.ps1 / run_source_test.ps1）
```

## 如何运行

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python run_app.py
```

## 与 main 的关系

```
qt-legacy  ← 你在这里（Qt 前端，冻结）
main       ← Electron 前端（活跃开发）
```

两个分支**共享同一份 Python 核心**（`voxsub/` 里除 `ui/` 之外的部分、
`voxsub/translate/`、`tests/` 的大部分）。核心层的修复可以 cherry-pick 过来：

```bash
git checkout qt-legacy
git cherry-pick <main 上的核心修复提交>
```

但**界面相关的改动不会再回到这里**。

## 还原点

- 标签 `qt-final-5471fbf` —— 不可变指针，指向这个分支创建时的状态
- 完整备份：`D:\OneDrive\app_dve\_backup_branch_20260913_013339\`

## 如果将来要恢复 Qt 版

```bash
git checkout qt-legacy
```

源码完整可用。但注意：`main` 上的核心层改动（如新增的迁移模块、
release_notes 数据抽离）不会自动出现在这里。
