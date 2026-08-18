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
- [x] M3 ASR：Zipformer 混合精度 beam search + Fun-ASR-Nano/Qwen3-ASR 模型广场适配；短句硬切与局部字幕
- [x] M4 翻译：OPUS 低资源兜底 + Hy-MT2 1.8B/7B GGUF 质量档 + cloud；严格译文输出校验与重试
- [x] M5 TTS：sherpa piper 中文模型 31.6MB + 英文 31.7MB，16k 归一；10 测试绿
- [x] M6 Pipeline：三模式编排 + C 模式 srt 导出 + 翻译容错延迟注入；7 测试绿
- [x] M7 UI：PySide6 + QFluentWidgets Soft Premium；主窗/字幕浮窗/托盘/设置/诊断；32 测试绿，桌面启动正常，自动接真实 Pipeline
- [x] M8 路由诊断：router/diagnostics/models(下载锁/断点续传)；15 测试绿；六项自检全 ok
- [x] **集成：全量 pytest 158 passed / 3 skipped / 0 failed（2026-08-18）**
- [x] **端到端实盘**：A 模式实时字幕全链 1.01s + C 模式 srt 导出（真实模型全链路）
- [x] 首个可运行 exe（515MB onedir，自签+DigiCert 时间戳，GUI 冒烟通过）
- [x] v0.2 用户测试修复：B 模式默认端点、采集异常可见、Qt 线程桥、字幕浮窗自动显示
- [x] 音视频文件选择 + 内置 ffmpeg 自动提音；麦克风/输出设备选择
- [x] Windows 进程级 loopback：指定应用及子进程树，真机验证可捕获目标且排除其它进程声音
- [x] App 内置 DEBUG 模式与实时日志；日志文件被占用时自动退化为内存日志
- [x] Hermes Soft Premium（VARIANCE=6 / MOTION=4 / DENSITY=3）第二轮桌面截图验收
- [x] 模型广场：精选新模型、质量排序、硬件推荐、双源自动切换、后台下载/卸载/选择
- [x] 加速器优先级：独显 GPU → NPU → 核显 → CPU；运行时不支持时可解释降级，禁止伪报
- [x] llama.cpp b10470 CPU/Vulkan/OpenVINO 三后端构建矩阵与 SHA256 锁定
- [x] Qwen3-ASR 自然停顿分句 + 独立识别队列；常规积压缓冲而不再静默丢音
- [x] 用户识别调优页（每项带面向非 AI 用户的 `i` 说明）、同传录音、会话导出/清空/复制
- [x] 字幕浮窗字号修复、悬停控件、锁定鼠穿透；锁定后可在浮窗原地悬停解锁
- [x] 调优宽范围 + 保存/放弃事务 + 悬停 `i`；启停 Pipeline 移出 UI 线程
- [x] 自审门禁：compileall + 153 测试 + 打包前/打包后 Soft Premium Windows GUI 真实点击验收
- [x] v0.3.2-beta 已生成自签名安装包与 SHA256，写入 `Release`（203.37 MiB；SHA256 `1F480C61...D75B1F25`）
- [x] v0.3.3-beta：修复真实字体渲染和解锁后原生穿透位残留；153 passed / 3 skipped，真实鼠标解锁/点击/拖动验收通过，Release 安装包 203.41 MiB（SHA256 `A4C527FC...BC14309B`）
- [x] v0.3.4-beta 源码：主窗/设置/模型广场/诊断/浮窗完成 Soft Premium 统一改版；全量 154 passed / 3 skipped；PyInstaller dist 已生成
- [x] v0.3.4-beta 安装包：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.3.4-beta.exe`（203.41 MiB）；SHA256 `FF08F72ECE76AE6D8B0A7AA555A6572D55FB2FB9A0386E8C37CFBF1D5BDE3827`
- [ ] v0.3.4-beta 签名：当前构建环境没有可用的 VoxSub 代码签名证书，安装包暂为未签名状态
- [x] v0.3.5-beta 源码：翻译档位单选控件改为稳定圆环 + 圆心；新增 SenseVoice Small INT8 和 Hy-MT2 1.8B/7B 的 Q5/Q8 档位；全量 158 passed / 3 skipped
- [x] v0.3.5-beta 安装包：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.3.5-beta.exe`（203.37 MiB，未签名）；SHA256 `BD2B56302ABFF06DE6E2FE3DAAB8B4916D49C5681E3E1E050699D0F0B5FAC2FF`
- [x] v0.3.6-beta 源码：基础 VAD 随包分发并首用自修复；Pipeline 初始化事务化；会话、日志和诊断报告导出使用应用内保存框 + 后台原子写入；全量 164 passed / 3 skipped
- [x] v0.3.6-beta 安装包：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.3.6-beta.exe`（205.09 MiB，未签名）；SHA256 `EA7B3BD9F73DD97AC876723F8B90CB2726F6843B97A39F06F7973C8807B9A57E`
- [x] v0.3.7-beta 源码：云 STT 与云翻译独立配置、旧配置迁移、四种本地/云端混合链路；云 STT 分段独立队列；修复云翻译默认超时；全量 177 passed / 3 skipped
- [x] v0.3.7-beta 安装包：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.3.7-beta.exe`（205.05 MiB，未签名）；SHA256 `701E1AC4C2188629D503318A7F8EE758C5D53BF9149051D51BA6E523AF4AE698`；隔离配置启动冒烟通过
- [x] v0.3.8-beta 源码：单选、开关和模型广场筛选统一为稳定新版控件；Inno Setup 支持英/简中/繁中并按 Windows UI 语言自动匹配；全量 181 passed / 3 skipped
- [x] v0.3.8-beta 安装包：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.3.8-beta.exe`（205.11 MiB，未签名）；SHA256 `A8B2D9AD82A7544033F6640116F5E783848F3F49EAB0A43DB55A6B4EA32BA99F`；隔离配置启动冒烟通过
- [x] v0.3.9-beta 源码：设置与模型广场改为主窗内置页面；内置 OPUS/Zipformer 支持缺失文件检测与在线修复；全量 190 passed / 3 skipped
- [x] v0.3.9-beta 安装包：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.3.9-beta.exe`（205.15 MiB，未签名）；SHA256 `D500E7045B503C58F13C81ECCA37675205097157F13CB48B088ED089A4182F29`
- [!] 已知问题：Intel NPU 目前可能被检测到但未真正用于推理，模型可能回退到 GPU/CPU；后续继续排查调度、运行时和模型支持链路
- [ ] M9 发布候选：完成更多真实推理与无独显 NPU 轻薄本验收

