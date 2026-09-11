# AGENTS.md — 语幕 VoxSub

面向 AI 编码代理的项目说明。人类入口是 `README.md`；本文件只写「代理动手前必须知道的事」。

## 注意事项

- 每次改动完成后，都必须创建一个对应的 Git commit，以便后续追踪和回滚。
- 每次改动后，都必须编写或更新相关测试，并在交付给用户前，确保所有测试和验证全部通过。

## 0. 先读哪份文档

不要靠猜。按任务查表：

| 你要做的事 | 先读 |
|---|---|
| 接手 / 交接 / 不确定进度 | `STATUS.md`（状态书）+ `TODO.txt`（时间戳任务追踪） |
| 需求范围争议（该不该做） | `REQUIREMENTS.md` |
| 技术选型、里程碑 M1–M9 | `PLAN.md` |
| 写任何代码之前 | `DESIGN.md`（架构图 + 全部接口契约 + UI 设计令牌） |
| 改结构 / 加新模块 / 拆文件 | `MAINTAINABILITY.md`（结构边界）+ `CODE_REVIEW_2026-09.md`（热点清单与重构路线） |
| 发布 / 打包 / 签名 | `RELEASE_NOTES.md` + `scripts/build.ps1` |
| 对外文案 | `README.md` / `README_EN.md`（必须同步改两份） |

## 1. 项目是什么

Windows 10/11 大众实时翻译软件。四个平级模式：A 麦克风同传、B 系统/指定应用声音、C 音视频文件字幕、D 屏幕 OCR 原位覆盖。STT 与翻译**独立选择**（本地/云自由组合），硬件路由 **独显 GPU → NPU → 核显 → CPU**，默认全本地离线。

技术栈：Python 3.11/3.12 · onnxruntime-directml · sherpa-onnx · soundcard/PyAudioWPatch · RapidOCR · PySide6 + QFluentWidgets · PyInstaller + Inno Setup · pytest。

## 2. 环境（踩过坑，逐条照做）

**a. 先验证 venv 能不能用。** 本仓库在 OneDrive 同步盘上、会被多台机器共用，`.venv/pyvenv.cfg` 可能指向另一台机器上不存在的解释器。开工第一步：

```bash
unset PYTHONPATH PYTHONHOME
./.venv/Scripts/python.exe -c "import sys; print(sys.version)"
```

报 `No Python at ...` 说明这个 venv 是别的机器建的，换用本机可用的环境或按 `scripts/run_source_test.ps1` 重建（它会自动备好 uv/Python 并同步 `requirements.lock`）。

**b. 所有 python 命令必须前缀 `unset PYTHONPATH PYTHONHOME`。** 宿主的 agent 运行时会向终端注入 `PYTHONPATH` 指向它自己的 venv，不清掉会 import 到错位的包，症状是莫名其妙的 `ImportError` 或版本不对。

**c. 目录在 OneDrive 上。** 偶发文件锁：报 `os error 5` / `拒绝访问` 时等 1–2 秒重试，不要当成逻辑 bug。

**d. 不要 `git add -A` / `git add .`。** 工作区里有 1.1GB 的 `.venv-codex` 和 200+ 个 `.pytest-*` / `.npu-*` / `.smoke-*` 临时目录；`.gitignore` 只覆盖了 `.venv/`，**没有覆盖 `.venv-codex/`**。按路径显式 add。

**e. 测试会在仓库根生成临时目录**，用 `--basetemp` 指到唯一目录（`build.ps1` 已经这么做：`.pytest-build-<guid>`）。

## 3. 常用命令

```bash
# 全量测试（加 -o "addopts=" 才会跑被 pytest.ini 默认排除的 hardware_audio）
unset PYTHONPATH PYTHONHOME
./.venv/Scripts/python.exe -m pytest tests/ -q

# 单文件 / 单用例
./.venv/Scripts/python.exe -m pytest tests/test_architecture.py -q

# 真实音频设备测试（会占用麦克风，默认不跑）
./.venv/Scripts/python.exe -m pytest tests/test_audio.py -q -o "addopts=" -m hardware_audio

# 构建正式安装包 → 输出到仓库上一级的 Release/（含安装包 + .sha256）
powershell -ExecutionPolicy Bypass -File scripts/build.ps1

# 源码方式运行
./.venv/Scripts/python.exe run_app.py
```

`scripts/build.ps1` 是**发布门禁**，不是普通打包脚本：它依次跑 pytest → compileall → PyInstaller → `VoxSub.exe --qt-smoke` → `VoxSub.exe --ocr-smoke` → 打包后安装器退出握手 → Inno Setup → 签名 → SHA256。任一步失败即中断。改动打包相关代码后必须跑它。

## 4. 架构硬约束（`tests/test_architecture.py` 会自动拒绝）

这四条是自动化门禁，违反直接红灯，不要试图绕过：

1. **核心层不得导入 UI。** `voxsub/` 下除 `voxsub/ui/` 外的模块，禁止 `import voxsub.ui...`。跨层通信用回调、协议或不可变数据对象。目的是让诊断、模型管理、命令行工具不加载 Qt 也能跑。
2. **禁止无容量队列。** 生产代码里 `queue.Queue()` 必须显式写 `maxsize=`，或用 `queue.Queue(N)`。用无界队列掩盖慢消费者是禁止的。
3. **函数分支复杂度预算 15。** 新增的 `if/for/while/try/BoolOp/IfExp/Match` 累计 ≥15 就失败。按「探测 → 决策 → I/O → 提交」分段拆开。
4. **正式文件不得直接覆盖写。** 文本走 `voxsub.file_io.write_text_atomically`，二进制走 `copy_file_atomically`。

