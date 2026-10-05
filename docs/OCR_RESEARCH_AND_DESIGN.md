# OCR 研究与设计决策

## 范围与产品定位

研究核查时间：2026-10-05 UTC（本机 UTC+08:00 为 2026-10-06）。依据官方文档、项目自身 README；不是安装所有竞品后的横向性能测评。VoxSub 是普通 Windows 用户的实时翻译工具，默认离线、OCR/翻译独立选择，需要与音频模式共存。目标排序：正确链路与语言契约 → 不积压/可取消/资源可释放 → 可理解操作 → 有证据的性能优化。

## 竞品与开源方案矩阵

| 对象 / 官方证据 | 可借鉴能力 | 本轮决策与限制 |
|---|---|---|
| [LunaTranslator OCR 自动化](https://docs.lunatranslator.org/zh/ocrparam.html) | 周期执行、图像稳定/一致阈值、输入触发等待稳定、文字编辑距离过滤 | 采用周期调度与结果复用的思想；现阶段使用完整像素精确指纹，不以模糊相似度吞掉数字/否定词变化。未复制其代码。稳定性/ROI 策略待专门数据集评估。 |
| [Translumo](https://github.com/ramjke/Translumo) | 多 OCR 结果评分、Windows OCR/EasyOCR、字幕和游戏区域捕获、建议缩小捕获区域 | 用户选定区域 + 一个在途任务；不默认常驻多个 OCR 模型。其宣传低延迟不作为本产品速度证据。 |
| [PowerToys Text Extractor](https://learn.microsoft.com/en-us/windows/powertoys/text-extractor) | 快速区域提取/复制，Windows OCR 与语言包有关 | 落地只识别/复制、不强制翻译；Windows OCR 作为可选后端需另验语言包、部署和权限，不在本轮默认切换。 |
| [Text Grab](https://github.com/TheJoeFin/Text-Grab) | 文字提取与后续文本处理 | 提取和翻译分离、重试当前图；文字编辑校正界面未实现，不借它的能力假称已有。 |
| [Pot](https://github.com/pot-app/pot-desktop) | 跨平台划词/OCR与多服务配置 | 保留现有 OCR + MT 独立配置，翻译只在用户开启时调用；未移植插件体系。 |
| [ScreenTranslator](https://github.com/OneMoreGres/ScreenTranslator) | 捕获、Tesseract/Leptonica OCR 和翻译模块组合 | 参考分层职责；不同时引入另一套 OCR 依赖，以免加重安装和模型维护。 |
| [DeepL 图像翻译](https://www.deepl.com/en/features/translate-image) | Windows 截屏翻译入口与图像翻译产品交互 | 参考清晰任务入口。不是离线能力证据，本产品不擅自上传截图或切云。 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR)、[参数文档](https://rapidai.github.io/RapidOCRDocs/latest/install_usage/rapidocr/parameters/) | 指定本地模型、识别置信度、多推理后端，兼容现有 Python/ONNX 栈 | 保持现有引擎，修复 IPC 忽略用户型号/置信度；实时实例复用，静态实例回收。库代码 Apache-2.0；权重许可需按上游 MODEL_LICENSES 分别核实，不把库许可等同任意模型许可。 |
| [PaddleOCR PP-OCRv6](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv6/PP-OCRv6.en.md) | Tiny/Small/Medium 分档；Small/Medium 官方统一语言列表含日语，Tiny 不含日语 | UI 只展示应用当前路由支持子集：Tiny 中英、Small/Medium 中英日。官方 v6 列表未列韩语，不能沿用硬编码中英日韩。上游基准不可换算成本机性能。 |

v5 目录契约暂保守维持中英：上游多语言模型家族不等于当前随包权重；没有实测、词典与下载契约核对前不扩张语言宣称。未知 OCR 型号不猜能力。OCR 输入能力限制源语言；译文目标能力由当前 MT 决定，例如识别英语仍可以由兼容 MT 翻成韩语。自动源语言暂不开放，避免把 OCR 的通用 Latin 输出误当已支持路由。

## 全功能审计 → 实施

### 1. 上传 / 框选 / 仅识别
- 修复识别 handler 默认创建引擎、无视配置；透传模型、权重目录与置信度。
- 图片统一 EXIF 方向；识别前限制 4000 万像素，避免不受控大图分配。
- 公共 ToggleSwitch/Field/Button 实现“识别后自动翻译”。关闭后不调用翻译器、不发云文本；保留复制和重试。
- 同页请求代号、分页/页面生命周期隔离旧结果；导出打开对话框前固定源路径。原图 URI 正确编码空格、#、?、%、UNC。
- 原生对话框/框选拒绝给出可读错误，不报假完成；空译文不生成可导出的“伪译后图”。

### 2. 实时识别与翻译
- 原生产链路只有 live-frame 事件、没有消费者；改为主进程调度器直接调用真实 OCR IPC，再向覆盖窗下发译文和图像尺寸。
- 一个 capture + backend 在途任务；忙时跳过 tick，不排队。stop 不解开在途锁；epoch 隔离重开、语言/配置变更和迟到结果。
- 实时 OCR 单实例；完整 RGB 像素 SHA256 + 形状 + 配置/语言/是否翻译作用域，单条 2 秒缓存。仅完全一致时复用；失败不缓存，单像素变化重识别。
- 复用既有有界翻译服务：512 条行缓存、精确去重和有限批量；实时默认 24 行 / 2400 字符，静态 48 行 / 6000 字符。达到预算未翻译与真正失败分别报告。
- 新会话默认独立拥有 MT，不借同时运行的音频模型实例。语言、MT配置、模型目录变化使缓存/实例失效。
- 实际失败与部分失败按 1/2/4/8 秒退避；每帧 finally 删除截图。停止释放 OCR、MT 和缓存。

### 3. 坐标 / 覆盖 / 图像导出
- 分开处理桌面 DIP、实际截图像素与覆盖 CSS 像素；按 Electron 实际 thumbnail 尺寸裁剪，不把请求尺寸视为真实尺寸。
- 框选器覆盖虚拟桌面，支持副屏/负坐标；跨屏选择裁到最大交集所在单屏。**不是混合 DPI 的跨屏拼图实现**。
- 无匹配 display_id 失败，不回退第一屏。框选恢复后再次隐藏主窗等待合成，避免自己入镜。
- overlay ready 后显示并设置捕获保护，随后才启动轮询；加载失败销毁窗口，停止会话。迟到 ready 不复活已销毁窗。
- 覆盖只显示译文，不用原文伪装译文；无效框过滤，保守跳过相交块，固定高度裁切。旧译文最长 2500ms，空/失败帧标旧但不能续期限。
- 图像导出按框独立 tile 绘制，中英文换行、缩字号、最低字号省略；不越框污染邻文，返回截断数量，完整正文仍可复制。背景均值估计不是图像修复/复杂排版复原。

### 4. 缓存 / 隐私 / 诊断
- 译后图使用用户 ConfigStore 指定缓存根目录与条数上限，非法位置报错，不静默回退系统盘；本轮修复候选验收暴露的未传 ConfigStore 问题。
- OCR会话和翻译失败诊断记录阶段、字符数、错误类型/代码、当前模型/provider、耗时、请求数，不新增正文日志。图片仍为本地文件缓存，不把“本地”误写成“从不落盘”。
- IPC新增字段/命令同步严格参数与返回值契约，未放宽额外字段规则；新增 ocr_release，保持兼容新增字段。
- source API 仅允许主窗口拥有者操作 OCR，不让覆盖窗/外来 renderer 改会话。

## 需要继续验收的路线（未实现/未宣称通过）

| 优先级 | 路线 | 采用前门槛 |
|---|---|---|
| P0 | 真桌面截图/多屏/覆盖排除 | 用户不被打扰的独立桌面条件，真实 Electron、125/150/200% DPI、多显示器、窗口化/全屏/远程桌面验证；不能只看 isContentProtected=true。 |
| P1 | 字幕 ROI + 局部变化/稳定策略 | 含动态背景、小字、数字、否定词、逐字动画的标注集；先证明不漏变化，再调阈值。当前精确缓存对动态背景命中率低。 |
| P1 | 针对型号的速度/准确率基准 | 固定样本与CPU/GPU预算，分别测捕获、OCR、MT、绘制及端到端P50/P95；按语种比较Tiny/Small/Medium，不盲切小模型。 |
| P2 | Windows OCR / 窗口绑定捕获 | 语言包、授权、Win10/11行为、native生命周期和回退；不擅加隐式下载。 |
| P2 | 复杂段落/竖排/旋转原位排版 | 保留原始框与可撤回映射，验证漏行/混合阅读顺序；本轮按行翻译，不做版式复原。 |
| P2 | 可编辑校正 / 词库 / 上下文翻译 | 显式区分原文/用户校正/译文，避免把识别幻觉写回历史或掩盖源文本。 |

本轮真实 CPU 新画面仍约 2.5–3.9 秒。精确帧复用和翻译缓存改善不重复计算及不积压，并不意味着高帧率视频 OCR 已满足低延迟要求；2500ms 过期可能在慢设备显示间歇，需后续按上述端到端数据调整。

## 捕获依据与证据边界

- [Electron desktopCapturer](https://www.electronjs.org/docs/latest/api/desktop-capturer)：thumbnail 尺寸是请求，实际返回不保证相同。
- [Electron BrowserWindow](https://www.electronjs.org/docs/latest/api/browser-window)：Windows 捕获排除在下一次桌面合成生效；Windows 10 2004 之前可能捕获成黑块。API 保护状态不是实际捕获排除证明。
- [Windows Graphics Capture](https://learn.microsoft.com/en-us/windows/uwp/audio-video-camera/screen-capture)：可作为后续窗口绑定路线；本轮未接入。

不复制竞品实现，不以研究报告替代真运行。实际实现与验收参见 `docs/OCR_DEEP_ACCEPTANCE.md`。
