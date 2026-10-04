# 模型语言范围联动验收（2026-10-04）

## 实施规则

- 识别语言 = 当前有效识别模型可执行语言 ∩ 当前有效翻译器输入语言。
- 翻译语言 = 当前源语言对应的翻译器输出语言，不再与识别模型求交集。因此英文识别模型仍可翻译成中文。
- 尽量保留合法的原选择；不合法时自动调整并显示原因。能力未确认、保存未确认或无可用组合时禁止开始；正在运行时仍可结束。
- 能力由 Python 统一计算，IPC 查询不加载权重、不打开音频。开始前与 set_langs 均验证，拒绝不合法组合。
- 模型/档位设置先在实际 Pipeline owner 生效，再确认保存；忙碌/收尾拒绝修改，不伪报成功。ASR 模型广场选择同步保存；翻译模型同时选择正确档位。
- 自动识别仅在自动识别全部可能语言均有安全目标时提供。并发模型修改结束后才刷新；迟到的查询/语言保存不覆盖最新选择；断连失效并阻止开始。
- D/OCR 由文字翻译能力决定，不受音频 ASR 限制，不调用音频 set_langs。

## 当前能力边界

本轮对齐的是现有应用语言归一化与翻译适配器的 **中文/英文/日文/韩文** 可执行合同，不直接展示模型宣传语言总数。

| 识别适配器 | 当前显式语言 | 自动识别 |
| --- | --- | --- |
| Zipformer | 中文、英文 | 安全时提供 |
| FunASR Nano | 中文、英文、日文 | 安全时提供 |
| Qwen3-ASR / SenseVoice | 中文、英文、日文、韩文 | 未提供：无约束解码可能超出翻译合同 |
| Moonshine EN / Parakeet TDT | 英文 | 未提供 |
| 云 STT | 应用路由四种语言 | 未提供：自定义服务未声明检测边界 |

OPUS 有效档位仅支持中英；质量档按所选模型及工厂默认计算，云翻译使用现有四语言适配合同。方向性矩阵保留同语言透传。Parakeet 模型本身的欧洲多语能力没有在本轮全面开放，需先补齐应用路由/翻译适配和质量验收。

## 验证证据

外部证据目录：D:/OneDrive/app_dve/language-capabilities-20261004-145103。

- 前端 npm run check：PASS，包含 TypeScript、语言控制器/队列、其他逻辑回归、页面生命周期 126/126；验收契约 26/26、11 个结构变异负对照通过。
- 新增生产语言控制器测试：方向性、保留/调整、无交集、保存失败/重试、延迟查询、旧保存、并发模型变更、OCR、断连重连、Start/Stop 与 DOM 焦点保持。
- Python 安全全量：**986 passed / 5 skipped / 17 deselected / 1 xfailed**（1 条现有 tarfile 弃用警告），见 pytest-delivery-final.log；integration / hardware_audio 未运行。新增能力测试 19 项。
- 回归发现既有收尾门禁测试的竞态：IDLE 发布早于 stop finalizer 放弃 owner。测试改为等待实际资源门禁开放，没有放宽生产安全门禁；首次失败保留于 pytest-delivery.log。
- 真正隐藏 Electron + 真正 Python IPC：初始 Moonshine 源语言仅英文，OPUS 目标中/英；失效 zh-en 调整 en-en 并提示；手工 en-zh 保存确认；设置页切云翻译后目标中/英/日/韩、源仍仅英文；非法 zh-en 被后端拒绝；无关日志/能力查询不重建选项且保持 DOM 内焦点；会话始终 idle。结果 real-ui-acceptance.json。
- 使用 VOXSUB_HEADLESS=1、VOXSUB_DEVTOOLS=0 与隔离 APPDATA/LOCALAPPDATA/TEMP；不启动会话，不采集或播放音频。本次 CDP 端口对应 Electron PID 36672，MainWindowHandle=0、无主窗口标题（hidden-window.json）；不以此声称有持续全系统焦点监测。应用通过 requestQuit 正常退出，启动器会话退出码 0。
- 独立配置中的 models_root 只读引用上轮验收模型。不下载安装模型、不修改真实用户配置、对话历史或安装版。

## 未验证 / 不包含

NOT_RUN：真实麦克风/系统音频、完整音视频文件推理、云 API 成功翻译、多语言质量、TTS 听感、GPU/NPU、打包/安装升级。上述结果是语言选择和入口防护验收，不是全功能发布验收。

## 回滚与交付

基线 a04caff8545489a23352f71c3f0fc64e4e2c78fb；覆盖前备份 .backups/language-capabilities-20261004-145103（RESTORE.txt）。本轮仅本地提交，不推送、不更新安装包。最终 SHA、测试结果与 clean 状态记录在证据目录 DELIVERY.json。首选 git revert 最终交付提交；手工恢复前先备份后续工作，恢复相同相对路径并归档本轮新增文件，不使用 reset --hard。
