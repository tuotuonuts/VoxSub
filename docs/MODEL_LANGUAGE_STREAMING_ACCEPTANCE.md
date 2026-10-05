# 模型语种、语言列表与实时更新验收

验收时间：2026-10-05 UTC（本地 Asia/Singapore 为 2026-10-06）。

## 根因与改动

1. 旧语言上限贯穿 `language_guard`、`language_capabilities`、翻译器语言声明、配置 schema 与前端标签，并非只是下拉框少几项。新增唯一语言注册表；保存、重启解析、语言别名、脚本文字变体（zh-hant）、诊断和 OCR 共用契约。目标语言不错误地与 ASR 输入求交集。
2. Qwen 按每段快照传全名语言选项，覆盖官方 30 种语言。SenseVoice 的 pinned sherpa 1.13.5 从 recognizer config 读取语种，现由串行识别 worker 在每次 decode 前调用真实 SetConfig（不重载权重），覆盖中英日韩粤。
3. FunASR 的 native prompt 拼接为“语音转写成 + language”，改传“中文/英语/日语”，保留 system/user prompt；该版本 FunASR 有自己的 config，不能把基类 SetConfig 冒充热更新，所以会话中拒绝改变源语言，UI 说明需先停止。目标语言仍可改。
4. Moonshine EN 属于单语模型；Parakeet TDT v3、Zipformer 无当前已接入的强制语种接口，显示并记录该限制，不假装提示已执行。不恢复按猜测丢弃源文的门禁。
5. Hy-MT2 采用官方明确支持表的 38 个语言/文字变体条目，而非过时的“33种”标题。所有在架量化档位共用注册表；单句、草稿、批量和目标脚本检查贯通。移除多语 auto→English 用“拉丁字母=英语”直接透传的路径。
6. 草稿取两次假设的一致完整词前缀；静默 300ms 后释放尾部，同一输入不重复翻译；最终任务保持优先，后续源文不被旧终句覆盖。整段识别源文可先于最终翻译显示，不额外识别。
7. 原生 partial 展示采样由 360ms 调整至 140ms（不增加原生解码频次）；store/浮窗去重重复 partial+draft，主页同值不写 DOM/重算贴底。字幕使用 dir=auto 适配 RTL 文本。
8. 既有中英 Zipformer 辅助预览原先隐含绑定 context 档。现成为明确的、默认关闭的公共 Toggle 选项，所有档位按开关生效，只用于 zh/en；不下载新模型，不给其他语言套中英识别器。默认不加载第二个 ASR，不做二次重识别。

## 实际组合示例（质量档选择 Hy-MT2）

| ASR | 可选明确源语言数 | 自动源语言 | 目标语言/文字变体数 |
| --- | ---: | --- | ---: |
| Qwen3-ASR | 23（30 与 Hy-MT2 的交集） | 不提供：其余输出语种无完整翻译路径 | 38 |
| SenseVoice | 5（含粤语） | 提供 | 38 |
| FunASR Nano | 3 | 提供 | 38 |
| Parakeet TDT v3 | 11（25 与 Hy-MT2 的交集） | 不提供 | 38 |
| Moonshine Tiny EN | 1 | 不需要 | 38 |
| Zipformer bilingual | 2 | 提供 | 38 |

OPUS 快档仍只有中英，这是实际翻译能力而非全局四语上限。旧默认 GGUF 和自定义云端没有可靠的扩展语种声明，保留现有保守契约，不把注册表全部语种宣称为未知云模型能力。OCR 仍按所选已接入的识别权重限制输入语种，输出则按翻译器能力扩展。TTS 未增加新语种。

## 已验证

