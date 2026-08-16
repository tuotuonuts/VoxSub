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