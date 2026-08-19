# 语幕 VoxSub —— 设计 (DESIGN)

## 架构与数据流

```
                        ┌─ 环形缓冲 ─ 16kHz PCM ─┐
[audio 采集] 麦克风 ────┤                        ├─▶ [vad 切句] ─▶ [本地 ASR / 云 STT]
             loopback ──┘   (soundcard/WASAPI)   │      (sherpa-vad)   (仅发送完整语音片段)
             ffmpeg 提轨 ────────────────────────┘                        │
                                                                    on_utterance(text)
                                                                          ▼
[model_catalog] ── 硬件检测/推荐/双源下载 ────────▶ [translate] ◀── 独立队列+缓存
                                                     │ opus 兜底 / Hy-MT2 / cloud
                                                     ▼
                                   [tts piper] ──▶ UI 悬浮字幕窗 (双语滚动/历史)
                                                     │
                                                     ▼
                                        C模式: srt/vtt 导出器
```

## 模块划分

```
voxsub/
  audio/      采集（mic / loopback / 文件提轨），环形缓冲
  vad/        silero 风格 VAD 封装（sherpa 内置模型）
  asr/        sherpa-onnx Zipformer / Fun-ASR-Nano / Qwen3-ASR 适配，句子回调
  cloud_stt/  OpenAI 兼容 /v1/audio/transcriptions 客户端，WAV 分段上传
  translate/  Translator 基类 + OPUS / Hy-MT2(llama.cpp) / cloud 三实现
  tts/        piper 合成封装
  router/     设备枚举（CPU/GPU/NPU）+ 按任务实测计分 + 降级链
  diagnostics/ 自检中心、模型完整性、冒烟测试
  models/     通用下载器（镜像/断点/SHA256）、本地缓存管理
  model_catalog/ 精选模型目录、硬件推荐、双源安装/切换/卸载
  config/     config.json 读写（语言对/档位/设备/UI 偏好）
  ui/         PySide6：主窗、托盘、悬浮字幕窗、设置页
  pipeline/   三模式编排（A/B/C）
```

## 核心接口契约

```python
class Translator(ABC):
    """翻译层抽象——三实现共用契约"""
    @abstractmethod
    def translate(self, text: str, src_lang: str, dst_lang: str) -> str: ...

class DeviceInfo(NamedTuple):
    provider: str      # "cpu" | "cuda" | "dml" | "npu"
    name: str
    score: float       # 实测延迟分，越小越好

def select_device(task: str) -> DeviceInfo: ...   # task ∈ {asr, tts, translate}

# ASR 事件回调（pipeline 订阅）
def on_utterance(text: str, lang: str, is_final: bool) -> None: ...
```

## audio / asr 模块契约（M2/M3，子代理实现基准）

### audio（voxsub/audio.py）

```python
class AudioDeviceInfo(NamedTuple):
    name: str
    kind: str          # "mic" | "loopback"
    device: object     # soundcard 设备对象

def list_microphones(include_loopback: bool = True) -> list[AudioDeviceInfo]: ...
def list_loopbacks() -> list[AudioDeviceInfo]: ...   # 从 include_loopback=True 的 mic 中按 isloopback 过滤

class AudioSource(ABC):
    sample_rate: int = 16000          # 统一 16k 输出
    def start(self) -> None: ...
    def read_chunk(self) -> np.ndarray | None   # float32 mono 16k 块, 停止后返回 None
    def stop(self) -> None: ...
    def close(self) -> None: ...

class MicSource(AudioSource): ...        # 默认麦克风
class LoopbackSource(AudioSource): ...   # 系统声音 (WASAPI loopback)
```

注意（soundcard 0.4.6 实测）：`all_speakers()` 无 `include_loopback` 参数；loopback 一律从 `all_microphones(include_loopback=True)` 获取，并按 `isloopback` 属性识别（设备名通常不含 loopback）。默认 B 模式必须匹配系统默认扬声器的端点 ID，不能取枚举列表第一项。驱动格式协商失败时回退 PyAudioWPatch WASAPI，并在 UI 状态与实时日志中显示完整错误。

按应用捕获使用 Windows `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`（Windows 10 2004+），目标 PID 的子进程树一并捕获；其它进程声音不会进入流。UI 通过可见顶层窗口枚举 PID，实际隔离边界是进程树而不是单一 HWND。

### asr（voxsub/asr.py）——已适配 sherpa-onnx 1.13.5（M1-spike 实测）

