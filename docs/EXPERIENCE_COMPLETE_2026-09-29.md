# 历史体验接入报告 — 2026-09-29（本轮返修已更正其范围）

## 当前更正

本文件原报告过度概括了“录音已完整接入”：controller 单测不能证明 workspace 持续状态链。验收指出实时 observe 接线、Pipeline 最后事件、剩余 native 窗口操作及文档存在缺口。本轮对应源码修复、测试矩阵和回滚见 [ACCEPTANCE_REPAIR_2026-09-29.md](ACCEPTANCE_REPAIR_2026-09-29.md)，不以本文件作为当前完整验收证明。

原 `68c3b4d4c9d8a5e86d87c540a3a87bb28156c554` 是功能代码提交；原报告自身提交为 `ce74a02988c3e11cd82c33cbcdd1d119027207cd`。二者不是当前最终 HEAD。本轮 main 代码 HEAD `992790b73201524f9c770c42e1c9c1b7640fe32b`；最终文档SHA、clean及回滚结果见 `C:/Users/Zhang Ruiduo/Hermes/Tool_cache/voxsub-acceptance-20260929/DELIVERY.json`。

下方旧数字为历史记录，不重新背书；本轮隔离基线实际 934 passed / 5 skipped / 17 deselected / 1 xfailed，与原944不同，以新原始日志为准。本轮不推送、不重建安装版。

## 以下为原始报告（非当前状态）

The previously deferred subtitle scrolling and recording-save controls are now integrated and pushed.

## Commits

- `68c3b4d4c9d8a5e86d87c540a3a87bb28156c554` — complete subtitle scrolling and confirmed recording control.
- Earlier pushed commits remain in `main`: window destruction guard, absolute log time, and closeout docs.

## Verified

- Python: `944 passed, 4 skipped, 17 deselected, 1 xfailed, 1 warning`.
- Focused recording/contract/time tests: `87 passed`.
- Frontend `npm run check`: exit 0.
- Recording controller: all focused cases passed.
- Headless Chromium production-module/CSS validation: `5 viewport/DPR/language configurations; 25 evidence records`, exit 0.
- Scroll behavior verified: overflow region, controls remain visible, long text wraps, near-bottom follow, history reading is not pulled down, draft updates preserve position, wheel scrolling, clear, and A/B/C/D mode transitions.

## Recording semantics

The switch controls confirmed microphone WAV saving in microphone mode. It does not start/stop system-audio capture or recognition. Backend state is authoritative; unknown, rejected, disconnected, unsupported, and idle-only states are shown honestly. Changes are only accepted while the pipeline is fully settled.

## Not run

Real microphone/loopback, real model inference, hardware acceleration, packaged installer, and formal Release validation were not run. No user configuration, model data, or Release files were touched.

## Delivery

Remote: `https://github.com/tuotuonuts/VoxSub.git`
Branch: `main`
Historical code SHA (not current remote HEAD): `68c3b4d4c9d8a5e86d87c540a3a87bb28156c554`
Historical working-tree claim: clean at the reported snapshot; current final status is in DELIVERY.json
