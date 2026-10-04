## 公共 UI 组件收编（本轮）

- 用户“新增 UI 优先公共组件”约定已写入 AGENTS.md，并建立 renderer/ui/README.md 组件索引。
- 标准按钮/筛选按钮、卡片、文本输入/下拉/单选/开关、设置式分页导航收编；主导航、设置、诊断、开发者、日志容量、模型广场、OCR、工作区、迁移向导及路径选择已有调用已迁移，业务 IPC 与生命周期不搬入组件。
- 新真实组件测试16/16、最终前端check/build和Python安全回归1059 passed；隐藏真实Electron页面验收16/16，138窗口采样0可见/焦点违规，正常退出无进程残留。详见 docs/SHARED_UI_ACCEPTANCE.md。
- 源码/本地构建已更新，安装版/用户配置/历史未改；未推送、打包或发布。最终本地提交见仓库外shared-ui-acceptance/DELIVERY.json。

## 主页任务提示文案修复（本轮）

- 主页不再显示 set_config/set_langs 等命令名或任务 ID：59 命令可读名称，配置使用“设置已保存”等自然提示；失败/取消/未知任务和中英文覆盖，原技术信息留在诊断日志。
- 新生产 DOM 回归9/9、前端check/build、Python安全回归通过；隐藏真实Electron+Python IPC验证3种成功提示，66次窗口监测0聚焦/显示违规，正常退出。详见docs/HOME_STATUS_COPY_ACCEPTANCE.md。
- 仅源码/本地构建，安装版与用户配置/历史未改；本轮本地提交见仓库外user-status-copy-acceptance/DELIVERY.json，未推送/打包/发布。

## 智能上下文识别/翻译优化（本轮）

- 智能上下文 A/B 实时与 C 文件字幕加入有界近期定稿参考；质量/云支持，快档和普通模式旁路；草稿、失败、旧配置不进入参考。纠偏/断句/填充词安全与性能同步优化。
- Python 安全全套 1059 passed, 4 skipped, 17 deselected, 1 xfailed, 1 warning in 56.14s；前端完整 check 与架构门禁 PASS。真实 Hy-MT2 CPU 文本对照观察到一项消歧改善，也保留一项未消歧，不能称质量全面通过。详见 docs/SEMANTIC_CONTEXT_ACCEPTANCE.md。
- 仅源码，本地提交见仓库外 20261005-semantic-context/DELIVERY.json；不打包、不发布、不修改用户配置/历史/安装版。本轮不改变上一任务的模型未上架/单模型未集成边界。

## 诊断、开发者模式与专用模型验收（2026-10-05）

- 初始 main@e840097 已按授权推送 GitHub。
- 诊断、容量 MB/GB、5 秒 10 次版本号开发者入口、模型元数据追踪完成；1021 Python 安全测试、前端完整 check、合同及隐藏真实 IPC 通过。详见 docs/DIAGNOSTICS_MODELS_ACCEPTANCE_2026-10-05.md。
- 两款专用模型已实际加载并生成；补充短句基础语义与 Index 时间戳有限通过，但 CPU 2 线程 RTF 约 8～14，应用自身缺 torch/transformers/torchaudio，长文件/生产运行时/双源下载未验收。**未上架，单模型 UI 与生产流程仍待完成；不能标成全需求完成。**
- 全程静默，无用户音频采集/播放，未打包或更换安装版；最终本地提交与 push 状态见仓库外 20261005-diagnostics-models/DELIVERY.json。

# 语幕 VoxSub —— 项目状态书 (STATUS)

> 本文件是项目的"交接单"。任何 AI / 开发者接手时：先读本文件 + TODO.txt 即可定位当前进度。**每次里程碑 / 关键节点后必须更新**。

## 当前：系统/应用声音 WAV 录音修复完成；专用模型推理资源阻塞（本轮目录标签 2026-10-04）

