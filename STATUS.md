# 语幕 VoxSub —— 项目状态书 (STATUS)

> 本文件是项目的"交接单"。任何 AI / 开发者接手时：先读本文件 + TODO.txt 即可定位当前进度。**每次里程碑 / 关键节点后必须更新**。

## 项目一览

- 一句话：Windows 10/11 大众实时翻译软件（麦克风同传 / 系统声音字幕 / 文件双语字幕导出），默认全本地离线运行
- 语言：Python 3.11；技术栈全表见 PLAN.md
- 核心库：sherpa-onnx / onnxruntime-directml / soundcard / argostranslate；PySide6（M7）· PyInstaller（M9）后续追加

## 文档地图（按阅读顺序）

| 文档 | 内容 | 何时读 |
|---|---|---|
| STATUS.md（本文件） | 当前进度、环境事实、决策记录、下一步 | 每次接手先读 |
| TODO.txt | 按时间戳分段的任务追踪（[x]/[ ]） | 开发中每次更新 |
| REQUIREMENTS.md | 需求与范围、P0/P1/P2、明确不做的事 | 需求争议时 |
| PLAN.md | 技术选型理由、里程碑 M1-M9、风险 Top3 | 开始/续接开发前 |
| DESIGN.md | 架构图、模块划分、接口契约、存储设计 | 写任何代码前 |
| README.md | 项目入口与开发环境 | 搭建环境时 |

## 当前进度（里程碑视角）

- [x] 阶段0-2：REQUIREMENTS / PLAN / DESIGN 完成（commit `4421e64`）
- [x] M1 骨架：git init(main) + venv(CPython 3.11.15) + requirements.txt
- [x] M1 spike 全绿：
  - [x] 录音设备枚举 OK（12 输入设备：Comica 无线麦 ×2、网易虚拟、Dubbing AI、Steam Streaming、e2eSoft iVCam、Realtek、NVIDIA HD）
  - [x] onnxruntime providers = **DmlExecutionProvider + CPUExecutionProvider**（DirectML 生效）
  - [x] sherpa-onnx ASR+VAD 加载、create_stream / accept_waveform / decode 循环跑通
  - [!] **BLOCKED: soundcard loopback 枚举不到**（include_loopback=True 无结果）→ M2 audio 子代理处理
- [~] M2 audio 模块 + M3 asr 模块：**子代理并行开发中**
- [ ] M4 翻译 → M5 TTS → M6 编排 → M7 UI → M8 诊断 → M9 发布

## 环境事实（接手必知）

- 项目根：`D:\OneDrive\app_dve\VoxSub`（OneDrive 同步盘——**偶发文件锁，报 os error 5 时等 1-2s 重试**）
- venv：`.venv`（uv 创建，2026-08-17 因 argostranslate 冲突重建过一次）。装依赖：`uv pip install --python .venv/Scripts/python.exe <pkg>`
- **关键坑：本机 Hermes 向终端注入 PYTHONPATH 指向 hermes-agent venv——所有 python 命令必须前缀 `unset PYTHONPATH PYTHONHOME` 再调 `.venv/Scripts/python.exe`，否则 import 会错位加载 hermes 的包**
- git：main 分支；身份 `DeepFirstLoaf <rzha0212@student.monash.edu>`；未添加远端
- 开发机：Win11 专业版 / i5-13600KF（无核显）/ RTX 4060 8GB / 32GB RAM —— **仅开发验证用，产品按大众 CPU 基准**
- 本机无 NPU；存在大量虚拟声卡（远程控制/变声软件），loopback 兼容性是重点验证对象

## 关键决策记录（ADR 简版）

1. 推理统一走 onnxruntime：`onnxruntime-directml` 包（含 CPU EP 兜底），**不与标准 onnxruntime 同装**（包名冲突）——大众 CPU 基准，无 CUDA 硬依赖
2. ASR 用 sherpa-onnx zipformer 流式；节奏为句子级"说一句翻一句"，非逐词
3. 翻译双模：本地快档 OPUS-MT / 质量档 Qwen 1.5B（onnxruntime-genai，M4 引入）+ 可选云 API（OpenAI 兼容端点，用户自填 key）；Translator 抽象接口三实现
4. 设备路由：启动枚举 CPU/GPU/NPU → 按任务类型（asr/tts/translate）实测计分 → 静默降级链
5. 四层兼容防线：静态打包 / 装前体检 / 自检诊断中心 / 模型自愈（SHA256 + 断点续传，ModelScope 主源 + HF 备源）
6. 模型与运行时数据不入 git（`%LOCALAPPDATA%\VoxSub\models`）
7. 产品名：语幕 VoxSub（中英双名，需 M9 前做撞名查重）
8. UI 风格（2026-08-17 用户选定）：**柔和高级感 Soft Premium**；三档主题（浅/深/跟随系统）；技术底座 QFluentWidgets 1.11.3（Fluent 设计语言、无边框窗、darkdetect 主题跟随）；详细令牌见 DESIGN.md「UI 设计规范」

## 下一步（当前唯一任务线）

1. 等待 M1 依赖安装完成（后台进程，notify 通知）→ 立即跑 M1-spike 三连验证
2. M1-spike 验收标准：
   - [ ] `soundcard` 枚举到 ≥1 麦克风 + ≥1 loopback 输出设备
   - [ ] onnxruntime `get_available_providers()` 含 `CPUExecutionProvider`（含 `DmlExecutionProvider` 更佳）
   - [ ] sherpa-onnx 加载 zipformer 流式模型 + 内置 VAD 模型，跑通一次"静音→识别→输出文本"
3. spike 全过 → 进入 M2（audio 模块：双源采集 + 环形缓冲 + 16k 重采样）；任一失败 → 记录 BLOCKED 与原因，换备选（如 pyaudiowpatch）

## 风险悬挂

- loopback 兼容性（本机虚拟声卡多，spike 即验证）→ 已获源码级确认（isloopback 属性），M2 实现验证中
- onnxruntime-directml 与 sherpa-onnx 的版本兼容性（spike 验证）→ 已通过
- 模型下载源中国大陆可达性（M8 落地，ModelScope 主源）
- **⚠️ 英文名撞名（2026-08-17 查重）：GitHub 存在两个同名同类项目 `sixiaolong1117/VoxSub`（Whisper 字幕工具）与 `yiifish/VoxSub`（离线字幕工具集）——M9 发布前必须复查并决策：保留 VoxSub 或更换英文名（中文名"语幕"暂未发现明显撞名）**