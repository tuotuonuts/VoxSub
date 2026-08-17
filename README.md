# 语幕 VoxSub

> [!WARNING]
> **本项目仍处于早期开发阶段，功能、模型兼容性和稳定性尚不成熟，不建议用于生产环境或关键场景。** 当前 Windows 安装包使用开发者自签名证书，SmartScreen 或部分杀毒软件可能提示“未知发布者”、风险警告或误报。请只从本仓库的 Releases 下载，并在安装前核对 SHA256；不要为了安装而盲目关闭安全软件。

Windows 10/11 大众实时翻译软件：麦克风对话、会议/网课系统声音、本地视频音频 → 实时双语字幕。默认全本地离线，可选云端高质量翻译。

当前版本：`0.3.3-beta`。欢迎测试和反馈，但请预期仍可能出现识别质量、设备兼容、性能和界面交互方面的问题。

## 下载

- [GitHub Release v0.3.3-beta](https://github.com/tuotuonuts/VoxSub/releases/tag/v0.3.3-beta)
- 安装包：`VoxSub-Setup-0.3.3-beta.exe`
- SHA256：`A4C527FCF71D2A916E05F61DC32A5F763ED91328CEF110C0885EDCB5BC14309B`

## 当前可用功能

- A 麦克风同传：可选择麦克风，语音分句后显示原文与译文；可选同时录音，按开始 → 暂停/继续 → 结束并保存操作
- B 应用 / 系统声音：可选择系统输出端点，或只捕获指定应用进程树的声音
- C 音视频字幕：选择 MP4/MKV/MOV/MP3/WAV 等文件，内置 ffmpeg 自动提音并导出同名 SRT
- 模型广场：按识别/翻译质量排序，可下载、切换、卸载开源模型；根据本机 CPU、内存、GPU/显存标注“不推荐 / 较为推荐 / 推荐 / 满载”
- 国内外双下载源：自动并行测速并故障切换，也可手动固定 Hugging Face/GitHub 全球源或 ModelScope 中国源
- 大众硬件路由：独显 GPU → NPU → 核显 → CPU；只有模型与运行时确实支持时才启用，失败会继续降级并写入应用内日志
- 内置诊断与实时日志：无需打开或占用日志文件，可在 App 内切换 DEBUG 级别
- 识别调优：自动/响应优先/均衡/准确优先预设，也可在宽范围内 DIY 灵敏度、断句停顿、最长单句、候选数、最大文字量和常用词；鼠标悬停 `i` 即显示通俗说明，调优草稿需显式保存
- 字幕会话：主窗与浮窗文字可复制，当前会话可清空或保存为 TXT/SRT/VTT；浮窗锁定后鼠标悬停即可原地调字号或解锁
- Soft Premium UI：深色 / 浅色 / 跟随系统；字幕浮窗支持字号、拖动、锁定与鼠标穿透，锁定后可从主窗解锁

模型广场不是模型仓库的全量镜像。目录只保留 VoxSub 已有运行时适配、许可证明确，并在相近资源档位仍有价值的模型；内置 Zipformer/OPUS 作为极低资源兜底，高质量档使用 Fun-ASR-Nano、Qwen3-ASR 与 Hy-MT2。

## 文档体系（按阅读顺序）

- [STATUS.md](STATUS.md) — **项目状态书 / 交接单（先读这个）**
- [TODO.txt](TODO.txt) — 修改追踪（时间戳分段）
- [REQUIREMENTS.md](REQUIREMENTS.md) — 需求与范围
- [PLAN.md](PLAN.md) — 技术选型与里程碑
- [DESIGN.md](DESIGN.md) — 架构与接口契约

## 开发环境

```bash
uv venv                 # 创建虚拟环境 (.venv)
uv pip install -r requirements.txt
```

Python 3.11+。源码启动：

```powershell
.\.venv\Scripts\python.exe -m voxsub.ui.app
```

构建安装包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

安装包输出到 `D:\OneDrive\app_dve\Release`。模型仍放在 `%LOCALAPPDATA%\VoxSub\models`，不随安装包重复分发。

## 目录结构

```
voxsub/     主包（模块见 DESIGN.md）
tests/      pytest 测试
scripts/    构建/工具脚本
models/     运行时模型缓存（gitignore）
```