```python
class StreamingASR:
    """包装 OnlineRecognizer.from_transducer; 模型路径一律 str"""
    def __init__(self, model_dir: Path, provider: str = "cpu", num_threads: int = 1): ...
    def create_stream(self) -> object: ...
    def feed(self, stream, samples: np.ndarray) -> None   # 内部 accept_waveform(16000, wav) —— 参数顺序 (sr, wav)!
    def decode(self, stream) -> str                       # is_ready/decode_stream 循环, 返回当前结果
    def get_result(self, stream) -> str: ...
    def is_endpoint(self, stream) -> bool: ...
    def reset(self, stream) -> None: ...

class WindowVAD:
    """包装 VadModel.create(VadModelConfig); 逐窗口喂入判断"""
    def __init__(self, model_path: str, threshold: float = 0.5,
                 min_silence: float = 0.5, min_speech: float = 0.25): ...
    @property
    def window_size(self) -> int: ...
    def is_speech(self, chunk: np.ndarray) -> bool: ...
    def reset(self) -> None: ...

class UtteranceSegmenter:
    """VAD+ASR 组装: 语音开始建流, 静音超阈值触发 on_utterance(final_text) 并重置流"""
    def __init__(self, asr: StreamingASR, vad: WindowVAD,
                 on_utterance: Callable[[str], None], min_silence_ms: int = 500): ...
    def feed(self, samples: np.ndarray) -> None   # 任意长度 float32 mono 16k 块
    def flush(self) -> None: ...                  # 强制结束当前语音段
```

模型目录约定：`%LOCALAPPDATA%\VoxSub\models\{asr,tokens.txt|*encoder*.onnx|*decoder*.onnx|*joiner*.onnx, vad\silero_vad_v5.onnx}`；多精度并存时优先 int8。

## 翻译层契约（M4，细化版）

### Translator 抽象（voxsub/translate/base.py）

```python
class Translator(ABC):
    name: str                        # "opus-fast" | "qwen-quality" | "cloud"
    langs: tuple[str, ...]           # 支持的语言代码
    local: bool                      # True=离线可用

    @abstractmethod
    def translate(self, text: str, src_lang: str, dst_lang: str, *,
                  timeout_ms: int = 15000) -> str: ...

    @abstractmethod
    def close(self) -> None: ...
    def health(self) -> str: ...     # "ok" 或缺陷描述（用于诊断页展示）

class OpusFastTranslator(Translator):        # 快档: OPUS-MT zh-en/en-zh(Xenova onnx int8 + ORT 手写 seq2seq 循环), 目标 <0.5s/句
class QwenQualityTranslator(Translator):     # 兼容类名；质量档实际加载模型广场所选 Hy-MT2 GGUF
class CloudTranslator(Translator):           # 云: OpenAI 兼容文本模型（独立 translate_* 配置）

class TranslatorFactory:
    @staticmethod
    def create(kind: str, config) -> Translator: ...
    @staticmethod
    def list_available() -> dict[str, bool]: ...   # 档位 → 模型/凭据就绪?
```

### 实时性机制（与 ASR 拼接的关键）

- **PrefetchEngine**：ASR 部分文本到达即预热翻译（按句子碎片双路发送），整句结束后合并/修正；防抖 800ms；同一句只出一次终稿
- **TranslationCache**：LRU，key=(norm_text, src, dst)，上限 2000 条；重复出现的字幕/短句零延迟
- **失败降级**：单句失败重试 1 次 → 保留原文 + 字幕标记 `[翻译失败]`；连续 3 句失败 → 弹提示（网络/模型问题），不崩管道

### 模型清单（目录版本 2026-08-18）

