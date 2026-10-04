# 系统/应用声音同时录音修复与专用语音翻译模型验收

本轮目录标签：2026-10-04。基线：`dc7fa8321b0b40d1f99ec50a0afb7b71d3f33393`。最终本地提交与交付状态见 `D:/OneDrive/app_dve/20261004-recording-s2tt/DELIVERY.json`。

## 1. 授权范围和静默约束

用户授权修复 B 系统/指定应用模式的同时录音按钮，并先验收 Granite 4.0 1B Speech 与 Index-Echo S2TT 2B，不先接入或上架。只修改源码、测试和交接文档；不推送、不打包、不替换安装版、不改用户配置、历史或已安装模型。使用隔离 APPDATA/LOCALAPPDATA、已有公开语音和仅写文件的合成语音；没有播放音频或采集用户麦克风、系统/应用声音。

## 2. 录音缺陷：根因和修复

根因不是 Windows 按钮样式或录音权限，而是三道能力门都仅允许 A：生产 workspace 限制 mode=a；Pipeline.recording_state 只为 A 声明 supported；_new_realtime_threads 也只在 A 创建 WAV recorder。只解除前端禁用会出现“能勾选但未真正落盘”。

修复三处共同放行 A/B，复用既有 _accept_capture_chunk 写入当前选中采集源，不额外开启麦克风。B 可保存默认系统、指定输出设备或指定应用的采集帧。暂停不写入，继续后续写，结束完成 WAV 收尾；运行、暂停、启动和收尾阶段仍禁止改变保存偏好，完全停止后才可修改。红点依赖真实 recorder 所有权和后端 active 状态，不只看勾选值。

新会话清空旧 WAV 路径，关闭录音或 C/取消的新会话不冒报上次录音；非 A/B 收尾不再显示“正在结束录音/未保存”提示。B 关闭提示明确是系统/指定应用音频，英文录音开关改为中性的 Save audio，避免暗示 B 采集麦克风。

### 操作方式

停止会话 → 切到 B 并选择系统/应用音源 → 勾选“同时录音” → 开始 → 结束后保存 WAV。运行或暂停时开关灰色是有意的生命周期保护，不表示录音功能失效。保存格式沿用现有 16kHz / 单声道 / PCM16。

## 3. 录音与应用验证

| 验证 | 结果 | 证据 |
|---|---|---|
| 安全 Python 全套 | PASS：1002 passed、7 skipped、7 deselected、1 xfailed；1 个既有 tar 提取弃用告警 | pytest-release.log |
| 最终录音相关 Python 测试 | PASS：28 passed | pytest-delivery-target.log |
| 最终前端 check：类型、配色及全部逻辑 | PASS；真实 workspace MiniDOM 47/47，末项页面释放测试 126/126 | frontend-check-delivery.log |
| 验收契约与负对照 | PASS：26 contracts + 11 in-memory negative controls | acceptance-contracts-delivery.log |
| 最终 renderer 构建 | PASS | build-latest.log |
| 最新源码隐藏 Electron + 真实 Python IPC | PASS：B on/off 写入、配置确认、A/B/C 往返、D 独立 OCR、旧路径 null | real-ui-acceptance.json、real-ui-acceptance-latest.log |
| 原生窗口/焦点采样及正常退出 | PASS：52 次采样，0 显示/聚焦违规，无本轮测试实例遗留 | silent-window-latest.json、electron-launch-latest.log |
| 真实硬件回环、指定应用音频采集与听感 | NOT_RUN：保护用户音频与桌面 | 未调用真实音频 Start |

真实 WAV 验证使用合成帧，不是仅检查属性：A/B 都通过实际 WaveSessionRecorder 落盘，再读取 WAV 头和 PCM 字节，核对暂停帧未写入、继续可写、关闭保存不影响识别输入、下一次录音使用新文件。所选源路由测试包含系统默认、指定端点和指定进程，若误开麦克风直接失败。实际生命周期 start/stop 与可修改能力事件也覆盖 A/B。

隐藏 Electron 验证没有开始会话，因此不冒称真实系统/应用硬件采集已验收；真实红点运行/暂停行为由生产 workspace MiniDOM 与 Pipeline recorder 所有权测试覆盖。

首次补充英文文案后的 check 被沙箱拒绝读取 Windows 用户临时目录的短路径别名；保留 frontend-check-latest.log。使用隔离 TEMP/TMP 并经授权重跑后全部通过，未为此修改产品逻辑。

## 4. 两款专用模型：完成的准备与阻塞边界

| 候选 | 固定版本 | 完整下载清单 | 推理验收 |
|---|---|---|---|
| IBM Granite 4.0 1B Speech（计划英文语音→中文） | bd87ab862416353633ea431fe49b1614003623c5 | PASS：16 文件，4,637,929,114 字节；safetensors 4,626,527,776 字节 | BLOCKED：加载前内存准入未通过，未生成译文 |
| Bilibili Index-Echo S2TT 2B（计划中文语音→英文并输出原文/时间戳） | bc45ecb31f3fcd78ec0340851fe21c71458d4a75 | PASS：20 文件，5,900,687,060 字节；safetensors 5,877,680,272 字节 | BLOCKED：加载前内存准入未通过，未生成字幕 |

