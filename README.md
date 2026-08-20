<div align="center">

[![简体中文](https://img.shields.io/badge/LANG-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-EA4C4C?style=for-the-badge)](README.md)
[![English](https://img.shields.io/badge/LANG-English-22A699?style=for-the-badge)](README_EN.md)

</div>

# 语幕 VoxSub

> [!WARNING]
> **本项目仍处于早期开发阶段，功能、模型兼容性和稳定性尚不成熟，不建议用于生产环境或关键场景。** 本次本地构建的 `0.4.1-beta` 安装包使用 VoxSub 开发者自签名证书，Windows 可能显示“未知发布者”，安全软件也可能误报。请只从本仓库的 Releases 下载，并在安装前核对 SHA256；不要为了安装而盲目关闭安全软件。

Windows 10/11 大众实时翻译软件：麦克风对话、会议/网课系统声音、本地视频音频 → 实时双语字幕。默认全本地离线，云 STT 与云翻译可独立配置并混合使用。

当前源码版本：`0.4.1-beta`。本版仍属于开发测试版本。欢迎测试和反馈，但请预期仍可能出现识别质量、设备兼容、性能和界面交互方面的问题。

> **Intel NPU 支持仍有限。** `0.4.1-beta` 已在 Intel AI Boost 真机验证 Hy-MT2 1.8B Q4/Q6/Q8：VoxSub 自动调度和禁止 CPU 回退的强制 NPU 推理均通过。Hy-MT2 7B Q4/Q6/Q8 仅依据 llama.cpp OpenVINO 公开兼容资料列为“NPU 待验证”，启动真实翻译探针失败时会自动改用核显或 CPU；现有 sherpa-onnx ASR 和 OPUS 运行时不支持 NPU。

## 下载

- [GitHub Releases](https://github.com/tuotuonuts/VoxSub/releases)
- 安装包：`VoxSub-Setup-0.4.1-beta.exe`（VoxSub 开发者自签名，Windows 仍可能显示未知发布者）
- SHA256：`F48A304B56C763484A73AB40784255647C84889FCBA387FFDD16B59EA71ACF26`（见同名 `.sha256` 文件）
- 本地构建路径：`D:\OneDrive\app_dve\Release\VoxSub-Setup-0.4.1-beta.exe`

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
- 识别调优：自动/响应优先/均衡/准确优先预设，也可在宽范围内 DIY 灵敏度、断句停顿、最长单句、候选数、最大文字量和常用词；鼠标悬停 `i` 即显示通俗说明，调优草稿需显式保存
- 字幕会话：主窗与浮窗文字可复制，当前会话可清空或保存为 TXT/SRT/VTT；浮窗锁定后鼠标悬停即可原地调字号或解锁
- Soft Premium UI：深色 / 浅色 / 跟随系统；设置、模型广场和诊断页统一视觉；字幕浮窗支持字号、拖动、锁定与鼠标穿透，锁定后可在浮窗悬停控制岛或设置页解锁
- 统一选择控件：设置页单选项保持稳定圆形，开关使用圆角轨道，模型广场筛选按钮保持胶囊形；不再因 Windows Qt 选中态变成方形
- 安装器语言：根据 Windows 界面语言自动选择简体中文、繁体中文或英文，无匹配翻译时回退英文
- 模型存储：新安装默认使用安装目录下的 `Models` 文件夹，并按 `stt`、`translate`、`vad`、`tts` 用途整理；升级用户继续使用原来的模型目录，只有在设置中主动更改时才迁移。设置支持更改位置、迁移已有模型和手动导入，更新安装包不会删除已下载模型。
- 0.4.1-beta 修复：识别调优的上/下调节按钮均可点击；模型迁移放在后台执行，避免卡死或关闭页面时崩溃；迁移后会同步修正模型清单并立即使用新目录，不再误报文件缺失或继续读取旧目录；升级期间会兼容查找旧模型根目录中的翻译模型；Teams 新版窗口会自动捕获其宿主进程及子进程声音。
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

安装包默认输出到项目上一级的 `Release` 目录；本项目开发工作区对应 `D:\OneDrive\app_dve\Release`。新安装默认把模型放在安装目录下的 `Models` 文件夹，并按用途分到 `stt`、`translate`、`vad`、`tts`；升级用户保留现有模型根目录，直到主动在设置中更改。模型属于用户数据，不会被安装包重复分发或在更新时删除。本次本地安装包大小为 214,733,432 字节。

## 目录结构

```
voxsub/     主包（模块见 DESIGN.md）
tests/      pytest 测试
scripts/    构建/工具脚本
models/     运行时模型缓存（gitignore）
```