- Python 安全全量：1323 passed / 8 skipped / 7 deselected / 1既有 xfail（`python-release.log`，75.75秒）；明确 `-m "not hardware_audio"`。
- `npm run check`、`npm run build`；生产 renderer/controller 通过 MiniDOM/VM 测试动态多语标签、保存/恢复、模型变更、异步竞态、识别语言锁定、重复事件不重复通知/绘制。不是可见浏览器验收。
- 真实离屏 Chromium：注入后端生成的 11源/38目标语言矩阵，在中英UI、明暗主题、多分辨率/缩放下440项布局检查全通过；960次采样中可见/聚焦违规0、renderer错误0。长语种提示跨两列，避免挤坏选择框。不是用户可见桌面验收。
- 独立 IPC 子进程握手与命令协议回归通过。开发过程中发现过顶层新 import 早于 IPC 安装包路径的回归，已移至路径初始化之后并复测，未忽略失败。
- CPU 单线程真实 Qwen：法、德、阿拉伯、俄、粤 5 个模型包公开 WAV（最长6秒）。在 decode 边界读回原生语言 option，5/5 非空且符合预期字符系统，每段只有1次 decode；第二次读取复用结果，不重复推理。
- CPU 单线程真实 SenseVoice：英、日、韩、粤、中 5 个包内 WAV 连续切换。5/5 非空且符合预期字符系统，真实 SetConfig 和 decode 均执行；没有 native GetConfig 接口，不能称为 native 配置读回。
- 同一个 Zipformer 英语 WAV（8.83s）加速回放：360ms→140ms 采样时，首个部分结果音频位置 1660→1380ms，partial数9→11，最终段数均为1。CPU用时215.06→226.43ms，仅单次顺序回放观测，不是稳定性能百分比或端到端实时基准。

## 未验证及边界

- NOT_RUN：用户原音/视频、WER/字幕语义准确率、完整真实翻译模型端到端、真实音频采集、可见桌面/物理DPI、GPU、其他ASR权重的实际推理、安装包、GitHub CI。
- 语言提示不保证零串语言；脚本检查不是统计语种识别器，也不能保证粤语与普通话、简体与繁体、不同拉丁语种完全正确。
- pinned sherpa 的 Qwen、SenseVoice 等接入仍为离线整段接口；不能把UI提频说成原生token流。若不显式启用辅助预览，这些模型仍须先完成当前音频段识别。未添加假打字动画。
- 默认不运行第二个 ASR、不重识别每句话、不改用户模型配置/正文/历史、不重启用户当前实例。
- 全部测试不播放、不录音、不弹窗。APPDATA/LOCALAPPDATA隔离，Python清理PYTHONPATH/PYTHONHOME；npm与全量测试由CREATE_NO_WINDOW子进程运行。

## 证据与复现

哈希、原生与离屏摘要：`docs/MODEL_LANGUAGE_STREAMING_EVIDENCE.json`。前端最终日志：`frontend-delivery.log`。

外部证据目录：`D:\OneDrive\app_dve\language-streaming-20261006`。
原生报告：`native.json`；原生脚本：`scripts/verify_model_language_streaming.py`。
回归：`tests/test_model_language_streaming.py`（72项新增）；语言前端、字幕前端及现有元数据/模型/流水线测试同步更新。
模型文件只读：`D:\VoxSub\Models`；只使用模型包公开 WAV。报告不含音频或转写正文。

官方契约来源（此次下载的原始文档和 native 源码已保存在外部证据目录）：
- https://huggingface.co/Qwen/Qwen3-ASR-0.6B/raw/main/README.md
- https://huggingface.co/tencent/Hy-MT2-1.8B/raw/main/README.md
- https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/raw/main/README.md
- https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.5/sherpa-onnx/csrc/offline-recognizer-sense-voice-impl.h
- https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.5/sherpa-onnx/csrc/offline-recognizer-funasr-nano-impl.cc
- https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.5/sherpa-onnx/csrc/offline-recognizer-funasr-nano-impl.h
- https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.5/sherpa-onnx/csrc/offline-recognizer-impl.cc

## 交付与回滚

改动前 tracked 文件备份：`.backups/language-streaming-20261006-034349`。
本轮创建独立本地提交，未 push；优先 `git revert <本轮提交>` 回滚，避免覆盖后续改动。
既有 `Cache/` 不删除、不提交。运行当前源码的新构建才会生效，不自动操作正在使用的旧实例。
