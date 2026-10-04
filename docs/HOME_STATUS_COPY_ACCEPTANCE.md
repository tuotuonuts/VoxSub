# 主页后台任务提示文案修复

## 原因与改动

`store.applyEvent(job)` 原先直接拼接内部 command/jobId 与状态，因而在主页显示 `set_config 已完成`。本轮新增独立纯函数 `shared/command-feedback.ts`，为全部 59 个已知命令提供可读名称，配置/语言/模型选择等使用自然成功提示；技术命令名及任务 ID 留在诊断日志，不展示于主页状态行。没有改变命令本身、请求投递、任务终态或退出保护。

- `set_config` → 设置已保存。
- `set_langs` → 翻译语言已更新。
- `set_recording` → 录音设置已更新。
- 识别/翻译模型 → 模型选择已更新，不冒称模型已加载。
- 失败提示对应操作及“请查看诊断日志”；取消明确显示已取消，不冒称失败/完成。
- 未知/缺少命令与原型属性名使用“后台任务”，不回退显示内部名称/编号。
- queued/running/cancelling 不写完成提示；启动/结束/取消请求受理与实际 Pipeline 状态保持区分；自检完成不等于检查全部通过。
- 新增中文/英文词条，59 个命令的三种终态均覆盖；后端正常运行进度状态原样显示。

## 验证

- 生产 store + 主页 DOM + i18n MiniDOM：9/9 通过；不是复制状态逻辑的测试。
- `npm run check` 全部 PASS，新测试已加入常规检查；构建 PASS。
- Python 安全回归：1059 passed, 4 skipped, 17 deselected, 1 xfailed, 1 warning in 55.01s；排除 integration/hardware_audio，保留既有 tar 弃用告警。
- 最新本地构建隐藏 Electron + 真实 Python IPC：实际保存隔离设置、关闭录音选项、切换语言后，真实主页分别显示“设置已保存”“录音设置已更新”“翻译语言已更新”。没有开始音频采集或模型推理。
- 窗口监测 66 次，0 可见/聚焦违规；正常退出、remaining_owned_pids=[]。
- 英文及失败/取消/未知命令由生产 MiniDOM 验证；隐藏真实 Electron 本轮只验中文成功路径，不把其他路径写成真实运行已验。

## 交付与回滚

仅更新源码和本地 dist，未改安装版/用户配置/历史，未打包、发布或推送。测试使用独立 APPDATA/LOCALAPPDATA/TEMP，不关闭用户应用。

证据：D:/OneDrive/app_dve/user-status-copy-acceptance。最终提交见同目录 DELIVERY.json。修改前源码和 dist 备份：D:\OneDrive\app_dve\VoxSub\.backups\20261005-041242-user-status-copy。可在干净工作区 git revert 本轮提交；重新构建或按相对路径恢复备份 dist，不覆盖后续用户改动。
