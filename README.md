<div align="center">

[![简体中文](https://img.shields.io/badge/LANG-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-EA4C4C?style=for-the-badge)](README.md)
[![English](https://img.shields.io/badge/LANG-English-22A699?style=for-the-badge)](README_EN.md)

</div>

# 语幕 VoxSub

> [!WARNING]
> **本项目仍处于早期开发阶段，功能、模型兼容性和稳定性尚不成熟，不建议用于生产环境或关键场景。** Releases 中的 `0.5.0-beta` 因当前证书不可用而未签名。Windows 可能显示“未知发布者”，安全软件也可能误报。请在安装前核对 SHA256；不要为了安装而盲目关闭安全软件。

Windows 10/11 大众实时翻译软件：麦克风对话、会议/网课系统声音、本地视频音频 → 实时双语字幕。默认全本地离线，云 STT 与云翻译可独立配置并混合使用。

当前源码版本：`0.6.0-beta`（候选，未发布）；当前公开下载版本：`0.5.0-beta`。公开安装包已经完成构建、隔离启动检查和 GitHub 资产摘要复核，两者仍都属于开发测试版本。

> **Intel NPU 支持仍有限。** `0.4.1-beta` 已在 Intel AI Boost 真机验证 Hy-MT2 1.8B Q4/Q6/Q8：VoxSub 自动调度和禁止 CPU 回退的强制 NPU 推理均通过。Hy-MT2 7B Q4/Q6/Q8 仅依据 llama.cpp OpenVINO 公开兼容资料列为“NPU 待验证”，启动真实翻译探针失败时会自动改用核显或 CPU；现有 sherpa-onnx ASR 和 OPUS 运行时不支持 NPU。

## 下载与候选包

- 本地候选包：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.6.0-beta.exe`（未签名），214,819,528 字节（204.87 MiB）；SHA256 `585116D524FD75BD9E672339FCF04CC681F9FFF9BC3E6F1B91E3BF8FBAC6E2D2`。请先本地验证，尚未创建 GitHub Release。
- 已发布版本：[VoxSub v0.5.0-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.5.0-beta) 中的 `VoxSub-Setup-0.5.0-beta.exe`（未签名），214,795,950 字节（204.85 MiB）；SHA256 `38CF47DE43CB39B45BAF8241464A7C06B5AEF6AF28CE15FF76E7B32063047EA8`。
- 上一个版本：[VoxSub v0.4.2-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.4.2-beta)。

## 当前可用功能

- A 麦克风同传：可选择麦克风，语音分句后显示原文与译文；可选同时录音，按开始 → 暂停/继续 → 结束并保存操作
- B 应用 / 系统声音：可选择系统输出端点，或只捕获指定应用进程树的声音
- C 音视频字幕：选择 MP4/MKV/MOV/MP3/WAV 等文件，内置 ffmpeg 自动提音并导出同名 SRT
- 模型广场：按识别/翻译质量排序，可下载、切换、卸载开源模型；根据本机 CPU、内存、GPU/显存标注“不推荐 / 较为推荐 / 推荐 / 满载”
- 国内外双下载源：自动并行测速并故障切换，也可手动固定 Hugging Face/GitHub 全球源或 ModelScope 中国源；数 GB 模型遇到 CDN 断流会保留进度并自动续传
- 大众硬件路由：独显 GPU → NPU → 核显 → CPU；当前源码已验证 Hy-MT2 1.8B Q4/Q6/Q8 可自动路由到 Intel NPU，其他模型按模型卡上的“已验证 / 待验证 / 不可用”标签处理
- 内置诊断与实时日志：无需打开或占用日志文件，可在 App 内切换 DEBUG 级别；日志、诊断报告和会话导出均使用应用内保存窗口并在后台写入
- 新设备基础模型自修复：安装包附带 Silero VAD；首次运行自动修复到当前用户模型目录，下载任意 ASR 模型后即可使用，不再需要另找 VAD 文件
- 云端与混合模式：STT 和翻译分别选择本地或云端；云 STT/云翻译各自设置 API Key、BaseURL、模型名，支持云 STT + 本地翻译、本地 STT + 云翻译以及全云链路。云 STT 仅上传本地 VAD 已结束的语音片段
- 识别调优：保留自动/响应优先/均衡/准确优先/自定义的原有逻辑；新增“智能上下文”模式，可按句意动态延长停顿、在硬等待上限内合并片段、用常用词和近期重复上下文做保守纠偏，并可关闭或轻度清理独立语气词；鼠标悬停 `i` 即显示通俗说明，调优草稿需显式保存
- 0.6.0-beta 候选新增：智能上下文的当前句会在同一条草稿中接近逐词更新，后续识别可纠正前文；译文随原文动态替换，只接受最新请求结果；整句稳定后才进入历史并开始呈现下一句。选择 Qwen3/Fun-ASR/SenseVoice 等生成式本地 ASR 或云 STT 时，内置 Zipformer 会作为轻量草稿旁路连续出字，当前高质量模型仍负责最终纠偏定稿，不会高频重跑大模型或重复上传音频。“实时双语草稿”可独立关闭，关闭后仍保留智能断句、纠偏和语气词清理。
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

模型广场不是模型仓库的全量镜像。目录只保留 VoxSub 已有运行时适配、许可证明确，并在相近资源档位仍有价值的模型：Fun-ASR-Nano、Qwen3-ASR、SenseVoice Small，以及 Hy-MT2 1.8B/7B 的 Q4/Q6/Q8 量化档。内置 Zipformer/OPUS 仅作为极低资源兜底。每张模型卡都会明确显示 NPU 可用性；“NPU 可用”只用于已完成强制 NPU 推理和应用自动调度双重验证的精确模型文件。

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

安装包默认输出到项目上一级的 `Release` 目录；本项目开发工作区对应 `D:\OneDrive\app_dve\Release`。新安装默认把模型放在安装目录下的 `Models` 文件夹，并按用途分到 `stt`、`translate`、`vad`、`tts`；升级用户保留现有模型根目录，直到主动在设置中更改。模型属于用户数据，不会被安装包重复分发或在更新时删除。当前 `0.6.0-beta` 本地候选安装包为 214,819,528 字节，SHA256 `585116D524FD75BD9E672339FCF04CC681F9FFF9BC3E6F1B91E3BF8FBAC6E2D2`。

## 目录结构

```
voxsub/     主包（模块见 DESIGN.md）
tests/      pytest 测试
scripts/    构建/工具脚本
models/     运行时模型缓存（gitignore）
```