每个下载文件核对当前固定 revision 的文件大小；LFS 文件逐个核对官方 SHA256，非 LFS 文件记录实际 SHA256。Granite 约 4.64GB（4.32GiB），Index 约 5.90GB（5.50GiB），不能把 1B/2B 名称当成下载体积。Granite API 的 usedStorage 包含历史存储，不能用它替代当前清单总量。

官方文件和入口以实际固定版本为准：Granite 正确仓库 ID 是 ibm-granite/granite-4.0-1b-speech（不是 granite-speech-4.0-1b）；Index 当前官方 infer.py 采用 AudioTransModel/translate_window，requirements 指定 torch 2.11.0 与 transformers 5.6.0，不沿用旧版 model.py/processor.py 的假设。已下载原始 README、requirements 和 infer.py 作为证据。

模型独立环境位于验收目录 model-env；实际安装 torch 2.11.0+cpu、torchaudio 2.11.0、transformers 5.6.0 等，未更改应用 .venv。依赖导入通过；对两段真实 WAV 的官方 Granite processor、Index tokenizer/WhisperFeatureExtractor 输入预处理通过，见 model-preprocessing.json。**预处理没有加载模型权重，也没有完成任何 ASR/AST 生成，不能算模型推理 PASS。**

本轮模型脚本设 CPU / BF16 / 两线程 / below-normal priority，逐款运行，低可用内存和 600 秒超时保护。首次 Granite 检查以及最终 Index 检查均在模型加载前拒绝启动，错误为 Insufficient free memory for safe bounded inference。后续 8 分钟内存观察 96 次，可用内存 4.94～5.76GiB，未达到稳定重试准入 12GiB；Index 检查后约 5.50GiB。脚本加载前保守门槛为 >10GiB，运行时 <3GiB 可用内存会停止本轮自己的子进程。这些是本轮保护用户工作负载的验收门槛，**不是厂商声明的最低硬件要求**；没有关闭用户应用或为了验收降低安全保护。

因此两款的模型加载兼容性、译文正确性、原文/时间戳完整性、RTF、真实模型峰值内存、连续会话、GPU/NPU 和多语质量均 NOT_RUN；不能因为依赖与预处理通过就声明可用、实时或值得上架。当前 pinned Index 官方 infer.py 为分窗串行、先全段 VAD 的伪流式文件流程，不能按原生低延迟实时流式接入；本轮未实际测该流程。Granite 的 AST 提示采用官方论文的 translate the speech to Chinese. 格式，但仍未完成翻译语义验证。

实际下载使用海外官方 Hugging Face 源；中国大陆源 NOT_RUN。两款均没有写入模型广场、没有启动产品内模型适配；以后若获准上架，仍须按原要求提供并验收海外/大陆两种下载路径。

### 证据与下一步

- weights/granite、weights/index；granite-bounded-manifest.json、index-bounded-manifest.json、weight-verification.json。
- model-environment-freeze.txt、model-preprocessing.json、samples.json、sample-en.wav、sample-zh.wav（仅写文件、未播放）。
- granite-inference.json/log、index-inference.json/log、index-run-delivery.log：明确 BLOCKED，tests 数组为空。
- memory-headroom.json：有限等待后 BLOCKED_MEMORY_HEADROOM，没有暗中长期监控。
- download-index-segments.log：保留原有字节、分段续传并合并后核对完整官方 SHA；完整下载通过。早期 HTTP/Xet 停滞与有限等待超时日志保留，不以它们冒称最终成功。

在用户腾出足够内存后，可直接使用已校验的隔离权重继续真实推理，无须重新下载；需要对真实输出做人工语义审查后，才决定是否接入/上架。这一部分需求当前仍未完成。

固定版本原始来源（源码取证，不代表本轮所有能力通过）：

- Granite：`https://huggingface.co/ibm-granite/granite-4.0-1b-speech/tree/bd87ab862416353633ea431fe49b1614003623c5`
- Index：`https://huggingface.co/IndexTeam/Index-Echo-S2TT-2B/tree/bc45ecb31f3fcd78ec0340851fe21c71458d4a75`
- Granite 提示参考：`https://arxiv.org/html/2505.08699v1`。

## 5. 回滚与交付纪律

修改前原始文件位于 `D:/OneDrive/app_dve/VoxSub/.backups/20261004-recording-s2tt`，按相同相对路径保存七个源码/测试文件以及 STATUS.md、TODO.txt；派生 renderer 和 launcher 日志也有备份。中间补充英文文案与验收脚本的快照保存在验收目录 handoff-backup。

推荐在干净工作区对 DELIVERY.json 的 final_commit 执行 git revert，生成新的回滚提交。也可逐文件从上述原始备份恢复；新建的本报告若要移除，先备份。不要 git reset --hard 或覆盖后续用户改动。未变更用户配置、历史、安装版与已安装模型，无需回滚这些内容。隔离权重、环境及分段下载证据保留在仓库外，未作删除。

结论：**录音修复限定范围 PASS；两款专用模型真实推理验收 BLOCKED（资源准入），语义与性能 NOT_RUN。** 最终本地提交必须包含本轮修复、相关测试和交接文档；不推送、不打包。所有证据相对于 `D:/OneDrive/app_dve/20261004-recording-s2tt`。