| 档位 | 模型 | 目标位置 | 大小 | 主源 / 镜像 |
|---|---|---|---|---|
| 快档 | OPUS-MT zh-en int8（onnx/encoder_model_int8.onnx + decoder_model_int8.onnx + config） | models/nmt/opus_zh_en/ | ~90MB | huggingface.co/Xenova/opus-mt-zh-en |
| 快档 | OPUS-MT en-zh int8（同构） | models/nmt/opus_en_zh/ | ~90MB | huggingface.co/Xenova/opus-mt-en-zh |
| 识别高质 | Fun-ASR-Nano 2512 INT8 | models/marketplace/asr-funasr-nano-2512-int8/ | ~1GB | GitHub sherpa-onnx / ModelScope |
| 识别多语 | Qwen3-ASR 0.6B INT8 | models/marketplace/asr-qwen3-0.6b-int8/ | ~1GB | GitHub sherpa-onnx / ModelScope 分文件镜像 |
| 识别轻量多语 | SenseVoice Small INT8 | models/marketplace/asr-sensevoice-small-int8/ | ~245MB | GitHub sherpa-onnx / ModelScope |
| 翻译均衡 | Hy-MT2 1.8B Q4/Q6/Q8 GGUF | models/marketplace/mt-hy-mt2-1.8b-q*/ | ~1.13–1.91GB | Hugging Face / ModelScope |
| 翻译高质 | Hy-MT2 7B Q4/Q6/Q8 GGUF | models/marketplace/mt-hy-mt2-7b-q*/ | ~4.62–7.98GB | Hugging Face / ModelScope |
| 云 | 用户 OpenAI 兼容端点 | — | — | 用户配置 |

注：目录按任务内质量分排序，不追求“列得多”。旧模型只有在显著更低资源/延迟下仍不可替代时才作为内置兜底保留。下载源支持自动测速故障切换和手动选择；安装过程使用断点续传、SHA256 与安全解压。

### 云 STT 与混合模式

- `CloudSTT` 使用 OpenAI 兼容的 `POST /v1/audio/transcriptions`。音频采集、VAD 与分句始终留在本机；仅把 VAD 已结束的 16 kHz WAV 片段上传，采集线程不等待网络。
- STT 与翻译的 API Key、BaseURL 和模型名完全独立：`stt_api_key` / `stt_base_url` / `stt_model` 和 `translate_api_key` / `translate_base_url` / `translate_model`。旧版单一 `api_key` / `base_url` / `model` 自动迁移到翻译侧。
- 设置页可自由组合四条实际链路：本地 STT + 本地翻译、云 STT + 本地翻译、本地 STT + 云翻译、云 STT + 云翻译。本地翻译包括 OPUS 快档和模型广场质量档。
- 两类云端点均限制为 HTTPS/HTTP 和受信任的 OpenAI 兼容服务白名单；日志只记录主机和模型名，不记录 API Key 或完整请求体。

## TTS 契约（M5）

```python
class TTSEngine:
    """sherpa-onnx 1.13.5 OfflineTts 封装（vits/piper 生态）"""
    def __init__(self, model_dir: Path, provider: str = "cpu", num_threads: int = 1): ...
    def synthesize(self, text: str, lang: str = "zh") -> np.ndarray | None
        # 返回 16k mono float32; 失败返回 None（调用方静默降级为仅字幕）
    def health(self) -> str: ...
```

- 模型：`models/tts/{zh,en}/` 下 sherpa-onnx piper 包（model.onnx + tokens.txt + model.card 字段）；从 k2-fsa/sherpa-onnx releases `asr-models` 找 vits-zh/vits-en 资产，用 model_fetch 工具下载
- TTS 失败只降级（无朗读），绝不阻断字幕流程

## Pipeline 契约（M6，UI 唯一依赖面）

```python
class Pipeline:
    mode: str                       # "a" 麦克风同传 | "b" 系统声音字幕 | "c" 文件
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def set_mode(self, mode: str) -> None: ...
    def set_stt(self, provider: str, config: dict) -> None: ...
    def set_translator(self, kind: str, config: dict) -> None: ...
    def on_utterance(self, cb: Callable[[str, str], None]) -> None: ...  # (原文, 译文)
    def on_status(self, cb: Callable[[str], None]) -> None: ...          # 状态文本
    def is_running(self) -> bool: ...
```

扩展配置入口：`set_langs`、`set_input_file`、`set_audio_devices`、`set_capture_process`、`set_stt`、`set_translator`。STT 和翻译分别构建，任一侧切换都不得重用另一侧凭据。所有工作线程回调必须经 Qt Signal 桥接到主线程；音频源 `start()` 失败也必须回收运行态并发出可读状态，禁止静默线程退出。

## Pipeline 编排设计（M6）

### 线程模型（三模式共用）

```
[采集线程] audio.read_chunk() 循环 ──队列──▶ [处理线程] segmenter.feed() → asr
        ──on_utterance(原文)──▶ 翻译(预取/缓存/降级) ──▶ UI 回调(线程安全: queue.Queue + Qt 信号桥)
```

