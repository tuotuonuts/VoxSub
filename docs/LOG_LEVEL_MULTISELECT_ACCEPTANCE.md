# 诊断日志级别多选验收 — 2026-10-05

## 本轮变更

- 原单选下拉改为公共 `buildMultiFilter` 的独立多选按钮；复用既有 FilterChip、filter-bar 样式，不复制页面私有控件。
- 默认全部级别，DEBUG / INFO / WARNING / ERROR / CRITICAL 分别可选；级别内部 OR，关键词、运行ID及模型会话与级别之间 AND。
- 全部取消显示暂无匹配日志，不自动解释为全部；全部级别一键恢复。默认/全部模式继续保留未知级别，不丢日志。按钮原生键盘可达、aria-pressed 表示选择，中文/English 可多选提示。
- 当前选择随分页重建保留，仅内存状态，不写配置。实时、历史文件及导出共用同一 visibleLogs 筛选，无删除或改写记录。
- 暂停时自动新日志仍不刷新；用户主动调整筛选可刷新且不自动跳到底部。迟到运行元数据尊重暂停，也不能写入新页面；旧级别按钮不能改变新页面筛选。
- 本轮不涉及 SenseVoice 下载修复；该问题仍待用户确认方案。

## 已验证

全部验证后台静默，无 Electron / 浏览器窗口、无麦克风、回环捕获或播放音频，无实际日志清除/导出对话框。组件/页面测试使用生产 TypeScript 与 mini-dom 事件执行，IPC 和保存对话框为模拟接口，不宣称是真实 Electron 运行验收。

- 公共组件测试：22/22，通过；新增6项（独立选择、零选择/全部恢复、aria/native button、稳定节点、实例/回调隔离、非法/重复值/文本安全与空列表）。
- 诊断生产页面及共享逻辑：23/23，通过；新增11项（多级别 OR、默认未知级别、空选择不丢记录/不发IPC、关键词/运行ID交集、暂停手动筛选、历史/导出一致、跨页状态与陈旧控件、English、模型会话交集、迟到元数据不解除暂停/不污染新页）。
- 完整 `npm run check` 通过：TypeScript、设计令牌、组件/逻辑等既有检查；`npm run build` 通过，构建前后受跟踪源码 diff 二进制快照一致。
- Python 安全回归 `pytest -q -m "not hardware_audio and not integration"`：1131 passed / 6 skipped / 17 deselected / 1既有xfailed / 0 failed。使用隔离 APPDATA、LOCALAPPDATA，不触碰用户应用日志/模型/历史/配置。
- `git diff --check` 通过。未改变 Python 后端、IPC 契约、下载逻辑、模型清单或生产日志。

## 未验证 / 未执行

- NOT_RUN：真实 Electron 窗口与键盘/屏幕阅读器、深浅/窄窗的实际视觉验收；实际系统日志读取/导出；真实模型/音频集成；安装包/安装版；GitHub CI。
- 未重启正在运行的用户应用，未推送本轮提交。新构建需要重新启动源码版应用后加载；此报告不是安装版更新声明。
- Python 跳过及既有 strict xfail 不计作通过；本轮未新增/改变跳过标记。

## 证据与回滚

- 证据根：`D:/OneDrive/app_dve/log-level-multi-20261005/`，frontend-check.log、frontend-build.log、python-safe.log、build-source-invariant.txt、源码前后快照及 DELIVERY.json。
- 修改前备份：`D:/OneDrive/app_dve/VoxSub/.backups/log-level-multi-20261005-160206/`，含既有源码、测试、STATUS/TODO 和原 frontend/dist；RESTORE.md 说明还原原相对路径。
- 可以 revert 本轮主仓库提交后重新构建；新增 multi-filter.ts / 本报告随 revert 移除。不要恢复或删除用户配置、模型、日志和历史。最终提交号记录在仓库外 DELIVERY.json。