A/B 同时录音共同放行前端开关、后端能力及真实 WAV 创建；B 保存当前所选系统/应用音源，不额外开麦克风。暂停不写、收尾后才可改偏好，旧 WAV 路径不冒报；补齐中英文提示与测试。安全 Python 1002 passed / 7 skipped / 7 deselected / 1 xfailed（既有告警 1），最终录音定向 28 passed；前端 check、26 验收契约 + 11 负对照、renderer 构建通过。最新隐藏 Electron + 真实 Python IPC 复验 B on/off、A/B/C/D 和隔离配置通过；原生窗口 52 次采样 0 显示/聚焦违规，正常退出无本轮测试实例遗留。硬件回环/指定应用真实音频采集 NOT_RUN；真实 WAV 落盘使用合成帧，不采集或播放用户声音。

Granite 4.0 1B Speech / Index-Echo S2TT 2B 固定版本官方完整下载与 SHA 校验 PASS（分别约 4.64GB / 5.90GB），隔离模型依赖和真实 WAV 输入预处理通过；但可用内存持续约 5GiB，加载前保守准入拒绝，实际生成/语义/性能 NOT_RUN，模型推理 BLOCKED，不能声明两款已验收或上架。这部分需求仍待内存资源允许后续验收。未接入产品、未修改用户配置/历史/安装版/已安装模型；大陆模型下载源 NOT_RUN，后续上架仍须海外/大陆双源。

报告：D:/OneDrive/app_dve/VoxSub/docs/RECORDING_S2TT_ACCEPTANCE_2026-10-04.md；证据与最终提交：D:/OneDrive/app_dve/20261004-recording-s2tt/DELIVERY.json。基线 dc7fa8321b0b40d1f99ec50a0afb7b71d3f33393；本地提交，不推送、不打包。原始文件备份 .backups/20261004-recording-s2tt，回滚方法见报告。

## 历史：模型语言范围联动与静默验收完成（2026-10-04）

识别语言按有效 ASR 与翻译输入能力求交集，目标语言按翻译方向决定、不受 ASR 限制；保留合法选择，失效时调整并提示。模型切换/语言保存未确认、无交集或断连时禁止开始，结束仍可用。能力查询与入口校验由 Python 统一提供；拒绝不兼容组合后才允许打开音频和加载模型。覆盖并发切换、迟到请求、失败重试与 OCR 独立路由。

安全 Python **986 passed / 5 skipped / 17 deselected / 1 xfailed**；前端 check 全通过，语言控制器/队列和验收契约 26/26 + 11 变异负对照通过。隐藏真实 Electron + Python IPC 复验英文识别→中文翻译、设置页云档位扩大目标范围、配置保存、非法组合拒绝、DOM 焦点稳定；会话未运行，应用正常退出。隔离配置、不采集/播放音频、不改用户历史或安装版。

当前应用能力合同限中/英/日/韩，Parakeet 暂仅开放英文路由；不按宣传语言总数扩展。实时音频、云 API、完整推理、多语质量、GPU/NPU 与安装升级 NOT_RUN。报告 docs/LANGUAGE_CAPABILITIES_2026-10-04.md；基线 a04caff8545489a23352f71c3f0fc64e4e2c78fb，最终本地提交与 clean 状态见 D:/OneDrive/app_dve/language-capabilities-20261004-145103/DELIVERY.json。不推送、不打包。

## 历史：模型广场扩容与静默验收完成（2026-10-04）

源码上架 Moonshine Tiny EN v2（英语离线）、Parakeet TDT v3 INT8（欧洲多语，不含中文）、Kokoro v1.1 INT8（中英朗读）。三个海外源及大陆源完整下载/校验通过，HF 大陆选项明确标记第三方并固定版本/逐文件 SHA；新增自动/海外优先/中国大陆优先选择，修复真实安装按钮 progress 接线、特殊路径 URL 编码及 OneDrive 缓存清理误报。

安全 Python **967 passed / 5 skipped / 17 deselected / 1 xfailed**；前端 check/契约/renderer 构建通过。真实 CPU 英语/德语识别和中英 TTS 数组生成通过，未播放；真实隐藏 Electron 下载按钮（缓存归档、真实 IPC）、双语模型选择通过，2617 次监测无显示/聚焦违规。验证使用隔离配置与模型，用户配置和安装版不替换。