- audio/asr/translate/tts 各在自己的线程/调用链中; UI 回调经 `queue.Queue` 桥接, 绝不在推理线程碰 Qt 控件
- 停止语义: stop() 置停止标志 → 采集线程退出 → segmenter.flush() 处理尾句 → 线程 join(超时 3s)

### 三模式组装

| 模式 | 输入 | 处理 |
|---|---|---|
| A 麦克风同传 | MicSource | 本地 ASR 或云 STT 分句识别 → 独立选择本地/云翻译 → 可选 TTS → 字幕 |
| B 系统声音字幕 | LoopbackSource | 同上（输入源不同，管线同一）|
| C 文件字幕 | ffmpeg 提轨 → 16k wav | 本地离线 ASR，或按本地 VAD 分段上传云 STT → 独立选择本地/云翻译 → 导出 srt/vtt/纯文本 |

### C 模式导出格式

- `srt`: 序号 + `HH:MM:SS,mmm --> HH:MM:SS,mmm` + 双语两行（原文行 / 译文行）
- `vtt`: 同构（WEBVTT 头 + `HH:MM:SS.mmm` 时间轴）
- 纯文本: 每句一行，原文 ⇄ 译文分栏（tab 分隔）
- 分句规则: 句尾标点（。！？.!?）+ 时长上限 8s 硬切（长句兜底）

### 设备路由接入

Pipeline 与翻译工厂通过 `router` 选择可真实调用的后端。全局优先级为 **独显 GPU → NPU → 核显 → CPU**；某模型运行时不支持前级设备时跳过并写日志，不能把 CPU 回退显示成硬件加速。

## 设备路由与诊断契约（M8）

```python
class DeviceInfo(NamedTuple):
    provider: str            # "cpu" | "dml" | "cuda" | "npu"
    name: str
    score_ms: float | None   # 实测延迟; None=未测
    kind: str                # "gpu" | "npu" | "igpu" | "cpu"
    raw_provider: str        # ORT provider 原名

def enumerate_devices() -> list[DeviceInfo]: ...   # ORT EP device + Windows 物理硬件画像
def select_device(task: str) -> DeviceInfo: ...    # GPU→NPU→iGPU→CPU；兼容性过滤+实测失败续降级
def preferred_onnx_providers(task: str) -> list: ...  # OPUS 等 ONNX 模型的实际 provider 链

def run_self_check() -> list[dict]: ...   # 每项 {check, status: ok|warn|fail, detail}
    # 项: 模型完整性(manifest 比对) / ORT providers / ASR 冒烟 / VAD 冒烟 / TTS 冒烟 / 磁盘内存余量
def export_report() -> str: ...           # 纯文本报告(诊断页一键导出)
```

## UI 设计规范（M7，风格=柔和高级感 Soft Premium，已定）

### 三旋钮（全程门控）
- DESIGN_VARIANCE: 6（有设计感但不失控）· MOTION_INTENSITY: 4（微交互，无复杂编排）· VISUAL_DENSITY: 3（低信息密度，留白多）

### Vibe 映射（三档主题，继承用户既有偏好）
- 深色档：**暗 OLED 玻璃**（#050505 基底）——字幕工具常在暗环境使用，字幕醒目
- 浅色档：**柔和结构主义**（银灰 #F7F7F5）——消费健康感，中性不腻
- 跟随系统档：darkdetect 自动切换（QFluentWidgets 原生支持）

### 设计令牌（禁 Inter/Roboto/Arial；禁紫蓝渐变背景；accent 仅 1 个）

| Token | 深色档 | 浅色档 |
|---|---|---|
| bg base | #050505 | #F7F7F5 |
| surface 分层 | #131313 / #1A1A1A | #FFFFFF / #F2F2F2 |
| text 主/次 | #F2F2F2 / #9CA3AF | #1A1A1A / #6B7280 |
| border | 白 8% 透明度 | 黑 8% 透明度 |
| accent（teal，唯一） | #14B8A6 → 深梯度 #0D9488 | 同左 |
| 语义色（低饱和） | 成功 #34D399 / 警告 #FBBF24 / 错误 #F87171 | 同左 |

