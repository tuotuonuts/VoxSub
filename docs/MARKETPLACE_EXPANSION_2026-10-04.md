# 模型广场扩容与静默验收 — 2026-10-04

## 结论与范围

基线：`8d74ae448c1295687e265dbd2e1f78f513c93313`。用户授权上架并提供海外/中国大陆两种下载方式。本轮源码上架 **3 个可实际运行的模型**，不是候选名单全部上架；本地提交，不推送、不打包、不替换安装版。最终提交及工作区状态见仓库外交付清单。

| 模型 ID | 功能 | 约束 | 许可证 |
|---|---|---|---|
| asr-moonshine-tiny-en-v2 | Moonshine Tiny 英语量化离线 ASR | 整句解码，不是原生流式 Moonshine Voice；仅验证 CPU | MIT |
| asr-parakeet-tdt-0.6b-v3-int8 | Parakeet TDT v3 欧洲多语离线 ASR | 25 种欧洲语言，不含中文；仅实测英语/德语和 CPU | CC-BY-4.0 |
| tts-kokoro-v1.1-int8-zh-en | Kokoro v1.1 中英离线 TTS | 完整音色/词典/音素包；中文 sid=3，英文 sid=0；仅验证 CPU | Apache-2.0 |

目录的内存需求及质量评分属于推荐元数据，不是本轮性能/准确率基准测量；GPU/iGPU/NPU 支持均未宣称。

**BLOCKED，未上架**：Qwen3-ASR 1.7B（当前适配器/已验证 sherpa 导出不足）、VibeVoice-ASR（未完成运行适配）、TranslateGemma 4B（下载授权门槛及适配未验证）。不得把这些候选记为完成。

## 下载方式与完整性

界面提供“自动（失败切换备用源）／海外优先／中国大陆优先”，传递 auto/global/china 给真实安装器。地区选择在本次页面会话内保留，不写用户配置；“优先”并非禁止备用源。安装器保留失败切源与取消不切源的行为。

海外均使用 k2-fsa/sherpa-onnx 的 GitHub release：

| 模型 | 发布目录 / 文件 | 字节数 | SHA256 |
|---|---|---:|---|
| Moonshine | asr-models / sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27.tar.bz2 | 29858559 | 9ec31b342d8fa3240c3b81b8f82e1cf7e3ac467c93ca5a999b741d5887164f8d |
| Parakeet | asr-models / sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2 | 487170055 | 5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf |
| Kokoro | tts-models / kokoro-int8-multi-lang-v1_1.tar.bz2 | 147031220 | a1e94694776049035c4f2c6529f003aaece993c76aae9a78995831c3c4dcafc6 |

实际完整 URL 及结果保存在证据目录 archive-downloads.json，代码目录也包含完整地址。三份海外完整归档已下载并通过大小/SHA 校验，实际归档安装到隔离 global-install 根目录。

大陆选项：

- Moonshine：ModelScope csukuangfj/asr-models 上游镜像，同名归档，同样的大小及 SHA；初次完整网络下载 28.33 秒，v2 安装复验使用已验证缓存（0 秒），不能把缓存复验当第二次网络下载。
- Parakeet：HF 第三方镜像，仓库 csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8，固定版本 2bda32ec70b097a55adaa07d9a7173915b43cc78；4 文件，共 670478772 字节。
- Kokoro：HF 第三方镜像，仓库 csukuangfj/kokoro-int8-multi-lang-v1_1，固定版本 155831f1b4ba23b1f5c058be6a61df90cefb2a37；376 文件，共 215321488 字节。包含 LICENSE、词典、espeak 数据等，不仅主 ONNX 文件。

HF 镜像地址基于 hf-mirror.com 的 resolve 固定版本路径；每个文件的路径、字节数和 SHA256 存于 voxsub/catalog_assets.py，并已与海外归档逐文件核对。含空格/特殊字符路径使用 URL 编码。

**大陆源安装验收强制只配置大陆源**，没有海外回退掩盖失败；Parakeet 修复后完整下载/校验 419.25 秒，Kokoro 376 文件 273.83 秒。以上仅证明本机当时能访问这些源，**不是中国大陆各地区网络实测，也不是持续可用性保证**；HF 镜像明确为第三方，不冒充官方 ModelScope 来源。

## 修复与回归覆盖