报告：docs/MARKETPLACE_EXPANSION_2026-10-04.md；基线 8d74ae448c1295687e265dbd2e1f78f513c93313；最终本地提交与 clean 状态见 D:/OneDrive/app_dve/marketplace-expansion-20261004-213622/DELIVERY.json。不推送、不打包。Qwen3-ASR 1.7B / VibeVoice-ASR / TranslateGemma 4B 因适配或授权问题 BLOCKED，未上架。大陆源本机可访问不代表中国大陆所有网络可用；GPU/NPU、实时采集、TTS听感、新模型完整文件→SRT及安装升级 NOT_RUN。

## 历史：静默真实验收返修完成（2026-10-04）

用户确认修复“C 文件处理完成后切回 A，录音开关仍禁用”。基线 3d8bbbb2bc6dab83b063a1b71b2ebb6aaa77107a；仅修改 Electron 渲染侧：set_mode 成功后刷新权威会话状态，跳过 renderer-only D/OCR，并拒绝跨模式的迟到响应。新增 7 个生产 index/store/workspace MiniDOM 场景，接入默认 check。

**本轮限定范围 PASS**：前端 check 通过；安全 Python 946 passed / 5 skipped / 17 deselected / 1 xfailed；隐藏真实 Electron 复验两次文件完成→A 录音开关恢复、停止后重新处理文件→A、A/B/C/D 及快速切换、静态 OCR、真实 sidecar 断连重连与正常退出。监测期间无窗口显示/聚焦；未采集或播放音频。详见 [docs/ACCEPTANCE_REPAIR_2026-10-04.md](docs/ACCEPTANCE_REPAIR_2026-10-04.md)。最终提交与 clean 状态以仓库外 D:/OneDrive/app_dve/acceptance-repair-20261004-210245/DELIVERY.json 为准。

本地提交、不推送、不打包、不更新安装版。麦克风/系统声音实时链路、TTS 播放、屏幕 OCR 原位覆盖、云 API、NPU/硬件、安装升级仍为 **NOT_RUN**，不能把本轮 PASS 扩大为全功能或发布验收。下方历史记录不代表本轮结果。

## 历史：验收返修（2026-09-29）

唯一当前交接入口：[docs/ACCEPTANCE_REPAIR_2026-09-29.md](docs/ACCEPTANCE_REPAIR_2026-09-29.md)。基线 `ce74a02988c3e11cd82c33cbcdd1d119027207cd`；当前代码 HEAD `992790b73201524f9c770c42e1c9c1b7640fe32b`，main。录音实时状态接线、Pipeline 最终收尾能力通知、剩余窗口操作销毁保护已分阶段本地提交；滚动/日志沿用已实现源码。完整隔离回归与范围内独立审查已通过（包括审查发现的R1/R2返修）；Python946通过，真实生产workspace MiniDOM46/46及窗口替身46/46。最终提交/回退证据以新报告引用的交付清单为准。

最终含文档 HEAD、工作区状态及回退实测保存在 `C:/Users/Zhang Ruiduo/Hermes/Tool_cache/voxsub-acceptance-20260929/DELIVERY.json`，提交后生成，不能把上述代码 SHA 当最终 SHA。**本轮不推送，不更新正式安装版本。** 真实 Electron、系统声音、麦克风、模型/硬件、打包及 Release 均 NOT_RUN。请甲方按源码与安装版分别复验。

下文所有早期“当前/未完成/不要续跑/未推送”均为对应日期的历史快照，不能覆盖本节；历史测试数字不能替代本轮门禁。

## 历史：体验修复阶段性收尾（2026-09-27）

用户要求先收尾，范围冻结。已本地提交窗口销毁保护（`1d61547`）和日志时间统一（`ac957dc`）；合并安全套件 **922 passed**，前端 check/acceptance 通过。滚动与录音未完成草稿已暂存并撤出运行代码；真实 Electron 验证未完成，本次独立审查未追加。未推送、未打包、未替换运行版本。当前交接以 [docs/EXPERIENCE_CLOSE_2026-09-27.md](docs/EXPERIENCE_CLOSE_2026-09-27.md) 为准；不要自动续跑剩余项。

## 上一轮限定返修（2026-09-27）