- 字体栈：主 `"Segoe UI Variable","Microsoft YaHei UI"`；数据 mono `"Cascadia Code"`；标题可负字距 -0.02em
- 圆角分级：胶囊按钮全圆 / 卡片 12-16px / 输入框 10px / 弹窗 20px / **主壳 Double-Bezel 双层**（外壳 32px + ring + 内芯 24px inset 高光）
- 间距：4px 基准刻度；卡片内 padding 20-28px；节距大（py-24 级）
- 动效：QEasingCurve.OutCubic，时长 200-280ms（**>500ms 禁用**）；仅 animate opacity/pos
- 图标：QFluentWidgets FluentIcons（禁 emoji 图标）
- 选择控件：单选统一使用 `RoundRadioButton` 自绘圆环 + 圆心；二值设置使用圆角 `ToggleSwitch`；紧凑筛选使用胶囊 `PillChoiceButton`。禁止新增裸 `QRadioButton` / `QCheckBox` 进入业务界面，避免 Windows 原生选中态改变几何形状。

### UI 语言契约
- 应用 UI 当前支持简体中文与 English；配置默认值为 `system`，启动时按 Windows/Qt 界面语言解析。用户可在“设置 → 外观 → 语言”改为固定简体中文或 English，所有已打开窗口即时刷新。
- 所有用户可见的静态文案必须由 `voxsub/ui/i18n.py` 的 `tr()` 或集中词条生成；动态摘要必须保留源数据，并在语言改变时重新生成。不得把已经翻译出的字符串当作唯一状态保存。
- 字幕正文、设备的真实名称、文件名、日志内容和模型返回内容属于用户/系统数据，不得为了界面本地化而改写。
- **每次 UI 迭代都必须同步补齐全部已支持语言的文案，并扩展中英切换回归测试覆盖受影响窗口；未完成双语核验不得作为 UI 完成。**

### 组件清单（M7 验收依据）
1. **主窗**：编辑式左右分栏（左=模式三卡片 A/B/C + 语言对 + 状态灯；右=实时字幕流列表）；底部胶囊 CTA 在普通同传为“开始/停止”，录音同传为“开始/暂停/继续”，另提供“结束并保存”
2. **字幕浮窗**：无边框置顶半透明；Double-Bezel 双层壳；双语两行（原文+译文）可选中复制；可拖动；悬停字号 -/+/锁定/关闭；锁定后鼠标穿透、主窗解锁；透明度可调；历史滚动
3. **托盘**：模式快捷切换、开机自启开关（QStandardPaths 启动项）、退出
4. **设置页**（主窗内置二级页面）：本地/云 STT 与本地快档/质量档/云翻译独立选择；云 STT 和云翻译分别配置模型、Key、BaseURL，并展示当前本地/云/混合组合；另含识别调优预设与自定义参数、TTS 开关、麦克风/系统输出端点、应用进程隔离目标、主题三档。单选与开关必须使用公共选择控件；每个识别调优项必须附带鼠标悬停即显示的 `i` 通俗说明；调优参数使用保存/放弃事务，返回主页面不得隐式保存
5. **诊断页**：自检结果卡（✅/⚠️/❌ + 一句话处置）、应用内实时日志、运行时 DEBUG 开关、报告/日志导出

### 状态全覆盖（每组件）
默认 / hover / pressed / disabled / loading（拾音中转圈、推理中脉冲）/ selected；空状态（无字幕时引导文案）；错误态（设备失败提示换源）

## 数据与存储

- 模型目录：`%LOCALAPPDATA%\VoxSub\models\{asr,vad,nmt,llm,tts}\`（含 manifest.json 记录 SHA256/版本）
- 配置：`%LOCALAPPDATA%\VoxSub\config.json`
- 字幕历史：内存滚动 + 用户手动导出（不做自动落盘，避免隐私残留争议）
- 录音：仅在用户显式打开“同时录音”后写入 `%LOCALAPPDATA%\VoxSub\recordings\` 的 16kHz mono PCM WAV；暂停段不写入
- 下载缓存：`%LOCALAPPDATA%\VoxSub\downloads\`（未完成分片，续传用）

## 关键决策

- 推理全部经 onnxruntime，CPU 为兜底执行提供器；DirectML 为加速层（自动枚举 GPU/NPU）
- "说一句翻一句"句子级节奏，非逐词；翻译预取使感知延迟 ≈ 识别延迟 + 400ms
- 云 STT 与云翻译仅允许用户显式配置的 OpenAI 兼容端点，凭据分开保存在本地 config；云 STT 只上传已完成的语音片段
- 默认音频仅存在于内存流水线；只有用户明确打开同传录音时才本地落盘（C 模式仅处理用户主动导入的文件）
