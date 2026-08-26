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
- [x] Intel NPU 基础链路：Intel AI Boost（驱动 32.0.100.4841）上 Hy-MT2 1.8B Q4/Q6/Q8 已通过应用自动调度和禁用 CPU 回退的强制 NPU 推理
- [x] v0.4.0-beta 源码：真机验证通过的 no-NPUW OpenVINO 运行时进入正式构建；NPU 真实翻译探针、当前句后端重试和动态核显/CPU 降级完成；7B 大模型断流续传完成；全量 209 passed / 2 skipped
- [x] v0.4.0-beta 安装包：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.4.0-beta.exe`（204.73 MiB，开发者自签名）；SHA256 `408AE75789EDDD880BF1A50976363CA27564C80D27988382EA6B5AE887BDDFCA`；隔离配置启动冒烟通过
- [!] Intel NPU 剩余边界：Hy-MT2 7B Q4/Q6/Q8 仅按公开兼容资料列为待验证，精确权重尚未真机实测；现有 sherpa-onnx ASR 与 OPUS 运行时不支持 NPU
- [x] v0.4.1-beta 源码：模型存储初始化、升级保留旧路径、用途目录整理、模型库迁移/导入、首次启动更新说明、关于页历史更新说明和全屏页面导航修复已完成
- [x] v0.4.1-beta 修复包：调优按钮、异步目录选择、后台模型迁移、诊断模块打包和 Teams 宿主进程捕获已修复；全量 193 passed / 7 skipped；安装包 204.80 MiB（开发者自签名），SHA256 `4196F2DF...9D96CF8`；隔离配置启动与真实目录选择器冒烟通过
- [x] v0.4.2-beta 源码候选：完成配置 schema、全链路有界队列、TTS worker、原子文件提交、Pipeline/UI/下载/硬件/llama 启动职责拆分及自动架构守卫；外观页译文字号和浮窗不透明度改用可靠箭头；全量 229 passed / 8 skipped
- [x] v0.4.2-beta 已发布：标签指向 `d912f8e`；安装包 214,762,511 字节（204.81 MiB），SHA256 `9B691BF2FE6B5F9F3AD7C0B53CB936547D38550CC894BDC8FAA43684770E99D6`；安装包与 `.sha256` 已上传 GitHub 预发布 Release；未签名
- [x] v0.5.0-beta 源码候选：新增独立“智能上下文（动态断句）”调优模式；流式 ASR 语义延长、生成式/云 STT 片段合并、不可续期硬等待、保守可审计纠偏和可关闭的轻度语气词清理完成；旧模式旁路；全量 243 passed / 8 skipped
- [x] v0.5.0-beta 已发布：标签指向 `0791575`；安装包 214,795,950 字节（204.85 MiB），SHA256 `38CF47DE43CB39B45BAF8241464A7C06B5AEF6AF28CE15FF76E7B32063047EA8`；安装包与 `.sha256` 已上传 GitHub 预发布 Release；未签名
- [x] v0.6.0-beta 候选：智能上下文新增单行双语动态草稿、Zipformer 140ms partial、partial 保守纠偏、最新 revision 临时翻译和终句优先策略；Qwen3/生成式/云 STT 由 Zipformer 流式旁路提供草稿，所选模型负责最终定稿；新增默认开启、仅智能上下文可调的“实时双语草稿”开关，关闭后不加载旁路且不影响智能断句、纠偏和语气词清理；旧模式不发送临时翻译请求；真实 Qwen3 + 旁路链路产生 7 次草稿更新；全量与构建门禁均为 251 passed / 8 skipped；安装包 214,819,528 字节，SHA256 `585116D524FD75BD9E672339FCF04CC681F9FFF9BC3E6F1B91E3BF8FBAC6E2D2`；隔离启动通过；未签名、未发布 Release
- [x] v0.7.0-beta 候选：模型广场新增 MeloTTS 中英双语、AISHELL3 中文轻量和 LJSpeech 英文轻量 TTS；语音朗读设置可按中英语种选择已安装模型，开关和切换可在当前 Pipeline 立即生效；实时双语草稿只朗读定稿译文；兼容旧 `tts/zh` 与 `tts/en`，支持运行中安装后惰性发现；诊断按当前选择做冒烟；构建门禁 `256 passed / 7 skipped`；安装包 214,865,840 字节，SHA256 `252DE9C8881D1E268DA6053158F01368539B56D7A86902DDAE30D0E089F46D20`；隔离启动通过；开发者自签名，未发布 Release
- [x] v0.7.1-beta 候选：英文全大写 partial 在显示层转为句子式大小写；草稿翻译由可被连续 partial 持续推迟的防抖改为合并节流，兼容的已完成译文保留至新版接替，终句仍优先；构建门禁 `262 passed / 7 skipped`；安装包 214,877,960 字节，SHA256 `64B54C296C3CF5A53BB867889DA9CF479373BD79933E1377EB70C62BCEDC2C0B`；隔离启动通过；开发者自签名，未发布 Release
- [x] v0.7.2-beta 已发布：安装器改用有界的专用退出协议并为旧版保留快速进程树回退，消除约 30 秒假死和最终关闭失败；Pipeline 工作线程共享 8 秒退出截止时间；构建门禁 `265 passed / 7 skipped`；安装包 214,817,504 字节（204.87 MiB），SHA256 `313714AE3C9557B88EDBCEBBFCB768A15BBDD65915A5266E0B3EB1D82CAF2211`；打包程序退出协议冒烟通过；开发者自签名；安装包与 `.sha256` 已上传 GitHub 预发布 Release
- [x] v0.8.0-beta OCR 候选：新增一次性截图 OCR 翻译和选定区域实时 OCR 原位覆盖；RapidOCR/几何/翻译缓存、屏幕采集、覆盖层、页面与单所有者 worker 已按职责拆分；像素只在本机内存处理，云端只接收识别文字；构建门禁 `276 passed / 8 skipped`，成品 OCR 自检、真实 Windows 捕获/覆盖排除及隔离启动/退出均通过；安装包 277,266,873 字节，SHA256 `04997F7A48B5EB878C4303F6BCCAEC5A72CDD82864131DC5E994FB03AE8E1365`；未签名，等待用户验证，不发布 Release
- [x] v0.9.0-beta OCR 完善候选：OCR 改为与 A/B/C 平级的 D 模式，共用常驻翻译方向；进入模式后台预热快速 OCR 与翻译器，变化画面使用 Small 快速结果，稳定画面在实际 GPU 可用时用所选质量模型纠偏。实时首轮限制为 20–24 个版面块和约 2.2–2.4K 字符，批量 JSON 异常不再逐行回退；采集队列只保留最新画面并丢弃过期结果。实时覆盖捕获排除已修正，不再周期隐藏闪烁；碎片/连续正文按段落块合并，主阅读列优先，目标语言界面过滤，长译文按度量整块换行，空结果保留旧画面重试。安装器将安全收尾扩至 5 秒、强制关闭后复核 2 秒，运行标记保持到进程真正终止；成品退出握手 2.34 秒通过。构建门禁 `306 passed / 7 skipped`，成品 OCR 自检与真实 Windows 捕获排除通过。安装包 277,329,504 字节（264.49 MiB），SHA256 `CE797039...E02448D`；继续沿用同版本候选，开发者自签名，等待用户安装验收，不发布 Release
- [ ] M9 发布候选：完成更多真实推理与无独显 NPU 轻薄本验收

## 环境事实（接手必知）

- 项目根：`D:\OneDrive\app_dve\VoxSub`（OneDrive 同步盘——**偶发文件锁，报 os error 5 时等 1-2s 重试**）
- venv：`.venv`（uv 创建，2026-08-17 因 argostranslate 冲突重建过一次）。装依赖：`uv pip install --python .venv/Scripts/python.exe <pkg>`
- **关键坑：本机 Hermes 向终端注入 PYTHONPATH 指向 hermes-agent venv——所有 python 命令必须前缀 `unset PYTHONPATH PYTHONHOME` 再调 `.venv/Scripts/python.exe`，否则 import 会错位加载 hermes 的包**
- git：main 分支；身份 `DeepFirstLoaf <rzha0212@student.monash.edu>`；远端 `origin` 为 `https://github.com/tuotuonuts/VoxSub.git`
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

1. 用户安装验收 v0.9.0-beta：先确认运行中的 VoxSub 能由安装器在约 2–5 秒内自动关闭且不再报错；再检查实时覆盖不闪烁、邮件/文档连续正文合并为大块且无裁切、主正文未被侧栏挤掉，以及 A/B/C/D 布局、翻译方向、快速结果/稳定纠偏、截图/上传/导出和非 C 盘分离缓存；验收前不创建新 Release
2. 收集印刷体、手写体、艺术字和竖排文字样本，对 PP-OCRv6 与 PP-OCRv5 可复现评测；不对艺术字做无证据保证
3. 继续验证 Hy-MT2 7B Q4/Q6/Q8 的 Intel NPU 内存与算子兼容性；使用已发布 v0.7.2-beta 验证实时语音链路和安装升级稳定性

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