## 环境事实（接手必知）

- 项目根：`D:\OneDrive\app_dve\VoxSub`（OneDrive 同步盘——**偶发文件锁，报 os error 5 时等 1-2s 重试**）
- venv：`.venv`（uv 创建，2026-08-17 因 argostranslate 冲突重建过一次）。装依赖：`uv pip install --python .venv/Scripts/python.exe <pkg>`
- **关键坑：本机 Hermes 向终端注入 PYTHONPATH 指向 hermes-agent venv——所有 python 命令必须前缀 `unset PYTHONPATH PYTHONHOME` 再调 `.venv/Scripts/python.exe`，否则 import 会错位加载 hermes 的包**
- git：main 分支；身份 `DeepFirstLoaf <rzha0212@student.monash.edu>`；未添加远端
- 开发机：Win11 专业版 / i5-13600KF（无核显）/ RTX 4060 8GB / 32GB RAM —— **仅开发验证用，产品按大众 CPU 基准**
- 本机无 NPU；存在大量虚拟声卡（远程控制/变声软件），loopback 兼容性是重点验证对象

## 关键决策记录（ADR 简版）

1. 推理统一走 onnxruntime：`onnxruntime-directml` 包（含 CPU EP 兜底），**不与标准 onnxruntime 同装**（包名冲突）——大众 CPU 基准，无 CUDA 硬依赖
2. ASR：内置 Zipformer 作为低资源实时兜底，模型广场提供 Fun-ASR-Nano 2512、Qwen3-ASR 0.6B 与 SenseVoice Small INT8；离线模型只在句界解码
3. STT/翻译：本地 ASR 或独立云 STT，与本地 OPUS/Hy-MT2 或独立云翻译自由组合；音频采集、VAD 与云 STT 请求分离，ASR 与翻译分离队列，慢网络不堵采集
4. 设备路由：独显 GPU → NPU → 核显 → CPU。先验证模型运行时支持，再选择/实测；sherpa 不支持 DML/NPU 时明确回 CPU
5. 四层兼容防线：静态打包 / 装前体检 / 自检诊断中心 / 模型自愈（SHA256 + 断点续传，ModelScope + GitHub/Hugging Face 双源）
6. 模型与运行时数据不入 git（`%LOCALAPPDATA%\VoxSub\models`）
7. 产品名：语幕 VoxSub（中英双名，需 M9 前做撞名查重）
8. UI 风格（2026-08-17 用户选定）：**柔和高级感 Soft Premium**；三档主题（浅/深/跟随系统）；技术底座 QFluentWidgets 1.11.3（Fluent 设计语言、无边框窗、darkdetect 主题跟随）；详细令牌见 DESIGN.md「UI 设计规范」
9. 模型广场采用“小而精选”目录：只列已有运行时适配、许可证明确、在相近资源档仍有价值的模型；任务内按质量分降序
10. GGUF 质量档使用 Hy-MT2 + llama-server；构建时固定打包 CPU/Vulkan/OpenVINO，按独显→Intel NPU→核显→CPU 选择。AMD/Qualcomm NPU 仅在兼容 EP/模型存在时启用
11. 生成式 ASR 不沿用 Zipformer 的小窗口反复解码；VAD 只负责自然分句，完整句由独立识别线程处理
12. 可选录音明确由用户开启，仅保存到本地 WAV；默认仍不落盘

