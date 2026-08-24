<div align="center">

[![简体中文](https://img.shields.io/badge/LANG-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-EA4C4C?style=for-the-badge)](README.md)
[![English](https://img.shields.io/badge/LANG-English-22A699?style=for-the-badge)](README_EN.md)

</div>

# 语幕 VoxSub

> [!WARNING]
> **本项目仍处于早期开发阶段，功能、模型兼容性和稳定性尚不成熟，不建议用于生产环境或关键场景。** `0.7.2-beta` 使用 `CN=VoxSub Dev (self-signed)` 开发者自签名，但该证书不受 Windows 公共信任链信任，系统仍可能显示风险提示，安全软件也可能误报。请在安装前核对 SHA256；不要为了安装而盲目关闭安全软件。

Windows 10/11 大众实时翻译软件：麦克风对话、会议/网课系统声音、本地视频音频 → 实时双语字幕。默认全本地离线，云 STT 与云翻译可独立配置并混合使用。

当前源码候选版本：`0.9.0-beta`；GitHub 当前公开下载仍为 `0.7.2-beta`。本次 OCR 性能修复已按“先推送源码、再打包”流程重建同版本安装包，在用户验收前不会冒充已发布版本。

> **Intel NPU 支持仍有限。** `0.4.1-beta` 已在 Intel AI Boost 真机验证 Hy-MT2 1.8B Q4/Q6/Q8：VoxSub 自动调度和禁止 CPU 回退的强制 NPU 推理均通过。Hy-MT2 7B Q4/Q6/Q8 仅依据 llama.cpp OpenVINO 公开兼容资料列为“NPU 待验证”，启动真实翻译探针失败时会自动改用核显或 CPU；现有 sherpa-onnx ASR 和 OPUS 运行时不支持 NPU。

## 下载

- 最新预发布版本：[VoxSub v0.7.2-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.7.2-beta)。安装包 `VoxSub-Setup-0.7.2-beta.exe` 为 214,817,504 字节（204.87 MiB）；SHA256 `313714AE3C9557B88EDBCEBBFCB768A15BBDD65915A5266E0B3EB1D82CAF2211`。
- [直接下载安装包](https://github.com/tuotuonuts/VoxSub/releases/download/v0.7.2-beta/VoxSub-Setup-0.7.2-beta.exe) · [下载 SHA256 文件](https://github.com/tuotuonuts/VoxSub/releases/download/v0.7.2-beta/VoxSub-Setup-0.7.2-beta.exe.sha256)
- 上一个公开版本：[VoxSub v0.5.0-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.5.0-beta)。

## 当前可用功能

- A 麦克风同传：可选择麦克风，语音分句后显示原文与译文；可选同时录音，按开始 → 暂停/继续 → 结束并保存操作
- B 应用 / 系统声音：可选择系统输出端点，或只捕获指定应用进程树的声音
- C 音视频字幕：选择 MP4/MKV/MOV/MP3/WAV 等文件，内置 ffmpeg 自动提音并导出同名 SRT
- OCR 图片与屏幕翻译：可框选屏幕或上传 PNG/JPG/WebP/BMP/TIFF，查看识别原文、译文、原图标框与原位覆盖译文，并导出译后图片；实时模式持续观察选定区域，只在画面明显变化时重新识别。原图和未导出的译后图分别缓存，路径禁止位于 C 盘，默认每类保留 15 张，设置为 0 可无限保留
- 模型广场：按语音识别/翻译/语音朗读/OCR 分类和质量排序，可下载、切换、卸载开源模型；OCR 提供 PP-OCRv6 Tiny/Small/Medium 与 PP-OCRv5 文档手写增强档。根据本机 CPU、内存、GPU/显存标注“不推荐 / 较为推荐 / 推荐 / 满载”
- 国内外双下载源：自动并行测速并故障切换，也可手动固定 Hugging Face/GitHub 全球源或 ModelScope 中国源；数 GB 模型遇到 CDN 断流会保留进度并自动续传
- 大众硬件路由：独显 GPU → NPU → 核显 → CPU；当前源码已验证 Hy-MT2 1.8B Q4/Q6/Q8 可自动路由到 Intel NPU，其他模型按模型卡上的“已验证 / 待验证 / 不可用”标签处理
- 内置诊断与实时日志：无需打开或占用日志文件，可在 App 内切换 DEBUG 级别；日志、诊断报告和会话导出均使用应用内保存窗口并在后台写入
- 新设备基础模型自修复：安装包附带 Silero VAD；首次运行自动修复到当前用户模型目录，下载任意 ASR 模型后即可使用，不再需要另找 VAD 文件
- 云端与混合模式：STT 和翻译分别选择本地或云端；云 STT/云翻译各自设置 API Key、BaseURL、模型名，支持云 STT + 本地翻译、本地 STT + 云翻译以及全云链路。云 STT 仅上传本地 VAD 已结束的语音片段
- 识别调优：保留自动/响应优先/均衡/准确优先/自定义的原有逻辑；新增“智能上下文”模式，可按句意动态延长停顿、在硬等待上限内合并片段、用常用词和近期重复上下文做保守纠偏，并可关闭或轻度清理独立语气词；鼠标悬停 `i` 即显示通俗说明，调优草稿需显式保存
- 0.6.0-beta 候选新增：智能上下文的当前句会在同一条草稿中接近逐词更新，后续识别可纠正前文；译文随原文动态替换，只接受最新请求结果；整句稳定后才进入历史并开始呈现下一句。选择 Qwen3/Fun-ASR/SenseVoice 等生成式本地 ASR 或云 STT 时，内置 Zipformer 会作为轻量草稿旁路连续出字，当前高质量模型仍负责最终纠偏定稿，不会高频重跑大模型或重复上传音频。“实时双语草稿”可独立关闭，关闭后仍保留智能断句、纠偏和语气词清理。
- 0.7.0-beta 候选新增：模型广场可下载 MeloTTS 中英双语、AISHELL3 中文轻量和 LJSpeech 英文轻量音色；设置 → 语音朗读可分别选择中英模型并在运行中立即切换。修复 TTS 开关只保存但当前会话不生效；实时双语草稿模式下会朗读定稿译文，不会反复抢读每一次草稿修订。旧版 `models/tts/zh` 和 `models/tts/en` 自动兼容。
- 0.7.1-beta 候选修复：英文实时草稿的全大写中间结果改为只读性更好的句子式显示；连续 partial 不再无限重置翻译防抖，动态译文会按节流频率追赶原文，同时保留终句翻译优先级。
- 0.7.2-beta 修复：安装器不再依赖会把主窗关闭误解为托盘隐藏的 Windows Restart Manager；新版使用专用退出信号，旧版则快速关闭 VoxSub 进程树，消除约半分钟假死与最终关闭失败。
- 0.8.0-beta 候选新增：独立 OCR 工作区包含“截图 OCR 翻译”和“实时区域 OCR”两种模式；内置离线 RapidOCR 保留每行坐标，实时覆盖窗不会进入后续截图，静止画面不会反复运行模型。截图像素始终只在本机内存处理；选择云翻译时仅发送识别后的文字。当前通用模型以印刷体为主，手写体和艺术体将通过可替换 OCR 后端继续优化。
- 0.9.0-beta 候选新增与修复：修复安装包缺少 `rapidocr.main` 以及旧 PyInstaller 运行时残留遮蔽依赖导致两种 OCR 均失败；升级前只清理应用管理的运行时目录，保留 `Models` 与 `Cache`；框选前等待 Windows 完成隐藏主窗，消除应用虚影；支持上传图片、原位覆盖预览及译后图片导出；原图与译后图采用非 C 盘分离缓存，默认每类 15 张/0 无限；模型广场新增四档 OCR 模型并可直接切换。实时 OCR 自动使用 DirectML GPU，失败时安全回退 CPU；多行译文合并为有界批量请求并继续复用缓存，页面变化后的更新不再逐行排队。OCR 入口移到顶部，A/B/C 模式卡不再被挤压。
- 字幕会话：主窗与浮窗文字可复制，当前会话可清空或保存为 TXT/SRT/VTT；浮窗可选仅原文、仅译文或对照翻译，并可分别调节内容边距和原译间距
- Soft Premium UI：深色 / 浅色 / 跟随系统；设置、模型广场和诊断页统一视觉；字幕浮窗支持更宽字号范围、自由缩放、拖动、锁定与鼠标穿透，锁定后悬停只显示“解锁”
- 固定尺寸长字幕：翻译到长句时不再自动扩大浮窗，内容会在用户设定的宽高内换行；普通滚轮查看当前长句，`Ctrl + 滚轮`翻看字幕历史
- 统一选择控件：设置页单选项保持稳定圆形，开关使用圆角轨道，模型广场筛选按钮保持胶囊形；不再因 Windows Qt 选中态变成方形
- 安装器语言：根据 Windows 界面语言自动选择简体中文、繁体中文或英文，无匹配翻译时回退英文
- 模型存储：新安装默认使用安装目录下的 `Models` 文件夹，并按 `stt`、`translate`、`vad`、`tts` 用途整理；升级用户继续使用原来的模型目录，只有在设置中主动更改时才迁移。设置支持更改位置、迁移已有模型和手动导入，更新安装包不会删除已下载模型。
- 0.5.0-beta 新增：独立、有界的上下文处理阶段；生成式/云 STT 可在翻译前合并未完片段，流式 Zipformer 可在句意未完时延长静音边界；等待始终受硬上限约束，纠偏只做可记录的小范围替换，旧调优模式完全旁路新阶段。
- 0.4.2-beta 候选修复：配置集中校验和安全迁移；采集、识别、翻译、TTS 队列全部有容量与过载策略；TTS 已接入独立播放线程；下载、模型提交和字幕导出使用完整性校验与原子写入；核心 Pipeline、硬件探测和 llama 启动流程已按职责拆分；外观页的译文字号和浮窗不透明度改用与识别调优一致、上下均可点击的箭头控件。
- 0.4.1-beta 修复：识别调优的上/下调节按钮均可点击；模型迁移放在后台执行，避免卡死或关闭页面时崩溃；迁移后会同步修正模型清单并立即使用新目录，不再误报文件缺失或继续读取旧目录；升级期间会兼容查找旧模型根目录中的翻译模型；Teams 新版窗口会自动捕获其宿主进程及子进程声音；长字幕不会再把浮窗撑到屏幕外。
- 更新说明：新版首次启动只显示一次本版更新内容，之后可在设置 → 关于中查看历史更新说明。
- 全屏体验：主窗口全屏时打开设置或模型广场，返回后仍保持全屏。

模型广场不是模型仓库的全量镜像。目录只保留 VoxSub 已有运行时适配、许可证明确，并在相近资源档位仍有价值的模型：Fun-ASR-Nano、Qwen3-ASR、SenseVoice Small，Hy-MT2 1.8B/7B 的 Q4/Q6/Q8 量化档，以及 MeloTTS 中英双语、AISHELL3 中文轻量、LJSpeech 英文轻量朗读模型。内置 Zipformer/OPUS 仅作为极低资源兜底。每张模型卡都会明确显示 NPU 可用性；“NPU 可用”只用于已完成强制 NPU 推理和应用自动调度双重验证的精确模型文件。

## 文档体系（按阅读顺序）

- [STATUS.md](STATUS.md) — **项目状态书 / 交接单（先读这个）**
- [TODO.txt](TODO.txt) — 修改追踪（时间戳分段）
- [REQUIREMENTS.md](REQUIREMENTS.md) — 需求与范围
- [PLAN.md](PLAN.md) — 技术选型与里程碑
- [DESIGN.md](DESIGN.md) — 架构与接口契约

## 开发环境

```bash
uv venv --python 3.11
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

Python 3.11+。源码启动：

```powershell
.\.venv\Scripts\python.exe -m voxsub.ui.app
```

构建安装包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

安装包默认输出到项目上一级的 `Release` 目录；本项目开发工作区对应 `D:\OneDrive\app_dve\Release`。新安装默认把模型放在安装目录下的 `Models` 文件夹，并按用途分到 `stt`、`translate`、`vad`、`tts`、`ocr`；升级用户保留现有模型根目录，直到主动在设置中更改。模型属于用户数据，不会被安装包重复分发或在更新时删除。当前公开的 `0.7.2-beta` 安装包为 214,817,504 字节，SHA256 `313714AE3C9557B88EDBCEBBFCB768A15BBDD65915A5266E0B3EB1D82CAF2211`。本地候选 `0.9.0-beta` 为 277,338,824 字节（264.49 MiB），SHA256 `9B6D57572E1A2C17178C2A169587DD57CA312C265D9EF343A8D3AD6B3334ED6B`；当前未签名，用户验证前不会创建 GitHub Release。

## 目录结构

```
voxsub/     主包（模块见 DESIGN.md）
tests/      pytest 测试
scripts/    构建/工具脚本
models/     运行时模型缓存（gitignore）
```