基线 `1a3d47769c0c2ef914648b2794cb1bf3d0d976a8`；本轮仅修复迁移接管退出保护、idle stop 失效收尾，并闭环安全命令与完整 Git 回退。代码及范围内复审已闭环：安全套件 917 passed，独立 sidecar 7 passed，前端 check/acceptance 通过；watcher 初审阻断已返修并独立复验。最终完整回退演练与最终 HEAD/clean 状态保存在仓库外交付日志。当前交接以 [docs/LAST_REPAIR_2026-09-27.md](docs/LAST_REPAIR_2026-09-27.md) 为准，下方旧批次记录不是本轮验收结论。未推送、不打包。

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
- [x] v0.9.0-beta OCR 完善候选：OCR 改为与 A/B/C 平级的 D 模式，共用常驻翻译方向；进入模式后台预热快速 OCR 与翻译器，变化画面使用 Small 快速结果，稳定画面在实际 GPU 可用时用所选质量模型纠偏。实时首轮限制为 20–24 个版面块和约 2.2–2.4K 字符，批量 JSON 异常不再逐行回退；采集队列只保留最新画面并丢弃过期结果。实时覆盖捕获排除已修正，不再周期隐藏闪烁；碎片/连续正文按段落块合并，主阅读列优先，目标语言界面过滤；译文以原 OCR 框高度为字号基准，在不侵入其他 OCR 框的空闲区域内自适应扩张，覆盖框严格不重叠，长文本支持任意字符换行，空结果保留旧画面重试。安装器将安全收尾扩至 5 秒、强制关闭后复核 2 秒，运行标记保持到进程真正终止；Pipeline 退出时主动关闭本地翻译进程，安装器按当前安装目录清理旧版孤儿 `llama-server.exe`，避免 `ggml-cpu.dll` 锁定。成品退出握手通过。构建门禁 `319 passed / 7 skipped`，成品 OCR 自检与真实 Windows 捕获排除通过。安装包 277,285,664 字节（264.44 MiB），SHA256 `F09E62016FD04A9E6847186234F58E5BA1534E3A50A52D5E03F60C5FA745C06D`；继续沿用同版本候选，本次构建使用本机自签名证书，等待用户安装验收，不发布 Release
- [ ] M9 发布候选：完成更多真实推理与无独显 NPU 轻薄本验收

- [x] **2026-09-21 代码可维护性整顿（阶段0–4，共 10 个提交，基线 `0882505`）**：删除授权从"调用方给路径"改成"台账记录 + 护栏"（清理只认 `record_id` 并要求显式 confirm，新增 `migration_ledger.py`）；后台任务控制通道与耗时工作分离（新增 `job_runner.py` 有界单 worker + 诚实取消语义、`ipc_loop.py` 读循环与控制命令插队）；**Pipeline 停止超时不再伪装空闲**（保持"停止中"，资源门禁收敛成 `_may_replace_resources`，有界观察者等 worker 真退出）；**切语言/档位改用提交时快照**（`_LangSnapshot` + `config_generation`，在途任务不被新配置重新解释，`configGeneration` 已接入 `state` 负载）；**IPC 适配层按业务域拆分**（`ipc_server.py` 1779 → 691 行，命令实现进 `handlers/*`，协议 I/O 独立成 `ipc_protocol.py`，既有入口保留为兼容 facade）；配置版本兼容加固（未来版本只读保护、未知字段保留、损坏先备份）；架构门禁从只扫 `voxsub/` 扩到含 `frontend/backend/` 并新增原子发布零豁免、`subprocess` 编码、复杂度棘轮、测试卫生等规则；原子写入收敛成 `file_io.replace_with_retry` 唯一实现。**干净检出实测**：`build-release.py --check-only` 通过、测试套件 738 passed / 0 failed。文档新增 `docs/{ARCHITECTURE,HANDOVER,MODULE_CATALOG,DECISIONS,MAINTAINABILITY_REPORT}.md` 与 `contracts/`。详见 TODO.txt 的 2026-09-21 段与主交付报告。
- [x] 独立审查（无上下文的只读代理）：逐条核实了声称已修的缺陷，并**实测出三个真问题** ——
  迁移源没有授权校验（两步 IPC 可 rmtree 任意用户目录）、我修 #6 时带出的一条静默回归
  （收尾窗口点开始不发 session 事件却清空字幕）、原子发布门禁只认 `os.replace` 而 3 处
  `Path.replace` 全放行。均已修复并补测试；另有 CI 不跑前端测试、`ConfigStore.save()` 绕过
  版本保护、`ensure_pipeline` 无锁等一并闭环。审查剩余的未处理项记在 TODO。