## 下一步（当前唯一任务线）

1. 从模型广场分别下载 Fun-ASR-Nano/Qwen3-ASR 与 Hy-MT2，做真实中文、混合语言、噪声素材 A/B 对比
2. 在至少一台无独显 NPU 轻薄本验证硬件识别和实际后端日志；Intel NPU 重点验证 OpenVINO，AMD/Qualcomm 记录兼容性边界
3. 使用 v0.3.8-beta 在更多真实音视频素材上继续验证识别质量、断句参数、云端兼容服务和长时间运行稳定性

## 发布约定（2026-08-17 用户指定）

- **正式版安装包/发布物统一编译到 `D:\OneDrive\app_dve\Release`**（用户约定路径，勿改）
- 内测/开发产物在 `dist\`；正式发布版才进 Release
- 每个正式版 = 安装包 + SHA256 + 签名 + RELEASE_NOTES 更新
- **每次源码版本号迭代必须在同一轮完成安装包、签名、SHA256 与 Release 核对；未打包不得宣布该版完成。**
- 撞名决策、OV 证书签名、商店上架等正式版事项见 RELEASE_NOTES.md

- **⚠️ 英文名撞名（2026-08-17 复查确认）**：GitHub 共存 8 个同名仓库，其中 2 个同类（离线字幕工具 `sixiaolong1117/VoxSub`、`yiifish/VoxSub`）；PyPI `voxsub`、NuGet `VoxSub` 均已占用。两个同名项目均极冷门（近零 star），无商标/侵权风险。**决策建议保留英文 VoxSub + 主打中文【语幕】**（国内 C 端以中文名传播为主）；若未来做国际化/开源检索需改名，候选 AltSub / LinguaSub / SubVox。**发布前由用户确认此决策。**
- loopback 兼容性（本机虚拟声卡多）→ 已源码级确认 isloopback 属性，M2 真机闭环通过
- onnxruntime-directml 与 sherpa-onnx 版本兼容 → 已通过（DirectML 生效）
- 模型下载源中国大陆可达性 → 2026-08-18 已逐一 HEAD 验证目录内 GitHub/HF/ModelScope 地址均返回 200
