# AGENTS.md — 语幕 VoxSub

面向 AI 编码代理的项目说明。人类入口是 `README.md`；本文件只写「代理动手前必须知道的事」。

> **本文件已按 Electron 版现状重写（2026-09）。** 旧版描述的是 Qt 前端，其中
> 「不要提议换 Web 前端」等结论与现状直接矛盾。Qt 时代的完整说明保留在
> `qt-legacy` 分支的同名文件里。

## 注意事项（用户强制要求，勿绕过）

- 每次改动完成后，都必须创建一个对应的 Git commit，以便后续追踪和回滚。
- 每次改动后，都必须编写或更新相关测试，并在交付给用户前，确保所有测试和验证全部通过。
- 删除 / 移动 / 覆盖任何文件前，先建带时间戳的备份目录并说明回滚方法。
- 诊断类问题先给**根因 + 证据链**，等用户确认走哪条路再动手；不要擅自修。
- 改配置或模型设置时只改设置字段，**绝不改对话消息与历史正文**。
- 报告要区分「已验证」与「未验证」，证据不足就保留 `NOT_RUN` / `BLOCKED`，不要推测。

## 0. 先读哪份文档

不要靠猜。按任务查表：

| 你要做的事 | 先读 |
|---|---|
| 接手 / 交接 / 不确定进度 | `STATUS.md`（状态书）+ `TODO.txt`（时间戳任务追踪） |
| 需求范围争议（该不该做） | `REQUIREMENTS.md` |
| 技术选型、里程碑 | `PLAN.md` |
| 写任何代码之前 | `DESIGN.md`（架构图 + 接口契约 + UI 设计令牌） |
| 改结构 / 加新模块 / 拆文件 | `MAINTAINABILITY.md` + `CODE_REVIEW_2026-09.md` |
| 发布 / 打包 | `RELEASE_NOTES.md` + `frontend/tools/build-release.py` |
| 前端（Electron）怎么组织 | 本文件第 7 节 + `frontend/PRODUCT.md` |
| 对外文案 | `README.md` / `README_EN.md`（**必须同步改两份**） |

## 1. 项目是什么

Windows 10/11 大众实时翻译软件。四个平级模式：**A** 麦克风同传、**B** 系统/指定应用声音、**C** 音视频文件字幕、**D** 屏幕 OCR 原位覆盖。STT 与翻译**独立选择**（本地/云自由组合），硬件路由 **独显 GPU → NPU → 核显 → CPU**，默认全本地离线。

**技术栈（当前）**

| 层 | 技术 |
|---|---|
| 前端 | Electron 44 + TypeScript（`frontend/`） |
| 后端 | Python 3.11/3.12（`voxsub/`），无 Qt 依赖 |
| 前后端通信 | NDJSON over stdio（`frontend/backend/ipc_server.py`） |
| 推理 | onnxruntime-directml · sherpa-onnx · RapidOCR · llama.cpp |
| 音频 | soundcard / PyAudioWPatch |
| 打包 | PyInstaller（sidecar）+ electron-builder |
| 测试 | pytest（后端）· Node 脚本 + CDP（前端） |

Qt 前端（PySide6 + QFluentWidgets）已迁至 `qt-legacy` 分支并**冻结**，不再更新。标签 `qt-final-5471fbf` 是它的最终状态。

## 2. 仓库结构

```
VoxSub/
  voxsub/           Python 核心（无 Qt）—— 识别/翻译/OCR/模型/硬件/配置
  tests/            Python 测试（323 passed）
  scripts/          辅助脚本（installer.iss、NPU 探针、模型抓取…）
  frontend/         Electron 前端
    src/main/       主进程（窗口、托盘、IPC、后端拉起）
    src/renderer/   渲染层（视图、样式、i18n）
    backend/        Python sidecar（ipc_server.py 是 IPC 适配层）
    tools/          验证与测试脚本
    scripts/        启动器与构建辅助
  启动Electron版.bat  一键启动（转发到 frontend/scripts/run-launcher.bat）
  assets/icon.ico   应用图标（窗口 + 托盘）
  Release/          正式发布物输出目录（用户约定，勿改）
```

## 3. 环境（踩过坑，逐条照做）

**a. 先验证 venv 能不能用。** 本仓库在 OneDrive 同步盘上、会被多台机器共用，`.venv/pyvenv.cfg` 可能指向另一台机器上不存在的解释器。开工第一步：

```bash
unset PYTHONPATH PYTHONHOME
./.venv/Scripts/python.exe -c "import sys; print(sys.version)"
```

报 `No Python at ...` 说明这个 venv 是别的机器建的，换用本机可用的环境重建。

**b. 所有 python 命令必须前缀 `unset PYTHONPATH PYTHONHOME`。** 宿主 agent 运行时会向终端注入 `PYTHONPATH` 指向它自己的 venv，不清掉会 import 到错位的包，症状是莫名其妙的 `ImportError` 或版本不对。

**c. 目录在 OneDrive 上。** 偶发文件锁：报 `os error 5` / `拒绝访问` / `Device or resource busy` 时等 1–2 秒重试，不要当成逻辑 bug。**在 OneDrive 目录里删大量文件后，目录本身可能被云同步短暂锁住**（内容已删、只剩空目录），等同步处理完再删。

