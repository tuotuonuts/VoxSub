# 语幕 VoxSub —— 规划 (PLAN)

## 技术栈与选型理由

| 模块 | 选型 | 理由 |
|---|---|---|
| 录音 | soundcard (WASAPI) | 系统级 loopback 支持，纯 pip 无编译依赖 |
| VAD | sherpa-onnx 内置 VAD (ONNX) | 与 ASR 同生态，免 torch 大头 |
| ASR | sherpa-onnx zipformer 流式 | CPU 实时、DirectML/NPU 加速，模型 ~200MB |
| 翻译(本地) | argostranslate(OPUS-MT 快档) + onnxruntime-genai(Qwen2.5-1.5B 质量档) | 双档位纯本地零成本 |
| 翻译(云) | OpenAI 兼容端点(DeepSeek/Gemini) | 用户自填 key，一行配置切换 |
| TTS | piper（经 sherpa-onnx 生态） | 轻量 CPU 实时，中英模型小 |
| 推理后端 | onnxruntime + onnxruntime-directml | 同一代码库跨 CPU/GPU/NPU |
| UI | PySide6 | 大众桌面 UI 成熟，托盘/悬浮窗 |
| 打包 | PyInstaller + InnoSetup | 一键安装 exe，含 vc_redist 静默安装 |
| 依赖管理 | uv | 快速干净，lockfile |
| 测试 | pytest | 标准 |

## 里程碑（每个可独立验证）

- M1 骨架 + spike：依赖可用性（录音设备枚举 / onnxruntime 加载冒烟 / sherpa ASR+VAD 模型加载）
- M2 录音：麦克风 + loopback 双源，环形缓冲，采样转 16k
- M3 ASR：sherpa 流式识别，句子完成回调
- M4 翻译：Translator 接口三实现（opus / qwen / cloud），并发预取 + 结果缓存
- M5 TTS：piper 集成，朗读开关，失败静默降级
- M6 编排：A/B/C 三模式完整管道（含 ffmpeg 提轨、srt 导出）
- M7 UI：PySide6 主窗 + 托盘 + 悬浮字幕窗 + 设置页
- M8 诊断：设备枚举计分 / 自检中心 / 模型下载器自愈
- M9 发布：exe 安装包 + 模型分发 + RELEASE_NOTES

## 风险清单（Top 3 + 应对）

1. **系统声音 loopback 兼容性**（虚拟声卡/驱动差异）→ M2 spike 先验证本机；失败给设备选择 UI，多设备枚举
2. **GPU/NPU 驱动参差** → 设备路由实测计分 + 静默降级链，诊断报告标注
3. **模型下载源可达性**（中国用户）→ ModelScope 主源 + HF 备源，断点续传 + SHA256

## 质量纪律（约定流程）

- 每里程碑结束：requesting-code-review 自审（安全扫描 + 质量门禁）
- git 小步提交，信息清晰；.gitignore 先于代码
- TODO.txt 按时间戳分段追踪；pytest 覆盖核心逻辑（正常/边界/异常）
- 发布前对照 personal-dev-workflow 交付检查清单