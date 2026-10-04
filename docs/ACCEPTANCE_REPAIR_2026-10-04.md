# 2026-10-04：C→A 录音状态修复与静默真实复验

## 结论与范围

**PASS：本次已确认缺陷修复并复验通过。** 这不是全功能或发布验收。

- 用户授权：修复“文件处理完成后切回 A，录音开关仍禁用”，并复验；沿用静默、不抢焦点、不发声要求。
- 基线：3d8bbbb2bc6dab83b063a1b71b2ebb6aaa77107a，当前本地检出源码，版本 0.9.0-beta；未拉取远端。
- 仅修改 Electron 渲染侧，不改 Python 后端，不推送、不打包、不更新安装版。
- 最终提交 SHA 与工作区状态：D:/OneDrive/app_dve/acceptance-repair-20261004-210245/DELIVERY.json（提交后生成，避免自引用 SHA）。

## 根因与最小修复

原验收的 C 文件结束事件包含 recordingSupported=false / recordingCanChange=false。切换到 A 后，后端实际支持录音，但 UI 沿用 C 的录音快照。

1. frontend/src/renderer/index.ts：switchMode 原先只发送 set_mode，没有请求新状态。现在检查明确成功回执，并在 A/B/C 模式且该模式仍拥有工作区时调用 refreshSessionState。
2. D/OCR 是 renderer-only 模式；后端音频 Pipeline 只支持 A/B/C。D 不做此音频状态刷新，避免 state.mode 把 D 改回之前的音频模式。
3. frontend/src/renderer/store.ts：sessionAuthority 额外捕获 mode；模式变化会使已发出的旧会话读/写响应失效。延用既有 revision、snapshot、连接事件保护，不另建状态通道。
4. frontend/tools/test-mode-recording.mjs：临时测试编译只暴露 private switchMode/工作区挂载入口，生产源码不增加测试 API；同一 bundle 驱动真实 index/store/workspace 和 MiniDOM。接入 package.json 的默认录音测试链。

## 自动化回归（PASS）

- npm run check：exit=0，包括 TypeScript、配色、默认逻辑套件；原生产 workspace 46 场景、窗口生命周期 46 场景及末尾页面生命周期 126 场景均通过。各数字是子套件数，不能合并当作完整前端总数。
- 新增 7 个场景：C 完成→A 权威刷新与真实 workspace 录音写入；重复 A/B/C 及录音偏好保留；D 不被音频模式覆盖；在途 state 不覆盖较新模式；迟到 set_mode 回执不覆盖较新 D；拒绝回执不虚构录音能力；缺失权威快照保持 unknown/禁用。
- 旧 index 源码负对照：exit=1，缺少 set_mode 后的 state 查询被正确检出；不是把“无异常”误当通过。日志 regression-baseline-negative.log。
- npm run test:acceptance-contracts：exit=0，包括 11 个内存变异负对照；未修改生产文件。
- 安全 Python 回归：946 passed / 5 skipped / 17 deselected / 1 xfailed / 1 warning，40.15s，exit=0。排除 integration、hardware_audio，警告为 tar.extractall 的未来版本行为弃用提示。
- 初次 Python 命令因 PowerShell --basetemp 参数拼接错误未执行测试（exit=4）；改为显式字符串参数后完整重跑通过。保留 python-regression.log，不混为源码失败。

## 真实运行复验（PASS，不是模拟后端）

使用重建后的生产 Electron 44.3.0 / Chromium 152，真实 Python sidecar 与 NDJSON IPC，通过 CDP 执行界面动作及 DOM 断言。单独运行目录/userData，--mute-audio、VOXSUB_HEADLESS=1、DevTools 关闭、TTS 关闭。为避免生产启动器清理用户已有实例，采用独立静默启动包装，不调用实例清理。