1. 新增 Moonshine v2 与 Parakeet 离线适配，复用现有非流式识别路由；校验所需文件，不伪称原生 streaming。
2. Kokoro 与旧 VITS 配置分流，完整目录校验、双语词典和中文 FST、语言音色选择、16k float32 输出；保留旧 VITS 兼容路径。
3. 真实大陆安装发现已提交文件后 OneDrive 缓存清理 WinError 5 会误报安装失败：改为清理告警，模型提交成功不被临时缓存锁否定；测试覆盖。
4. Kokoro 特殊文件名导致未编码 URL 失败：修复编码，完整下载复验通过。
5. 真实隐藏 UI 发现旧安装按钮传入 progress_callback，而安装 API 参数实际为 progress：修正接线并新增源参数校验/转发及进度事件测试。
6. 增加界面地区选择，将原本未插入工具栏的刷新按钮显示出来；生产 catalog MiniDOM 验证三种选择到 IPC 的参数。

## 已验证的真实运行

环境：Windows、Python 3.12、sherpa-onnx 1.13.5；全部使用隔离配置/模型目录。

- Moonshine 英语和 Parakeet 英语均输出：Ask not what your country can do for you. Ask what you can do for your country.
- Parakeet 德语输出：Alles hat ein Ende, nur die Wurst hat zwei.
- Kokoro 中文/英文分别生成 66068 / 64056 个 16k float32 样本，sid 分别为 3 / 0。只生成数组，**未播放、未听感验收**。原生词典有 Unknown token: ❓ 告警，未将其隐藏为质量保证。
- 真实隐藏 Electron DOM 存在三个新卡片和三个下载源选项。Moonshine 按钮经真实 IPC 安装已校验缓存归档并写入 china 安装记录；此 UI 下载验证 network=false，不与独立大陆网络安装混淆。
- 隔离模型根目录刷新后，Kokoro“使用”按钮正确选择中英两项模型 ID，TTS 播放开关保持关闭。未改用户模型配置。
- 最终隐藏窗口监测 2617 次采样，显示/聚焦违规 0；两窗口 invisible/unfocused，正常退出 code=0。首轮另有 5138 次采样、0 违规。

## 自动门禁

- 最终安全 Python 全量：**967 passed / 5 skipped / 17 deselected / 1 xfailed**，45.98 秒。排除 integration 和 hardware_audio；不是包含真实录音/硬件的全量验收。
- 1 条现有 tar extractall 的未来 Python 3.14 弃用警告，未当作本轮失败或本轮已修复事项。
- 新增模型/下载/适配/handler 及架构定向回归 33 passed。
- 前端 npm run check（TypeScript、调色板、逻辑测试）通过；catalog 测试含新增 4 项，共 15 项。
- 前端契约检查通过（含 11 个内存负对照）；build:renderer 通过，仅生成源码运行所需的忽略目录构建文件，不等于安装包构建。

最初 Python 回归有环境失败（沙箱无法枚举捕获目标、Git 所有权保护、中文输出编码），使用提升权限的隔离运行、进程级 safe.directory 与 UTF-8 后通过；未为通过测试修改这些生产功能。早期失败日志保留，不将旧失败或旧通过当最终结论。

## NOT_RUN

新模型文件到字幕/SRT 的完整端到端、实时麦克风/系统声音、TTS 播放听感、屏幕原位 OCR、云 API、GPU/iGPU/NPU、25 语言完整质量基准、安装包/升级/Release 均 NOT_RUN。真实推理证据限于上述样例和 CPU，不扩大为全功能发布验收。

## 证据与回滚

证据根：D:/OneDrive/app_dve/marketplace-expansion-20261004-213622。

主要证据：archive-downloads.json；三个模型 *-china-result.json / *-china-result-v2.json；china-install-v2.log；real-inference.json / real-inference-v3.log；ui-final/catalog-real-ui.json、catalog-ui-monitor.json、exit.json；full-tests-final.log；frontend-check-v5.log；frontend-contracts-v2.log；build-renderer.log。

覆盖前原文件备份：D:/OneDrive/app_dve/VoxSub/.backups/marketplace-expansion-20261004-213622，目录内 RESTORE.txt 说明逐文件恢复。补充修改前备份和失败日志在仓库外证据目录。新增文件是 catalog_assets.py、test_marketplace_expansion.py 及本报告。忽略的 frontend/dist/renderer/index.js 也有单独备份。

优先使用 git revert <DELIVERY.json 中的最终提交 SHA> 回滚本批源码；不使用 reset --hard，不覆盖其他工作。未提交运行构建可按 RESTORE.txt 恢复或从回滚后的源码重新构建。用户配置、现有用户模型目录及安装版未被替换；验证模型均在仓库外证据目录。最终本地提交 SHA / clean 状态记录于该目录 DELIVERY.json。