**d. 不要 `git add -A` / `git add .`。** 工作区里有 1.1GB 的 `.venv-codex` 和 270+ 个 `.pytest-*` / `.npu-*` / `.smoke-*` 临时目录。虽然 `.gitignore` 现在已覆盖它们（`.venv-codex/`、`/.pytest-*/` 等），仍按路径显式 add，避免误提交。

**e. 测试会在仓库根生成临时目录**，用 `--basetemp` 指到唯一目录。

**f. 本机 bash 是 git-bash（MSYS），不是 PowerShell。** 用 POSIX 语法；`grep`/`find`/`rm` 等 MSYS 工具认 `/d/xxx` 路径，而 `robocopy`/`tar` 等**原生程序不认**（`tar` 需加 `--force-local`，否则把 `E:` 当成远程主机名）。

## 4. 常用命令

```bash
# ---- 后端（Python）
unset PYTHONPATH PYTHONHOME
./.venv/Scripts/python.exe -m pytest tests/ -q          # 全量（323 passed）

# ---- 前端（Electron）—— 在 frontend/ 目录下
npm run build                  # 构建 main + renderer + 静态资源
npm run typecheck              # tsc --noEmit
npm run check                  # typecheck + 调色板契约
npm run verify:palette         # 设计令牌契约（对比度/色相/层级分离）

# 启动（也可双击 启动Electron版.bat）
npm run launch                 # 增量构建后启动
npm run launch:silent          # 静默：窗口不显示、不抢焦点（自动化用）
npm run launch:debug           # 开远程调试端口 9222
npm run launch:dev             # 启动后打开 DevTools

# 前端测试（多数需要应用已用 --debug 启动）
npm run smoke:ui               # 界面冒烟（51 项）
npm run test:reported          # 人工测试报告的问题回归（34 项）
npm run test:overlay-menus     # 浮窗下拉菜单（11 项）
npm run test:overlay-display   # 浮窗显示模式 + 无 DOM 风暴（25 项）
npm run test:recorder          # 录音控件（15 项）
npm run test:launchers         # 启动器 .bat（23 项）
npm run test:launcher-cleanup  # 实例清理判定（14 项）
npm run test:launcher-e2e      # 实例清理（真实进程，6 项）
npm run test:icon              # 图标加载（6 项，独立 Electron 进程）
npm run verify:packaged        # 打包版可用性（9 项）

# ---- 发布
python frontend/tools/build-release.py          # 完整发布构建（含全量测试）
python frontend/tools/build-release.py --dir-only   # 只出免安装目录
```

## 5. 架构硬约束（`tests/test_architecture.py` 会自动拒绝）

这四条是自动化门禁，违反直接红灯，不要试图绕过：

1. **核心层不得导入 UI。** `voxsub/` 下禁止 `import voxsub.ui...`。跨层通信用回调、协议或不可变数据对象。目的是让诊断、模型管理、命令行工具**不加载 Qt 也能跑** —— Electron 版的后端正是靠这条才不用带 Qt。（`voxsub/ui/` 本身已在 `qt-legacy` 分支，但这条规则仍被测试守卫。）
2. **禁止无容量队列。** 生产代码里 `queue.Queue()` 必须显式写 `maxsize=`。用无界队列掩盖慢消费者是禁止的。
3. **函数分支复杂度预算 15。** 新增的 `if/for/while/try/BoolOp/IfExp/Match` 累计 ≥15 就失败。按「探测 → 决策 → I/O → 提交」分段拆开。
4. **正式文件不得直接覆盖写。** 文本走 `voxsub.file_io.write_text_atomically`，二进制走 `copy_file_atomically`。

`tests/test_packaging.py` 另外守卫打包契约。**它靠读取源文件文本来断言** —— 改了相关文件必须同步改它，否则红。

## 6. 代码约定

- **UI 文案必须双语。** 所有用户可见静态文案经 `frontend/src/renderer/i18n.ts` 的 `tr()`；改 UI 必须同时补简体中文与 English。**字幕正文、设备真实名称、文件名、日志、模型返回内容属于用户/系统数据，不得为了本地化改写。**
- **失败必须降级，不能伪装成功。** TTS 失败 → 仅字幕；OCR 翻译失败 → 保留识别原文；翻译单句失败 → 保留原文；云服务不可用 → 明确提示。生产路径的导入失败不允许用 stub 冒充正常。
- **降级 ≠ 伪报。** 硬件路由只在模型运行时真正支持时才选该设备；不通就写日志并往下走，不能把 CPU 回退显示成硬件加速。
- **资源要有唯一 owner 和可测试的 `close()`。** 组件的工作线程未退出前，不得关闭或替换它持有的原生/网络资源。
- **版本号有 6 处必须同步递增**：`voxsub/__init__.py`、`voxsub/release_notes.py`、`frontend/package.json`、`scripts/installer.iss`（3 处）、`README.md`、`README_EN.md`。
- **不要用 GUI 逐点自动化验证界面**（用户明确反对：太慢）。用 CDP + 脚本读 DOM/计算样式。

