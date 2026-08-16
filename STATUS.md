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
- [~] M1 依赖安装：后台 `uv pip install` 进行中
- [ ] M1-spike：录音设备枚举 / onnxruntime providers / sherpa 模型冒烟
- [ ] M2 录音 → M3 ASR → M4 翻译 → M5 TTS → M6 编排 → M7 UI → M8 诊断 → M9 发布

## 环境事实（接手必知）

- 项目根：`D:\OneDrive\app_dve\VoxSub`（OneDrive 同步盘，git 正常工作）
- venv：`.venv`（uv 创建）。装依赖：`uv pip install --python .venv/Scripts/python.exe -r requirements.txt`；激活：`.venv\Scripts\activate`
- git：main 分支；身份 `DeepFirstLoaf <rzha0212@student.monash.edu>`；未添加远端
- 开发机：Win11 专业版 / i5-13600KF（无核显）/ RTX 4060 8GB / 32GB RAM —— **仅开发验证用，产品按大众 CPU 基准**（勿以 4060 为基准调参）
- 本机**无 NPU**（曾探测到 gvinput/OrayIddDriver 等，均为远程控制软件虚拟设备）
- 本机存在多个虚拟声卡（远程控制类）——loopback spike 的重点验证对象

## 关键决策记录（ADR 简版）

1. 推理统一走 onnxruntime：`onnxruntime-directml` 包（含 CPU EP 兜底），**不与标准 onnxruntime 同装**（包名冲突）——大众 CPU 基准，无 CUDA 硬依赖
2. ASR 用 sherpa-onnx zipformer 流式；节奏为句子级"说一句翻一句"，非逐词
3. 翻译双模：本地快档 OPUS-MT / 质量档 Qwen 1.5B（onnxruntime-genai，M4 引入）+ 可选云 API（OpenAI 兼容端点，用户自填 key）；Translator 抽象接口三实现
4. 设备路由：启动枚举 CPU/GPU/NPU → 按任务类型（asr/tts/translate）实测计分 → 静默降级链
5. 四层兼容防线：静态打包 / 装前体检 / 自检诊断中心 / 模型自愈（SHA256 + 断点续传，ModelScope 主源 + HF 备源）
6. 模型与运行时数据不入 git（`%LOCALAPPDATA%\VoxSub\models`）
7. 产品名：语幕 VoxSub（中英双名，需 M9 前做撞名查重）

## 下一步（当前唯一任务线）

1. 等待 M1 依赖安装完成（后台进程，notify 通知）→ 立即跑 M1-spike 三连验证
2. M1-spike 验收标准：
   - [ ] `soundcard` 枚举到 ≥1 麦克风 + ≥1 loopback 输出设备
   - [ ] onnxruntime `get_available_providers()` 含 `CPUExecutionProvider`（含 `DmlExecutionProvider` 更佳）
   - [ ] sherpa-onnx 加载 zipformer 流式模型 + 内置 VAD 模型，跑通一次"静音→识别→输出文本"
3. spike 全过 → 进入 M2（audio 模块：双源采集 + 环形缓冲 + 16k 重采样）；任一失败 → 记录 BLOCKED 与原因，换备选（如 pyaudiowpatch）

## 风险悬挂

- loopback 兼容性（本机虚拟声卡多，spike 即验证）
- onnxruntime-directml 与 sherpa-onnx 的版本兼容性（spike 验证）
- 模型下载源中国大陆可达性（M8 落地，ModelScope 主源）