| 项目 | 用户可感知结果 | runtime-evidence.jsonl 标签 |
|---|---|---|
| 两次 C 文件完成→A | 两个独立 WAV 经实际本地 Qwen ASR→OPUS zh-en→SRT，100% 后 idle；切回 A 后 checkbox.disabled=false、indeterminate=false，与后端支持/可改状态一致 | real-file-completion-to-A-recording |
| A 录音开关 | 真实 checkbox 点击可开/关，checked 与 recordingEnabled 一致；idle 的 recordingActive 始终 false，未开始麦克风采集 | real-file-completion-to-A-recording |
| A/B/C/D 与快速切换 | B/C 录音禁用、A 可用；D 的 OCR 工作区保留；12 轮快速 A→B→D 后仍在 D，返回 A 能恢复 | real-mode-navigation-and-static-ocr |
| 静态 OCR | 真实引擎返回 Hello world. This is a silent test.；识别 1516ms；没有抓取用户屏幕或原位覆盖 | real-mode-navigation-and-static-ocr |
| 真实断连重连 | 仅终止本测试拥有的 sidecar PID 20108，真实 disconnected 后录音禁用/未知提示；新 sidecar PID 7876 发 ready 后 A 可操作 | terminate-owned-sidecar / true-disconnect-reconnect / final-hidden-monitor |
| 停止与重新处理 | 运行中 stop→stopping→idle，按钮恢复“开始”；新文件再次完成100%并导出，切回 A 录音可用 | real-stop-restart-to-A |
| 静默监测 | 两个窗口隐藏且不聚焦；100ms 监测取得2410次样本，show/focus事件和违规均为0；渲染器 errors=[] | hidden-window-monitor / final-hidden-monitor / graceful-exit |
| 正常退出 | app.requestQuit，Electron exit=0；测试拥有的 Electron/Python 进程和19332/19333监听均不残留 | exit.json / final-integrity.json |

本轮共有 3 次完整文件处理（两次复现序列与一次 stop 后 restart）及一次取消收尾。SRT 产物 sample-zh.srt、sample-zh-second.srt、sample-restart.srt 已落盘，非空。短素材只验证链路，不代表识别或翻译质量达标。Qwen 实际 CPU 推理；不能据 OPUS 的 provider 列表宣称 GPU/NPU 验收通过。

## 完整性与备份

- 启动前记录用户 config.json、模型 manifest.json、catalog_installs.json 的 SHA256；退出后全部一致。此次基线在启动前采集，较前一轮补齐了时点。结论只覆盖这三份文件，不扩展为所有用户文件。
- 测试隔离 APPDATA/LOCALAPPDATA、TEMP 与 userData；未读取用户 API 密钥，未请求模型下载、安装或迁移。旧版迁移提示仅在隐藏窗口内出现并选择“稍后再说”。
- 覆盖任何已存在的相关源码/文档及 dist 前，完整备份至 D:/OneDrive/app_dve/VoxSub/.backups/recording-mode-repair-20261004-210245；包括 index.ts、store.ts、package.json、STATUS.md、TODO.txt 和 dist-before-repair。此快照是**本轮修复前**，不补造前一轮缺失的构建前备份。
- 回滚：停止测试实例后，从备份按对应相对路径恢复源码/文档；dist-before-repair 可恢复 frontend/dist。新增测试和报告是新文件。对本地提交需要版本化撤销时，可审阅后 git revert 本次 DELIVERY.json 指定提交，不用 reset --hard。未执行真实工作区回退演练。

## NOT_RUN

实时麦克风采集与 WAV 保存、系统/指定应用声音采集、TTS/音频播放、真实屏幕区域 OCR/覆盖、云 API、Hy-MT 质量档、NPU/硬件路由实证、浮窗半透明/穿透/置顶/捕获排除、安装包及升级。保持静默的隐藏运行不能证明全部桌面视觉与原生能力。

未进行独立代理审查；本轮执行了本地代码复查、默认回归及限定真实运行复验。

## 证据位置

D:/OneDrive/app_dve/acceptance-repair-20261004-210245

包含 baseline.json、启动/CDP脚本、runtime-evidence.jsonl、frontend-check.log、acceptance-contracts.log、python-regression-isolated.log、负对照日志、build-*.log、protected-files-before.json、final-integrity.json、exit.json、SRT 产物；最终 DELIVERY.json 包含提交、clean、退出及校验事实。
