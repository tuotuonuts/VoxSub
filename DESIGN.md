# 语幕 VoxSub —— 设计 (DESIGN)

## 架构与数据流

```
                        ┌─ 环形缓冲 ─ 16kHz PCM ─┐
[audio 采集] 麦克风 ────┤                        ├─▶ [vad 切句] ─▶ [asr 流式识别]
             loopback ──┘   (soundcard/WASAPI)   │      (sherpa-vad)   (sherpa zipformer)
             ffmpeg 提轨 ────────────────────────┘                        │
                                                                    on_utterance(text)
                                                                          ▼
[router 设备路由] ── CPU/GPU/NPU 枚举+实测计分 ──▶ [translate] ◀── 并发预取+缓存
                                                     │ opus 快档 / qwen 质量档 / cloud
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
  asr/        sherpa-onnx 流式识别封装，句子回调
  translate/  Translator 基类 + opus / qwen / cloud 三实现
  tts/        piper 合成封装
  router/     设备枚举（CPU/GPU/NPU）+ 按任务实测计分 + 降级链
  diagnostics/ 自检中心、模型完整性、冒烟测试
  models/     模型下载器（双源/断点/SHA256）、本地缓存管理
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
def list_loopbacks() -> list[AudioDeviceInfo]: ...   # 从 include_loopback=True 的 mic 中按名字过滤

class AudioSource(ABC):
    sample_rate: int = 16000          # 统一 16k 输出
    def start(self) -> None: ...
    def read_chunk(self) -> np.ndarray | None   # float32 mono 16k 块, 停止后返回 None
    def stop(self) -> None: ...
    def close(self) -> None: ...

class MicSource(AudioSource): ...        # 默认麦克风
class LoopbackSource(AudioSource): ...   # 系统声音 (WASAPI loopback)
```

注意（soundcard 0.4.6 实测）：`all_speakers()` 无 `include_loopback` 参数；loopback 一律从 `all_microphones(include_loopback=True)` 获取，按设备名含 "loopback"（不区分大小写）识别；`Recorder(samplerate=16000)` 自动重采样。

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

class OpusFastTranslator(Translator):        # 快档: OPUS-MT zh-en/en-zh, 目标 <0.5s/句
class QwenQualityTranslator(Translator):     # 质量档: Qwen2.5-1.5B-Instruct ONNX(onnxruntime-genai), 目标 2-5s/句
class CloudTranslator(Translator):           # 云: OpenAI 兼容端点(DEEPSEEK_API_KEY/BASE_URL 用户配置), 白名单 base_url

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

### 模型清单（下载 URL 由 M4 spike 确认后补全）

| 档位 | 模型 | 目标位置 | 大小 | 下载源 |
|---|---|---|---|---|
| 快档 | OPUS-MT zh-en / en-zh（ctranslate2 或 oonnx） | models/nmt/ | ~100MB×2 | HF + ModelScope 镜像 |
| 质量档 | Qwen2.5-1.5B-Instruct（Q4 ONNX） | models/llm/ | ~1GB | HF(Qwen 官方) + ModelScope |
| 云 | 用户 OpenAI 兼容端点 | — | — | 用户配置 |

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

### 组件清单（M7 验收依据）
1. **主窗**：编辑式左右分栏（左=模式三卡片 A/B/C + 语言对 + 状态灯；右=实时字幕流列表）；底部胶囊 CTA「开始/停止」内嵌圆形箭头岛
2. **字幕浮窗**：无边框置顶半透明；Double-Bezel 双层壳；双语两行（原文+译文）；可拖动；字号/透明度可调；历史滚动；模式切换不中断
3. **托盘**：模式快捷切换、开机自启开关（QStandardPaths 启动项）、退出
4. **设置页**（独立窗口）：模型档位（快档/质量档/云 API key）、语言对、TTS 开关、主题三档、设备路由（CPU/GPU/NPU 实测速度展示）
5. **诊断页**：设备清单 + 自检结果卡（✅/⚠️/❌ + 一句话处置），一键导出一份纯文本报告

### 状态全覆盖（每组件）
默认 / hover / pressed / disabled / loading（拾音中转圈、推理中脉冲）/ selected；空状态（无字幕时引导文案）；错误态（设备失败提示换源）

## 数据与存储

- 模型目录：`%LOCALAPPDATA%\VoxSub\models\{asr,vad,nmt,llm,tts}\`（含 manifest.json 记录 SHA256/版本）
- 配置：`%LOCALAPPDATA%\VoxSub\config.json`
- 字幕历史：内存滚动 + 用户手动导出（不做自动落盘，避免隐私残留争议）
- 下载缓存：`%LOCALAPPDATA%\VoxSub\downloads\`（未完成分片，续传用）

## 关键决策

- 推理全部经 onnxruntime，CPU 为兜底执行提供器；DirectML 为加速层（自动枚举 GPU/NPU）
- "说一句翻一句"句子级节奏，非逐词；翻译预取使感知延迟 ≈ 识别延迟 + 400ms
- 云翻译仅允许用户显式配置的 OpenAI 兼容端点，key 存本地 config
- 音频仅存在于内存流水线，不落盘录音（隐私优先；C 模式仅处理用户主动导入的文件）