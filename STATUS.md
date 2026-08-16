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
- [x] M1 骨架 + spike 全绿：录音枚举 / Dml+CPU providers / sherpa ASR+VAD 冒烟
- [x] M2 audio：voxsub/audio.py；loopback 真机闭环打通；12 测试绿
- [x] M3 asr：StreamingASR/WindowVAD/UtteranceSegmenter；真实中文识别 + 自动分句；9 测试绿
- [x] M4 翻译：opus 快档(Zh↔en 573/570ms) + qwen 质量档(llama-server HTTP, 0.69s/句) + cloud(白名单) + cache/prefetch/factory + 14 测试绿
- [x] M5 TTS：sherpa piper 中文模型 31.6MB + 英文 31.7MB，16k 归一；10 测试绿
- [x] M6 Pipeline：三模式编排 + C 模式 srt 导出 + 翻译容错延迟注入；7 测试绿
- [x] M7 UI：PySide6 + QFluentWidgets Soft Premium；主窗/字幕浮窗/托盘/设置/诊断；32 测试绿，桌面启动正常，自动接真实 Pipeline
- [x] M8 路由诊断：router/diagnostics/models(下载锁/断点续传)；15 测试绿；六项自检全 ok
- [x] **集成：全量 pytest 99 passed / 0 failed**
- [x] **端到端实盘**：A 模式实时字幕全链 1.01s + C 模式 srt 导出（真实模型全链路）
- [x] 首个可运行 exe（515MB onedir，自签+DigiCert 时间戳，GUI 冒烟通过）
- [~] 自审门禁：静态扫描干净，99 测试绿，独立审查代理运行中
- [ ] M9 发布：InnoSetup 安装器 + 撞名决策 + RELEASE_NOTES

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
9. 翻译质量档技术路线（2026-08-17 查证）：Qwen2.5 ONNX 权重在 HF 全部 gated(401, 含 onnx-community 镜像) → 弃 onnxruntime-genai，改 **llama-cpp-python + Qwen2.5-1.5B-Instruct-GGUF(Q4_K_M, 官方非 gated, ModelScope 有镜像)**；快档用 Xenova OPUS-MT onnx int8 + ORT 手写 seq2seq 循环（与主推理栈统一）

## 下一步（当前唯一任务线）

1. 等待 M1 依赖安装完成（后台进程，notify 通知）→ 立即跑 M1-spike 三连验证
2. M1-spike 验收标准：
   - [ ] `soundcard` 枚举到 ≥1 麦克风 + ≥1 loopback 输出设备
   - [ ] onnxruntime `get_available_providers()` 含 `CPUExecutionProvider`（含 `DmlExecutionProvider` 更佳）
   - [ ] sherpa-onnx 加载 zipformer 流式模型 + 内置 VAD 模型，跑通一次"静音→识别→输出文本"
3. spike 全过 → 进入 M2（audio 模块：双源采集 + 环形缓冲 + 16k 重采样）；任一失败 → 记录 BLOCKED 与原因，换备选（如 pyaudiowpatch）

## 风险悬挂

- **⚠️ 英文名撞名（2026-08-17 复查确认）**：GitHub 共存 8 个同名仓库，其中 2 个同类（离线字幕工具 `sixiaolong1117/VoxSub`、`yiifish/VoxSub`）；PyPI `voxsub`、NuGet `VoxSub` 均已占用。两个同名项目均极冷门（近零 star），无商标/侵权风险。**决策建议保留英文 VoxSub + 主打中文【语幕】**（国内 C 端以中文名传播为主）；若未来做国际化/开源检索需改名，候选 AltSub / LinguaSub / SubVox。**发布前由用户确认此决策。**
- loopback 兼容性（本机虚拟声卡多）→ 已源码级确认 isloopback 属性，M2 真机闭环通过
- onnxruntime-directml 与 sherpa-onnx 版本兼容 → 已通过（DirectML 生效）
- 模型下载源中国大陆可达性 → M8 已用 ModelScope 主源 + HF 备源 + 断点续传 + SHA256