## 7. 前端（Electron）

**进程模型。** 主进程（`src/main/`）管窗口/托盘/原生对话框，并通过 stdio 拉起 Python sidecar（`backend/ipc_server.py`）；渲染层（`src/renderer/`）只做界面，业务逻辑一律在后端。IPC 协议：stdin 收 `{"id":N,"command":"...","args":{...}}`，stdout 发 `{"event":...}` 或 `{"id":N,"ok":true,"data":...}`。

**验证纪律（血泪教训，务必遵守）。**

- **一律用 `npm run launch:silent -- --debug` 做自动化验证。** 不带 `--silent` 会把窗口弹到用户桌面上抢焦点，用户已就此批评过。
- **断言要落到「用户能感知的东西」上**，而不是某个属性值。踩过两次：
  - 浮窗菜单测试读 `el.hidden` 报「已关闭」，而真实问题是 CSS 的 `display: flex` 盖掉了 `[hidden]`，菜单一直挂在屏幕上；
  - 显示模式测试若只看状态变量，会漏掉「IPC 往返死循环导致 DOM 被重建 18 万次/3 秒」。
  正确做法：查 `getComputedStyle()`，或用 `MutationObserver` 数真实 DOM 变更。

**四个必须先知道的坑**（都造成过真实故障）：

1. **`.bat` 文件内容必须纯 ASCII。** cmd.exe 按 OEM 代码页（本机 CP936）读 `.bat`，不是 UTF-8 —— 内容里的中文路径会被解成乱码，`if exist` 全部失败、窗口一闪而过。文件**名**可以是中文，文件**内容**不行。
2. **Electron 单实例锁 + 共用 userData。** userData 目录是 `%APPDATA%\voxsub-electron`（取自 `package.json` 的 `name`），**所有 VoxSub 检出共用它**。已有实例在跑时新实例会静默退出 —— 表现是「双击没反应」。清理逻辑见 `frontend/scripts/lib/electron-instances.mjs`。
3. **`hidden` 属性需要 CSS 兜底。** 作者样式里的 `display` 优先级高于 UA 的 `[hidden] { display: none }`。五个 CSS 文件都加了 `[hidden] { display: none !important }`，不要删。
4. **不要在渲染函数里发 IPC 通知。** 主进程会把状态回传给窗口，渲染函数再发通知就形成 `renderer → main → renderer` 无限往返。通知放在**用户动作**的处理函数里。

**启动器。** `启动Electron版.bat` 有三个入口（仓库根 / `frontend/` / 迁移前旧检出），全部转发到 `frontend/scripts/run-launcher.bat`（唯一实现）→ `frontend/scripts/launch.mjs`。改启动逻辑只改 `run-launcher.bat` 与 `launch.mjs`，**不要在入口文件里重复实现**。

## 8. 已知陷阱

**后端 / 打包**

- **git-bash 命令行传中文会被 GBK 转码**，`llama-server` 会拒收 ill-formed UTF-8。请求体必须走 UTF-8 文件（`--data @file`）。
- **silero VAD 对纯正弦音零触发**，冒烟素材必须用真实语音。
- **sherpa-onnx 的 provider 只认 `cpu`/`cuda`/`coreml`**，传 `DmlExecutionProvider` 会被拒绝并静默回退 CPU —— 需要时显式映射，别让它误导日志。
- **TTS 模型按包各异采样率输出**（8k / 22.05k），且 `samples` 是 float64；引擎内必须重采样到 16k 并转 float32。
- **PyInstaller 用模块级延迟导出时收集不到子模块**（`rapidocr.main` 就这么丢过一次），新增依赖后跑一次 `--ocr-smoke` 确认。
- **`subprocess.run(["npx", ...])` 在 Windows 上会 `FileNotFoundError`** —— npm/npx 是 `.cmd` 脚本，不带 `shell=True` 时找不到。用 `shutil.which()` 解析绝对路径。

**前端**

- **npm 会拦截 postinstall 脚本**（供应链安全策略），`electron`/`esbuild` 的二进制不会被下载。换机器后要手动跑 `node node_modules/electron/install.js`。
- **`window.resizeTo` 在 Electron 里不可靠**（只生效一次后窗口卡住）。测响应式布局用 CDP 的 `Emulation.setDeviceMetricsOverride`。
- **`Browser.getWindowForTarget` 在 Electron 不存在**。
- **`setContentProtection` 必须在窗口已显示之后调用**，在 `ready-to-show` 阶段调用会静默失效（返回成功但读回 `WDA_NONE`）。
- **升 Electron 后要重跑四项硬能力**：半透明 / 点击穿透 / 置顶无边框 / 捕获排除。

## 9. 交互偏好

- 与用户交流用中文。
- 改动前先说明；诊断类问题先给根因 + 证据链，等确认再动手。
- **所有自动化/测试后台静默、不占桌面**：不抢焦点、不弹窗、不占音频。
- 报告要区分「已验证」与「未验证」，证据不足就保留 `NOT_RUN` / `BLOCKED`，不要推测。
- 交付物给文件路径文本即可，不要用内联媒体展示。