- [ ] 待甲方确认：清理 244 个 `.pytest-*`（527MB）+ 3 个 venv（3.3GB）；是否重新打包 sidecar（当前 `dist/` 是旧构建）；模型路径统一；Electron 静默端到端冒烟（L4）；真机四模式验收（L5）

## 2026-09-25 公共模块持续改进

- [x] 保留并加强既有公共实现；修复 IPC 入队登记、任务取消与开始/终态提交竞态、migration 旧句柄释放新页面、catalog 迟到请求覆盖，以及状态提示英文文案。
- [x] 修复 TTS 停止超时后误置空 worker、热切换可能并行创建 worker；实时组件构建失败时按逆序释放有 `close()` 的已创建资源。
- [x] 迁移提交改为异步 job 跟踪并携带唯一 `clientMigrationId`：回执超时后仍可关联终态；成功报告只随迁移终态事件返回；迁移页在真实终态前保持退出保护，终态早于回执且随后断连时仍保留报告，旧页终态不会改写新向导。
- [x] 独立复审确认迁移终态先于 failed/unavailable 回执时优先读取缓存报告；补齐两种回执的页面行为测试。入站 `clientMigrationId` 契约与事件结果隔离验证通过。后端全量 852 passed / 6 skipped / 7 deselected / 1 xfailed；页面行为 62/62。
- [x] 最终独立只读复审完成；实现和专项测试无剩余阻断。分阶段本地提交已完成，未推送远端。
- [ ] 已知边界：热切换时旧 TTS worker 仍被阻塞，当前译文 `submit()` 可能被拒绝而跳过朗读（字幕仍显示）；不允许新旧 worker 并行。
- [ ] NOT_RUN：真实模型/硬件链路、Electron 静默 E2E、打包/安装包验收；本轮未修改真实配置、模型或 Release。
- [x] 本轮代码与文档按功能阶段完成本地提交；严格按附件未推送远端。

## 当前维护收尾（2026-09-26）

**限定源码修复已完成并本地提交，未推送。** 最新验收以 [docs/FINISH_2026-09-26.md](docs/FINISH_2026-09-26.md) 为准：909 passed；npm run check 通过；页面 126/126、退出保护 8/8、catalog 11/11；三阶段代码回退 tree/clean 匹配。提交 f50dd05 / 869be20 / 8af1258。GUI、真实迁移、模型/音频、打包仍 NOT_RUN；清理硬超时保留退出保护且不能自动恢复。

### 以下为早先中间快照（已被上方收尾报告覆盖）

- 当前基线 `main@1c0781e3d647846844040c21ed38f4882c5db93c`；本地分支较 `origin/main` 超前 14 个既有提交。本轮改动尚未暂存或提交；禁止 push。
- 本轮已修复/覆盖：迁移异步回执严格校验前置于 JobRunner 入队；Pipeline 资源 setter 与 `start()` 领取启动权通过同一状态锁串行；测试用时钟改为模块局部代理，B 模式 loopback 选择测试改用假设备，避免枚举真实设备。
- 当前 Python 安全套件：显式排除 `integration` 与 `hardware_audio`，`883 passed / 5 skipped / 17 deselected / 1 xfailed`，另有 1 条 `DeprecationWarning`（`voxsub/model_catalog.py` 的 `tarfile.extractall`）；测试日志、LocalAppData、pytest basetemp 均定向至 Hermes scratch。定向 Pipeline/IPC 子集 `250 passed`。
- 前端 `npm run check` 退出码 0；页面逻辑汇总 `113/113`，catalog 乱序 harness `8/8`。`npm run test:acceptance-contracts` 为 `25/25` 结构正例与 11 个内存负对照；它不是运行时/成品证明。
- `CONTRACT_ENFORCE=False` 仍是默认兼容告警模式。严格模式全 UI 兼容、冻结 sidecar/安装包、Electron 窗口、真实迁移、模型、音频和硬件工作流均 **NOT_RUN**；当前返修整阶段回退演练也 **NOT_RUN**，不可拿前一轮隔离 revert 结果替代。
- 当前三个独立只读复审已重新派发，结果待回传；审查通过后再完成最终报告与本地分阶段提交。

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