`tests/test_packaging.py` 另外守卫打包契约（installer.iss 的关机协议、Release 输出目录、PyInstaller 参数、模型目录不被删除）。**它靠读取源文件文本来断言**——改了相关文件必须同步改它，否则红。

`MAINTAINABILITY.md` 的「新功能合入守则」是这些约束的完整版，写新模块前读一遍。

## 5. 代码约定

- **UI 文案必须双语。** 所有用户可见静态文案经 `voxsub/ui/i18n.py` 的 `tr()` 或集中词条；改 UI 必须同时补简体中文与 English，并扩展中英切换回归测试。未完成双语核验不算 UI 完成。字幕正文、设备真实名称、文件名、日志、模型返回内容属于用户/系统数据，**不得**为了本地化改写。
- **选择控件统一。** 单选 `RoundRadioButton`、二值 `ToggleSwitch`、紧凑筛选 `PillChoiceButton`（都在 `voxsub/ui/selection_controls.py`）。禁止新增裸 `QRadioButton` / `QCheckBox` 进业务界面——Windows 原生选中态会改变控件几何形状。
- **失败必须降级，不能伪装成功。** TTS 失败 → 仅字幕；OCR 翻译失败 → 保留识别原文；翻译单句失败 → 保留原文；云服务不可用 → 明确提示。生产路径的导入失败不允许用 stub 冒充正常。
- **降级 ≠ 伪报。** 硬件路由只在模型运行时真正支持时才选该设备；不通就写日志并往下走，不能把 CPU 回退显示成硬件加速。
- **队列/线程/进程/HTTP client/模型 engine 都要有唯一 owner 和可测试的 `close()`。** 组件的工作线程未退出前，不得关闭或替换它持有的原生/网络资源。新增资源要挂到 `voxsub/ui/shutdown_coordinator.py`，不要再往 `app_runtime.py` 堆临时退出闭包。
- **后台线程禁止直接碰 Qt 控件。** 一律经 `queue.Queue` + Qt Signal 桥接到主线程。
- **版本号有 6 处必须同步递增**：`voxsub/__init__.py`、`voxsub/ui/__init__.py`、`voxsub/ui/release_notes.py`、`scripts/build.ps1` 的 `$Version`、`scripts/installer.iss`（注释 + `MyAppVersion` + `OutputBaseFilename`）、`README.md` + `README_EN.md`。

## 6. 当前状态（每次会话开始核对，勿沿用旧结论）

**CI 红灯。** 最近两次推送（`544c9b7` Refactor application lifecycle orchestration、`a3129ad` Add Japanese and Korean translation support）的 Quality 工作流均为 failure，3 个用例失败：

| 失败用例 | 原因 |
|---|---|
| `test_production_functions_stay_below_complexity_budget` | `voxsub/language_guard.py:100 text_matches_language` 分支评分 18（预算 15） |
| `test_installer_uses_bounded_shutdown_instead_of_restart_manager_wait` | 断言 `voxsub/ui/app.py` 里还有 `app.aboutToQuit.connect(lambda: _close_pipeline(win.pipeline))`，但 `544c9b7` 已把退出编排搬进 `app_runtime.py` / `shutdown_coordinator.py`；测试未同步 |
| `test_ui.py::TestMainWindow::test_construct_and_widgets` | 已提交的 `main_window.py` 用 16 项 `LANG_PAIRS`，而已提交的 `test_ui.py` 断言 `lang_combo.count() == 4` |

**工作区有未提交改动**：`voxsub/ui/main_window.py`（+211/−65，语言对单选下拉改为「识别语言 + 翻译为」双下拉）、`voxsub/ui/i18n.py`（+6 词条）、`tests/test_ui.py`（+60/−30）。这三处正是上表第三项的修复，且本机验证 `test_construct_and_widgets` 已通过——**但它们还没提交，CI 上没有**。

另外有 4 个 `*-LAPTOP-FERQV95J.*` 未跟踪残留文件（README + 3 个测试），是跨机器同步冲突产物，`CODE_REVIEW_2026-09.md` 记录为「未修改，保留原样」。动它们之前先问。

**发布纪律（用户约定，勿改）：** 正式发布物统一编译到仓库上一级的 `Release/` 目录；每个版本 = 安装包 + SHA256 + 签名 + `RELEASE_NOTES.md` 更新；**源码版本号迭代必须在同一轮完成安装包、签名、SHA256 与 Release 核对，未打包不得宣布该版完成**。`0.8.0-beta` / `0.9.0-beta` 目前是「用户验收前不创建 GitHub Release」状态。

## 7. 已知陷阱

- **git-bash 命令行传中文会被 GBK 转码**，`llama-server` 会拒收 ill-formed UTF-8。请求体必须走 UTF-8 文件（`--data @file`）。
- **silero VAD 对纯正弦音零触发**，冒烟素材必须用真实语音。
- **sherpa-onnx 的 provider 只认 `cpu`/`cuda`/`coreml`**，传 `DmlExecutionProvider` 会被拒绝并静默回退 CPU——需要时显式映射，别让它误导日志。
- **TTS 模型按包各异采样率输出**（8k / 22.05k），且 `samples` 是 float64；引擎内必须重采样到 16k 并转 float32。
- **WebView2 不支持半透明**（alpha 只接受 0 或 255）——这是本项目 UI 留在 Qt 的原因之一，不要提议换 Web 前端而不做验证。
- **PyInstaller 用模块级延迟导出时收集不到子模块**（`rapidocr.main` 就这么丢过一次），新增依赖后跑一次 `--ocr-smoke` / `--qt-smoke` 确认。

## 8. 交互偏好

- 与用户交流用中文。
- 改动前先说明；诊断类问题先给根因 + 证据链，等确认再动手。
- 报告要区分「已验证」与「未验证」，证据不足就保留 `NOT_RUN` / `BLOCKED`，不要推测。
